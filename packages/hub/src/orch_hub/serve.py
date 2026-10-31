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
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from . import colocation as CO
from .access import Access, AccessSnapshots
from .access.cli import ACCESS_FILE
from .access.orgcleanup import org_cleanup
from .activity import ActivityLog
from .audit import open_log
from .backup.hubop import FENCE as RESTORE_FENCE
from .backup.hubop import hold_serve_lock, hold_state, release_state
from .backup.restore import finish as finish_restore
from .backup.restore import recover as recover_swap
from .backup.service import BackupService
from .changes import Changes
from .channels import Channels
from .channels.pushkeys import PushKeyStore
from .channels.service import CHANNELS_FILE
from .closing import Closing
from .closing_launch import Launcher
from .config import HubConfig
from .connections import ConnectionAuthz, Connections
from .contract.hub import Refused
from .custodial import CustodialEngine
from .custodial.deliver import build_delivery
from .dataformat import check_update
from .hooks.inbox import HookInbox
from .hooks.listener import HookListener, parse_public_url
from .hooks.store import HookStore
from .invites import INVITES_FILE, Invites
from .keys import HubKeys
from .link import LinkServer, Registry
from .link.wkseal import WkSeals
from .oidc.service import OidcService
from .oidc.store import OidcStore
from .publish import HttpPublishSync, HubPublish, PublishConfig
from .reconcile import reconcile_full_only, reconcile_relay_purge
from .relay import PushNotifier, RelayClient, RelayError
from .relay.client import Transport
from .remote import wire_remote
from .restart import exec_self
from .router import AccessAuthz, RouteQueue, Router
from .services import Services
from .services.helper_client import HelperClient
from .services.helper_server import SOCK_FILE
from .settings import SETTINGS_FILE, SettingsError, serve_origins, where_answer
from .settings import load as load_settings
from .shares import ShareService, ShareStore, hxk_handle
from .spaces import SpaceService, SpaceStore
from .switch import SwitchService
from .view import build_view_services
from .webapi import SessionBridge, WebApi, WebDeps, WebServer
from .webapi.settings_api import SettingsControl

REGISTRY_FILE = "registry.db"
HOOKS_FILE = "hooks.db"  # #1285: webhook inbox (hook secrets, one ring per hook); short-lived, not in backups
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


def _link_endpoint(state: Path, fallback: str) -> str:
    """The link address the connect link carries: the first advertised address that has one (hub-settings.json, read when
    the link is made so a change shows at once); the bound address only when the file has none."""
    try:
        s = load_settings(state)
    except Exception:  # noqa: BLE001  # a damaged file must not stop "connect a workspace"; the bound address answers
        return fallback
    link = next((a["link"] for a in (s or {"addresses": []})["addresses"] if a["link"]), None)
    return link or fallback


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


def publish_hint(publish_url: str | None) -> str | None:
    """The publish address a joined workspace may keep as a hint (P02): an origin core accepts (https anywhere, plain http on
    loopback only), else None: a workspace is then asked as before. It carries no authority."""
    from .contract.where import publish_ok

    if publish_url is None:
        return None
    u = urllib.parse.urlsplit(publish_url)
    origin = f"{u.scheme}://{u.netloc}".lower()
    return origin if publish_ok(origin) and u.path in ("", "/") else None


def make_admission(
    state: Path, registry: Registry, access: Access, hub_id: str
) -> Callable[[str], tuple[str, int] | None]:
    """Who the hub vouches for at orch-publish (``hub.allow``, protocol 11.9), as ``workspace -> (owner, binding_seq)``:
    only a workspace that is bound to THIS hub right now, whose owner is the hub user the admin bound at the invite and that
    user is a hub admin, and only while the hub setting "Let this hub's workspaces share files" is not off. Read live on
    every call: switching it off or removing the admin stops the next admission. Anything unknown is None (fail closed)."""

    def admission(ws: str) -> tuple[str, int] | None:
        try:
            cfg = load_settings(state)
        except SettingsError:
            return None
        if cfg is not None and cfg.get("share_files", True) is not True:
            return None
        st, pin, alias = registry.binding_state(ws), registry.pin(ws), registry.owner_alias(ws)
        b = None if st is None else st.get("binding")
        if st is None or pin is None or alias is None or st.get("status") != "bound" or type(b) is not dict:
            return None
        if b.get("hub_id") != hub_id or type(st.get("binding_seq")) is not int:
            return None
        if not access.is_hub_admin(alias):
            return None
        return pin["owner_person_id"], st["binding_seq"]

    return admission


def serve(
    state: Path,
    listen: str | None,
    publish_url: str,
    invites: list[str],
    *,
    link_listen: str | None = None,
    dev_shared_machine: bool = False,
    bind_all: bool = False,
    link_ws_listen: str | None = None,
    link_ws_public: str | None = None,
    link_ws_behind_front: bool = False,
    accounts: CO.Accounts | None = None,
    assigned: Callable[[str], bool] = CO.locally_assigned,
    api_host: str | None = None,
    web_listen: str | None = None,
    relay_url: str | None = None,
    trusted_peers: frozenset[str] = frozenset(),
    behind_edge: bool = False,
    web_origins: frozenset[str] = frozenset(),
    api_origin: str | None = None,
    hooks_listen: str | None = None,
    public_hooks_url: str | None = None,
    relay_transport: Transport | None = None,
    stop: threading.Event | None = None,
    out: TextIO | None = None,
    restart_exec: Callable[[], None] | None = None,
) -> int:
    out = out or sys.stdout
    stop = stop or threading.Event()
    restarting = threading.Event()  # set by the settings route: serve returns, then the process starts again (C3)
    if (listen is None) == (link_listen is None):
        raise ValueError("exactly one of --listen (loopback) and --link-listen (network) is required")
    if listen is not None:
        if dev_shared_machine:
            raise ValueError("--dev-shared-machine only goes with --link-listen")
        host, port = parse_listen(listen)
    else:
        assert link_listen is not None
        host, port = CO.parse_link_listen(link_listen, assigned=assigned)  # raises BindRefused (a ValueError)
    ws_bind: tuple[str, int] | None = None
    if (
        link_ws_listen is not None
    ):  # R6: refused before any state is opened (a public bind needs --link-ws-behind-front)
        ws_bind = CO.parse_link_ws_listen(
            link_ws_listen, assigned=assigned, bind_all=bind_all, behind_front=link_ws_behind_front
        )
    if link_ws_public is not None:
        from .contract.hub import Refused
        from .contract.linkws import parse_link_address

        try:
            if parse_link_address(link_ws_public).scheme == "tcp":
                raise Refused("malformed")
        except Refused:
            raise ValueError("--link-ws-public must be ws://host[:port] or wss://host[:port]") from None
    gate = FixedInvites(invites)  # a bad invite refuses before any state is opened
    u = urllib.parse.urlsplit(publish_url)
    api_host = api_host or u.netloc
    hold_serve_lock(state)  # first: an offline `orch-hub restore` (mid-swap) refuses us, and refuses while we live
    # The format lock first, then the recovery: recover_swap renames and writes the state dir, which must not happen under an
    # `orch update` (it takes the lock exclusive, and refuses on an unfinished journal). A state dir that is gone (a restore
    # stopped between its two renames) cannot be locked yet: only the journal and the sidecar are checked, and the lock is taken
    # as soon as recover_swap has put a dir in place. recover_swap takes the lock again on a dir it swaps (the lock follows the file).
    state = Path(state)
    held = state.exists()
    if held:
        hold_state(state)  # also refuses a state that is not at this code's format, before any database
    else:
        check_update(state)
    recover_swap(state)  # a restore that stopped between its two renames is completed or undone first
    if not held:
        hold_state(state)
    keys = HubKeys.open(state)
    # Opus condition 5: once `orch install` wrote hub-settings.json, the advertised addresses, the console origins and the
    # API origin come from that file ONLY. The start arguments keep the listen sockets; --web-origin / --api-origin are
    # ignored (a restart must not bring an old value back).
    from_file = serve_origins(state)
    file_settings = load_settings(state) if from_file is not None else None
    if from_file is not None and (web_origins or api_origin):
        print("orch-hub WARN hub-settings.json decides the origins: --web-origin / --api-origin ignored", file=out)
    if from_file is not None:
        web_origins, api_origin = (from_file[0], from_file[1]) if behind_edge else (frozenset(), None)
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
    ws_net = ws_bind is not None and not ipaddress.ip_address(ws_bind[0]).is_loopback
    if link_listen is not None or ws_net:  # the same machine rules as the link port for any non-loopback bind
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
    oidc_store = OidcStore(state)  # company login (HUB-PE-04): settings, secret, cookie key, links
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
        access=access,  # a signed audience (P02) is resolved against current group membership
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
        hub_name=file_settings["name"] if file_settings is not None else "Hub",
        badge=lambda ws, user: len(views.reader.needs_you(ws, user)),
    )

    def request_restart() -> None:
        """Called by the address route: stop after the reply had time to go out, then serve returns and restarts."""

        def go() -> None:
            restarting.set()
            stop.set()

        timer = threading.Timer(0.5, go)
        timer.daemon = True
        timer.start()

    def check_origins(origins: frozenset[str], api: str) -> None:
        """The start-up rules for a new origin set (the next start applies them to the file): ValueError if refused."""
        if behind_edge:
            HubConfig.from_card(
                keys.card(),
                listen=cfg.listen,
                web_listen=web_listen,
                trusted_peers=trusted_peers,
                behind_edge=True,
                allowed_origins=origins,
                api_origin=api,
            )

    closing = Closing(
        keys=keys,
        access=access,
        registry=registry,
        channels=channels,
        now_ms=lambda: int(time.time() * 1000),
        launcher=Launcher(state),
        activity=activity,
        audit=audit,
    )  # #1463: removing the hub; its guard sits in the web pipeline and the link server
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
                closing=closing,
                user_invites=user_invites,
                oidc=OidcService(oidc_store),
                engine=engine,
                shares=shares,
                view=views.reader,
                allowed_origins=from_file[0] if from_file is not None else cfg.allowed_origins,
                trusted_peers=cfg.trusted_peers,
                behind_edge=cfg.behind_edge,
                api_origin=from_file[1] if from_file is not None else cfg.resolved_api_origin(),
                invites=gate,
                link_endpoint=lambda: _link_endpoint(state, f"{host}:{server.address[1]}"),
                push=notifier,
                setup_token_file=state / SETUP_TOKEN_FILE,
                settings_file=state / SETTINGS_FILE,
                settings_ctl=SettingsControl(bind_all=bind_all, restart=request_restart, check_origins=check_origins),
                switch=switch,
                services=Services(
                    relay_url=cfg.relay_url,
                    publish_url=publish_url,
                    behind_edge=cfg.behind_edge,
                    edge_url=cfg.resolved_api_origin(),
                ),
                helper=HelperClient(state / SOCK_FILE),
                routes=queue,
                authz=authz,
                connections=connections,
                spaces=spaces,
                delivery=deliveries,
                secure_cookies=cfg.behind_edge,  # plain http on loopback (dev) cannot carry a Secure cookie
            )
        )
        switch.badge = api.needs_count  # R14b: the rail badge is the home list's own count
        switch.set_hub_origin(lambda: api.d.api_origin or cfg.resolved_api_origin())  # the dev server learns it late
        web = WebServer.from_listen(
            api, cfg.web_listen, bind_all=bind_all
        )  # loopback, or one private IP (dev, plain http)
        if not cfg.allowed_origins or (
            from_file is not None and not cfg.behind_edge
        ):  # loopback dev: the console is served from this very address
            # --bind-all makes web.address 0.0.0.0: allow the advertised address (and loopback: a computer dials itself there)
            adv = parse_web_listen(cfg.web_listen)[0]
            api.allow_loopback_origin(f"http://{adv}:{web.address[1]}")
            if adv not in ("127.0.0.1", "::1"):
                api.allow_origin(f"http://127.0.0.1:{web.address[1]}")
            if bind_all or adv in ("127.0.0.1", "::1"):  # loopback answers: tell switch tickets (FB10)
                loop_origin = f"http://127.0.0.1:{web.address[1]}"
                switch.set_loopback_origin(lambda: loop_origin)
    fw = HubPublish(
        keys,
        HttpPublishSync(publish_url),
        PublishConfig(publish_url, api_host),
        admission=make_admission(state, registry, access, keys.hub_id),
    )
    # #1285: the public webhook inbox. Off unless BOTH the public URL and its own listener are given (a half setting
    # is refused: a hook URL that nothing serves, or a listener nobody can name, would only mislead)
    hook_inbox: HookInbox | None = None
    hook_listener: HookListener | None = None
    if (hooks_listen is None) != (public_hooks_url is None):
        raise ValueError("--hooks-listen and --public-hooks-url go together")
    if hooks_listen is not None and public_hooks_url is not None:
        hook_inbox = HookInbox(
            HookStore(str(state / HOOKS_FILE)),
            lambda: int(time.time() * 1000),
            parse_public_url(public_hooks_url, dev=cfg.dev),
        )
        hook_listener = HookListener(hook_inbox, hooks_listen, trusted_peers=cfg.trusted_peers)
    # R4 (#1592): remote access streams. The hub forwards opaque Noise frames and judges only abuse limits (D87); the
    # rail rule and the remote-scope token decide who may open. Registry counts every stream_open by outcome.
    stream_router, remote_endpoint = wire_remote(
        channels, registry, access, hub_id=keys.hub_id, count_op=registry.count_op
    )
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
        hooks=hook_inbox,
        activity=activity,
        where=lambda: where_answer(state, extra_public=link_ws_public, publish=publish_hint(publish_url)),
        closing=closing,
        deliveries=deliveries,
        streams=stream_router,
        remote=remote_endpoint,
        host="0.0.0.0" if bind_all and link_listen is not None else host,  # the address stays the validated one
        port=port,
        ws_host=ws_bind[0] if ws_bind is not None else None,
        ws_port=ws_bind[1] if ws_bind is not None else 0,
        dev_mode=cfg.dev,
    )
    closing.push = server.push_closing
    try:
        org_cleanup(access).retry()  # a drop that a crash or a failure left undone
        shares.reconcile()  # items of a workspace that is no longer a bound selective one are deleted
        spaces.reconcile()  # a workspace that is no longer bound leaves its spaces (also after a crash)
        reconcile_full_only(registry, wk_seals, views.store)  # wraps and synced view of a non-full workspace go
        fw.restore(registry.binding_objects())
        if notifier is not None:
            notifier.restart_pass_async()  # a revoked channel's relay cleanup that a crash left undone

        backup_svc = BackupService(keys, audit=audit, activity=activity)

        def _retry() -> None:  # D9-1 share purge, D9/D15 wrap retire and view drop: retried while the hub runs
            while not stop.wait(30):
                if RESTORE_FENCE.is_set() or closing.active():  # a restore or a removal runs: no background write lands
                    continue
                shares.retry_pending()
                org_cleanup(access).retry()  # company content of a workspace that left, if a drop failed
                reconcile_full_only(registry, wk_seals, views.store)
                reconcile_relay_purge(notifier)  # a CLI revoke (another process) reaches the relay within a tick
                try:
                    changes.sweep()  # access changes past their 14 days expire (and say so in the activity)
                except Exception:  # noqa: BLE001, S110  # the next tick (or the next page load) sweeps again
                    pass
                try:
                    backup_svc.tick()  # the daily backup (once a key is set)
                except Exception:  # noqa: BLE001, S110  # tried again after an hour; the page lists what exists
                    pass

        closing.resume()  # a removal that a restart interrupted goes on from its last step
        threading.Thread(target=_retry, name="state-retry", daemon=True).start()
        deliveries.start()
        stream_router.start()  # the 60 s sweep: users who lost access, expired streams
        server.start()
        if web is not None:
            web.start()
        if hook_listener is not None:
            hook_listener.start()
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
        if server.ws_address is not None:
            print(f"orch-hub link-ws port={server.ws_address[1]}", file=out, flush=True)
        if web is not None:
            print(f"orch-hub web port={web.address[1]}", file=out, flush=True)
        if hook_listener is not None:
            print(f"orch-hub hooks port={hook_listener.address[1]}", file=out, flush=True)
        finish_restore(state)  # a good start after a restore: the pre-restore state dir (old keys) goes
        for code in gate.codes():  # protocol §5 invite form: what the workspace owner passes to `orch hub join`
            print(invite_line(keys.hub_id, code), file=out, flush=True)
        stop.wait()
    finally:
        failed = False
        for close in (
            web.close if web is not None else None,
            hook_listener.close if hook_listener is not None else None,
            hook_inbox.store.close if hook_inbox is not None else None,
            stream_router.close_all,  # every client connection ends before the link server goes
            server.close,
            deliveries.close,
            channels.close,
            views.close,
            queue.close,
            space_store.close,
            share_store.close,
            changes.close,
            activity.close,
            connections.close,
            user_invites.close,
            oidc_store.close,
            audit.store.close,
            registry.close,
            wk_seals.close,
            access.close,
        ):
            if close is None:
                continue
            try:
                close()
            except Exception as e:  # noqa: BLE001  # keep closing the rest, then exit non-zero (a service manager restarts us)
                failed = True
                print(f"orch-hub WARN close failed: {type(e).__name__}", file=out, flush=True)
        release_state()  # last: no database is open any more, `orch update` may take the data now
    if failed:  # also when a restart was asked for: no exec on top of a half-closed process
        return 1
    if (
        restarting.is_set()
    ):  # everything above is closed; exec replaces this process (same pid) with a clean descriptor table
        (restart_exec or exec_self)()
    return 0
