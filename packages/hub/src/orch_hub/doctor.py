"""``orch-hub doctor`` (dev, D12 step 1): checks that a hub installation is sound, one plain line per check.

Checks: state dir and key file modes/owner (lstat only, no key file is ever opened), the key store opens and the
current card verifies and is ``dev: true``, the registry is readable and 0600, with ``--listen`` the TLS key the hub
serves matches the card's pin, with ``--publish-url`` orch-publish is reachable and its ``PUT /v2/hub/card`` route is
live (a malformed body must answer 400 ``malformed``; nothing is stored). Output is ``PASS <check>`` or
``FAIL <check>: <reason>``; reasons are fixed words, never file contents. :func:`run` returns the number of FAILs.

Co-location (D82, spec §4a) and the split dev layout (D12b, design check G9): the hub user name is a parameter
(``--hub-user``, default ``orch-hub``); ``--link-listen`` runs the TLS pin check against the served key and the same bind
rules as ``serve`` (a wildcard or a host/agent-user machine without the dev exception is a FAIL; the dev exception is a
WARN: "network link, not machine separation"); the firewall file is compared with the dump of the LOADED table that
``orch-hub-fw.service`` writes (``--fw-dump``), so the kernel and the file cannot drift unseen.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sqlite3
import stat
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .colocation import Accounts, BindRefused, check_bind_allowed, parse_link_listen
from .keys import HubKeys, KeysError
from .keys.fsutil import StateError, check_dir
from .link import tls
from .publish.forwarder import _endpoint
from .serve import REGISTRY_FILE, parse_listen

DEFAULT_FW_FILE = Path("/etc/orch/hub-fw.nft")
DEFAULT_FW_DUMP = Path("/run/orch/hub-fw.loaded.json")
HUB_USER = "orch-hub"
_SKUID_LINE = re.compile(r"tcp\s+dport\s+(\d+)\b.*?meta\s+skuid\s*!=\s*\{([^}]*)\}.*?\b(reject|drop)\b")
_IIF_ACCEPT = re.compile(r"tcp\s+dport\s+(\d+)\b.*?iifname\s+\"?([\w.-]+)\"?.*?\baccept\b")


class Check(Exception):
    """A failed check; ``str(e)`` is the (secret-free) reason."""


def check_hub_user(acc: Accounts, hub_user: str, state_uid: int, unit_user: str | None) -> None:
    """§4a: the hub process is its own OS user (``hub_user``), shared with nobody.

    Judged by who owns the hub's state dir and (when systemd answers) the unit's ``User=``, not by who runs doctor.
    """
    if hub_user not in acc.users:
        raise Check(f"no {hub_user} account on this machine")
    owner = acc.name_of(state_uid)
    if owner != hub_user:
        raise Check(f"state dir is owned by {owner or 'uid ' + str(state_uid)}, expected {hub_user}")
    if unit_user is not None and unit_user != hub_user:
        raise Check(f"the hub unit runs as {unit_user or 'root'}, expected {hub_user}")
    uid, gid = acc.users[hub_user]
    if uid == 0:
        raise Check(f"{hub_user} has uid 0")
    others = sorted(n for n, (u, g) in acc.users.items() if n != hub_user and (u == uid or g == gid))
    if others:
        raise Check(f"{hub_user} uid or primary group shared with {', '.join(others)}")
    members = sorted(acc.groups.get(gid, frozenset()) - {hub_user})
    if members:
        raise Check(f"group {hub_user} has other members: {', '.join(members)}")


def check_separation(acc: Accounts, hub_user: str) -> None:
    """§4a: each workspace host keeps its own ``orch-host-<ws>`` user; agents are never the hub or a host user."""
    if hub_user not in acc.users:
        raise Check(f"no {hub_user} account on this machine")
    hub_uid, hub_gid = acc.users[hub_user]
    seen: dict[int, str] = {}
    for h in acc.host_users:
        uid, gid = acc.users[h]
        if uid == hub_uid or gid == hub_gid:
            raise Check(f"{h} shares uid or group with {hub_user}")
        if uid in seen:
            raise Check(f"{h} and {seen[uid]} share a uid")
        seen[uid] = h
    for n in acc.agent_users:
        uid, gid = acc.users[n]
        if uid == hub_uid or uid in seen or gid == hub_gid:
            raise Check(f"agent user {n} shares an identity with the hub or a host")
        if n in acc.groups.get(hub_gid, frozenset()):
            raise Check(f"agent user {n} is in group {hub_user}")


def check_server_profile(acc: Accounts) -> None:
    """§4a: co-location requires the server custody profile: a separate host user per workspace AND agents under their
    own UID. A P2-style host (agents run as the person) shows here as a host user without a matching agent user.
    Limit: this reads accounts only; it cannot see a host process started by a login user outside this naming."""
    agents = set(acc.agent_users)
    for h in acc.host_users:
        ws = h.removeprefix("orch-host-")
        if f"agent-{ws}" not in agents:
            raise Check(f"{h} has no agent user agent-{ws}: not the server custody profile (agents as the person)")


@dataclass(frozen=True)
class FwRule:
    hook: str  # "output" | "input" ("" when the text form does not say)
    iface: str | None  # oifname (output) / iifname (input)
    port: int
    ids: frozenset[int] | None  # the "meta skuid != {...}" set
    verdict: str  # reject | drop | accept


def _resolve_ids(acc: Accounts, raw: str, where: str) -> frozenset[int]:
    ids: set[int] = set()
    for tok in (t.strip().strip('"') for t in raw.split(",")):
        if not tok:
            continue
        if tok.isdigit():
            ids.add(int(tok))
        elif tok in acc.users:
            ids.add(acc.users[tok][0])
        else:
            raise Check(f"{where}: allows unknown user {tok!r}")
    return frozenset(ids)


def parse_fw_text(acc: Accounts, text: str, where: str) -> list[FwRule]:
    """The skuid rules and the iifname accept rules of an nft file (comments ignored, one rule per line)."""
    rules: list[FwRule] = []
    for line in ("\n".join(x.split("#", 1)[0] for x in text.splitlines())).splitlines():
        m = _SKUID_LINE.search(line)
        if m:
            oif = re.search(r"oifname\s+\"?([\w.-]+)\"?", line)
            rules.append(
                FwRule(
                    "output",
                    oif.group(1) if oif else None,
                    int(m.group(1)),
                    _resolve_ids(acc, m.group(2), where),
                    m.group(3),
                )
            )
            continue
        m = _IIF_ACCEPT.search(line)
        if m:
            rules.append(FwRule("input", m.group(2), int(m.group(1)), None, "accept"))
        elif re.search(r"tcp\s+dport\s+(\d+)\s+drop\b", line):
            rules.append(FwRule("input", None, int(re.search(r"dport\s+(\d+)", line).group(1)), None, "drop"))  # type: ignore[union-attr]
    return rules


def parse_fw_dump(dump: object) -> list[FwRule]:
    """The rules of ``nft -j list table inet orch_hub``, in kernel order."""
    if type(dump) is not dict or type(dump.get("nftables")) is not list:
        raise Check("loaded-rule dump is not nft JSON")
    hooks: dict[str, str] = {}
    rules: list[FwRule] = []
    for item in dump["nftables"]:
        if type(item) is not dict:
            continue
        if "chain" in item:
            hooks[item["chain"].get("name", "")] = item["chain"].get("hook", "")
        elif "rule" in item:
            r = item["rule"]
            iface = port = ids = verdict = None
            for e in r.get("expr", []):
                if "match" in e:
                    m = e["match"]
                    left, right = m.get("left", {}), m.get("right")
                    if "meta" in left and left["meta"].get("key") in ("oifname", "iifname") and m.get("op") == "==":
                        iface = right
                    elif "meta" in left and left["meta"].get("key") == "skuid" and m.get("op") == "!=":
                        ids = frozenset(right["set"] if type(right) is dict else [right])
                    elif left.get("payload", {}).get("protocol") == "tcp" and left["payload"].get("field") == "dport":
                        port = right
                else:
                    for v in ("reject", "drop", "accept"):
                        if v in e:
                            verdict = v
            if type(port) is int and verdict is not None:
                rules.append(FwRule(hooks.get(r.get("chain", ""), ""), iface, port, ids, verdict))
    return rules


def _read_dump(path: Path) -> list[FwRule]:
    try:
        data = json.loads(path.read_text())
    except OSError:
        raise Check(f"loaded-rule dump {path} missing: orch-hub-fw.service writes it") from None
    except ValueError:
        raise Check(f"loaded-rule dump {path} is not JSON") from None
    return parse_fw_dump(data)


def check_link_firewall(acc: Accounts, hub_user: str, fw_file: Path, fw_dump: Path, port: int) -> None:
    """D12-1: only root, the hub user and the orch-host-* users may dial the hub's loopback port (nft skuid rule, on
    ``oifname lo``), in the file AND in the loaded table (the dump), and the two must agree."""
    if hub_user not in acc.users:
        raise Check(f"no {hub_user} account on this machine")
    try:
        text = fw_file.read_text()
    except OSError:
        raise Check(f"{fw_file} missing or unreadable") from None
    allowed = {0, acc.users[hub_user][0]} | {acc.users[h][0] for h in acc.host_users}

    def judge(rules: list[FwRule], where: str) -> frozenset[tuple[str | None, frozenset[int], str]]:
        mine = [r for r in rules if r.hook in ("output", "") and r.ids is not None and r.port == port]
        if not mine:
            raise Check(f"{where}: no 'tcp dport {port} … meta skuid != {{…}} … reject|drop' rule")
        for r in mine:
            if r.iface != "lo":
                raise Check(f"{where}: the rule must match oifname lo (found {r.iface or 'none'})")
            extra = (r.ids or frozenset()) - allowed
            if extra:
                raise Check(
                    f"{where} allows uid(s) {sorted(extra)} which are not root, {hub_user} or an orch-host-* user"
                )
            if acc.users[hub_user][0] not in (r.ids or frozenset()):
                raise Check(f"{where} does not allow the {hub_user} uid")
        return frozenset((r.iface, r.ids or frozenset(), r.verdict) for r in mine)

    want = judge(parse_fw_text(acc, text, "firewall file"), "firewall file")
    got = judge(_read_dump(fw_dump), "loaded ruleset")
    if want != got:
        raise Check("the loaded ruleset differs from the firewall file (reload orch-hub-fw.service)")


def check_link_surface(fw_dump: Path, port: int, iface: str) -> None:
    """D12b (3): the network link port accepts only on ``iface`` (wt0) and is dropped otherwise, accept before drop.
    Surface reduction, never authorization (that is the pinned TLS and the WSK proof)."""
    rules = [r for r in _read_dump(fw_dump) if r.hook == "input" and r.port == port]
    acc_i = next((i for i, r in enumerate(rules) if r.verdict == "accept" and r.iface == iface), None)
    drop_i = next((i for i, r in enumerate(rules) if r.verdict == "drop" and r.iface is None), None)
    if acc_i is None:
        raise Check(f"no 'tcp dport {port} iifname {iface} accept' input rule in the loaded table")
    if drop_i is None:
        raise Check(f"no 'tcp dport {port} drop' input rule in the loaded table")
    if acc_i > drop_i:
        raise Check(f"the drop rule for port {port} comes before the {iface} accept rule")
    if any(r.verdict == "accept" and r.iface not in (iface,) for r in rules):
        raise Check(f"port {port} is accepted on an interface other than {iface}")


def unit_user_of(unit: str = "orch-hub") -> str | None:
    """The systemd unit's ``User=``, or None when systemd does not answer (then only the state dir owner counts)."""
    try:
        r = subprocess.run(
            ["systemctl", "show", unit, "-p", "User", "--value"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if r.returncode != 0:
        return None
    v = r.stdout.strip()
    return v if v else None


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


def check_tls_pin(listen: str, pin: str, *, link: bool = False) -> None:
    try:
        if link:  # --link-listen: the same address rules as serve; assignment is not probed (doctor only dials)
            host, port = parse_link_listen(listen, assigned=lambda ip: True)
        else:
            host, port = parse_listen(listen)
    except ValueError as e:
        raise Check(str(e)) from None
    try:
        tls.connect_pinned(host, port, pin, timeout=5.0).close()
    except tls.PinMismatch:
        raise Check("served TLS key does not match the card pin") from None
    except tls.LinkClosed:
        raise Check("nothing answers TLS on the listen address") from None


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
    link_listen: str | None = None,
    dev_shared_machine: bool = False,
    publish_url: str | None = None,
    out: TextIO,
    accounts: Accounts | None = None,
    fw_file: Path = DEFAULT_FW_FILE,
    fw_dump: Path = DEFAULT_FW_DUMP,
    link_iface: str = "wt0",
    hub_user: str = HUB_USER,
    layout_flag: str = "auto",
    host_users: Iterable[str] = (),
    agent_users: Iterable[str] = (),
    state_uid: int | None = None,
    unit_user: Callable[[], str | None] = unit_user_of,
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
        except Exception as e:  # noqa: BLE001 fail closed: an unexpected error is a FAIL, never a crash or a silent pass
            fails += 1
            print(f"FAIL {name}: unexpected {type(e).__name__}", file=out)
            return None
        print(f"PASS {name}", file=out)
        return r if r is not None else True

    def fail(name: str, why: str) -> None:
        nonlocal fails
        fails += 1
        print(f"FAIL {name}: {why}", file=out)

    step("state dir and key file modes", lambda: check_state_modes(state))
    keys = step("key store opens", lambda: open_keys(state))
    card = step("card verifies and is dev: true", lambda: check_card(keys)) if keys else None
    if keys is None:
        fail("card verifies and is dev: true", "key store did not open")
    step("registry readable", lambda: check_registry(state))
    if listen is not None and link_listen is not None:
        fail("listen addresses", "--listen and --link-listen are exclusive")
    elif listen is not None or link_listen is not None:
        addr, is_link = (link_listen, True) if link_listen is not None else (listen, False)
        if isinstance(card, dict):
            step("TLS pin matches the served key", lambda: check_tls_pin(addr, card["tls_spki_sha256"], link=is_link))
        else:
            fail("TLS pin matches the served key", "no verified card")
    if publish_url is not None:
        step("publish URL answers /v2/hub/card", lambda: check_publish(publish_url))

    holder: list[Accounts] = []
    if accounts is not None:
        holder.append(accounts)
    elif step("OS accounts readable", lambda: holder.append(Accounts.load(host_users, agent_users))) is None:
        holder = []
    if not holder:
        fail("layout", "OS accounts unreadable, layout unknown")
    else:
        acc = holder[0]
        hosts, edge = acc.host_users, acc.edge_users
        colocated = layout_flag == "one-vps" or bool(hosts or edge)
        try:
            suid = state_uid if state_uid is not None else os.stat(state).st_uid
        except OSError:
            suid = -1  # no state dir: the checks above already failed; the owner is then unknown
        if link_listen is not None:  # the same bind rules as serve (G1-G3); the root rule is for the serving process
            dev = isinstance(card, dict) and card.get("dev") is True

            def bind() -> object:
                parse_link_listen(link_listen, assigned=lambda ip: True)
                return check_bind_allowed(
                    acc,
                    hub_user=acc.name_of(suid) or hub_user,
                    card_dev=dev,
                    dev_shared_machine=dev_shared_machine,
                    check_root=False,
                )

            try:
                warn = bind()
                print("PASS network link bind is allowed here", file=out)
                if warn:
                    print(f"WARN {warn}", file=out)
            except BindRefused as e:
                fail("network link bind is allowed here", str(e))
        if layout_flag == "split" and (hosts or edge):
            fail("layout", "--layout split but host/relay/publish users exist on this machine")
        elif colocated:
            what = ", ".join(
                x for x in (f"{len(hosts)} workspace host(s)" if hosts else "", "relay/publish" if edge else "") if x
            )
            print(
                f"INFO layout: one machine (hub + {what or 'nothing found, forced by --layout'}): weaker isolation (D82, §4a)",
                file=out,
            )
            step("hub runs as its own OS user", lambda: check_hub_user(acc, hub_user, suid, unit_user()))
            step("host and agent users are separate from the hub", lambda: check_separation(acc, hub_user))
            if hosts:
                step("hosts use the server custody profile", lambda: check_server_profile(acc))
                if listen is not None:
                    port = int(parse_listen(listen)[1])
                    step(
                        "link firewall allows only root, hub and host users",
                        lambda: check_link_firewall(acc, hub_user, fw_file, fw_dump, port),
                    )
                elif link_listen is None:
                    fail("link firewall allows only root, hub and host users", "needs --listen (the port to look for)")
            if link_listen is not None:
                lport = int(link_listen.rpartition(":")[2])
                step(
                    "network link port is limited to " + link_iface,
                    lambda: check_link_surface(fw_dump, lport, link_iface),
                )
        else:
            print(
                "INFO layout: hub alone (no orch-host-*, agent-, relay or publish users found; split layout)", file=out
            )
    print("doctor: all checks passed" if not fails else f"doctor: {fails} check(s) FAILED", file=out)
    return fails
