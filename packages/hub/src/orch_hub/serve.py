"""``orch-hub serve``: the dev hub process (link server + publish forwarding). Dev only in V3: loopback listen only.

Starts :class:`~orch_hub.link.LinkServer` from the key store, with :class:`~orch_hub.publish.HubPublish` (over
:class:`~orch_hub.publish.HttpPublishSync`) as the publish forwarder and as the downstream that learns the accepted
bindings. The registry (pins, cards, signed binding objects) lives in ``<state>/registry.db`` and is re-pushed to
orch-publish after a restart. One ready line is printed on stdout once the port is bound; SIGTERM / SIGINT stop it.
"""

from __future__ import annotations

import ipaddress
import os
import signal
import sys
import threading
import urllib.parse
from pathlib import Path
from typing import TextIO

from .contract.hub import Refused
from .keys import HubKeys
from .link import LinkServer, Registry
from .publish import HttpPublishSync, HubPublish, PublishConfig
from .router import RouteQueue, Router

REGISTRY_FILE = "registry.db"
ROUTE_QUEUE_FILE = "route-queue.db"


class FixedInvites:
    """Invite codes given on the command line: not bound to a workspace, single use."""

    def __init__(self, codes: list[str]) -> None:
        self._codes = set(codes)
        self._lock = threading.Lock()

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
    u = urllib.parse.urlsplit(publish_url)
    api_host = api_host or u.netloc
    keys = HubKeys.open(state)
    reg_path = state / REGISTRY_FILE
    registry = Registry(str(reg_path))
    os.chmod(reg_path, 0o600)
    queue_path = state / ROUTE_QUEUE_FILE
    queue = RouteQueue(str(queue_path))
    os.chmod(queue_path, 0o600)
    router = Router(registry, queue)
    fw = HubPublish(keys, HttpPublishSync(publish_url), PublishConfig(publish_url, api_host))
    stop = stop or threading.Event()
    server = LinkServer.from_keys(
        keys,
        registry=registry,
        invites=FixedInvites(invites),
        publish=fw,
        downstream=fw,
        router=router,
        host=host,
        port=port,
    )
    try:
        fw.restore(registry.binding_objects())
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
        registry.close()
    return 0
