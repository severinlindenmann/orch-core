"""``orch install server``: one role of the pilot server for the current OS user (see :mod:`orch.install.server`)."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import ops, server
from . import parts as P
from .cli import (
    Ctx,
    _hub_accepts,
    check_block,
    choose_address,
    install_part,
    load,
    refuse_unfinished_update,
    save,
)
from .helper import sync_helper
from .parts import InstallError
from .server import (
    CADDY_DIR,
    ROLES,
    Addr,
    Names,
    Plan,
    caddy_snippet,
    check_dns,
    check_vpn_ip,
    default_resolver,
    detect_vpn_ip,
    local_interfaces,
    proxy_networks,
    root_block,
    user_dir,
    vpn_network,
    write_snippets,
)

__all__ = ["server_setup"]

BACKUP_OUT = "orch-backup-out"  # in the home of the hub user: the folder the owner's Mac pulls from


def _systemd_dir(ctx: Ctx) -> Path:
    return ctx.layout.home / ".config" / "systemd" / "user"


def _install_timers(ctx: Ctx, files: dict[str, str], timers: list[str]) -> None:
    if ctx.runner.kind != "systemd":
        ctx.say("  ops timers: skipped (they need systemd user units; this computer runs parts another way)")
        return
    d = _systemd_dir(ctx)
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text)
    ctx.run(["systemctl", "--user", "daemon-reload"], dict(ctx.env))
    for t in timers:
        ctx.run(["systemctl", "--user", "enable", "--now", t], dict(ctx.env))


def _update_record(ctx: Ctx) -> Path:
    """install.sh's install record: ``$XDG_DATA_HOME/orch/install.json`` (else ``~/.local/share/orch/``)."""
    base = ctx.env.get("XDG_DATA_HOME")
    return (Path(base) if base else ctx.layout.home / ".local" / "share") / "orch" / "install.json"


def _ops(
    ctx: Ctx,
    role: str,
    parts: list[str],
    names: Names,
    ports: dict[str, int],
    backup_drop: str | None = None,
    hosts: dict[str, str] | None = None,
) -> None:
    """The health timer for every role; the nightly backup copy for the hub."""
    lay = ctx.layout
    ops_dir = lay.root / "ops"
    ops_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg = {
        "parts": [
            {
                "name": p,
                "url": f"http://{(hosts or {}).get(p, '127.0.0.1')}:{ports[p]}{'/' if p == 'hub' else '/healthz'}",
            }
            for p in parts
        ],
        "cert_names": [n for p in parts if (n := names.get(p))],
        "disk_path": str(lay.home),
        "status_file": str(ops_dir / "health-status.json"),
        "min_mem_mb": ops.MIN_MEM_MB,
        "update_record": str(_update_record(ctx)),
    }
    if role == "hub" and backup_drop:
        cfg |= {"backup_drop": backup_drop, "backup_record": str(ops_dir / "backup-copied.json")}
    cfg_file = ops_dir / "health.json"
    cfg_file.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    cfg_file.chmod(0o600)
    files = ops.health_unit_files(sys.executable, cfg_file)
    timers = ["orch-health.timer"]
    if role == "hub":
        out = Path(backup_drop) if backup_drop else lay.home / BACKUP_OUT
        files |= ops.backup_unit_files(
            sys.executable,
            lay.data("hub") / "backups",
            out,
            shared=bool(backup_drop),
            record=ops_dir / "backup-copied.json",
        )
        timers.append("orch-hub-backup.timer")
    _install_timers(ctx, files, timers)
    ctx.say(f"  health check every 5 minutes: the status is in {cfg['status_file']}, warnings go to the journal")
    if role == "hub":
        ctx.say(f"  nightly backup copy: {out} (sealed files; the hub itself makes the backup)")


def real_user() -> str:
    """The OS user this process runs as, from the password database by euid (not LOGNAME/USER, which anyone can set)."""
    import pwd  # POSIX only: imported where used (modules import on every OS, tests/test_oslock.py)

    return pwd.getpwuid(os.geteuid()).pw_name


def server_setup(
    ctx: Ctx,
    role: str,
    *,
    names: Names,
    hub_vpn_only: bool = False,
    vpn_ip: str | None = None,
    address: str | None = None,
    caddy_dir: str = CADDY_DIR,
    source: str | None = None,
    ref: str | None = None,
    dev: bool = False,
    resolve: Callable[[str], list[str]] = default_resolver,
    local: Callable[[], list[Addr]] | None = None,
    user: str | None = None,
    allow_non_cgnat: bool = False,
    backup_drop: str | None = None,
    drop_check: Callable[[str], None] = server.check_drop_path,
    bus_check: Callable[[str], None] | None = None,
    behind_proxy: bool = False,
    hub_name: str | None = None,
    no_hub_name: bool = False,
    publish_url: str | None = None,
    relay_url: str | None = None,
    no_publish_url: bool = False,
    no_relay_url: bool = False,
) -> Plan:
    """Install the parts of ``role`` for this OS user. ``resolve``/``local`` are the DNS seams of the tests."""
    if role not in ROLES:
        raise InstallError("--role is hub or edge")
    refuse_unfinished_update(ctx)  # before anything is stopped, written or installed
    parts = [p for p in ROLES[role]]
    wanted = {p: n for p in parts if (n := names.get(p))}
    if hub_vpn_only and role != "hub":
        raise InstallError("--hub-vpn-only belongs to --role hub")
    if hub_vpn_only and not names.hub:
        raise InstallError("--hub-vpn-only needs a name for the hub (--domain or --domain-hub)")
    if behind_proxy and role != "edge":
        raise InstallError("--behind-proxy belongs to --role edge (the hub is never served through a proxy)")
    if behind_proxy and (hub_vpn_only or address):
        raise InstallError(
            "--behind-proxy listens on the private-network address only: no --address, no --hub-vpn-only"
        )
    named = any(names.get(p) for p in ("hub", "relay", "publish"))  # any domain name: the Caddy and https path
    conflicts = named or hub_vpn_only or behind_proxy or address in (P.ADDRESS_LOOPBACK, "localhost")
    if no_hub_name and hub_name is not None:
        raise InstallError("--hub-name and --no-hub-name exclude each other")
    if (hub_name is not None or no_hub_name) and role != "hub":
        raise InstallError(f"{'--hub-name' if hub_name is not None else '--no-hub-name'} belongs to --role hub")
    if hub_name is not None:
        if named or hub_vpn_only or behind_proxy:
            raise InstallError(
                "--hub-name is for a hub without a domain (plain http on the private network): "
                "not with --domain, --domain-hub, --domain-relay, --domain-publish, --hub-vpn-only or --behind-proxy"
            )
        hub_name = server.check_hub_name(hub_name)
        if address in (P.ADDRESS_LOOPBACK, "localhost"):
            raise InstallError("--hub-name needs the private-network address: not with --address 127.0.0.1")
    elif role == "hub" and not no_hub_name and (saved := load(ctx.layout).get("hub_name")):
        try:  # the state file is input too
            hub_name = server.check_hub_name(saved)
        except (InstallError, TypeError):
            raise InstallError(
                "the hub name saved by an earlier setup is not a valid host name. Run this command again with "
                "--no-hub-name (the hub then answers on its private-network address) or give --hub-name."
            ) from None
        if conflicts:
            raise InstallError(
                f"this hub has the private name {hub_name} from an earlier setup, and the options you gave do not fit "
                "it. To keep the name, leave out --domain, --domain-hub, --domain-relay, --domain-publish, "
                "--hub-vpn-only, --behind-proxy and --address 127.0.0.1. To drop the name, add --no-hub-name."
            )
    hub_urls = _hub_urls(ctx, role, names, publish_url, relay_url, no_publish_url, no_relay_url)
    if backup_drop:
        drop_check(backup_drop)
    user = user or real_user()
    server.check_user_name(user)  # before anything is installed or printed
    if bus_check is not None:
        bus_check(user)
    elif sys.platform.startswith("linux") and ctx.runner.kind == "systemd":
        server.ensure_user_bus(os.environ, os.geteuid(), user)  # before anything is installed
    check_block(ctx, parts)  # --port-base: refuse a busy port before anything is written
    ifaces = (local or (lambda: local_interfaces(lambda argv: ctx.run(argv, dict(ctx.env))[1])))()
    mine = {a.ip for a in ifaces}
    plan = Plan(role=role, parts=tuple(parts), names=names, vpn_only=hub_vpn_only)

    if role == "hub":
        # Never wider than asked: an explicit loopback is honoured, no address means the VPN address, any other address
        # is refused (a public bind would need its own explicit flag, which does not exist yet). Never --bind-all.
        loopback = address in (P.ADDRESS_LOOPBACK, "localhost")
        if loopback and wanted:
            raise InstallError(
                "--address 127.0.0.1 cannot be combined with domain names: the link needs the VPN address"
            )
        need_vpn = not loopback or hub_vpn_only
        chosen, given_prefix = (
            check_vpn_ip(vpn_ip, ifaces, allow_non_cgnat=allow_non_cgnat) if vpn_ip else (detect_vpn_ip(ifaces), None)
        )
        if chosen is None and need_vpn:  # the hub link never listens on every interface: no VPN address, no hub
            raise InstallError(
                "no private-network (VPN) address found on this computer. "
                "Connect the VPN first, or give it with --vpn-ip."
            )
        if address and not loopback and chosen and address != chosen.ip:
            raise InstallError(
                f"--address {address}: the hub of a server listens on the private-network address {chosen.ip} or on "
                "127.0.0.1 only (a public address is not offered yet)"
            )
        if not loopback and chosen:
            plan.vpn_ip = chosen.ip
            if allow_non_cgnat and vpn_ip and not server.is_cgnat(chosen.ip):
                ctx.say(server.non_cgnat_warning(chosen, given_prefix))
        if chosen and hub_vpn_only:  # the gate lets in the VPN's real network, not the whole 100.64.0.0/10
            plan.vpn_net = str(vpn_network(chosen, given_prefix))
        plan.loopback = loopback
        if hub_name:
            if not plan.vpn_ip:
                raise InstallError("--hub-name needs the private-network (VPN) address of this computer")
            server.check_hub_name_target(hub_name, plan.vpn_ip, resolve)  # before anything is stopped or written

    if behind_proxy:
        _proxied(ctx, plan, names, ifaces, vpn_ip, allow_non_cgnat, source=source, ref=ref, dev=dev)
        return plan

    if not wanted:
        _fallback(
            ctx,
            plan,
            address=address,
            source=source,
            ref=ref,
            dev=dev,
            backup_drop=backup_drop,
            hub_name=hub_name,
            forget_hub_name=no_hub_name,
            hub_urls=hub_urls,
        )
        return plan

    plan.dns = check_dns(wanted, resolve, mine)
    for d in plan.dns:
        ctx.say(("  ok  " if d.ok else "  !!  ") + d.words())
    tls = set(plan.tls_parts)

    ports: dict[str, int] = {}
    loop = P.ADDRESS_LOOPBACK
    for p in parts:
        https = names.url(p) if p in tls else None
        extra: dict[str, Any] = {}
        edge = https is not None
        if p == "hub":
            extra = dict(
                exact_bind=True,
                behind_edge=edge,
                link_address=plan.vpn_ip,
                web_origin=https,
                vpn_only=hub_vpn_only,
                forget_hub_name=no_hub_name,
                publish_url=hub_urls.get("publish")
                or (names.url("publish") if "publish" in tls_all(names, plan, role) else None),
                relay_url=hub_urls.get("relay")
                or (names.url("relay") if "relay" in tls_all(names, plan, role) else None),
                publish_share_url=names.url("publish") if "publish" in tls_all(names, plan, role) else None,
            )
        if p == "relay":
            extra = dict(behind_edge=edge, exact_bind=True)
        if p == "publish":
            extra = dict(public_origin=https, workspace_side=False, exact_bind=True)
        rec = install_part(ctx, p, address=loop, source=source, ref=ref, dev=dev, sync=False, **extra)
        ports[p] = rec["port"]
        if p in tls:
            plan.snippets[p] = caddy_snippet(
                p,
                wanted[p],
                rec["port"],
                vpn_only=(p == "hub" and hub_vpn_only),
                vpn_net=plan.vpn_net or server.VPN_NET,
            )
    sync_helper(ctx)
    _remember_hub_urls(ctx, role, hub_urls)

    st = load(ctx.layout)
    st["server"] = {
        "role": role,
        "names": {p: n for p in parts if (n := names.get(p))},
        "hub_vpn_only": hub_vpn_only,
        "vpn_ip": plan.vpn_ip,
        "tls": sorted(tls),
    }
    save(ctx.layout, st)

    _caddy(ctx, plan, caddy_dir, user)
    _ops(ctx, role, parts, names, ports, backup_drop)
    _closing(ctx, plan, wanted, tls, user, caddy_dir)
    _say_binds(
        ctx,
        plan,
        {
            p: {"bind": loop, "port": ports[p], "link_port": load(ctx.layout)["parts"]["hub"].get("link_port")}
            for p in ports
            if p == "hub"
        },
    )
    return plan


def _hub_urls(
    ctx: Ctx,
    role: str,
    names: Names,
    publish_url: str | None,
    relay_url: str | None,
    no_publish_url: bool = False,
    no_relay_url: bool = False,
) -> dict[str, str]:
    """``--publish-url`` / ``--relay-url`` of the hub (split install: relay and publish run as another user), per key:
    an explicit flag wins and is saved; else a name-derived (domain) URL wins and the saved value is dropped; else the
    saved value is reused (checked again: the state file is input). ``--no-…-url`` drops the saved value. The result is
    what the record keeps. Nothing is written here."""
    if role != "hub":
        if publish_url is not None or relay_url is not None or no_publish_url or no_relay_url:
            raise InstallError("--publish-url, --relay-url, --no-publish-url and --no-relay-url belong to --role hub")
        return {}
    keys = (
        ("publish", publish_url, no_publish_url, "--publish-url", "--no-publish-url"),
        ("relay", relay_url, no_relay_url, "--relay-url", "--no-relay-url"),
    )
    for _, given, forget, flag, no_flag in keys:
        if forget and given is not None:
            raise InstallError(f"{flag} and {no_flag} exclude each other")
    raw = load(ctx.layout).get("hub_urls")
    damaged = raw is not None and not isinstance(raw, dict)
    saved: dict[str, Any] = raw if isinstance(raw, dict) else {}
    out: dict[str, str] = {}
    for key, given, forget, flag, no_flag in keys:
        url = given
        if given is None and not forget and not names.get(key):  # nothing overrides it: the saved value is reused
            if damaged:
                raise InstallError(
                    "the hub URLs saved by an earlier setup are damaged (the saved value is not valid): "
                    f"give {flag} or {no_flag} again"
                )
            url = saved.get(key)
            if key in saved and not isinstance(url, str):
                raise InstallError(
                    f"{flag} {url!r}: the saved value is not valid (not a text): give the flag again or add {no_flag}"
                )
        if url is None:
            continue
        if not _hub_accepts(url):
            raise InstallError(
                f"{flag} {url!r}: the hub accepts https, or http on a loopback or private-network address only"
                + ("" if given is not None else " (the saved value is not valid: give the flag again)")
            )
        out[key] = url
    return out


def _remember_hub_urls(ctx: Ctx, role: str, urls: dict[str, str]) -> None:
    """Written after the install: the record keeps exactly ``urls`` (a dropped key is gone). Hub role only."""
    if role != "hub":
        return
    st = load(ctx.layout)
    if st.get("hub_urls") == (urls or None):
        return
    if urls:
        st["hub_urls"] = urls
    else:
        st.pop("hub_urls", None)
    save(ctx.layout, st)


def tls_all(names: Names, plan: Plan, role: str) -> set[str]:
    """Parts of the OTHER role of this server are not checked from here; their names are advertised as given
    (the edge user sets up relay and publish; the hub only needs their addresses)."""
    ok = set(plan.tls_parts)
    return ok | {p for p in ("relay", "publish") if role == "hub" and names.get(p)}


def _fallback(
    ctx: Ctx,
    plan: Plan,
    *,
    address: str | None,
    source: str | None,
    ref: str | None,
    dev: bool,
    backup_drop: str | None = None,
    hub_name: str | None = None,
    forget_hub_name: bool = False,
    hub_urls: dict[str, str] | None = None,
) -> None:
    """No names: the IP:port behaviour of ``orch install`` (a private-network hub takes its VPN address)."""
    st = load(ctx.layout)
    hub = plan.role == "hub"
    addr = (P.ADDRESS_LOOPBACK if plan.loopback else plan.vpn_ip) if hub else (address or choose_address(ctx, None, st))
    ctx.say("No domain name given: the parts listen on an IP address and a port (no secure https address).")
    recs = {}
    for p in plan.parts:
        # never --bind-all, and no part listens wider than the address it was given
        extra = dict(exact_bind=True)
        if p == "publish":
            extra["workspace_side"] = False  # the edge user has no workspace; the Macs set publish up themselves
        if p == "hub" and not plan.loopback:
            extra["link_address"] = plan.vpn_ip
            if hub_name:
                extra["hub_name"] = hub_name
            extra["forget_hub_name"] = forget_hub_name
            extra.update({f"{k}_url": v for k, v in (hub_urls or {}).items()})
        recs[p] = install_part(ctx, p, address=addr, source=source, ref=ref, dev=dev, sync=False, **extra)
    sync_helper(ctx)
    _remember_hub_urls(ctx, plan.role, hub_urls or {})
    if hub_name:
        st = load(ctx.layout)
        st["hub_name"] = hub_name
        save(ctx.layout, st)
        ctx.say(
            f"The hub console answers at http://{hub_name}:{recs['hub']['port']}; phones pair with this name. "
            f"The VPN address http://{plan.vpn_ip}:{recs['hub']['port']} keeps working on the private network."
        )
        ctx.say(f"Each Mac must resolve {hub_name} to {plan.vpn_ip} (private DNS); the hub link stays on the IP.")
    for p, rec in recs.items():
        ctx.say(f"  {p} address: http://{rec['bind']}:{rec['port']}")
    _ops(
        ctx,
        plan.role,
        list(plan.parts),
        plan.names,
        {p: r["port"] for p, r in recs.items()},
        backup_drop,
        {p: r["bind"] for p, r in recs.items()},
    )
    _say_binds(ctx, plan, recs)


def _proxied(
    ctx: Ctx,
    plan: Plan,
    names: Names,
    ifaces: list[Addr],
    vpn_ip: str | None,
    allow_non_cgnat: bool,
    *,
    source: str | None,
    ref: str | None,
    dev: bool,
) -> None:
    """``--behind-proxy``: the private network's reverse proxy is the public front. Relay and publish listen on the
    private-network address ONLY (never loopback-wider, never every interface), no Caddy snippet is written, and the
    forwarded client address is honoured only from the VPN's own networks as a rate-limit key. Every request is public:
    being on the VPN or forwarded grants nothing."""
    chosen, given_prefix = (
        check_vpn_ip(vpn_ip, ifaces, allow_non_cgnat=allow_non_cgnat) if vpn_ip else (detect_vpn_ip(ifaces), None)
    )
    if chosen is None:
        raise InstallError(
            "no private-network (VPN) address found on this computer. Connect the VPN first, or give it with --vpn-ip."
        )
    nets = proxy_networks(chosen, given_prefix, ifaces)
    plan.behind_proxy, plan.vpn_ip, plan.proxy_nets = True, chosen.ip, tuple(nets)
    if allow_non_cgnat and vpn_ip and not server.is_cgnat(chosen.ip):
        ctx.say(server.non_cgnat_warning(chosen, given_prefix))
    ctx.say(f"Behind the private network's proxy: relay and publish listen on {chosen.ip} only (no Caddy snippet).")
    recs = {}
    for p in plan.parts:
        extra: dict[str, Any] = dict(exact_bind=True, trusted_proxy=",".join(nets))
        if p == "publish":
            extra |= dict(workspace_side=False, public_origin=names.url("publish"))
        recs[p] = install_part(ctx, p, address=chosen.ip, source=source, ref=ref, dev=dev, sync=False, **extra)
    sync_helper(ctx)
    st = load(ctx.layout)
    st["server"] = {
        "role": plan.role,
        "names": {p: n for p in plan.parts if (n := names.get(p))},
        "behind_proxy": True,
        "vpn_ip": chosen.ip,
        "proxy_networks": nets,
    }
    save(ctx.layout, st)
    _ops(
        ctx,
        plan.role,
        list(plan.parts),
        names,
        {p: r["port"] for p, r in recs.items()},
        None,
        {p: r["bind"] for p, r in recs.items()},
    )
    for p, rec in recs.items():
        ctx.say(f"  {p}: {chosen.ip}:{rec['port']} (point the private network's proxy at this address)")
    ctx.say(f"Forwarded client addresses are used as a rate-limit key only, from: {', '.join(nets)}.")


def _caddy(ctx: Ctx, plan: Plan, caddy_dir: str, user: str) -> None:
    if not plan.snippets:
        return
    try:
        written = write_snippets(user_dir(caddy_dir, user), plan.snippets)
    except InstallError as e:
        ctx.say(f"  Caddy snippets: not written ({e}).")
        return
    except OSError as e:
        ctx.say(
            f"  Caddy snippets: not written ({user_dir(caddy_dir, user)}: {e.strerror or e}). "
            "Do the root steps below, then rerun."
        )
        return
    for w in written:
        ctx.say(f"  Caddy snippet written: {w}")


def _closing(ctx: Ctx, plan: Plan, wanted: dict[str, str], tls: set[str], user: str, caddy_dir: str) -> None:
    ctx.say()
    skipped = [d for d in plan.dns if not d.ok]
    if skipped:
        ctx.say("Not finished: " + ", ".join(d.name for d in skipped) + " does not point at this computer yet.")
        ctx.say("Fix the DNS record, wait a few minutes, then run this same command again.")
    if tls:
        ctx.say(root_block(user, caddy_dir))
        ctx.say("Run the block above after the setup (and again if you change a name), then Caddy serves:")
        for p in sorted(tls):
            ctx.say(f"  {p}: https://{wanted[p]}")
    if plan.names.publish and "publish" in tls:
        pub = f"https://{plan.names.publish}"
        ctx.say(f"On each Mac, once: orch publish setup --server {pub} --share {pub}")
    if plan.role == "hub" and plan.vpn_ip:
        ctx.say(
            f"The hub link is at {plan.vpn_ip}:{load(ctx.layout)['parts']['hub']['link_port']} (private network only)."
        )
        if plan.vpn_only and plan.names.hub:
            ctx.say(
                f"Each Mac must resolve {plan.names.hub} to {plan.vpn_ip} (a private-network DNS record, or an "
                "/etc/hosts line); see docs/pilot-server.md."
            )


def _say_binds(ctx: Ctx, plan: Plan, recs: dict[str, dict[str, Any]]) -> None:
    """Plainly: where the hub listens (console and link). Printed at the end of every hub setup."""
    rec = recs.get("hub")
    if rec is None:
        return
    link = plan.vpn_ip or P.ADDRESS_LOOPBACK
    ctx.say(f"The hub listens on: console {rec['bind']}:{rec['port']}, link {link}:{rec['link_port']} (nothing else).")
