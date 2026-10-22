"""``orch-hub serve``: the dev hub process (link server + publish forwarding). Dev only in V3: loopback listen only.

Starts :class:`~orch_hub.link.LinkServer` from the key store, with :class:`~orch_hub.publish.HubPublish` (over
:class:`~orch_hub.publish.HttpPublishSync`) as the publish forwarder and as the downstream that learns the accepted
bindings. The registry (pins, cards, signed binding objects) lives in ``<state>/registry.db`` and is re-pushed to
orch-publish after a restart. One ready line is printed on stdout once the port is bound; SIGTERM / SIGINT stop it.
"""

from __future__ import annotations

import ipaddress
import re
import signal
import sys
import threading
import urllib.parse
from pathlib import Path
from typing import TextIO

from .access import Access, AccessSnapshots
from .access.cli import ACCESS_FILE
from .audit import open_log
from .contract.hub import Refused
from .keys import HubKeys
from .link import LinkServer, Registry
from .link.wkseal import WkSeals
from .publish import HttpPublishSync, HubPublish, PublishConfig
from .router import AccessAuthz, RouteQueue, Router
from .shares import ShareService, ShareStore, hxk_handle
from .spaces import SpaceService, SpaceStore

REGISTRY_FILE = "registry.db"
ROUTE_QUEUE_FILE = "route-queue.db"
SPACES_FILE = "spaces.db"
WK_SEAL_FILE = "wk-seal.db"
SHARES_FILE = "shares.db"


_HEX16 = re.compile(r"[0-9a-f]{32}")


class FixedInvites:
    """Invite codes given on the command line: not bound to a workspace, single use. ``CODE=USER_ID`` also aliases the
    joining workspace's owner to that hub user (V3-D19): the hub admin says who the owner is, the host cannot."""

    def __init__(self, codes: list[str]) -> None:
        self._codes, self._owner = set(), {}
        for i, c in enumerate(codes, 1):
            code, sep, user = c.rpartition("=")
            if not sep:
                self._codes.add(c)
            elif code and _HEX16.fullmatch(user):
                self._codes.add(code)
                self._owner[code] = user
            else:  # CODE=<not a 32-hex user id>: refuse to start rather than silently drop the alias
                raise ValueError(f"invite #{i}: the part after '=' ({user[:40]!r}) is not a 32-hex hub user id")
        self._lock = threading.Lock()

    def owner_user(self, invite: str) -> str | None:
        return self._owner.get(invite)

    def check(self, invite: str, now_ms: int) -> str | None:
        with self._lock:
            if invite not in self._codes:
                raise Refused("bad_invite")
        return None

    def consume(self, invite: str, workspace_id: str) -> None:
        with self._lock:
            self._codes.discard(invite)


def parse_listen(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    try:
        ok = bool(sep) and ipaddress.ip_address(host.strip("[]")).is_loopback and port.isdigit() and int(port) < 65536
    except ValueError:
        ok = False
    if not ok:
        raise ValueError("--listen must be a loopback host:port (dev only), e.g. 127.0.0.1:0")
    return host.strip("[]"), int(port)


def serve(
    state: Path,
    listen: str,
    publish_url: str,
    invites: list[str],
    *,
    api_host: str | None = None,
    stop: threading.Event | None = None,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    host, port = parse_listen(listen)
    gate = FixedInvites(invites)  # a bad invite refuses before any state is opened
    u = urllib.parse.urlsplit(publish_url)
    api_host = api_host or u.netloc
    keys = HubKeys.open(state)
    reg_path = state / REGISTRY_FILE
    registry = Registry(str(reg_path))
    access = Access(str(state / ACCESS_FILE))
    queue_path = state / ROUTE_QUEUE_FILE
    queue = RouteQueue(str(queue_path))
    wk_seals = WkSeals(str(state / WK_SEAL_FILE), hub_id=keys.hub_id, hxk_pub=keys.hxk_pub, exchange=keys.hxk_exchange)
    router = Router(registry, queue, AccessAuthz(registry, access))
    audit = open_log(state, keys)
    access.observer = audit.access_change  # D4: every grant and membership change is audited
    space_store = SpaceStore(str(state / SPACES_FILE))
    spaces = SpaceService(
        space_store,
        registry,
        access,
        keys.op_signer,
        keys.hub_id,
        lambda: keys.op_cert()["o"]["cert_id"],
        on_change=audit.space_change,
    )
    share_store = ShareStore(str(state / SHARES_FILE))
    shares = ShareService(
        share_store,
        registry,
        hub_id=keys.hub_id,
        hxk=hxk_handle(keys.hxk_pub, keys.hxk_exchange),
        dev_mode=True,  # dev serve only (loopback); ShareService itself defaults to the fail-closed False
        on_change=audit.share_change,
    )
    fw = HubPublish(keys, HttpPublishSync(publish_url), PublishConfig(publish_url, api_host))
    stop = stop or threading.Event()
    server = LinkServer.from_keys(
        keys,
        snapshots=AccessSnapshots(access, keys),
        registry=registry,
        invites=gate,
        publish=fw,
        downstream=fw,
        router=router,
        access=access,
        spaces=spaces,
        audit=audit,
        wk_seals=wk_seals,
        shares=shares,
        host=host,
        port=port,
    )
    try:
        shares.reconcile()  # items of a workspace that is no longer a bound selective one are deleted
        spaces.reconcile()  # a workspace that is no longer bound leaves its spaces (also after a crash)
        fw.restore(registry.binding_objects())

        def _retry_shares() -> None:  # D9-1: a pending share purge is retried while the hub runs
            while not stop.wait(30):
                shares.retry_pending()

        threading.Thread(target=_retry_shares, name="share-purge-retry", daemon=True).start()
        server.start()
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
        card = keys.card()["o"]
        print(
            f"orch-hub ready port={server.address[1]} tls_spki_sha256={card['tls_spki_sha256']} hub_id={keys.hub_id}",
            file=out,
            flush=True,
        )
        stop.wait()
    finally:
        server.close()
        queue.close()
        space_store.close()
        share_store.close()
        audit.store.close()
        registry.close()
        wk_seals.close()
        access.close()
    return 0
