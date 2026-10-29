"""Company secrets at the workspace host (M3 S-3, #1431-#1433; hub contract 7.5, ``state/devA/secrets-design.md``).

Only a tier B host (agents run as their own OS user, :func:`orgstate.isolated`) ever asks for, keeps or opens a wrap.
Files under ``hub/org/`` (0600, host UID, written like the other org files):

* ``secrets.json``: ``{binding, issued_ms, wraps: [entry]}``. The SEALED wraps (the hub's entries as
  they came) of exactly the secrets named by an accepted, served ``hub_secret`` MCP server. Nothing else is kept.
  Deleted at once when the host is found not isolated.
* ``secret-pins.json``: ``{hub_id, bundle: {version, digest}, ids: {secret_id: highest version seen}}``. The rollback
  pins. They survive a sync and a loss of isolation; only :func:`orgstate.purge` (unbind, leave) drops them.

A value exists in memory only: opened per call or from a cache of at most 60 s that never outlives ``expires_ms`` and
is dropped on a sync that changes the wraps, on unbind or leave, and when isolation is lost. It is never written, logged
or returned by an op."""

from __future__ import annotations

import datetime
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .service import WorkspaceService

__all__ = [
    "CACHE_TTL_S",
    "drop_all",
    "drop_cache",
    "prune",
    "restore_usage",
    "secret_status",
    "sync_secrets",
    "take_usage",
    "used_names",
    "value",
]

CACHE_TTL_S = 60.0
PINS_MAX = 1000  # secret ids pinned per hub; a bundle that would go beyond is refused


class PinsUnusable(Exception):
    """``secret-pins.json`` exists but cannot be trusted: the sync refuses (fail closed)."""


USAGE_MAX = 50
_MU = threading.RLock()
_CACHE: dict[tuple[str, str, int, int], tuple[str, float, int]] = {}  # key -> (value, cached_at, expires_ms)
_USAGE: dict[str, dict[tuple[str, str], int]] = {}  # workspace -> {(secret_id, day): n}


def _orgstate() -> Any:
    from . import orgstate

    return orgstate


# -- cache and usage (memory only) ----------


def drop_cache(service: WorkspaceService) -> None:
    """Forget every opened value of this workspace (sync change, unbind, leave, loss of isolation)."""
    with _MU:
        for key in [k for k in _CACHE if k[0] == service.workspace_id]:
            del _CACHE[key]


def take_usage(service: WorkspaceService) -> list[dict[str, Any]]:
    """The counts to report (``secret_use {secret_id, day, n}``), at most :data:`USAGE_MAX`; they leave the memory."""
    with _MU:
        mine = _USAGE.get(service.workspace_id, {})
        keys = sorted(mine)[:USAGE_MAX]
        return [{"secret_id": k[0], "day": k[1], "n": mine.pop(k)} for k in keys]


def restore_usage(service: WorkspaceService, rows: list[dict[str, Any]]) -> None:
    with _MU:
        mine = _USAGE.setdefault(service.workspace_id, {})
        for r in rows:
            k = (r["secret_id"], r["day"])
            mine[k] = min(mine.get(k, 0) + r["n"], 10**9)


def _count(service: WorkspaceService, secret_id: str, now_ms: int) -> None:
    day = datetime.datetime.fromtimestamp(now_ms / 1000, datetime.UTC).strftime("%Y-%m-%d")
    with _MU:
        mine = _USAGE.setdefault(service.workspace_id, {})
        if len(mine) < 1000 or (secret_id, day) in mine:
            mine[(secret_id, day)] = min(mine.get((secret_id, day), 0) + 1, 10**9)


# -- files ------------------------------------------------------------------------------------------------------------


def _wraps(service: WorkspaceService, state: Any) -> tuple[list[dict[str, Any]], int]:
    """The stored wraps of THIS binding and the ``issued_ms`` of the bundle that carried them, else nothing."""
    from orch.hubclient import orgbundles

    os_ = _orgstate()
    doc = os_._read(service, "secrets.json")  # noqa: SLF001
    if type(doc) is not dict or doc.get("binding") != os_._binding(state):  # noqa: SLF001
        return [], 0
    rows, issued = doc.get("wraps"), doc.get("issued_ms")
    if type(issued) is not int or not orgbundles.valid_org_secrets(rows):
        return [], 0
    return list(rows), issued


def _pins(service: WorkspaceService, state: Any) -> dict[str, Any]:
    """The rollback pins of this HUB (a mode switch keeps the hub and bumps ``binding_seq``: the pins stay; only
    :func:`orgstate.purge` resets them). A missing file is no pins; a file that is there but unusable (not an object,
    over :data:`PINS_MAX`, bad entries) raises :class:`PinsUnusable` so the sync refuses instead of reading it as
    empty."""
    os_ = _orgstate()
    empty = {"hub_id": str(state.hub_id), "bundle": None, "ids": {}}
    if not (os_.org_dir(service) / "secret-pins.json").exists():
        return empty
    doc = os_._read(service, "secret-pins.json")  # noqa: SLF001
    if type(doc) is not dict or type(doc.get("ids")) is not dict or len(doc["ids"]) > PINS_MAX:
        raise PinsUnusable
    bundle = doc.get("bundle")
    if bundle is not None and not (
        type(bundle) is dict and type(bundle.get("version")) is int and type(bundle.get("digest")) is str
    ):
        raise PinsUnusable
    if any(type(k) is not str or type(v) is not int or v < 1 for k, v in doc["ids"].items()):
        raise PinsUnusable
    if doc.get("hub_id") != str(state.hub_id):
        return empty  # another hub's pins are not this hub's (unbind purges them anyway)
    return {"hub_id": empty["hub_id"], "bundle": bundle, "ids": dict(doc["ids"])}


def drop_all(service: WorkspaceService) -> None:
    """Not isolated (or anything else that must not keep a wrap): delete ``secrets.json`` and the cache. The pins
    stay."""
    import contextlib

    drop_cache(service)
    with _MU:
        with contextlib.suppress(OSError):
            (_orgstate().org_dir(service) / "secrets.json").unlink()


def used_names(service: WorkspaceService) -> set[str]:
    """The secret names of accepted, served ``hub_secret`` servers under the current binding (what may be kept)."""
    try:
        return {
            r["secret"] for r in _orgstate().served(service)["mcp"] if r["auth"] == "hub_secret" and r.get("secret")
        }
    except Exception:  # noqa: BLE001 - any doubt: nothing is used
        return set()


def _wxk(service: WorkspaceService) -> tuple[Any, int] | None:
    try:
        from . import spaceops

        key, version = spaceops._wxk(service)  # noqa: SLF001 - the workspace key held in host custody
        return key, int(version)
    except Exception:  # noqa: BLE001
        return None


def _write_wraps(service: WorkspaceService, state: Any, rows: list[dict[str, Any]], issued: int) -> None:
    os_ = _orgstate()
    if not rows:
        drop_all(service)
        return
    doc = {"binding": os_._binding(state), "issued_ms": issued, "wraps": rows}  # noqa: SLF001
    os_._write(service, "secrets.json", doc)  # noqa: SLF001


def prune(service: WorkspaceService) -> None:
    """Keep only the wraps a served ``hub_secret`` server uses (an owner declined a server: its wrap goes)."""
    try:
        with _orgstate()._MU, _MU:  # noqa: SLF001 - the same order as a sync (orgstate first)
            state = _orgstate()._state(service)  # noqa: SLF001
            rows, issued = _wraps(service, state)
            used = used_names(service)
            keep = [r for r in rows if r["name"] in used]
            if len(keep) != len(rows):
                drop_cache(service)
                _write_wraps(service, state, keep, issued)
    except Exception:  # noqa: BLE001 - a missing binding has nothing to prune
        return


# -- sync -------------------------------------------------------------------------------------------------------------


def sync_secrets(service: WorkspaceService, state: Any, held: Any, body: Any, now_ms: int) -> str | None:
    """Take the reply body of ``org_secrets_get`` (an error string for a refusal, ``None`` when it was not requested).
    Caller holds ``orgstate._MU``. Returns ``new``, ``same``, ``skipped`` or a refusal code; ``None`` when the host is
    not isolated.

    * not isolated: ``secrets.json`` and the cache are deleted, nothing is verified (condition 11);
    * otherwise the bundle is verified like the other two (no rollback), and the stored wraps become exactly the entries
      of this bundle that a served ``hub_secret`` server names, are not expired and are not older than the highest
      version seen for that secret id (condition 12, 13). Everything else is gone."""
    from orch.hubclient import orgbundles
    from orch.hubclient.errors import HubRefused

    os_ = _orgstate()
    if not os_.isolated(service):
        drop_all(service)
        return None
    if body is None:
        prune(service)
        return "skipped"
    if type(body) is str:
        prune(service)
        return body
    try:
        pins = _pins(service, state)
    except PinsUnusable:
        return "pins_unusable"
    try:
        o = orgbundles.verify_org_secrets(
            body["bundle"],
            held,
            workspace_id=state.binding["o"]["workspace_id"],
            dev_mode=bool(state.binding["o"]["dev"]),
            pinned=pins["bundle"],
            revoked_op_certs=frozenset(state.revoked_op_certs),
        )
    except HubRefused as exc:
        return exc.code
    except (KeyError, TypeError):
        return "malformed"
    ids = dict(pins["ids"])
    if len(ids | {e["id"]: 0 for e in o["secrets"]}) > PINS_MAX:
        return "too_many_secrets"
    used = used_names(service)
    keep: list[dict[str, Any]] = []
    for e in o["secrets"]:
        seen = ids.get(e["id"], 0)
        if e["version"] < seen:
            continue  # a rollback of that secret: refused, and its old wrap is not kept either
        ids[e["id"]] = max(seen, e["version"])
        if e["name"] in used and e["expires_ms"] > now_ms:
            keep.append(e)
    before, _ = _wraps(service, state)
    fresh = pins["bundle"] is None or pins["bundle"]["version"] != o["version"]
    os_._write(  # noqa: SLF001
        service,
        "secret-pins.json",
        {
            "hub_id": pins["hub_id"],
            "bundle": {"version": o["version"], "digest": orgbundles.secrets_digest(o)},
            "ids": ids,
        },
    )
    if before != keep:
        drop_cache(service)
    _write_wraps(service, state, keep, o["issued_ms"])
    return "new" if fresh else "same"


# -- use --------------------------------------------------------------------------------------------------------------


def _entry(service: WorkspaceService, state: Any, name: str) -> tuple[dict[str, Any], int] | None:
    rows, issued = _wraps(service, state)
    e = next((r for r in rows if r["name"] == name), None)
    return None if e is None else (e, issued)


def value(service: WorkspaceService, name: str) -> str | None:
    """The value of company secret ``name`` for this workspace, or ``None`` for any reason at all (not isolated, no
    wrap, expired, a wrap for another hub, workspace, key version, name or version, a value that breaks the rule).
    Nothing is logged."""
    try:
        return _value(service, name)
    except Exception:  # noqa: BLE001 - never a value, never an exception text, out of here
        return None


def _value(service: WorkspaceService, name: str) -> str | None:
    from orch import crypto
    from orch.hubclient import orgsecrets

    os_ = _orgstate()
    if not os_.isolated(service):
        drop_cache(service)
        return None
    state = os_._state(service)  # noqa: SLF001
    got = _entry(service, state, name)
    if got is None:
        return None
    e, issued = got
    now_ms = int(service.clock() * 1000)
    if e["expires_ms"] <= now_ms:
        drop_cache(service)
        return None
    key = (service.workspace_id, e["id"], e["version"], e["wxk_version"])
    wxk = _wxk(service)
    if wxk is None or wxk[1] != e["wxk_version"]:
        drop_cache(service)  # the workspace key moved on: nothing opened with the old one stays
        return None
    now = service.clock()
    with _MU:
        hit = _CACHE.get(key)
        if hit is not None:
            if now - hit[1] < CACHE_TTL_S and now_ms < hit[2]:
                _count(service, e["id"], now_ms)
                return hit[0]
            del _CACHE[key]
    try:
        v = orgsecrets.open_org_secret(
            wxk[0], str(state.hub_id), service.workspace_id, e["id"], e["version"], e["wxk_version"], e["expires_ms"],
            e["name"], crypto.unb64u(e["sealed"]), now_ms, issued,
        )  # fmt: skip
    except Exception:  # noqa: BLE001
        return None
    with _MU:
        _CACHE[key] = (v, now, e["expires_ms"])
    _count(service, e["id"], now_ms)
    return v


def secret_status(service: WorkspaceService, state: Any, name: str | None) -> str:
    """``ready``, ``expired``, ``not_received`` (no wrap, or one for an old workspace key: the hub needs the value
    again) or ``needs_isolation``. No value, no wrap."""
    if not _orgstate().isolated(service):
        return "needs_isolation"
    try:
        got = _entry(service, state, name) if name else None
    except Exception:  # noqa: BLE001
        got = None
    if got is None:
        return "not_received"
    e, _ = got
    if e["expires_ms"] <= int(service.clock() * 1000):
        return "expired"
    wxk = _wxk(service)
    if wxk is None or wxk[1] != e["wxk_version"]:
        return "not_received"
    return "ready"
