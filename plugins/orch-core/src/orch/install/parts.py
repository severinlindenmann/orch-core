"""What each part is: package, command line, default port. Addresses: localhost, a detected LAN or NetBird IP, or a
domain (a domain only changes what is printed, never how a part listens)."""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ADDRESS_LOOPBACK", "DEFAULT_PORTS", "PARTS", "Part", "InstallError", "classify", "detect_addresses",
           "free_port", "port_free", "valid_port", "WORKSPACE_FIRST_PORT"]

ADDRESS_LOOPBACK = "127.0.0.1"
WORKSPACE_FIRST_PORT = 8151
DEFAULT_PORTS = {"hub": 8100, "relay": 8101, "publish": 8102}
HUB_LINK_PORT = 8105  # the hub link (workspaces dial it); the console is the part's own port


class InstallError(Exception):
    """A refusal with a plain message (printed as ``orch install: <message>``)."""


@dataclass(frozen=True)
class Part:
    name: str
    repo: str  # https clone URL (pinned to a ref at install time)
    binary: str
    port: int
    what: str


PARTS = {
    "hub": Part("hub", "https://github.com/severinlindenmann/orch-hub", "orch-hub", 8100, "hub (console and link server)"),
    "relay": Part("relay", "https://github.com/severinlindenmann/orch-relay", "orch-relay", 8101, "relay (phone push and mailboxes)"),
    "publish": Part("publish", "https://github.com/severinlindenmann/orch-publish", "orch-publish", 8102, "publish (shares)"),
}


def valid_port(port: int) -> int:
    if not 1024 <= port <= 65535:
        raise InstallError(f"port {port}: use a number from 1024 to 65535 (below 1024 needs root)")
    return port


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def free_port(start: int, taken: set[int], is_free=port_free) -> int:
    port = start
    while port in taken or not is_free(port):
        port += 1
        if port > 65535:
            raise InstallError("no free port left")
    return port


_NETBIRD = ipaddress.ip_network("100.64.0.0/10")


def classify(ip: str, iface: str = "") -> str:
    a = ipaddress.ip_address(ip)
    if a.is_loopback:
        return "this computer only"
    if a in _NETBIRD or iface.startswith("wt"):
        return "NetBird"
    if a.is_private:
        return "local network"
    return "public"


def detect_addresses(run=None) -> list[tuple[str, str]]:
    """``[(ip, label)]``: localhost first, then the IPv4 addresses of this machine (NetBird 100.x, LAN)."""
    run = run or _run
    found: list[tuple[str, str]] = [(ADDRESS_LOOPBACK, "this computer only")]
    text = run(["ifconfig"]) or run(["ip", "-o", "-4", "addr"]) or ""
    iface = ""
    for line in text.splitlines():
        m = re.match(r"^(\S+?):?\s", line)
        if m and not line.startswith((" ", "\t")):
            iface = m.group(1).rstrip(":")
        if " inet " in line or line.lstrip().startswith("inet "):
            m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)", line)
            if m:
                if "-o" in line or re.match(r"^\d+:", line):  # `ip -o` form: "2: eth0    inet 1.2.3.4/24 ..."
                    iface = line.split()[1]
                ip = m.group(1)
                if not ipaddress.ip_address(ip).is_loopback and not ip.startswith("169.254."):
                    if all(ip != f[0] for f in found):
                        found.append((ip, classify(ip, iface)))
    return found


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def hub_link_port() -> int:
    return HUB_LINK_PORT


def command_for(part: str, *, binary: Path, data: Path, address: str, port: int, publish_url: str | None,
                relay_url: str | None, dev: bool) -> list[str]:
    """The ``serve`` command line of a part (argv; environment is :func:`env_for`)."""
    bind = address if address else ADDRESS_LOOPBACK
    if part == "hub":
        loop = bind in (ADDRESS_LOOPBACK, "localhost")
        link = ["--listen", f"127.0.0.1:{HUB_LINK_PORT}"] if loop else ["--link-listen", f"{bind}:{HUB_LINK_PORT}", "--dev-shared-machine"]
        cmd = [str(binary), "serve", "--state", str(data), *link, "--web-listen", f"127.0.0.1:{port}",
               "--publish-url", publish_url or f"http://127.0.0.1:{DEFAULT_PORTS['publish']}"]
        if relay_url:
            cmd += ["--relay-url", relay_url]
        return cmd
    if part == "relay":
        return [str(binary), "serve", "--host", bind, "--port", str(port)]
    cmd = [str(binary), "serve", "--data", str(data), "--bind", f"{bind}:{port}", "--api-host", f"{bind}:{port}"]
    return cmd + (["--dev-mode"] if dev else [])


def env_for(part: str, data: Path) -> dict[str, str]:
    return {"ORCH_RELAY_DATA": str(data)} if part == "relay" else {}
