"""The workspace side of the hub's public webhook inbox (hub contract 10.11, orch-core#1285).

The hub receives the ring (a signed ``POST /h/<token>``), keeps one row per hook and hands it over on ``hook_pull``.
This module registers a hook (``hook_put``), pulls and acknowledges rings, and decides what a ring may do here:

* the hub's word is only ``token_hash`` + closed words. The follow-up and the source come from THIS host's own map
  (``hub-hooks.json``, written when the hook was registered), found by ``token_hash``; a ring whose hash is not in
  the map is acked and dropped. A ``kind`` or ``source`` outside the closed lists never gets here: the link reply
  check refuses the whole answer;
* a ring only calls the follow-up runner's ``ring(hook hash)``, the same call the direct receiver makes: it records
  "this hook rang" for a follow-up that is pending right now. Nothing a request carried exists here and nothing is
  run from it;
* at most :data:`WAKES_PER_HOUR` rings an hour from the hub per workspace are passed on; the rest are acknowledged
  and that follow-up waits for its own deadline (the fallback, ``no_ring``);
* the map holds no token and no secret (only the hub holds the secret; the token was shown once).

No hub, no link or no inbox on the hub: every function here is best effort and the deadline fallback is the whole
story."""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any

from orch.model import followup_events as fe

from . import custody

LOG = logging.getLogger("orch.host.hubhooks")
WAKES_PER_HOUR = 6
HOUR_MS = 3_600_000
HOOK_LIFETIME_DAYS = 15  # the longest follow-up horizon (14 days) plus one day; the hub allows up to 31
LINK_TIMEOUT_S = 5.0
SOURCES = ("github", "generic", "databricks")
_FOLLOWUP = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_LOCK = threading.Lock()


def _path(service: Any) -> Path:
    return Path(service.state_dir) / "hosts" / service.workspace_id / "hub-hooks.json"


def _load(service: Any) -> dict[str, Any]:
    doc = custody.read_json(_path(service), service.host_uid) or {}
    hooks = doc.get("hooks") if isinstance(doc.get("hooks"), dict) else {}
    wakes = [w for w in doc.get("wakes", []) if isinstance(w, int)]
    return {"hooks": hooks, "wakes": wakes}


def _save(service: Any, doc: dict[str, Any]) -> None:
    path = _path(service)
    custody.ensure_private_dir(path.parent, service.host_uid)
    custody.write_private(path, doc)


def watching(service: Any) -> bool:
    """Whether any hub hook of this workspace is registered (cheap: one small file)."""
    return bool(_load(service)["hooks"])


def _link(service: Any) -> tuple[Any, Any]:
    """``(HubLink, transport)`` for the bound hub; raises when there is none (the caller treats that as 'no hub')."""
    from . import hubops

    state = hubops._load_state(service)  # noqa: SLF001
    if state is None or state.status != "bound":
        raise LookupError("no bound hub")
    wsk_pub, sign = hubops._wsk_sign(service)  # noqa: SLF001
    return hubops._snapshot_link(  # noqa: SLF001
        service, state, wsk_pub, sign, hubops.workspace_dev(service), send_profile=False, timeout=LINK_TIMEOUT_S
    )


def register(service: Any, *, token: str, secret: str, source: str, followup: str) -> str:
    """Tell the hub about a new hook and remember it here. Returns the hub's ``url_base`` (``https://hub/h/``). The
    token and the secret are not kept: the hub keeps the hash and the secret, the caller shows both once."""
    if source not in SOURCES or not fe.HUB_SECRET_RE.fullmatch(secret):
        raise ValueError("bad webhook source or secret")
    th = fe.hub_token_hash(token)
    if not _FOLLOWUP.fullmatch(followup):
        followup = "hook"  # bookkeeping text of the hub's row only; never decides anything here
    now_ms = int(service.clock() * 1000)
    link, transport = _link(service)
    try:
        reply = link.call(
            "hook_put",
            {
                "token_hash": th,
                "followup": followup,
                "source": source,
                "secret": secret,
                "expires_ms": now_ms + HOOK_LIFETIME_DAYS * 86_400_000,
            },
        )
    finally:
        getattr(transport, "close", lambda: None)()
    with _LOCK:
        doc = _load(service)
        doc["hooks"] = {k: v for k, v in doc["hooks"].items() if v.get("exp", 0) > now_ms}
        doc["hooks"][th] = {
            "hook": fe.hook_hash(token, service.workspace_id),
            "source": source,
            "exp": now_ms + HOOK_LIFETIME_DAYS * 86_400_000,
        }
        _save(service, doc)
    return str(reply["url_base"])


def revoke(service: Any, token_hash: str) -> None:
    """Best effort: forget the hook here and ask the hub to drop it."""
    with _LOCK:
        doc = _load(service)
        if doc["hooks"].pop(token_hash, None) is None:
            return
        _save(service, doc)
    try:
        link, transport = _link(service)
    except Exception:  # noqa: BLE001 - no hub: the hub's own expiry removes it
        return
    try:
        link.call("hook_revoke", {"token_hash": token_hash})
    except Exception:  # noqa: BLE001
        LOG.warning("hub hook not revoked; it expires on its own")
    finally:
        getattr(transport, "close", lambda: None)()


def pull(service: Any) -> tuple[list[str], int]:
    """``(hook hashes to ring, highest seq to acknowledge)``. Empty when there is no hub, no inbox or nothing rang. Only
    rings whose ``token_hash`` is in this host's map pass; the wake limit is applied here; a failure returns nothing
    and the rings wait at the hub (until they expire)."""
    now_ms = int(service.clock() * 1000)
    with _LOCK:
        doc = _load(service)
    doc["hooks"] = {k: v for k, v in doc["hooks"].items() if v.get("exp", 0) > now_ms}
    if not doc["hooks"]:
        return [], 0
    try:
        link, transport = _link(service)
    except Exception:  # noqa: BLE001 - no hub: nothing to pull
        return [], 0
    try:
        reply = link.call("hook_pull", {"since": 0})
    except Exception as exc:  # noqa: BLE001
        LOG.info("hub hook pull failed (%s)", getattr(exc, "code", type(exc).__name__))
        return [], 0
    finally:
        getattr(transport, "close", lambda: None)()
    rings, top, out = reply["rings"], 0, []
    room = WAKES_PER_HOUR - len([w for w in doc["wakes"] if now_ms - w < HOUR_MS])
    for r in rings:
        top = max(top, r["seq"])
        mine = doc["hooks"].get(r["token_hash"])
        if mine is None:
            continue  # not a hook this host registered: acked and dropped
        if len(out) >= room:
            LOG.info("hub hook ring over the hourly limit: the follow-up waits for its deadline")
            continue
        out.append(mine["hook"])
    return out, top


def count_wake(service: Any) -> None:
    """A ring from the hub was accepted by a pending follow-up: it counts against the hour."""
    now_ms = int(service.clock() * 1000)
    with _LOCK:
        doc = _load(service)
        doc["wakes"] = [w for w in doc["wakes"] if now_ms - w < HOUR_MS] + [now_ms]
        doc["hooks"] = {k: v for k, v in doc["hooks"].items() if v.get("exp", 0) > now_ms}
        _save(service, doc)


def ack(service: Any, seq: int) -> None:
    if seq < 1:
        return
    try:
        link, transport = _link(service)
    except Exception:  # noqa: BLE001
        return
    try:
        link.call("hook_ack", {"seq": seq})
    except Exception as exc:  # noqa: BLE001 - the same rings come again; the runner's rings map is first-wins
        LOG.info("hub hook ack failed (%s)", getattr(exc, "code", type(exc).__name__))
    finally:
        getattr(transport, "close", lambda: None)()
