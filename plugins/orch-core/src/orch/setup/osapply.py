"""The OS-user setup APPLY step, ``orch-os-setup`` (H1 L2, spec 13.2): root, Linux, once.

``apply(...)`` takes the L1 plan and carries it out. Rules:

- **Fixed absolute binaries** (``BIN``): no PATH lookup, no shell, an empty environment. The executor refuses any other
  program.
- **Repair, never widen.** An existing correct item is left alone and nothing is run for it (a second run runs no
  changing command). An existing item with wrong properties is refused, never loosened and never silently changed. A
  directory that the same run just created is set to its exact mode and owner, which can only tighten it.
- **The pin is the product.** Root writes ``/etc/orch/pins/<uid>.json``. A user created here first gets the base pin
  (class, host uid), without which the next plan would refuse it as foreign. The agent pin gets the policy verdict
  (``assess_policy``: sudo's and sshd's own answers plus stat signatures) only if that verdict is OK; otherwise the
  result says in plain words why, and the host sees no policy block, so ``isolation.detect()`` answers broken.
- Writes: nothing outside the files named below. Dry run prints the exact actions and changes nothing.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import pwd
import secrets
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .osfacts import UNREADABLE, OsFacts, RealOsFacts
from .osusers import (
    AGENT_HOME_ROOT,
    NORMAL_PERSON,
    SLOT_HOME_ROOT,
    SSHD_DROPIN,
    PinEntry,
    Plan,
    PolicyVerdict,
    UserPlan,
    assess_policy,
    plan,
)

__all__ = ["BIN", "ApplyResult", "DryRunExecutor", "RealExecutor", "apply", "main", "why_not_ok"]

BIN = {
    "useradd": "/usr/sbin/useradd",
    "groupadd": "/usr/sbin/groupadd",
    "usermod": "/usr/sbin/usermod",
    "tmpfiles": "/usr/bin/systemd-tmpfiles",
    "systemctl": "/usr/bin/systemctl",
    "install": "/usr/bin/install",
    "setfacl": "/usr/bin/setfacl",
    "sshd": "/usr/sbin/sshd",
}
_ALLOWED = frozenset(BIN.values())
TMPFILES_CONF = "/etc/tmpfiles.d/orch.conf"
PIN_DIR = "/etc/orch/pins"
_ROOT = "root"
_SSH_SERVICES = ("ssh.service", "sshd.service")


@dataclass(frozen=True)
class RunResult:
    rc: int
    out: str = ""


class Executor(Protocol):
    """Everything that touches the machine. ``query`` is read-only and runs in a dry run too."""

    def run(self, argv: list[str]) -> RunResult: ...
    def query(self, argv: list[str]) -> RunResult: ...
    def write_file(self, path: str, content: str, mode: int) -> None: ...  # root-owned, atomic
    def read_file(self, path: str) -> str | None: ...
    def remove(self, path: str) -> None: ...


def _spawn(argv: list[str]) -> RunResult:
    if argv[0] not in _ALLOWED:
        raise ValueError(f"{argv[0]} is not one of the fixed binaries")
    try:
        done = subprocess.run(  # no shell, fixed absolute program, empty environment
            argv, capture_output=True, text=True, timeout=120, check=False, env={"LC_ALL": "C"}
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return RunResult(127, f"{type(exc).__name__}: {exc}")
    return RunResult(done.returncode, done.stdout + done.stderr)


class RealExecutor:
    dry = False

    def run(self, argv: list[str]) -> RunResult:
        return _spawn(argv)

    query = run

    def write_file(self, path: str, content: str, mode: int) -> None:
        tmp = f"{path}.tmp-{secrets.token_hex(4)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fchmod(handle.fileno(), mode)
                os.fchown(handle.fileno(), 0, 0)
                os.fsync(handle.fileno())
            os.rename(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def read_file(self, path: str) -> str | None:
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        except FileNotFoundError:
            return None

    def remove(self, path: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


class DryRunExecutor(RealExecutor):
    """Records nothing itself (``apply`` records every action); changes nothing; queries and reads are real."""

    dry = True

    def run(self, argv: list[str]) -> RunResult:
        return RunResult(0)

    def query(self, argv: list[str]) -> RunResult:
        return _spawn(argv)

    def write_file(self, path: str, content: str, mode: int) -> None:
        return None

    def remove(self, path: str) -> None:
        return None


@dataclass
class ApplyResult:
    ok: bool = True
    actions: list[str] = field(default_factory=list)  # what was (or, in a dry run, would be) done, in order
    problems: list[str] = field(default_factory=list)  # plain reasons; any entry means not ok
    notes: list[str] = field(default_factory=list)
    policy_ok: bool | None = None  # None until judged
    dry_run: bool = False

    def fail(self, message: str) -> ApplyResult:
        self.ok = False
        self.problems.append(message)
        return self

    def report(self) -> str:
        head = "Would do:" if self.dry_run else "Done:"
        lines = [head, *[f"  {a}" for a in self.actions]] if self.actions else ["Nothing to change."]
        lines += [f"  note: {n}" for n in self.notes]
        lines += [f"PROBLEM: {p}" for p in self.problems]
        return "\n".join(lines)


def why_not_ok(v: PolicyVerdict, agent: str) -> str:
    """Plain reason the policy verdict is not OK (the first cause), naming /etc/doas.conf when it is the reason."""
    if v.files.get("/etc/doas.conf") is not None:
        return (
            f"/etc/doas.conf exists. orch cannot check doas rules, so '{agent}' is not recorded as safe. "
            "Remove /etc/doas.conf (or the doas package) and run this again."
        )
    if v.sudo == "allowed":
        return f"'{agent}' has a sudo rule. Remove it and run this again."
    if v.sudo == "unknown":
        return f"sudo could not say whether '{agent}' has a rule, so '{agent}' is not recorded as safe."
    if v.ssh == "allowed":
        return f"The SSH server still lets '{agent}' log in. Check /etc/ssh/sshd_config and run this again."
    if v.ssh == "unknown":
        return f"The SSH server could not be asked whether '{agent}' may log in (is sshd -T failing?)."
    if not v.ssh_dropin:
        return f"orch's SSH drop-in {SSHD_DROPIN} is missing or changed."
    if any(sig == list(UNREADABLE) for sig in v.files.values()):
        return "A sudo or SSH policy file could not be read, so the policy cannot be recorded."
    return "The sudo and SSH policy could not be confirmed."


class _Apply:
    def __init__(self, facts: OsFacts, ex: Executor, res: ApplyResult) -> None:
        self.facts, self.ex, self.res = facts, ex, res
        self.dry = bool(getattr(ex, "dry", False))
        self.created: set[str] = set()
        self.unit_written = False

    # recording ------------------------------------------------------------------------------------------------
    def run(self, argv: list[str], what: str | None = None) -> RunResult:
        self.res.actions.append(what or " ".join(argv))
        out = self.ex.run(argv)
        if out.rc != 0:
            self.res.fail(f"'{' '.join(argv)}' failed ({out.rc}): {out.out.strip()[:300]}")
        return out

    def put(self, path: str, content: str, mode: int, *, replace: bool = False) -> bool:
        """Write a root-owned file unless it already has this content. A different existing file is refused."""
        have = self.ex.read_file(path)
        if have == content:
            return False
        if have is not None and not replace:
            self.res.fail(f"{path} already exists with other content; setup will not overwrite it.")
            return False
        self.res.actions.append(f"write {path} (mode {mode:04o}, root)")
        self.ex.write_file(path, content, mode)
        return True

    def uid_of(self, name: str) -> int | None:
        u = self.facts.user(name)
        return None if u is None else u.uid

    # directories ----------------------------------------------------------------------------------------------
    def dir(self, path: str, owner: str, mode: int, *, ours: bool = False) -> bool:
        """Make sure ``path`` is a directory of ``owner`` with at most ``mode``. Returns True when created/tightened."""
        st = self.facts.stat(path)
        want = ["-d", "-m", f"{mode:04o}", "-o", owner, "-g", owner, path]
        if st is None or ours:
            self.run([BIN["install"], *want])
            return True
        u, g = self.uid_of(owner), self.facts.group(owner)
        if st.is_symlink or not st.is_dir:
            self.res.fail(f"{path} exists but is not a plain directory; setup will not change it.")
        elif owner == _ROOT and not (st.uid == 0 and st.mode & 0o022 == 0):
            self.res.fail(f"{path} exists but is not owned by root or can be written by others.")
        elif owner != _ROOT and (st.uid != u or g is None or st.gid != g.gid):
            self.res.fail(f"{path} exists but belongs to someone other than '{owner}'; setup will not change it.")
        elif owner != _ROOT and st.mode & 0o7777 & ~mode:
            self.res.fail(
                f"{path} is more open than it should be ({stat.S_IMODE(st.mode):04o}); setup will not fix it."
            )
        return False

    # steps ----------------------------------------------------------------------------------------------------
    def common_dirs(self) -> None:
        for path in ("/var/lib/orch", AGENT_HOME_ROOT, SLOT_HOME_ROOT, "/etc/orch", PIN_DIR, "/etc/tmpfiles.d"):
            self.dir(path, _ROOT, 0o755)
        if self.facts.sshd_installed():
            self.dir("/etc/ssh/sshd_config.d", _ROOT, 0o755)

    def create_users(self, p: Plan) -> None:
        for u in p.all_users():
            if u.action != "create":
                continue
            self.run([BIN["useradd"], *u.useradd_args()])
            self.run([BIN["usermod"], "--lock", u.name])
            self.created.add(u.name)
            if not self.dry and self.facts.user(u.name) is None:
                self.res.fail(f"'{u.name}' does not exist after useradd.")

    def base_pins(self, p: Plan) -> None:
        for e in p.pins:
            if e.user not in self.created:
                continue  # adopted users already carry a pin: the plan required it
            uid = self.uid_of(e.user)
            where = f"{PIN_DIR}/{uid if uid is not None else '<uid of ' + e.user + '>'}.json"
            if uid is None and not self.dry:
                continue
            doc = e.document()
            self.put(where, _json(doc), 0o644)

    def agent_dirs(self, p: Plan, u: UserPlan) -> None:
        ours = u.name in self.created
        parent = f"{AGENT_HOME_ROOT}/{u.name}"
        self.dir(parent, u.name, 0o700, ours=ours)
        new_home = self.dir(u.home, u.name, 0o700, ours=ours)
        new_state = self.dir(f"{parent}/state", u.name, 0o700, ours=ours)
        new_repos = self.dir(f"{parent}/repos", u.name, 0o750, ours=ours)
        person = p.person
        if ours or new_repos or new_state or new_home or not (self.facts.stat(parent) or _NOACL).has_acl:
            self.run([BIN["setfacl"], "-m", f"u:{person}:x", parent])
        if ours or new_repos or not (self.facts.stat(f"{parent}/repos") or _NOACL).has_acl:
            self.run([BIN["setfacl"], "-m", f"u:{person}:rX,d:u:{person}:rX", f"{parent}/repos"])

    def slot_dirs(self, slots: list[UserPlan]) -> None:
        for u in slots:
            self.dir(u.home, u.name, 0o700, ours=u.name in self.created)

    def tmpfiles(self, p: Plan) -> None:
        text = p.tmpfiles()
        changed = self.put(TMPFILES_CONF, text, 0o644)
        missing = any(self.facts.stat(path) is None for path in _tmpfiles_paths(text))
        if (changed or missing) and self.res.ok:
            self.run([BIN["tmpfiles"], "--create", TMPFILES_CONF])

    def sshd_dropin(self, p: Plan) -> None:
        if not self.facts.sshd_installed():
            self.res.notes.append("sshd is not installed, so nobody can log in over SSH; no SSH drop-in is needed.")
            return
        for spec in p.files:
            if not self.put(spec.path, spec.content, spec.mode):
                continue
            if self.dry:
                self.res.actions += [f"{BIN['sshd']} -t", "reload the running SSH service"]
                continue
            check = self.run([BIN["sshd"], "-t"])
            if check.rc != 0:
                self.ex.remove(spec.path)
                self.res.actions.append(f"remove {spec.path} (sshd rejected it)")
                return
            for svc in _SSH_SERVICES:
                if self.ex.query([BIN["systemctl"], "is-active", "--quiet", svc]).rc == 0:
                    self.run([BIN["systemctl"], "reload", svc])

    def units(self, p: Plan) -> None:
        for unit in p.units:
            if self.put(unit.path, unit.content, 0o644):
                self.unit_written = True
        if self.unit_written and self.res.ok:
            self.run([BIN["systemctl"], "daemon-reload"])
        for unit in p.units:
            stem = unit.name.replace("@.service", "")
            for inst in unit.instances:
                name = f"{stem}@{inst}.service"
                sysctl = BIN["systemctl"]
                if self.dry or self.ex.query([sysctl, "is-enabled", "--quiet", name]).rc != 0:
                    self.run([sysctl, "enable", name])
                if self.dry or self.ex.query([sysctl, "is-active", "--quiet", name]).rc != 0:
                    self.run([sysctl, "start", name])

    def policy_pin(self, p: Plan) -> None:
        agent = p.agent
        if agent is None:
            return
        uid = self.uid_of(agent.name)
        if self.dry:
            self.res.actions.append(
                f"record the sudo and SSH answers in {PIN_DIR}/<uid of {agent.name}>.json, only if sudo and sshd "
                "say the agent user has no sudo rule and cannot log in"
            )
            return
        if uid is None:
            return
        verdict = assess_policy(self.facts, agent.name, dropin_text=self.ex.read_file(SSHD_DROPIN))
        self.res.policy_ok = verdict.ok
        if not verdict.ok:
            self.res.fail(why_not_ok(verdict, agent.name))
            return
        entry = next(e for e in p.pins if e.user == agent.name)
        doc = PinEntry(entry.user, entry.cls, entry.host_uid, uid, verdict.document()).document()
        self.put(f"{PIN_DIR}/{uid}.json", _json(doc), 0o644, replace=True)


class _NoAcl:
    has_acl = False


_NOACL = _NoAcl()


def _json(doc: dict) -> str:
    import json

    return json.dumps(doc, sort_keys=True, indent=2) + "\n"


def _tmpfiles_paths(text: str) -> list[str]:
    return [ln.split()[1] for ln in text.splitlines() if ln.startswith("d ")]


def apply(
    facts: OsFacts,
    ex: Executor,
    person: str,
    person_uid: int,
    *,
    release: str = "/opt/orch/release",
    is_root: bool,
) -> ApplyResult:
    dry = bool(getattr(ex, "dry", False))
    res = ApplyResult(dry_run=dry)
    if facts.platform != "linux":
        p0 = plan(facts, person, person_uid, release=release)
        return res.fail(p0.refusals[0].message if p0.refusals else "This system is not supported yet.")
    if not is_root:
        return res.fail("orch-os-setup must run as root (use sudo). It changes system users and services.")
    p = plan(facts, person, person_uid, release=release)
    if p.refusals:
        for r in p.refusals:
            res.fail(r.message)
        return res
    st = _Apply(facts, ex, res)
    if p.create_person:
        st.run(
            [BIN["useradd"], "--create-home", "--user-group", "--shell", "/bin/bash", NORMAL_PERSON],
            f"create the normal user '{NORMAL_PERSON}' that runs orch",
        )
        if not res.ok:
            return res
        if dry:
            res.notes.append(f"then the plan is made again for '{NORMAL_PERSON}' and the remaining steps follow")
            return res
        else:
            p = plan(facts, person, person_uid, release=release)
            if p.refusals or p.create_person:
                for r in p.refusals:
                    res.fail(r.message)
                return res if res.problems else res.fail(f"'{NORMAL_PERSON}' does not exist after useradd.")
    st.common_dirs()
    if not res.ok:
        return res
    st.create_users(p)
    if not res.ok:
        return res
    st.base_pins(p)
    if not res.ok:
        return res
    if not dry:
        p = plan(facts, person, person_uid, release=release)  # now the new users are pinned and adopted
        if p.refusals:
            for r in p.refusals:
                res.fail(r.message)
            return res
    for step in (
        lambda: p.agent and st.agent_dirs(p, p.agent),
        lambda: st.slot_dirs(list(p.slots)),
        lambda: st.tmpfiles(p),
        lambda: st.sshd_dropin(p),
        lambda: st.units(p),
    ):
        step()
        if not res.ok:
            return res
    st.policy_pin(p)
    return res


# command line -------------------------------------------------------------------------------------------------------
def _code_is_root_trusted() -> bool:
    """Root must not run code that anyone else could have changed: this file and all its ancestors are root's and
    writable by root only."""
    path = Path(__file__).resolve()
    for node in (path, *path.parents):
        try:
            info = os.lstat(node)
        except OSError:
            return False
        if info.st_uid != 0 or info.st_mode & 0o022:
            return False
    return True


def main(argv: list[str] | None = None, *, _check_code: bool = True) -> int:
    ap = argparse.ArgumentParser(
        prog="orch-os-setup", description="Create the OS users that agents and add-ons run as."
    )
    ap.add_argument("--person", required=True, help="the login name that runs orch (the host user)")
    ap.add_argument("--release", default="/opt/orch/release", help="the release directory the services run")
    ap.add_argument("--dry-run", action="store_true", help="print the exact actions and change nothing")
    args = ap.parse_args(argv)
    if sys.platform != "linux":
        print("Agents run as their own user only on Linux for now; nothing was changed.", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("orch-os-setup must run as root (use sudo). Nothing was changed.", file=sys.stderr)
        return 2
    if _check_code and not _code_is_root_trusted():
        print(
            "This copy of orch is not owned by root, so root will not run it. Install orch to a root-owned "
            "location and run its orch-os-setup. Nothing was changed.",
            file=sys.stderr,
        )
        return 2
    try:
        uid = pwd.getpwnam(args.person).pw_uid
    except KeyError:
        print(f"There is no user named '{args.person}'. Nothing was changed.", file=sys.stderr)
        return 2
    ex: Executor = DryRunExecutor() if args.dry_run else RealExecutor()
    res = apply(RealOsFacts(), ex, args.person, uid, release=args.release, is_root=True)
    print(res.report())
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
