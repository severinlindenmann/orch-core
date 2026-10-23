"""Co-location and bind guards for the hub's network link (D12b, spec §4a; design check G1-G3).

``--listen`` stays loopback-only. ``--link-listen`` is the only way to a non-loopback link bind, and only for ONE literal
private unicast address that is assigned to this machine. It is refused on a machine that has workspace host or agent
users (§4a), except with ``--dev-shared-machine`` on a ``dev: true`` card, for a hub that is a different user outside
every host/agent group (S12 on the shared dev edge: it proves the network link, not machine separation).

:class:`Accounts` is the one accounts snapshot; ``orch-hub doctor`` uses the same code.
"""

from __future__ import annotations

import grp
import ipaddress
import os
import pwd
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass

HOST_PREFIX = "orch-host-"
AGENT_PREFIX = "agent-"

_PRIVATE_V4 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # NetBird / Tailscale overlay
_ULA = ipaddress.ip_network("fc00::/7")


class BindRefused(ValueError):
    """A non-loopback link bind that the rules refuse; ``str(e)`` is a fixed secret-free reason."""


@dataclass(frozen=True)
class Accounts:
    """A snapshot of the OS accounts: ``users`` name -> (uid, gid), ``groups`` gid -> member names."""

    users: dict[str, tuple[int, int]]
    groups: dict[int, frozenset[str]]
    extra_hosts: frozenset[str] = frozenset()
    extra_agents: frozenset[str] = frozenset()

    @classmethod
    def load(cls, extra_hosts: Iterable[str] = (), extra_agents: Iterable[str] = ()) -> Accounts:
        return cls(
            {p.pw_name: (p.pw_uid, p.pw_gid) for p in pwd.getpwall()},
            {g.gr_gid: frozenset(g.gr_mem) for g in grp.getgrall()},
            frozenset(extra_hosts),
            frozenset(extra_agents),
        )

    def is_agent(self, name: str) -> bool:
        return name.startswith(AGENT_PREFIX) or name in self.extra_agents

    @property
    def host_users(self) -> list[str]:
        return sorted(n for n in self.users if n.startswith(HOST_PREFIX) or n in self.extra_hosts)

    @property
    def agent_users(self) -> list[str]:
        return sorted(n for n in self.users if self.is_agent(n))

    def name_of(self, uid: int) -> str | None:
        return next((n for n, (u, _g) in self.users.items() if u == uid), None)

    def groups_of(self, name: str) -> set[int]:
        """Primary group plus every group that lists ``name`` as a member."""
        gids = {g for g, members in self.groups.items() if name in members}
        if name in self.users:
            gids.add(self.users[name][1])
        return gids


def locally_assigned(ip: str) -> bool:
    """True when ``ip`` is assigned to a local interface (a bind to it succeeds; port 0, closed at once)."""
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    s = socket.socket(fam)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def parse_link_listen(value: str, *, assigned: Callable[[str], bool] = locally_assigned) -> tuple[str, int]:
    """G1/G2: one literal private unicast IP assigned to this machine, plus a port 1-65535."""
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise BindRefused("--link-listen must be IP:port with a port of 1-65535")
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        raise BindRefused("--link-listen takes a literal IP address, not a hostname") from None
    if ip.is_unspecified:
        raise BindRefused("--link-listen refuses a wildcard address (0.0.0.0, ::)")
    if ip.is_multicast or ip == ipaddress.ip_address("255.255.255.255"):
        raise BindRefused("--link-listen refuses multicast and broadcast addresses")
    if ip.is_link_local:
        raise BindRefused("--link-listen refuses link-local addresses")
    if ip.is_loopback:
        raise BindRefused("--link-listen is for a non-loopback address; use --listen for loopback")
    v4 = ip.version == 4
    private = any(ip in n for n in _PRIVATE_V4) or ip in _CGNAT if v4 else ip in _ULA
    if not private:
        raise BindRefused("--link-listen refuses a public address (private, 100.64/10 or ULA only)")
    if not assigned(str(ip)):
        raise BindRefused("--link-listen address is not assigned to a local interface")
    return str(ip), int(port)


def check_bind_allowed(
    acc: Accounts,
    *,
    hub_user: str,
    card_dev: bool,
    dev_shared_machine: bool,
) -> str | None:
    """G3. Returns a WARN text when the dev exception applies, None when the machine has no host or agent users;
    raises :class:`BindRefused` otherwise."""
    others = sorted(set(acc.host_users) | set(acc.agent_users))
    if not others:
        if dev_shared_machine:
            raise BindRefused("--dev-shared-machine given but no host or agent users exist; drop the flag")
        return None
    if not dev_shared_machine:
        raise BindRefused(
            f"a non-loopback link needs a machine without workspace host or agent users (found {', '.join(others)}); "
            "that is the §4a split layout"
        )
    if not card_dev:
        raise BindRefused("--dev-shared-machine needs a dev: true hub card")
    if hub_user in others:
        raise BindRefused(f"the hub runs as {hub_user}, a host or agent user")
    if hub_user not in acc.users:
        raise BindRefused("the hub user has no account entry")
    shared = acc.groups_of(hub_user) & {g for n in others for g in acc.groups_of(n)}
    if shared:
        raise BindRefused("the hub user shares a group with a host or agent user")
    return "dev split on a shared machine: this proves the network link, not machine separation"


def current_user(acc: Accounts) -> str:
    return acc.name_of(os.geteuid()) or f"uid{os.geteuid()}"
