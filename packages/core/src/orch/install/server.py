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
    "check_dns",
    "derive_names",
    "detect_vpn_ip",
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
    v6 = ipaddress.ip_address(chosen.ip).version == 6
    if prefix is None or (prefix < 48 if v6 else prefix > 24):
        raise InstallError(
            f"cannot tell the network of your VPN from {chosen.ip}"
            + (f" (the interface says /{prefix})" if prefix is not None else "")
            + ". Give it with --vpn-ip, for example --vpn-ip 100.92.1.2/16."
        )
    net = ipaddress.ip_interface(f"{chosen.ip}/{prefix}").network
    if not (net.version == 4 and net.subnet_of(_CGNAT)) and not net.is_private:
        raise InstallError(f"--vpn-ip {chosen.ip}/{prefix}: that network is not a private one")
    return net  # type: ignore[return-value]


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
    v6 = a.version == 6
    if prefix is not None and not (48 <= prefix <= 128 if v6 else 8 <= prefix <= 24):
        raise InstallError(
            f"--vpn-ip {given}: the prefix is a number from "
            + ("48 to 128 (an IPv6 network wider than /48 is refused)" if v6 else "8 to 24 (your VPN's network size)")
        )
    if not _in_cgnat(ip) and not (allow_non_cgnat and a.is_private and not a.is_loopback and not a.is_link_local):
        raise InstallError(
            f"--vpn-ip {given}: use an address of your private network (VPN) in 100.64.x.x - 100.127.x.x"
            + ("" if allow_non_cgnat else " (another private range needs --allow-non-cgnat)")
        )
    have = next((x for x in addresses if x.ip == str(a)), None)
    if have is None:
        raise InstallError(f"--vpn-ip {given}: this computer does not have that address (is the VPN connected?)")
    if v6:  # a wide IPv6 prefix (a whole site or ISP block) would open the VPN gate to far too many hosts
        eff = prefix if prefix is not None else have.prefix
        if eff is None or eff < 48:
            raise InstallError(
                f"--vpn-ip {given}: an IPv6 address is accepted only with a network of /48 or narrower "
                "(give it as, for example, --vpn-ip fd12:3456:789a::1/64)"
            )
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
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise InstallError(f"{user!r} is not a usable user name for the Caddy snippet folder")
    return Path(root) / user


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
        if st.st_uid != os.geteuid() or st.st_mode & 0o022:
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
    user gets its own folder, and Caddy imports ``<dir>/*/*.caddy``."""
    d, mine = shell_quote(directory), shell_quote(f"{directory}/{user}")
    imp = f"import {directory}/*/*.caddy"
    return "\n".join(
        [
            "# Run these as an administrator (they are the only steps that need root; orch never runs them):",
            f"sudo install -d -m 0755 -o root -g root {d}",
            f"sudo install -d -m 0755 -o {shell_quote(user)} -g {shell_quote(user)} {mine}",
            f"grep -qxF {shell_quote(imp)} {shell_quote(caddyfile)} || echo {shell_quote(imp)} | "
            f"sudo tee -a {shell_quote(caddyfile)} >/dev/null",
            "sudo systemctl reload caddy",
            f"sudo loginctl enable-linger {shell_quote(user)}",
            "",
        ]
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
    loopback: bool = False
    dns: list[DnsResult] = field(default_factory=list)
    snippets: dict[str, str] = field(default_factory=dict)

    @property
    def tls_parts(self) -> list[str]:
        return [d.part for d in self.dns if d.ok]
