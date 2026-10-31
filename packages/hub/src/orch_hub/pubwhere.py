"""The hub's signed ``publish_where`` (protocol.md §10.9.1, #1838): where its workspaces publish and share.

At start-up :func:`prepare` builds the one ``hub.publish_where`` this process serves (in ``hub_where`` and the ``join``
reply), or says in one plain line why it serves none. The object is signed with ``HK_op`` and checked with
:func:`~orch_hub.contract.publish_where.check_publish_where` before it is served, so the hub never serves what core
would refuse.

``seq`` and the last signed object live in ``<state>/publish-where.json`` (0600, written atomically). While the origins, the
dev marker and the op cert are what they were, the stored object is served unchanged (the same bytes after a restart); any
change signs a new one with ``seq = max(stored seq + 1, now_ms)``.
"""

from __future__ import annotations

import ipaddress
import json
import threading
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .contract import publish_where as PW
from .contract.canon import CanonError
from .contract.hub import Refused
from .contract.keystore import KeyUseError
from .keys import HubKeys, KeysError
from .keys.fsutil import StateError, read_file, write_atomic

PUBLISH_WHERE_FILE = "publish-where.json"
_VERSION = 1


def _loopback_host(host: str) -> bool:
    h = host.strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def server_origin(api_host: str, dev: bool) -> str:
    """``https://<api_host>``; plain ``http`` only for a dev hub and a loopback host."""
    try:
        host = urllib.parse.urlsplit(f"//{api_host}").hostname or ""
    except ValueError:
        host = ""
    return f"{'http' if dev and _loopback_host(host) else 'https'}://{api_host}"


def _same_origin(a: str, b: str) -> bool:
    """Whether two URLs name one origin (scheme, host and port; default ports and case do not count)."""

    def key(url: str) -> tuple[str, str, int | None] | None:
        try:
            u = urllib.parse.urlsplit(url)
            return (
                u.scheme.lower(),
                (u.hostname or "").lower().removesuffix("."),
                u.port or {"https": 443, "http": 80}.get(u.scheme.lower()),
            )
        except ValueError:
            return None

    ka = key(a)
    return ka is not None and ka == key(b)


def _read_stored(state: Path) -> dict[str, Any] | None:
    try:
        d = json.loads(read_file(state / PUBLISH_WHERE_FILE))
    except StateError, ValueError, RecursionError:
        return None
    return d if type(d) is dict and d.get("v") == _VERSION else None


def prepare(
    state: Path,
    keys: HubKeys,
    *,
    api_host: str | None,
    share_url: str | None,
    publish_url: str,
    dev: bool,
    say: Callable[[str], None],
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
) -> dict[str, Any] | None:
    """The signed ``hub.publish_where`` to serve, or ``None`` (``say`` gets one line when something was asked for and refused).

    ``api_host`` is ``--api-host`` as given (never the default taken from ``--publish-url``), ``share_url`` the
    ``--publish-share-url``. Both must be known; nothing is signed from one."""
    if share_url is None:  # --api-host alone is the old forwarding setting: nothing asked, nothing said
        return None

    def refuse(why: str) -> None:
        say(f"orch-hub WARN publish_where not served: {why}")

    if api_host is None:
        refuse("--publish-share-url needs --api-host (the public API host) as well")
        return None
    try:
        server = PW.canonical_origin(server_origin(api_host, dev), dev)
    except Refused:
        refuse("the --api-host origin is not a public https origin (https, or http on loopback for a dev hub only)")
        return None
    try:
        share = PW.canonical_origin(share_url, dev)
    except Refused:
        refuse(
            "the --publish-share-url origin is not a public https origin (https, or http on loopback for a dev hub only)"
        )
        return None
    canonical_host = urllib.parse.urlsplit(server).netloc
    if (
        canonical_host != api_host
    ):  # the forwarder compares the Host of a send with --api-host as given: they must agree
        refuse(f"--api-host is not in canonical form: use {canonical_host}")
        return None
    if _same_origin(share, publish_url):
        refuse("the --publish-share-url is the private --publish-url")
        return None
    if _same_origin(server, publish_url):
        refuse("the --api-host is the private --publish-url")
        return None
    try:
        signer, cert_id, hub_id = keys.op_signer, keys.op_cert()["o"]["cert_id"], keys.hub_id
    except KeysError, KeyUseError, KeyError, TypeError:
        refuse("the hub's op key is not usable")
        return None

    held = _read_stored(state)
    stored_seq = 0
    if held is not None:
        try:
            o = held["signed"]["o"]
            stored_seq = o["seq"] if type(o["seq"]) is int and o["seq"] >= 0 else 0
            if (held.get("server"), held.get("share"), held.get("cert_id")) == (server, share, cert_id):
                PW.check_publish_where(held["signed"], op_pub=signer.public, hub_id=hub_id, dev=dev)
                return held["signed"]  # unchanged: the same bytes as before the restart
        except Refused, KeyError, TypeError, CanonError:
            pass  # damaged, or not what this hub would sign now: signed again below
    if stored_seq >= PW.SEQ_MAX:  # a new object would carry the stored seq with other bytes: every workspace refuses it
        refuse("the stored seq is at its ceiling, so a changed object cannot be signed")
        return None
    now = now_ms()
    try:
        signed = signer.sign_object(
            {
                "v": 2,
                "suite": 2,
                "kind": PW.KIND,
                "hv": 0,
                "hub_id": hub_id,
                "server": server,
                "share": share,
                "seq": min(max(stored_seq + 1, now), PW.SEQ_MAX),
                "issued_ms": now,
                "dev": dev,
            }
        )
    except KeysError, KeyUseError:
        refuse("the hub's op key is not usable")
        return None
    try:
        PW.check_publish_where(signed, op_pub=signer.public, hub_id=hub_id, dev=dev)
    except Refused as e:  # never serve what core would refuse
        refuse(f"the object does not pass its own check ({e.code})")
        return None
    try:
        write_atomic(
            state / PUBLISH_WHERE_FILE,
            (
                json.dumps(
                    {"v": _VERSION, "server": server, "share": share, "cert_id": cert_id, "signed": signed},
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode(),
        )
    except StateError, OSError:
        refuse("the state file could not be written (the seq would not survive a restart)")
        return None
    return signed


def publisher(
    state: Path,
    keys: HubKeys,
    *,
    api_host: str | None,
    share_url: str | None,
    publish_url: str,
    dev: bool,
    say: Callable[[str], None],
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
) -> Callable[[], dict[str, Any] | None]:
    """A callable that gives the ``hub.publish_where`` to serve NOW, for ``hub_where`` and ``join`` (like ``where``).

    It runs :func:`prepare` and remembers the answer for ``(op cert id, api_host, share_url)``: while those stay, the same
    object is returned without touching the disk; when the op cert changes in this process (a rotation) it is prepared
    again, so what is served is always signed by the key the card now certifies. It runs once at once, so a refusal
    is said at start and not on every request. Whether a rotation always restarts the process or not, a served object
    is never signed by an op key that the card no longer names."""
    mu = threading.Lock()
    memo: list[tuple[Any, dict[str, Any] | None]] = []

    def current() -> dict[str, Any] | None:
        try:
            cert_id: Any = keys.op_cert()["o"]["cert_id"]
        except KeysError, KeyError, TypeError:
            cert_id = None
        key = (cert_id, api_host, share_url)
        with mu:
            if memo and memo[0][0] == key:
                return memo[0][1]
            got = prepare(
                state,
                keys,
                api_host=api_host,
                share_url=share_url,
                publish_url=publish_url,
                dev=dev,
                say=say,
                now_ms=now_ms,
            )
            memo[:] = [(key, got)]
            return got

    current()
    return current
