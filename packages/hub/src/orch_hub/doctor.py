"""``orch-hub doctor`` (dev, D12 step 1): checks that a hub installation is sound, one plain line per check.

Checks: state dir and key file modes/owner (lstat only, no key file is ever opened), the key store opens and the
current card verifies and is ``dev: true``, the registry is readable and 0600, with ``--listen`` the TLS key the hub
serves matches the card's pin, with ``--publish-url`` orch-publish is reachable and its ``PUT /v2/hub/card`` route is
live (a malformed body must answer 400 ``malformed``; nothing is stored). Output is ``PASS <check>`` or
``FAIL <check>: <reason>``; reasons are fixed words, never file contents. :func:`run` returns the number of FAILs.
"""

from __future__ import annotations

import grp
import http.client
import json
import os
import pwd
import re
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .keys import HubKeys, KeysError
from .keys.fsutil import StateError, check_dir
from .link import tls
from .publish.forwarder import _endpoint
from .serve import REGISTRY_FILE, parse_listen

HUB_USER = "orch-hub"
HOST_PREFIX = "orch-host-"
EDGE_PREFIXES = ("orch-relay", "orch-publish")
DEFAULT_FW_FILE = Path("/etc/orch/hub-fw.nft")
_SKUID_SET = re.compile(r"meta\s+skuid\s*!=\s*\{([^}]*)\}")


class Check(Exception):
    """A failed check; ``str(e)`` is the (secret-free) reason."""


@dataclass(frozen=True)
class Accounts:
    """A snapshot of the OS accounts (injectable in tests): ``users`` name -> (uid, gid), ``groups`` gid -> members."""

    users: dict[str, tuple[int, int]]
    groups: dict[int, frozenset[str]]
    euid: int
    euser: str

    @classmethod
    def load(cls) -> Accounts:
        euid = os.geteuid()
        try:
            euser = pwd.getpwuid(euid).pw_name
        except KeyError:
            euser = f"uid{euid}"
        return cls(
            {p.pw_name: (p.pw_uid, p.pw_gid) for p in pwd.getpwall()},
            {g.gr_gid: frozenset(g.gr_mem) for g in grp.getgrall()},
            euid,
            euser,
        )

    def names(self, prefix: str | tuple[str, ...]) -> list[str]:
        return sorted(n for n in self.users if n.startswith(prefix))

    def is_agent(self, name: str) -> bool:
        return name.startswith("agent-")


def layout(acc: Accounts) -> tuple[list[str], list[str]]:
    """(workspace host users, edge service users) found on this machine."""
    return acc.names(HOST_PREFIX), acc.names(EDGE_PREFIXES)


def check_hub_user(acc: Accounts) -> None:
    """§4a: the hub process is its own OS user, ``orch-hub``, shared with nobody."""
    if acc.euid == 0:
        raise Check("hub tooling runs as root, not as its own user orch-hub")
    if acc.euser != HUB_USER:
        raise Check(f"runs as {acc.euser}, expected {HUB_USER}")
    if HUB_USER not in acc.users:
        raise Check("no orch-hub account on this machine")
    uid, gid = acc.users[HUB_USER]
    others = sorted(n for n, (u, g) in acc.users.items() if n != HUB_USER and (u == uid or g == gid))
    if others:
        raise Check(f"orch-hub uid or primary group shared with {', '.join(others)}")
    members = sorted(acc.groups.get(gid, frozenset()) - {HUB_USER})
    if members:
        raise Check(f"group orch-hub has other members: {', '.join(members)}")


def check_separation(acc: Accounts) -> None:
    """§4a: each workspace host keeps its own ``orch-host-<ws>`` user; agents are never the hub or a host user."""
    if HUB_USER not in acc.users:
        raise Check("no orch-hub account on this machine")
    hub_uid, hub_gid = acc.users[HUB_USER]
    hosts = acc.names(HOST_PREFIX)
    seen: dict[int, str] = {}
    for h in hosts:
        uid, gid = acc.users[h]
        if uid == hub_uid or gid == hub_gid:
            raise Check(f"{h} shares uid or group with orch-hub")
        if uid in seen:
            raise Check(f"{h} and {seen[uid]} share a uid")
        seen[uid] = h
    for n in acc.names("agent-"):
        uid, gid = acc.users[n]
        if uid == hub_uid or uid in seen or gid == hub_gid:
            raise Check(f"agent user {n} shares an identity with the hub or a host")
        if n in acc.groups.get(hub_gid, frozenset()):
            raise Check(f"agent user {n} is in group orch-hub")


def check_link_firewall(acc: Accounts, fw_file: Path) -> None:
    """D12-1: only root, orch-hub and the orch-host-* users may dial the hub's loopback port (nft skuid rule)."""
    try:
        text = fw_file.read_text()
    except OSError:
        raise Check(f"{fw_file} missing or unreadable") from None
    m = _SKUID_SET.search(text)
    if not m:
        raise Check("no 'meta skuid != { ... }' rule in the firewall file")
    if HUB_USER not in acc.users:
        raise Check("no orch-hub account on this machine")
    allowed = {0, acc.users[HUB_USER][0]} | {acc.users[h][0] for h in acc.names(HOST_PREFIX)}
    ids: set[int] = set()
    for tok in (t.strip().strip('"') for t in m.group(1).split(",")):
        if not tok:
            continue
        if tok.isdigit():
            ids.add(int(tok))
        elif tok in acc.users:
            ids.add(acc.users[tok][0])
        else:
            raise Check(f"firewall allows unknown user {tok!r}")
    extra = ids - allowed
    if extra:
        raise Check(f"firewall allows uid(s) {sorted(extra)} which are not root, orch-hub or an orch-host-* user")
    if acc.users[HUB_USER][0] not in ids:
        raise Check("firewall does not allow the orch-hub uid")


def _lstat_file(path: Path, what: str) -> None:
    try:
        st = os.lstat(path)
    except OSError:
        raise Check(f"{what} missing or unreadable") from None
    if not stat.S_ISREG(st.st_mode):
        raise Check(f"{what} is not a regular file")
    if st.st_uid != os.geteuid():
        raise Check(f"{what} owned by another user")
    if st.st_mode & 0o177:
        raise Check(f"{what} must be 0600")


def check_state_modes(state: Path) -> None:
    try:
        check_dir(state)
        for sub in ("uk", "op", "revoked"):
            check_dir(state / sub)
    except StateError as e:
        raise Check(f"{e.code} ({str(e).split(': ', 1)[-1] if ': ' in str(e) else 'state dir'})") from None
    for name in ("hk_root.key", "hxk.key", "meta.json"):
        _lstat_file(state / name, name)
    for d in (state / "uk", *sorted(p for p in (state / "op").iterdir() if p.is_dir())):
        try:
            check_dir(d)
        except StateError as e:
            raise Check(f"{e.code} ({d.name})") from None
        for f in d.iterdir():
            if f.suffix in (".key", "") and f.is_file() and not f.is_symlink():
                _lstat_file(f, f"{d.name}/{f.name}")


def open_keys(state: Path) -> HubKeys:
    try:
        return HubKeys.open(state, prune=False)
    except KeysError as e:
        raise Check(f"key store refused: {e.code}") from None


def check_card(keys: HubKeys) -> dict:
    try:
        card = keys.verify_card(keys.card())["card"]
    except KeysError as e:
        raise Check(f"card does not verify: {e.code}") from None
    if card.get("dev") is not True:
        raise Check("card is not dev: true")
    return card


def check_registry(state: Path) -> None:
    reg = state / REGISTRY_FILE
    if not reg.exists():
        return  # a hub that never served has none yet
    _lstat_file(reg, REGISTRY_FILE)
    try:
        db = sqlite3.connect(f"{reg.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            db.execute("SELECT COUNT(*) FROM workspace").fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        raise Check("registry.db not readable") from None


def check_tls_pin(listen: str, pin: str) -> None:
    try:
        host, port = parse_listen(listen)
    except ValueError as e:
        raise Check(str(e)) from None
    try:
        tls.connect_pinned(host, port, pin, timeout=5.0).close()
    except tls.PinMismatch:
        raise Check("served TLS key does not match the card pin") from None
    except tls.LinkClosed:
        raise Check("nothing answers TLS on --listen") from None


def check_publish(url: str) -> None:
    try:
        scheme, host, port = _endpoint(url)
    except ValueError as e:
        raise Check(str(e)) from None
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    conn = cls(host, port, timeout=10)
    try:
        conn.request("PUT", "/v2/hub/card", body=b"{}", headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read(4097)
    except OSError, http.client.HTTPException:
        raise Check("publish URL not reachable") from None
    finally:
        conn.close()
    try:
        code = json.loads(data).get("error") if len(data) <= 4096 else None
    except ValueError, AttributeError:
        code = None
    if resp.status != 400 or code != "malformed":
        raise Check(f"/v2/hub/card route did not answer 400 malformed (got HTTP {resp.status})")


def run(
    state: Path,
    *,
    listen: str | None = None,
    publish_url: str | None = None,
    out: TextIO,
    accounts: Accounts | None = None,
    fw_file: Path = DEFAULT_FW_FILE,
) -> int:
    fails = 0

    def step(name: str, fn: Callable[[], object]) -> object:
        nonlocal fails
        try:
            r = fn()
        except Check as e:
            fails += 1
            print(f"FAIL {name}: {e}", file=out)
            return None
        print(f"PASS {name}", file=out)
        return r if r is not None else True

    step("state dir and key file modes", lambda: check_state_modes(state))
    keys = step("key store opens", lambda: open_keys(state))
    card = step("card verifies and is dev: true", lambda: check_card(keys)) if keys else None
    if keys is None:
        print("FAIL card verifies and is dev: true: key store did not open", file=out)
        fails += 1
    step("registry readable", lambda: check_registry(state))
    if listen is not None:
        if isinstance(card, dict):
            step("TLS pin matches the served key", lambda: check_tls_pin(listen, card["tls_spki_sha256"]))
        else:
            print("FAIL TLS pin matches the served key: no verified card", file=out)
            fails += 1
    if publish_url is not None:
        step("publish URL answers /v2/hub/card", lambda: check_publish(publish_url))
    acc = accounts if accounts is not None else Accounts.load()
    hosts, edge = layout(acc)
    if hosts or edge:
        what = ", ".join(
            x for x in (f"{len(hosts)} workspace host(s)" if hosts else "", "relay/publish" if edge else "") if x
        )
        print(f"INFO layout: one machine (hub + {what}): shares a machine, weaker isolation (D82, §4a)", file=out)
        step("hub runs as its own OS user", lambda: check_hub_user(acc))
        step("host and agent users are separate from the hub", lambda: check_separation(acc))
        if hosts:
            step("link firewall allows only root, hub and host users", lambda: check_link_firewall(acc, fw_file))
    else:
        print("INFO layout: hub alone on this machine (split layout)", file=out)
    print("doctor: all checks passed" if not fails else f"doctor: {fails} check(s) FAILED", file=out)
    return fails
