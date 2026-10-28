"""``orch-hub serve``: the dev hub process (link server + publish forwarding). Dev only in V3: loopback listen only.

Starts :class:`~orch_hub.link.LinkServer` from the key store, with :class:`~orch_hub.publish.HubPublish` (over
:class:`~orch_hub.publish.HttpPublishSync`) as the publish forwarder and as the downstream that learns the accepted
bindings. The registry (pins, cards, signed binding objects) lives in ``<state>/registry.db`` and is re-pushed to
orch-publish after a restart. One ready line is printed on stdout once the port is bound; SIGTERM / SIGINT stop it.
"""

from __future__ import annotations

import ipaddress
import re
import secrets
import signal
import sys
import threading
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from . import colocation as CO
from .access import Access, AccessSnapshots
from .access.cli import ACCESS_FILE
from .activity import ActivityLog
from .audit import open_log
from .changes import Changes
from .channels import Channels
from .channels.pushkeys import PushKeyStore
from .channels.service import CHANNELS_FILE
from .config import HubConfig
from .connections import ConnectionAuthz, Connections
from .contract.hub import Refused
from .custodial import CustodialEngine
from .custodial.deliver import build_delivery
from .invites import INVITES_FILE, Invites
from .keys import HubKeys
from .link import LinkServer, Registry
from .link.wkseal import WkSeals
from .publish import HttpPublishSync, HubPublish, PublishConfig
from .reconcile import reconcile_full_only, reconcile_relay_purge
from .relay import PushNotifier, RelayClient, RelayError
from .relay.client import Transport
from .router import AccessAuthz, RouteQueue, Router
from .services import Services
from .shares import ShareService, ShareStore, hxk_handle
from .spaces import SpaceService, SpaceStore
from .switch import SwitchService
from .view import build_view_services
from .webapi import SessionBridge, WebApi, WebDeps, WebServer

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
        self._expires: dict[str, int] = {}  # minted codes only: code -> expiry ms
        self._lock = threading.Lock()

    def mint(self, now_ms: int, ttl_ms: int = 30 * 60_000, owner_user: str | None = None) -> tuple[str, int]:
        """A fresh single-use code (admin console "Connect a workspace"); expires after ``ttl_ms``.
        ``owner_user`` is the hub user the joining workspace's owner is aliased to (V3-D19)."""
        code = secrets.token_urlsafe(12)
        with self._lock:
            self._codes.add(code)
            if owner_user:
                self._owner[code] = owner_user
            self._expires[code] = now_ms + ttl_ms
        return code, now_ms + ttl_ms

    def codes(self) -> list[str]:
        """The unused codes, sorted (never the ``=USER_ID`` alias part)."""
        with self._lock:
            return sorted(self._codes)

    def owner_user(self, invite: str) -> str | None:
        return self._owner.get(invite)

    def check(self, invite: str, now_ms: int) -> str | None:
        with self._lock:
            if invite not in self._codes or self._expires.get(invite, now_ms + 1) <= now_ms:
                raise Refused("bad_invite")
        return None

    def consume(self, invite: str, workspace_id: str) -> None:
        with self._lock:
            self._codes.discard(invite)


SETUP_TOKEN_FILE = "setup-token"  # written by `orch install hub`; consumed by the console's first-admin page


def invite_line(hub_id: str, code: str) -> str:
    """The workspace invite form ``<hub_id>:<code>`` (protocol §5), printed wherever the hub shows an invite."""
    return f"invite {hub_id}:{code}"


def parse_listen(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    try:
        ok = bool(sep) and ipaddress.ip_address(host.strip("[]")).is_loopback and port.isdigit() and int(port) < 65536
    except ValueError:
        ok = False
    if not ok:
        raise ValueError("--listen must be a loopback host:port (dev only), e.g. 127.0.0.1:0")
    return host.strip("[]"), int(port)


def parse_web_listen(value: str, *, assigned: Callable[[str], bool] = CO.locally_assigned) -> tuple[str, int]:
    """The console bind: loopback, or (prototype, no TLS) one literal private/NetBird IP of this computer."""
    host, _, _port = value.rpartition(":")
    try:
        if ipaddress.ip_address(host.strip("[]")).is_loopback:
            return parse_listen(value)
    except ValueError:
        pass
    return CO.parse_link_listen(value, assigned=assigned)


def serve(
    state: Path,
    listen: str | None,
    publish_url: str,
    invites: list[str],
    *,
    link_listen: str | None = None,
    dev_shared_machine: bool = False,
    bind_all: bool = False,
    accounts: CO.Accounts | None = None,
    assigned: Callable[[str], bool] = CO.locally_assigned,
    api_host: str | None = None,
    web_listen: str | None = None,
    relay_url: str | None = None,
    trusted_peers: frozenset[str] = frozenset(),
    behind_edge: bool = False,
    web_origins: frozenset[str] = frozenset(),
    api_origin: str | None = None,
    relay_transport: Transport | None = None,
    stop: threading.Event | None = None,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    if (listen is None) == (link_listen is None):
        raise ValueError("exactly one of --listen (loopback) and --link-listen (network) is required")
    if listen is not None:
        if dev_shared_machine:
            raise ValueError("--dev-shared-machine only goes with --link-listen")
        host, port = parse_listen(listen)
    else:
        assert link_listen is not None
        host, port = CO.parse_link_listen(link_listen, assigned=assigned)  # raises BindRefused (a ValueError)
    gate = FixedInvites(invites)  # a bad invite refuses before any state is opened
    u = urllib.parse.urlsplit(publish_url)
    api_host = api_host or u.netloc
    keys = HubKeys.open(state)
    # The one HubConfig of this process (orch-hub#30): the dev flag is the held card's, every service gets it below.
    cfg = HubConfig.from_card(
        keys.card(),
        listen=listen if listen is not None else f"{host}:{port}",
        web_listen=web_listen,
        relay_url=relay_url,
        trusted_peers=trusted_peers,
        behind_edge=behind_edge,
        allowed_origins=web_origins,
        api_origin=api_origin,
    )
    bind_warn: str | None = None
    if link_listen is not None:
        acc = accounts if accounts is not None else CO.Accounts.load()
        bind_warn = CO.check_bind_allowed(
            acc, hub_user=CO.current_user(acc), card_dev=cfg.dev, dev_shared_machine=dev_shared_machine
        )
    reg_path = state / REGISTRY_FILE
    registry = Registry(str(reg_path))
    access = Access(str(state / ACCESS_FILE))
    queue_path = state / ROUTE_QUEUE_FILE
    queue = RouteQueue(str(queue_path))
    wk_seals = WkSeals(str(state / WK_SEAL_FILE), hub_id=keys.hub_id, hxk_pub=keys.hxk_pub, exchange=keys.hxk_exchange)
    connections = Connections(str(state / "connections.db"))
    access_authz = AccessAuthz(registry, access)
    authz = ConnectionAuthz(connections, access_authz)  # a connection AND the access rule, fail closed
    router = Router(registry, queue, authz, on_routed=connections.bump)
    audit = open_log(state, keys, dev_mode=cfg.dev)
    activity = ActivityLog(state / "activity.db")
    user_invites = Invites(state / INVITES_FILE)  # join / reset / recovery links of people (the token is never stored)
    access.observer = audit.access_change  # D4: every grant and membership change is audited
    space_store = SpaceStore(str(state / SPACES_FILE))
    spaces = SpaceService(
        space_store,
        registry,
        access,
        keys.op_signer,
        keys.hub_id,
        lambda: keys.op_cert()["o"]["cert_id"],
        dev_mode=cfg.dev,
        on_change=audit.space_change,
    )
    share_store = ShareStore(str(state / SHARES_FILE))
    shares = ShareService(
        share_store,
        registry,
        hub_id=keys.hub_id,
        hxk=hxk_handle(keys.hxk_pub, keys.hxk_exchange),
        dev_mode=cfg.dev,
        on_change=audit.share_change,
    )
    views = build_view_services(state, wk_seals=wk_seals, registry=registry, access=access)
    channels = Channels(keys, access, state / CHANNELS_FILE, audit=audit, dev_mode=cfg.dev)
    changes = Changes(  # access changes that wait for a workspace owner (M2-8a)
        str(state / "changes.db"),
        access=access,
        registry=registry,
        snapshots=AccessSnapshots(access, keys),
        keys=keys,
        channels=channels,
        activity=activity,
        connections=connections,
    )
    # first start with connections: carry over what routing allowed before (audited once, as ``migrated``)
    bound = [ws for ws in sorted(registry.pins()) if registry.mode(ws) is not None]
    migrated = connections.backfill([(a, b) for a in bound for b in bound if a != b and access_authz.may_route(a, b)])
    if migrated and activity is not None:
        activity.record("connection.migrated", actor_kind="hub", detail={"count": migrated})
    notifier: PushNotifier | None = None
    try:
        client = RelayClient(keys, cfg.relay_url, dev=cfg.dev, transport=relay_transport)
    except RelayError as e:
        if cfg.relay_url is not None:  # a relay that was named but is unusable: refuse rather than run without push
            raise ValueError(f"relay: {e.code}") from None
        client = None  # no relay URL on the card or the command line: no push, everything else runs
    if client is not None:
        notifier = PushNotifier(client, channels, PushKeyStore(channels.db))  # keys survive a restart
        channels.on_revoke = notifier.channel_revoked_async  # D10: a revoked channel's push target and mailbox go
    deliveries = build_delivery(str(state))  # §8.7: persisted, acked, resent; writes on its own attached links (#51)
    engine = CustodialEngine(
        keys=keys,
        snapshots=AccessSnapshots(access, keys),
        seals=wk_seals,
        view=views.workspace_view,
        channels=channels,
        sessions=SessionBridge(channels),
        audit=audit,
        deliver=deliveries,
        shares=shares,  # selective mode builds from one shared item (§8.6)
        mode_of=registry.mode,
        dev_mode=cfg.dev,
        notify=notifier.notify_async if notifier else None,
    )
    switch = SwitchService(
        hub_origin=cfg.resolved_api_origin,
        badge=lambda ws, user: len(views.reader.needs_you(ws, user)),
    )
    web: WebServer | None = None
    if cfg.web_listen is not None:
        api = WebApi(
            WebDeps(
                keys=keys,
                access=access,
                channels=channels,
                registry=registry,
                audit=audit,
                activity=activity,
                changes=changes,
                user_invites=user_invites,
                engine=engine,
                shares=shares,
                view=views.reader,
                allowed_origins=cfg.allowed_origins,
                trusted_peers=cfg.trusted_peers,
                behind_edge=cfg.behind_edge,
                api_origin=cfg.resolved_api_origin(),
                invites=gate,
                link_endpoint=lambda: f"{host}:{server.address[1]}",
                push=notifier,
                setup_token_file=state / SETUP_TOKEN_FILE,
                switch=switch,
                services=Services(
                    relay_url=cfg.relay_url,
                    publish_url=publish_url,
                    behind_edge=cfg.behind_edge,
                    edge_url=cfg.resolved_api_origin(),
                ),
                routes=queue,
                authz=authz,
                connections=connections,
                delivery=deliveries,
                secure_cookies=cfg.behind_edge,  # plain http on loopback (dev) cannot carry a Secure cookie
            )
        )
        switch.set_hub_origin(lambda: api.d.api_origin or cfg.resolved_api_origin())  # the dev server learns it late
        web = WebServer.from_listen(
            api, cfg.web_listen, bind_all=bind_all
        )  # loopback, or one private IP (dev, plain http)
        if not cfg.allowed_origins:  # loopback dev: the console is served from this very address
            # --bind-all makes web.address 0.0.0.0: allow the advertised address (and loopback: a computer dials itself there)
            adv = parse_web_listen(cfg.web_listen)[0]
            api.allow_loopback_origin(f"http://{adv}:{web.address[1]}")
            if adv not in ("127.0.0.1", "::1"):
                api.allow_origin(f"http://127.0.0.1:{web.address[1]}")
            if bind_all or adv in ("127.0.0.1", "::1"):  # loopback answers: tell switch tickets (FB10)
                loop_origin = f"http://127.0.0.1:{web.address[1]}"
                switch.set_loopback_origin(lambda: loop_origin)
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
        views=views.service,
        shares=shares,
        switch=switch,
        deliveries=deliveries,
        host="0.0.0.0" if bind_all and link_listen is not None else host,  # the address stays the validated one
        port=port,
        dev_mode=cfg.dev,
    )
    try:
        shares.reconcile()  # items of a workspace that is no longer a bound selective one are deleted
        spaces.reconcile()  # a workspace that is no longer bound leaves its spaces (also after a crash)
        reconcile_full_only(registry, wk_seals, views.store)  # wraps and synced view of a non-full workspace go
        fw.restore(registry.binding_objects())
        if notifier is not None:
            notifier.restart_pass_async()  # a revoked channel's relay cleanup that a crash left undone

        def _retry() -> None:  # D9-1 share purge, D9/D15 wrap retire and view drop: retried while the hub runs
            while not stop.wait(30):
                shares.retry_pending()
                reconcile_full_only(registry, wk_seals, views.store)
                reconcile_relay_purge(notifier)  # a CLI revoke (another process) reaches the relay within a tick
                try:
                    changes.sweep()  # access changes past their 14 days expire (and say so in the activity)
                except Exception:  # noqa: BLE001, S110  # the next tick (or the next page load) sweeps again
                    pass

        threading.Thread(target=_retry, name="state-retry", daemon=True).start()
        deliveries.start()
        server.start()
        if web is not None:
            web.start()
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
        card = keys.card()["o"]
        if bind_warn is not None:
            print(f"orch-hub WARN {bind_warn}", file=out, flush=True)
        print(
            f"orch-hub ready port={server.address[1]} tls_spki_sha256={card['tls_spki_sha256']} hub_id={keys.hub_id}",
            file=out,
            flush=True,
        )
        if web is not None:
            print(f"orch-hub web port={web.address[1]}", file=out, flush=True)
        for code in gate.codes():  # protocol §5 invite form: what the workspace owner passes to `orch hub join`
            print(invite_line(keys.hub_id, code), file=out, flush=True)
        stop.wait()
    finally:
        if web is not None:
            web.close()
        server.close()
        deliveries.close()
        channels.close()
        views.close()
        queue.close()
        space_store.close()
        share_store.close()
        changes.close()
        activity.close()
        connections.close()
        user_invites.close()
        audit.store.close()
        registry.close()
        wk_seals.close()
        access.close()
    return 0
