"""What each part is: package, command line, default port. Addresses: localhost, a detected LAN or NetBird IP, or a
domain (a domain only changes what is printed, never how a part listens)."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orch import childenv

__all__ = [
    "ADDRESS_ALL",
    "ADDRESS_LOOPBACK",
    "DEFAULT_PORTS",
    "DEFAULT_PORT_BASE",
    "port_block",
    "PARTS",
    "REPO_URL",
    "Part",
    "InstallError",
    "classify",
    "detect_addresses",
    "free_port",
    "port_free",
    "valid_port",
    "WORKSPACE_FIRST_PORT",
]

ADDRESS_LOOPBACK = "127.0.0.1"
ADDRESS_ALL = "0.0.0.0"  # every interface: parts on a network address listen here, the address is only advertised
WORKSPACE_FIRST_PORT = 8151
DEFAULT_PORTS = {"hub": 8100, "relay": 8101, "publish": 8102}
HUB_LINK_PORT = 8105  # the hub link (workspaces dial it); the console is the part's own port
DEFAULT_PORT_BASE = 8100
#: offsets inside one port block (--port-base N): hub N, relay N+1, publish N+2, hub link N+5
PORT_OFFSETS = {"hub": 0, "relay": 1, "publish": 2, "link": 5}


class InstallError(Exception):
    """A refusal with a plain message (printed as ``orch install: <message>``)."""


#: The one repository every part is installed from (pinned to a ref at install time); a part lives in
#: ``packages/<name>`` of it.
REPO_URL = "https://github.com/severinlindenmann/orch-core"


@dataclass(frozen=True)
class Part:
    name: str
    binary: str
    port: int
    what: str

    @property
    def subdirectory(self) -> str:
        return f"packages/{self.name}"

    def package_url(self, sha: str) -> str:
        """The pip/uv requirement of this part at commit ``sha``."""
        return f"git+{REPO_URL}@{sha}#subdirectory={self.subdirectory}"


PARTS = {
    "hub": Part("hub", "orch-hub", 8100, "hub (console and link server)"),
    "relay": Part("relay", "orch-relay", 8101, "relay (phone push and mailboxes)"),
    "publish": Part("publish", "orch-publish", 8102, "publish (shares)"),
}


def port_block(base: int = DEFAULT_PORT_BASE) -> dict[str, int]:
    """``{"hub", "relay", "publish", "link"}`` for ``--port-base base``; the whole block must fit 1024-65535."""
    top = base + max(PORT_OFFSETS.values())
    if base < 1024 or top > 65535:
        raise InstallError(f"--port-base {base}: the block {base}-{top} must fit in 1024 to 65535")
    return {k: base + off for k, off in PORT_OFFSETS.items()}


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
        return "private network"
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
        return subprocess.run(argv, capture_output=True, text=True, timeout=5, env=childenv.sanitize(os.environ)).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def command_for(
    part: str,
    *,
    binary: Path,
    data: Path,
    address: str,
    port: int,
    publish_url: str | None,
    relay_url: str | None,
    dev: bool,
    link_address: str | None = None,
    public_origin: str | None = None,
    link_port: int = HUB_LINK_PORT,
    publish_port: int = DEFAULT_PORTS["publish"],
    behind_edge: bool = False,
    exact_bind: bool = False,
) -> list[str]:
    """The ``serve`` command line of a part (argv; environment is :func:`env_for`)."""
    bind = address if address else ADDRESS_LOOPBACK
    if part == "hub":
        loop = bind in (ADDRESS_LOOPBACK, "localhost")
        if link_address:  # server role: the console stays on loopback behind Caddy, the link binds the VPN address only
            link = ["--link-listen", f"{link_address}:{link_port}"]
        elif loop:
            link = ["--listen", f"127.0.0.1:{link_port}"]
        else:
            # a network address is the advertised one; the hub listens on every interface (--bind-all) because a
            # computer cannot dial its own overlay address
            link = ["--link-listen", f"{bind}:{link_port}", "--bind-all"]
        cmd = [
            str(binary),
            "serve",
            "--state",
            str(data),
            *link,
            "--web-listen",
            f"{bind}:{port}",
            "--publish-url",
            publish_url or f"http://127.0.0.1:{publish_port}",
        ]
        if relay_url:
            cmd += ["--relay-url", relay_url]
        if behind_edge:  # Caddy on this computer is the edge: it states the client (XFF), the origin is the https name
            cmd += ["--behind-edge", "--trusted-peer", ADDRESS_LOOPBACK]
        if dev and not loop:  # one user on one dev machine: the hub allows its link on a network address
            cmd += ["--dev-shared-machine"]
        return cmd
    # exact_bind (the server role): never wider than asked, the part listens on exactly the address it was given
    listen = bind if exact_bind or bind in (ADDRESS_LOOPBACK, "localhost") else ADDRESS_ALL
    if part == "relay":
        return [str(binary), "serve", "--host", listen, "--port", str(port)]
    cmd = [
        str(binary),
        "run",
        "--data",
        str(data),
        "--listen",
        f"{listen}:{port}",
        "--public-origin",
        public_origin or f"http://{bind}:{port}",
    ]
    return cmd + (["--dev-mode"] if dev else [])


def dashboard_command(entry: list[str], *, address: str, port: int) -> list[str]:
    """``orch-dashboard serve``: loopback on ``port``; with a network ``address`` also listening on it (same port)."""
    cmd = [*entry, "serve", "--port", str(port)]
    return cmd if address in (ADDRESS_LOOPBACK, "localhost") else [*cmd, "--listen", address]


def env_for(part: str, data: Path, *, behind_edge: bool = False) -> dict[str, str]:
    if part != "relay":
        return {}
    env = {"ORCH_RELAY_DATA": str(data)}
    if behind_edge:  # the relay trusts X-Forwarded-For only from the Caddy on this computer
        env["ORCH_RELAY_TRUSTED_PROXY"] = ADDRESS_LOOPBACK
    return env
