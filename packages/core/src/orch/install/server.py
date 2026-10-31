"""The pilot server setup: ``orch install server --role hub|edge`` (also ``orch setup --server``).

A role is installed by the current OS user only (hub = the hub; edge = relay + publish). Names given: the DNS is
checked, one Caddy snippet per name is written and the root steps are PRINTED as one sudo block (never run). No
names: the IP:port behaviour of ``orch install`` stays. Everything that touches the machine is a seam of
:class:`Env`, so the tests use stubs and golden files."""

from __future__ import annotations

import contextlib
import errno
import ipaddress
import os
import re
import secrets
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from orch import privdir

from . import parts as P
from .parts import InstallError

__all__ = [
    "CADDY_DIR",
    "ROLES",
    "Plan",
    "Addr",
    "DnsResult",
    "Names",
    "caddy_snippet",
    "check_drop_path",
    "check_user_name",
    "ensure_user_bus",
    "check_dns",
    "derive_names",
    "detect_vpn_ip",
    "proxy_networks",
    "local_addresses",
    "local_interfaces",
    "vpn_network",
    "root_block",
    "user_dir",
    "write_snippets",
]

CADDY_DIR = "/etc/caddy/orch.d"
ROLES = {"hub": ("hub",), "edge": ("relay", "publish")}
VPN_NET = "100.64.0.0/10"
_CGNAT = ipaddress.ip_network(VPN_NET)
_NAME = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
#: request body limits (the dev Caddyfile: relay 8 MB); publish carries file uploads (shares 25 MB, app versions 50 MB)
BODY_LIMIT = {"hub": "8MB", "relay": "8MB", "publish": "64MB"}


@dataclass(frozen=True)
class Names:
    """The public name of each part (``None`` = no name: that part uses the IP:port fallback)."""

    hub: str | None = None
    relay: str | None = None
    publish: str | None = None

    def get(self, part: str) -> str | None:
        return getattr(self, part)

    def url(self, part: str) -> str | None:
        n = self.get(part)
        return f"https://{n}" if n else None


def _clean(name: str, flag: str) -> str:
    n = name.strip().lower().rstrip(".")
    if not _NAME.fullmatch(n):
        raise InstallError(f"{flag} {name!r}: that is not a domain name (example: hub.example.com)")
    return n


_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def check_hub_name(given: str) -> str:
    """``--hub-name``: a strict private host name (lower-cased). No wildcard, IP literal, port or trailing dot."""
    n = given.strip().lower()
    bad = InstallError(f"--hub-name {given!r}: that is not a host name (example: core.hub.example.com)")
    if not n or len(n) > 253 or n.endswith(".") or "*" in n or ":" in n:
        raise bad
    labels = n.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(x) for x in labels):
        raise bad
    if all(x.isdigit() for x in labels):  # a dotted number is an IP literal, not a name
        raise bad
    return n


def check_hub_name_target(name: str, vpn_ip: str, resolve: Callable[[str], list[str]]) -> None:
    """The name must resolve, and EVERY answer must be the hub's private-network address."""
    got = sorted(set(resolve(name)))
    if not got:
        raise InstallError(
            f"--hub-name {name}: it does not resolve. Make a private DNS record that points it at {vpn_ip}, then run "
            "this command again."
        )
    if got != [vpn_ip]:
        raise InstallError(
            f"--hub-name {name} resolves to {', '.join(got)}, not only to this hub's private-network address "
            f"{vpn_ip}. Fix the DNS record so it points at {vpn_ip} only."
        )


def derive_names(
    base: str | None, hub: str | None = None, relay: str | None = None, publish: str | None = None
) -> Names:
    """``--domain BASE`` gives hub./relay./publish.BASE; each ``--domain-<part>`` overrides one."""
    b = _clean(base, "--domain") if base else None
    pick = lambda given, label, flag: (  # noqa: E731
        _clean(given, flag) if given else (f"{label}.{b}" if b else None)
    )
    return Names(
        hub=pick(hub, "hub", "--domain-hub"),
        relay=pick(relay, "relay", "--domain-relay"),
        publish=pick(publish, "publish", "--domain-publish"),
    )


# ------------------------------------------------------------------------------------------------------ addresses


@dataclass(frozen=True)
class Addr:
    """One address of this computer: the address, the prefix length of its interface and the interface name."""

    ip: str
    prefix: int | None = None
    iface: str = ""


#: interface names that are a VPN (WireGuard-based, Tailscale-like, macOS utun)
_VPN_IFACES = ("wt", "wg", "tailscale", "nb-", "utun")


def local_interfaces(run: Callable[[list[str]], str] | None = None) -> list[Addr]:
    """Every IPv4 and IPv6 address assigned to this computer with its prefix and interface (``ip -o addr``)."""
    run = run or P._run
    text = run(["ip", "-o", "addr"]) or run(["ifconfig"]) or ""
    found: list[Addr] = []
    for line in text.splitlines():
        m = re.search(r"^\d+:\s+(\S+?):?\s.*?\binet6? ([0-9a-fA-F:.]+)(?:/(\d+))?", line)
        if m is None:
            for m2 in re.finditer(r"\binet6? ([0-9a-fA-F:.]+)(?:/(\d+))?", line):  # ifconfig: no prefix here
                found.append(Addr(_norm(m2.group(1)), int(m2.group(2)) if m2.group(2) else None))
            continue
        found.append(Addr(_norm(m.group(2)), int(m.group(3)) if m.group(3) else None, m.group(1)))
    return [a for a in found if a.ip]


def _norm(ip: str) -> str:
    try:
        return str(ipaddress.ip_address(ip.split("%")[0]))
    except ValueError:
        return ""


def local_addresses(run: Callable[[list[str]], str] | None = None) -> set[str]:
    return {a.ip for a in local_interfaces(run)}


def is_cgnat(ip: str) -> bool:
    return _in_cgnat(ip)


def _in_cgnat(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and a in _CGNAT


def detect_vpn_ip(addresses: Iterable[Addr]) -> Addr | None:
    """The address of this computer in 100.64.0.0/10 (the private network, VPN). Two or more: the one on a VPN
    interface if that is exactly one, else a refusal (never a guess); ``None`` when there is none."""
    hits = [a for a in addresses if _in_cgnat(a.ip)]
    if len(hits) <= 1:
        return hits[0] if hits else None
    on_vpn = [a for a in hits if a.iface.startswith(_VPN_IFACES)]
    if len(on_vpn) == 1:
        return on_vpn[0]
    raise InstallError(
        "this computer has more than one private-network (100.64.x.x - 100.127.x.x) address: "
        + ", ".join(a.ip for a in hits)
        + ". Say which one is your VPN with --vpn-ip."
    )


def vpn_network(chosen: Addr, given_prefix: int | None = None) -> ipaddress.IPv4Network:
    """The real network of the VPN (from the interface prefix, or from ``--vpn-ip IP/PREFIX``), never the whole /10."""
    prefix = given_prefix if given_prefix is not None else chosen.prefix
    if prefix is None or prefix > 24:
        raise InstallError(
            f"cannot tell the network of your VPN from {chosen.ip}"
            + (f" (the interface says /{prefix})" if prefix is not None else "")
            + ". Give it with --vpn-ip, for example --vpn-ip 100.92.1.2/16."
        )
    net = ipaddress.ip_interface(f"{chosen.ip}/{prefix}").network
    if not net.subnet_of(_CGNAT) and not net.is_private:
        raise InstallError(f"--vpn-ip {chosen.ip}/{prefix}: that network is not a private one")
    return net  # type: ignore[return-value]


def proxy_networks(chosen: Addr, given_prefix: int | None, addresses: Iterable[Addr]) -> list[str]:
    """The networks of the VPN interface (IPv4 and any global IPv6 on the same interface): the peers whose forwarded
    client address is a rate-limit key. The private network's proxy connects from anywhere in them. It grants no
    trust and no locality; every request stays public."""
    nets = [str(vpn_network(chosen, given_prefix))]
    for a in addresses:
        if not chosen.iface or a.iface != chosen.iface or ":" not in a.ip or a.prefix is None:
            continue
        ip = ipaddress.ip_address(a.ip)
        if ip.is_link_local or ip.is_loopback or a.prefix > 64:
            continue
        n = str(ipaddress.ip_interface(f"{a.ip}/{a.prefix}").network)
        if n not in nets:
            nets.append(n)
    return nets


def check_vpn_ip(given: str, addresses: Iterable[Addr], *, allow_non_cgnat: bool = False) -> tuple[Addr, int | None]:
    """``--vpn-ip IP[/PREFIX]``: an address this computer has, in 100.64.0.0/10 unless ``allow_non_cgnat``."""
    ip, _, pre = given.partition("/")
    try:
        a = ipaddress.ip_address(ip)
        prefix = int(pre) if pre else None
    except ValueError:
        raise InstallError(
            f"--vpn-ip {given!r}: that is not an IP address (example: 100.92.1.2 or 100.92.1.2/16)"
        ) from None
    if a.version != 4:
        raise InstallError(f"--vpn-ip {given}: the pilot's private network uses IPv4 addresses")
    if prefix is not None and not 8 <= prefix <= 24:
        raise InstallError(f"--vpn-ip {given}: the prefix is a number from 8 to 24 (your VPN's network size)")
    if not _in_cgnat(ip) and not (allow_non_cgnat and a.is_private and not a.is_loopback and not a.is_link_local):
        raise InstallError(
            f"--vpn-ip {given}: use an address of your private network (VPN) in 100.64.x.x - 100.127.x.x"
            + ("" if allow_non_cgnat else " (another private range needs --allow-non-cgnat)")
        )
    have = next((x for x in addresses if x.ip == str(a)), None)
    if have is None:
        raise InstallError(f"--vpn-ip {given}: this computer does not have that address (is the VPN connected?)")
    return have, prefix


def non_cgnat_warning(addr: Addr, prefix: int | None) -> str:
    """The plain warning printed when ``--allow-non-cgnat`` is used."""
    p = prefix if prefix is not None else addr.prefix
    net = f"{ipaddress.ip_interface(f'{addr.ip}/{p}').network}" if p is not None else f"the network of {addr.ip}"
    return (
        f"WARNING: {addr.ip} is not in the usual private-network (VPN) range 100.64.x.x - 100.127.x.x. "
        "The hub link will "
        f"listen on that address, and everyone on {net} will be let into the hub console. "
        "Only use this on a network you fully control."
    )


# ------------------------------------------------------------------------------------------------------------ DNS


@dataclass(frozen=True)
class DnsResult:
    part: str
    name: str
    status: str  # ok | missing | elsewhere | private
    found: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def words(self) -> str:
        tail = (
            "Fix the record, wait a few minutes, then run this command again. "
            f"The secure address (TLS) for {self.part} is skipped until then."
        )
        if self.status == "ok":
            return f"{self.name}: points at this computer"
        if self.status == "missing":
            return (
                f"{self.name}: no DNS record yet. Add an A (and AAAA) record for it that points at this computer, "
                f"wait a few minutes, then run this command again. The secure address (TLS) for {self.part} is "
                "skipped until then."
            )
        if self.status == "private":
            return (
                f"{self.name}: its DNS record points at {', '.join(self.found)}, a private address. The certificate "
                f"authority can only reach a public address. {tail}"
            )
        return (
            f"{self.name}: its DNS record points at {', '.join(self.found)}, which is not (only) this computer. {tail}"
        )


def default_resolver(name: str) -> list[str]:
    try:
        return sorted({i[4][0] for i in socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)})
    except OSError:
        return []


def check_dns(
    wanted: dict[str, str],
    resolve: Callable[[str], list[str]],
    local: set[str],
) -> list[DnsResult]:
    """``wanted`` maps part -> name. Fine only when EVERY answer is a public address of this computer (one answer that
    points elsewhere, or a private one, would send the certificate check or the people to the wrong place)."""
    out = []
    for part, name in wanted.items():
        found = tuple(resolve(name))
        if not found:
            out.append(DnsResult(part, name, "missing"))
        elif not all(a in local for a in found):
            out.append(DnsResult(part, name, "elsewhere", found))
        elif not all(_public(a) for a in found):
            out.append(DnsResult(part, name, "private", found))
        else:
            out.append(DnsResult(part, name, "ok", found))
    return out


def _public(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


# ----------------------------------------------------------------------------------------------------------- Caddy

_HEADER = (
    "# Managed by orch (orch install server): rerun that command instead of editing this file.\n"
    "# One site for one part; removing this file removes the part from Caddy.\n"
)


def caddy_snippet(part: str, name: str, port: int, *, vpn_only: bool = False, vpn_net: str = VPN_NET) -> str:
    """The site block of one part: it proxies to 127.0.0.1:<port> (the part listens on loopback only)."""
    if part not in P.PARTS:
        raise InstallError(f"unknown part {part!r}")
    lines = [_HEADER + f"{name} {{"]
    if part == "hub" and vpn_only:
        # The ACME HTTP-01 challenge is answered by Caddy itself before any route runs, so the certificate still
        # renews from the public internet while every real request from outside the private network gets 403.
        lines += [
            f"\t@notvpn not remote_ip {vpn_net}",
            '\trespond @notvpn "This address is only open from the private network (VPN)." 403',
            "",
        ]
    lines += ["\tencode zstd gzip", "", "\trequest_body {", f"\t\tmax_size {BODY_LIMIT[part]}", "\t}", ""]
    if part == "publish":
        # share links carry their token in the path: never in the access log (needs Caddy 2.8 or newer)
        lines += ["\t@shares path /s/*", "\tlog_skip @shares", ""]
    lines += [f"\treverse_proxy 127.0.0.1:{port}", "}", ""]
    return "\n".join(lines)


def user_dir(root: str | Path, user: str) -> Path:
    """The snippet directory of one OS user: ``<root>/<user>`` (root creates it, owned by that user, mode 0755)."""
    check_user_name(user)
    return Path(root) / user


def check_user_name(user: str) -> None:
    """The same rule for the snippet folder and for the root block: a plain Linux user name, nothing else."""
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise InstallError(f"{user!r} is not a usable user name for the Caddy snippet folder")


def write_snippets(directory: Path, snippets: dict[str, str]) -> list[Path]:
    """Write ``<part>.caddy`` files (0644) into ``directory``, which must be a real folder owned by this user.

    Every file is made under a random name with ``O_EXCL|O_NOFOLLOW`` relative to the opened folder and renamed over the
    target, so a link planted at any name is never followed (rename replaces the link itself, never what it points at).
    Raises :class:`InstallError` when the folder is a link or not ours, ``OSError`` when it is missing."""
    try:
        dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno == errno.ELOOP or e.errno == errno.ENOTDIR:
            raise InstallError(f"{directory} is not a plain folder (a link?): not writing Caddy files there") from None
        raise
    written = []
    try:
        st = os.fstat(dfd)
        if st.st_uid != os.geteuid() or privdir.others_can_write(st, os.geteuid()):
            raise InstallError(
                f"{directory} must be owned by this user and not writable by others: do the root steps first"
            )
        for part, text in snippets.items():
            tmp = f".{part}.{secrets.token_hex(8)}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.rename(tmp, f"{part}.caddy", src_dir_fd=dfd, dst_dir_fd=dfd)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp, dir_fd=dfd)
                raise
            written.append(directory / f"{part}.caddy")
    finally:
        os.close(dfd)
    return written


def root_block(user: str, directory: str = CADDY_DIR, *, caddyfile: str = "/etc/caddy/Caddyfile") -> str:
    """The ONE sudo block for a person with root; printed, never executed by orch. No shared writable folder: each OS
    user gets its own folder, and the Caddyfile imports it with its own line, ``<dir>/<user>/*.caddy`` (Caddy allows
    only one wildcard per glob, so there is no ``<dir>/*/*.caddy``)."""
    check_user_name(user)  # a bad name stops here: no block is printed
    d, mine = shell_quote(directory), shell_quote(f"{directory}/{user}")
    imp = f"import {directory}/{user}/*.caddy"
    return "\n".join(
        [
            f"# Administrator steps for {user} (the only steps that need root; orch never runs them).",
            "# The other server user gets its own block and its own import line when it is set up.",
            f"sudo install -d -m 0755 -o root -g root {d}",
            f"sudo install -d -m 0755 -o {shell_quote(user)} -g {shell_quote(user)} {mine}",
            f"grep -qxF {shell_quote(imp)} {shell_quote(caddyfile)} || echo {shell_quote(imp)} | "
            f"sudo tee -a {shell_quote(caddyfile)} >/dev/null",
            "sudo systemctl reload caddy",
            f"sudo loginctl enable-linger {shell_quote(user)}",
            "",
        ]
    )


def ensure_user_bus(
    env: dict[str, str],
    uid: int,
    user: str,
    exists: Callable[[str], bool] = os.path.exists,
) -> None:
    """``systemctl --user`` needs XDG_RUNTIME_DIR and the session bus. Under ``sudo -iu`` they are unset: when the
    user's bus is there (linger is on), set both in ``env`` (``os.environ`` of this process). Otherwise refuse BEFORE
    anything is installed, in plain words naming the fix."""
    if env.get("XDG_RUNTIME_DIR") and env.get("DBUS_SESSION_BUS_ADDRESS"):
        return
    run = env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    bus = f"{run}/bus"
    if exists(bus):
        env["XDG_RUNTIME_DIR"] = run
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={bus}")
        return
    raise InstallError(
        f"there is no systemd session for {user} ({bus} is missing), so its services cannot be started. Ask an "
        f"administrator to run the linger line of the root block (`sudo loginctl enable-linger {user}`), then log in "
        f"with `sudo machinectl shell {user}@` (or set XDG_RUNTIME_DIR={run}) and run this command again."
    )


def check_drop_path(path: str, lstat: Callable[[str], os.stat_result] = os.lstat) -> None:
    """``--backup-drop`` must be an absolute path whose every parent folder is owned by root and not writable by its
    group or by anyone else (a folder an ordinary user can write could be swapped for a link). The drop folder itself is
    made by root and checked each night by the copy. Raises :class:`InstallError` in plain words."""
    if not os.path.isabs(path) or ".." in Path(path).parts:
        raise InstallError(f"--backup-drop {path}: use a full path that starts with / (and has no ..)")
    for parent in list(Path(path).parents):
        try:
            st = lstat(str(parent))
        except OSError as e:
            raise InstallError(f"--backup-drop {path}: cannot look at {parent} ({e.strerror})") from None
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise InstallError(
                f"--backup-drop {path}: the folder {parent} must belong to root and not be writable by anyone else; "
                "use a place such as /var/lib/orch-backup-drop"
            )


def shell_quote(s: str) -> str:
    import shlex

    return shlex.quote(s)


@dataclass
class Plan:
    """What a run decided (also what the tests look at)."""

    role: str
    parts: tuple[str, ...]
    names: Names
    vpn_only: bool = False
    vpn_ip: str | None = None
    vpn_net: str | None = None
    behind_proxy: bool = False
    proxy_nets: tuple[str, ...] = ()
    loopback: bool = False
    dns: list[DnsResult] = field(default_factory=list)
    snippets: dict[str, str] = field(default_factory=dict)

    @property
    def tls_parts(self) -> list[str]:
        return [d.part for d in self.dns if d.ok]
