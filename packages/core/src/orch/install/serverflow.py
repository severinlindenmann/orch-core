"""``orch install server``: one role of the pilot server for the current OS user (see :mod:`orch.install.server`)."""

from __future__ import annotations

import getpass
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import ops, server
from . import parts as P
from .cli import Ctx, check_block, choose_address, install_part, load, save
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


def _ops(ctx: Ctx, role: str, parts: list[str], names: Names, ports: dict[str, int]) -> None:
    """The health timer for every role; the nightly backup copy for the hub."""
    lay = ctx.layout
    ops_dir = lay.root / "ops"
    ops_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg = {
        "parts": [{"name": p, "url": f"http://127.0.0.1:{ports[p]}{'/' if p == 'hub' else '/healthz'}"} for p in parts],
        "cert_names": [n for p in parts if (n := names.get(p))],
        "disk_path": str(lay.home),
        "status_file": str(ops_dir / "health-status.json"),
        "min_mem_mb": ops.MIN_MEM_MB,
    }
    cfg_file = ops_dir / "health.json"
    cfg_file.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    cfg_file.chmod(0o600)
    files = ops.health_unit_files(sys.executable, cfg_file)
    timers = ["orch-health.timer"]
    if role == "hub":
        out = lay.home / BACKUP_OUT
        files |= ops.backup_unit_files(sys.executable, lay.data("hub") / "backups", out)
        timers.append("orch-hub-backup.timer")
    _install_timers(ctx, files, timers)
    ctx.say(f"  health check every 5 minutes: the status is in {cfg['status_file']}, warnings go to the journal")
    if role == "hub":
        ctx.say(f"  nightly backup copy: {lay.home / BACKUP_OUT} (sealed files; the hub itself makes the backup)")


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
) -> Plan:
    """Install the parts of ``role`` for this OS user. ``resolve``/``local`` are the DNS seams of the tests."""
    if role not in ROLES:
        raise InstallError("--role is hub or edge")
    parts = [p for p in ROLES[role]]
    wanted = {p: n for p in parts if (n := names.get(p))}
    if hub_vpn_only and role != "hub":
        raise InstallError("--hub-vpn-only belongs to --role hub")
    if hub_vpn_only and not names.hub:
        raise InstallError("--hub-vpn-only needs a name for the hub (--domain or --domain-hub)")
    check_block(ctx, parts)  # --port-base: refuse a busy port before anything is written
    ifaces = (local or (lambda: local_interfaces(lambda argv: ctx.run(argv, dict(ctx.env))[1])))()
    mine = {a.ip for a in ifaces}
    plan = Plan(role=role, parts=tuple(parts), names=names, vpn_only=hub_vpn_only)
    user = user or getpass.getuser()

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
        if chosen and hub_vpn_only:  # the gate lets in the VPN's real network, not the whole 100.64.0.0/10
            plan.vpn_net = str(vpn_network(chosen, given_prefix))
        plan.loopback = loopback

    if not wanted:
        _fallback(ctx, plan, address=address, source=source, ref=ref, dev=dev)
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
                behind_edge=edge,
                link_address=plan.vpn_ip,
                web_origin=https,
                vpn_only=hub_vpn_only,
                publish_url=names.url("publish") if "publish" in tls_all(names, plan, role) else None,
                relay_url=names.url("relay") if "relay" in tls_all(names, plan, role) else None,
            )
        if p == "relay":
            extra = dict(behind_edge=edge)
        if p == "publish":
            extra = dict(public_origin=https, workspace_side=False)
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
    _ops(ctx, role, parts, names, ports)
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


def tls_all(names: Names, plan: Plan, role: str) -> set[str]:
    """Parts of the OTHER role of this server are not checked from here; their names are advertised as given
    (the edge user sets up relay and publish; the hub only needs their addresses)."""
    ok = set(plan.tls_parts)
    return ok | {p for p in ("relay", "publish") if role == "hub" and names.get(p)}


def _fallback(ctx: Ctx, plan: Plan, *, address: str | None, source: str | None, ref: str | None, dev: bool) -> None:
    """No names: the IP:port behaviour of ``orch install`` (a private-network hub takes its VPN address)."""
    st = load(ctx.layout)
    hub = plan.role == "hub"
    addr = (P.ADDRESS_LOOPBACK if plan.loopback else plan.vpn_ip) if hub else (address or choose_address(ctx, None, st))
    ctx.say("No domain name given: the parts listen on an IP address and a port (no secure https address).")
    recs = {}
    for p in plan.parts:
        extra = dict(link_address=plan.vpn_ip) if p == "hub" and not plan.loopback else {}  # never --bind-all
        recs[p] = install_part(ctx, p, address=addr, source=source, ref=ref, dev=dev, sync=False, **extra)
    sync_helper(ctx)
    for p, rec in recs.items():
        ctx.say(f"  {p} address: http://{rec['bind']}:{rec['port']}")
    _say_binds(ctx, plan, recs)


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
