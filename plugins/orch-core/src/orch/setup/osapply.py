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
import errno
import json
import os
import pwd
import secrets
import stat
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Protocol

from .osfacts import UNREADABLE, GroupInfo, OsFacts, RealOsFacts
from .oslauncher import TRUSTED_ROOT
from .osusers import (
    AGENT_HOME_ROOT,
    AGENT_PREFIX,
    NORMAL_PERSON,
    SHARE_ROOT,
    SLOT_HOME_ROOT,
    SSHD_DROPIN,
    PinEntry,
    Plan,
    PolicyVerdict,
    UserPlan,
    assess_policy,
    plan,
    share_dir,
    share_releases,
    valid_person,
)

__all__ = [
    "BIN",
    "ApplyResult",
    "DirSpec",
    "secure_dir",
    "DryRunExecutor",
    "RealExecutor",
    "apply",
    "main",
    "release_problem",
    "share_acl",
    "recheck",
    "why_not_ok",
]

BIN = {
    "useradd": "/usr/sbin/useradd",
    "usermod": "/usr/sbin/usermod",
    "tmpfiles": "/usr/bin/systemd-tmpfiles",
    "systemctl": "/usr/bin/systemctl",
    "install": "/usr/bin/install",
    "sshd": "/usr/sbin/sshd",
    "caddy": "/usr/bin/caddy",  # only `caddy version`, after osfront checked the file is root's (R1-7)
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
    def secure_dir(self, base: str, spec: DirSpec) -> tuple[bool, str | None]: ...


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

    def secure_dir(self, base: str, spec: DirSpec) -> tuple[bool, str | None]:
        return secure_dir(base, spec)


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

    def secure_dir(self, base: str, spec: DirSpec) -> tuple[bool, str | None]:
        return False, None


USER_OBJ, USER, GROUP_OBJ, MASK, OTHER = 0x01, 0x02, 0x04, 0x10, 0x20
_UNDEF = 0xFFFFFFFF
_ACL_NAMES = {"access": "system.posix_acl_access", "default": "system.posix_acl_default"}


def acl(entries: tuple[tuple[int, ...], ...]) -> bytes:
    """A POSIX ACL in its xattr form (version 2): ``(tag, perm)`` or ``(tag, perm, id)`` entries in kernel order."""
    out = struct.pack("<I", 2)
    for entry in entries:
        out += struct.pack("<HHI", entry[0], entry[1], entry[2] if len(entry) > 2 else _UNDEF)
    return out


def share_acl(agent_uid: int) -> bytes:
    """The exact access ACL of ``SHARE_ROOT/<person>`` (13.2 item 5): ``u::rwx,u:<agent>:--x,g::---,m::--x,o::---``.
    The agent may pass through to the subfolders the person opens for it, and may not list the folder."""
    return acl(((USER_OBJ, 7), (USER, 1, agent_uid), (GROUP_OBJ, 0), (MASK, 1), (OTHER, 0)))


@dataclass(frozen=True)
class DirSpec:
    chain: tuple[str, ...]  # path components below the root-owned base; the last one is the directory in question
    uid: int
    gid: int
    mode: int  # the mode to set on a directory this run creates or owns the creation of
    allowed: int  # an existing directory may have these bits at most (an ACL mask shows in the group bits)
    acl_access: bytes | None  # the exact access ACL, or None for none
    acl_default: bytes | None
    fresh: bool  # the user was created by this run: set owner and mode outright (that only tightens)


def _getx(fd: int, name: str) -> bytes:
    if not hasattr(os, "getxattr"):
        raise OSError(errno.ENOTSUP, "no extended attributes on this system")
    return os.getxattr(fd, name)


def _setx(fd: int, name: str, value: bytes) -> None:
    if not hasattr(os, "setxattr"):
        raise OSError(errno.ENOTSUP, "no extended attributes on this system")
    os.setxattr(fd, name, value)


def secure_dir(base: str, spec: DirSpec, *, base_uid: int = 0) -> tuple[bool, str | None]:
    """Create or check ``spec.chain`` below ``base`` using only descriptors opened with O_NOFOLLOW from ``base``.
    Returns ``(changed, problem)``. A symlink, a foreign owner, a wider mode or an ACL entry that differs is a problem;
    nothing existing is loosened or silently changed."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    name = "/".join((base, *spec.chain))
    fds: list[int] = []
    changed = False
    try:
        try:
            cur = os.open(base, flags)
        except OSError as exc:
            return False, f"{base} cannot be opened safely ({exc.strerror})."
        fds.append(cur)
        info = os.fstat(cur)
        if info.st_uid != base_uid or info.st_mode & 0o022:
            return False, f"{base} is not owned by root or can be written by others."
        for part in spec.chain[:-1]:
            try:
                cur = os.open(part, flags, dir_fd=cur)
            except OSError:
                return False, f"{name} cannot be used: {part} is missing or not a plain directory."
            fds.append(cur)
        last, created = spec.chain[-1], False
        try:
            fd = os.open(last, flags, dir_fd=cur)
        except FileNotFoundError:
            try:
                os.mkdir(last, 0o700, dir_fd=cur)
                fd, created = os.open(last, flags, dir_fd=cur), True
            except OSError as exc:  # a race with the agent: a problem to report, never a traceback
                return False, f"{name} could not be created safely ({exc.strerror}); setup stopped."
        except OSError:
            return False, f"{name} is a symlink or not a directory; setup will not follow or change it."
        fds.append(fd)
        info = os.fstat(fd)
        if created or spec.fresh:
            os.fchown(fd, spec.uid, spec.gid)
            os.fchmod(fd, spec.mode)
            changed = True
        elif info.st_uid != spec.uid or info.st_gid != spec.gid:
            return changed, f"{name} belongs to someone other than its user; setup will not change it."
        elif stat.S_IMODE(info.st_mode) & ~spec.allowed:
            return (
                changed,
                f"{name} is more open than it should be ({stat.S_IMODE(info.st_mode):04o}); setup will not fix it.",
            )
        for kind, want in (("access", spec.acl_access), ("default", spec.acl_default)):
            try:
                have = _getx(fd, _ACL_NAMES[kind])
            except OSError as exc:
                if exc.errno not in (errno.ENODATA, errno.ENOTSUP):
                    return changed, f"{name}: the {kind} ACL could not be read ({exc.strerror})."
                have = None
            if have == want:
                continue
            if have is not None:
                return changed, f"{name} has other ACL entries than orch sets; setup will not change them."
            try:
                _setx(fd, _ACL_NAMES[kind], want)
            except OSError as exc:
                return changed, f"{name}: the {kind} ACL could not be set ({exc.strerror})."
            changed = True
        return changed, None
    finally:
        for fd in fds:
            os.close(fd)


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
        if self.actions:
            lines = [head, *[f"  {a}" for a in self.actions]]
        else:
            lines = [] if self.problems else ["Nothing to change."]  # a refused run changed nothing, but is not a no-op
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
    if v.changed:
        return "A sudo or SSH policy file changed while it was being checked, so nothing is recorded. Run this again."
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


def release_problem(facts: OsFacts, release: str, person_uid: int | None) -> str | None:
    """Why ``release`` cannot be what the units name (``None``: it can). The release lives in the person's tree, so it
    need not be root's, but nobody else may be able to swap it: the directory and EVERY ancestor are owned by root or
    by the person and are not writable by group or other (a sticky ``/tmp`` fails this too). Root never executes
    anything in it (the units' root-run lines name the trusted copy only; the agent runs it as itself), and setup never
    writes an ACL for an agent or add-on user on it (the person does, §13.2 item 6)."""
    owners = {0} if person_uid is None else {0, person_uid}
    node = release
    while True:
        info = facts.stat(node)
        if info is not None and info.is_symlink:  # never followed: its target could be swapped by whoever owns the link
            return f"{node} is a symbolic link; give the release by its real path."
        if info is None or (node == release and not info.is_dir):
            return f"The release directory {release} must exist and be a plain directory."
        if info.uid not in owners or info.mode & 0o022:
            return (
                f"{node} (owner {info.uid}, mode {info.mode & 0o7777:04o}) must be owned by root or by you and not be "
                f"writable by group or other; it is part of the path of the release directory {release}."
            )
        parent = os.path.dirname(node)
        if parent == node:
            return None
        node = parent


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
    def root_dir(self, path: str, mode: int = 0o755) -> None:
        """A root-owned directory (never below anything an agent owns): created, or checked and left alone. An
        existing one may have fewer permission bits than ``mode``, never more."""
        st = self.facts.stat(path)
        if st is None:
            self.run([BIN["install"], "-d", "-m", f"{mode:04o}", "-o", _ROOT, "-g", _ROOT, path])
        elif st.is_symlink or not st.is_dir:
            self.res.fail(f"{path} exists but is not a plain directory; setup will not change it.")
        elif not (st.uid == 0 and st.mode & 0o022 == 0):
            self.res.fail(f"{path} exists but is not owned by root or can be written by others.")
        elif st.mode & 0o777 & ~mode:
            self.res.fail(f"{path} is more open than it should be ({st.mode & 0o777:04o}); setup will not fix it.")

    def secure(self, base: str, spec: DirSpec, what: str) -> bool:
        """Directories below an agent- or slot-owned one are never touched by path: ``ex.secure_dir`` opens them
        file-descriptor-relative from the root-owned ``base`` with O_NOFOLLOW, so a symlink planted by the agent is
        refused instead of followed."""
        changed, problem = self.ex.secure_dir(base, spec)
        if changed or self.dry:  # an unchanged directory is not an action (a second run must report nothing)
            self.res.actions.append(what)
        if problem:
            self.res.fail(problem)
        return changed

    # steps ----------------------------------------------------------------------------------------------------
    def common_dirs(self) -> None:
        for path in ("/var/lib/orch", AGENT_HOME_ROOT, SLOT_HOME_ROOT, "/etc/orch", PIN_DIR, "/etc/tmpfiles.d"):
            self.root_dir(path)
            if not self.res.ok:
                return
        self.root_dir(SHARE_ROOT, 0o711)  # one name can be passed through, none listed: not even who uses orch
        if not self.res.ok:
            return
        if self.facts.sshd_installed():
            self.root_dir("/etc/ssh/sshd_config.d")

    def create_users(self, p: Plan) -> None:
        for u in p.all_users():
            if u.action != "create":
                continue
            self.run([BIN["useradd"], *u.useradd_args()])
            self.created.add(u.name)
            if not self.res.ok:
                return
            uid = self.uid_of(u.name)
            if uid is None and not self.dry:
                self.res.fail(f"'{u.name}' does not exist after useradd.")
                return
            # the base pin goes in straight away: without it the next plan would call this user foreign
            entry = next(e for e in p.pins if e.user == u.name)
            where = f"{PIN_DIR}/{uid if uid is not None else '<uid of ' + u.name + '>'}.json"
            self.put(where, _json(entry.document()), 0o644)
            self.run([BIN["usermod"], "--lock", u.name])
            if not self.res.ok:
                return

    def agent_dirs(self, p: Plan, u: UserPlan) -> None:
        person = self.uid_of(p.person)
        uid, grp_ = self.uid_of(u.name), self.facts.group(u.group)
        if self.dry:
            uid, grp_ = uid or -1, grp_ or GroupInfo(u.group, -1, frozenset())
        if uid is None or grp_ is None or person is None:
            self.res.fail(f"'{u.name}' or '{p.person}' could not be looked up.")
            return
        ours, gid = u.name in self.created, grp_.gid
        x = acl(((USER_OBJ, 7), (USER, 1, person), (GROUP_OBJ, 0), (MASK, 1), (OTHER, 0)))
        rx = acl(((USER_OBJ, 7), (USER, 5, person), (GROUP_OBJ, 5), (MASK, 5), (OTHER, 0)))
        steps = (
            ((u.name,), 0o700, 0o710, x, None, "directory with the person allowed to pass through"),
            ((u.name, "home"), 0o700, 0o700, None, None, "home"),
            ((u.name, "state"), 0o700, 0o700, None, None, "state"),
            ((u.name, "repos"), 0o750, 0o750, rx, rx, "clones, readable by the person"),
        )
        for chain, mode, allowed, access, default, why in steps:
            spec = DirSpec(chain, uid, gid, mode, allowed, access, default, ours)
            self.secure(
                AGENT_HOME_ROOT, spec, f"secure {AGENT_HOME_ROOT}/{'/'.join(chain)} ({u.name}, {mode:04o}; {why})"
            )
            if not self.res.ok:
                return

    def share_dir(self, p: Plan, u: UserPlan) -> None:
        """``SHARE_ROOT/<person>``: the person's, 0700 with the one agent entry ``--x``. Root creates it and sets that
        ACL (file-descriptor-relative from the root-owned ``SHARE_ROOT``) and never opens anything below it: the person
        fills it (mirrors, release, interpreter) and nobody else writes there."""
        person, agent = self.facts.user(p.person), self.uid_of(u.name)
        if self.dry and agent is None:
            agent = _UNDEF - 1  # not created yet in a dry run: any placeholder id, nothing is written
        if person is None or agent is None:
            self.res.fail(f"'{u.name}' or '{p.person}' could not be looked up.")
            return
        spec = DirSpec(
            (p.person,), person.uid, person.gid, 0o700, 0o710, share_acl(agent), None, False
        )  # 0710: the mask shows in the group bits
        self.secure(
            SHARE_ROOT, spec, f"secure {share_dir(p.person)} ({p.person}, 0700; {u.name} may pass through only)"
        )

    def slot_dirs(self, slots: list[UserPlan]) -> None:
        for u in slots:
            uid, grp_ = self.uid_of(u.name), self.facts.group(u.group)
            if self.dry:
                uid, grp_ = uid or -1, grp_ or GroupInfo(u.group, -1, frozenset())
            if uid is None or grp_ is None:
                self.res.fail(f"'{u.name}' could not be looked up.")
                return
            spec = DirSpec((u.name,), uid, grp_.gid, 0o700, 0o700, None, None, u.name in self.created)
            self.secure(SLOT_HOME_ROOT, spec, f"secure {u.home} ({u.name}, 0700)")
            if not self.res.ok:
                return

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
        """Write the templates. The runner is started only after the policy verdict (``start_runner``). The add-on
        slot units stay off until L3 builds the slot launcher they run (TODO(L3): enable them there)."""
        for unit in p.units:
            if self.put(unit.path, unit.content, 0o644):
                self.unit_written = True
        if self.unit_written and self.res.ok:
            self.run([BIN["systemctl"], "daemon-reload"])

    def start_runner(self, p: Plan) -> None:
        sysctl = BIN["systemctl"]
        for unit in p.units:
            if not unit.name.startswith("orch-runner@"):
                continue
            for inst in unit.instances:
                name = f"orch-runner@{inst}.service"
                if self.dry or self.ex.query([sysctl, "is-enabled", "--quiet", name]).rc != 0:
                    self.run([sysctl, "enable", name])
                if self.dry or self.ex.query([sysctl, "is-active", "--quiet", name]).rc != 0:
                    self.run([sysctl, "start", name])

    def drop_verdict(self, p: Plan) -> bool:
        """Rewrite the agent's pin without the policy verdict (the host then answers broken). ``False``: there is
        nothing to drop (the agent user is not kept) or the pin could not be written (a problem is recorded)."""
        agent = p.agent
        uid = None if agent is None else self.uid_of(agent.name)
        if agent is None or agent.action != "keep" or uid is None:
            return False
        entry = next(e for e in p.pins if e.user == agent.name)
        base = _json(PinEntry(entry.user, entry.cls, entry.host_uid, uid).document())
        path = f"{PIN_DIR}/{uid}.json"
        try:
            have: str | None = self.ex.read_file(path)
        except (OSError, ValueError):
            have = None  # unreadable: write it anyway
        if have != base:
            self.res.actions.append(f"write {path} (mode 0644, root; without the policy verdict)")
            try:
                self.ex.write_file(path, base, 0o644)
            except OSError as exc:
                self.res.fail(f"{path} could not be rewritten ({exc.strerror}).")
                return False
        return True

    def quarantine(self, p: Plan) -> None:
        """The agent user no longer passes: drop any old verdict from its pin (the host then answers broken) and stop
        its runner. Used when a re-run finds, for example, a new sudo rule. Never leaves a running runner behind: a
        runner that is still active after ``disable --now`` failed is a problem, not a note."""
        agent = p.agent
        if agent is None or agent.action != "keep" or self.uid_of(agent.name) is None:
            return
        self.drop_verdict(p)
        sysctl = BIN["systemctl"]
        stopped = True
        for unit in p.units:
            if unit.name.startswith("orch-runner@"):
                for inst in unit.instances:
                    name = f"orch-runner@{inst}.service"
                    argv = [sysctl, "disable", "--now", name]
                    # always, whatever the queries would say; "not loaded" is fine, so the result is not a failure
                    self.res.actions.append(" ".join(argv))
                    if self.ex.run(argv).rc != 0 and self.ex.query([sysctl, "is-active", "--quiet", name]).rc == 0:
                        stopped = False
                        self.res.fail(
                            f"The runner of '{agent.name}' could not be stopped and is still running. "
                            f"Stop it by hand: systemctl stop {name}"
                        )
        if stopped:
            self.res.notes.append(f"the runner of '{agent.name}' was stopped until the check passes again")

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
            self.start_runner(p)
            return
        if uid is None:
            return
        verdict = assess_policy(
            self.facts, agent.name, dropin_text=self.ex.read_file(SSHD_DROPIN), host_uid=p.person_uid
        )
        self.res.policy_ok = verdict.ok
        if not verdict.ok:
            self.res.fail(why_not_ok(verdict, agent.name))
            self.quarantine(p)
            return
        entry = next(e for e in p.pins if e.user == agent.name)
        doc = PinEntry(entry.user, entry.cls, entry.host_uid, uid, verdict.document()).document()
        self.put(f"{PIN_DIR}/{uid}.json", _json(doc), 0o644, replace=True)
        if self.res.ok:
            self.start_runner(p)  # only now: a runner never starts while the policy check fails


_RECHECK_RELEASE = "/recheck-needs-no-release"  # plan() wants a well-formed path; a recheck never reads or uses it


def recheck(facts: OsFacts, ex: Executor, person: str, *, is_root: bool) -> ApplyResult:
    """``orch-os-setup --recheck <person>``: the root-side policy check of the runner's ``ExecStartPre=+`` (spec 13.9).
    Asks the authoritative tools again (``sudo -l -U`` and ``sshd -T``, so LDAP and sssd are covered too) and runs the
    same plan checks as setup (groups, properties, pin). ``ok`` only if every answer still holds; then the pin is
    refreshed with the new stat signatures (the verdict is what counts, the signatures are the host's change
    detector). Otherwise the verdict is dropped from the pin (the host answers broken) and ``ok`` is false, which makes
    systemd refuse the start. It never calls ``systemctl``: stopping the unit from inside its own start job would wait
    on itself, and the failed start is the stop. Fails closed on every error."""
    res = ApplyResult()
    if not is_root:
        return res.fail("orch-os-setup --recheck must run as root. The runner will not start.")
    if not valid_person(person):
        return res.fail(f"'{person}' is not a person orch set up agents for. The runner will not start.")
    if facts.platform != "linux":
        return res.fail("Agents run as their own user only on Linux. The runner will not start.")
    info = facts.user(person)
    if info is None or info.uid == 0:
        return res.fail(f"There is no user '{person}' to run agents for. The runner will not start.")
    st = _Apply(facts, ex, res)
    p = plan(facts, person, info.uid, release=_RECHECK_RELEASE)
    agent = p.agent
    if p.refusals or agent is None or agent.action != "keep":
        for r in p.refusals:
            res.fail(r.message)
        if not res.problems:
            res.fail(f"The user '{AGENT_PREFIX}{person}' does not exist. Run orch-os-setup again.")
        st.drop_verdict(p)
        return res
    verdict = assess_policy(facts, agent.name, dropin_text=ex.read_file(SSHD_DROPIN), host_uid=p.person_uid)
    res.policy_ok = verdict.ok
    if not verdict.ok:
        res.fail(why_not_ok(verdict, agent.name))
        st.drop_verdict(p)
        return res
    uid = agent.uid
    entry = next(e for e in p.pins if e.user == agent.name)
    doc = PinEntry(entry.user, entry.cls, entry.host_uid, uid, verdict.document()).document()
    st.put(f"{PIN_DIR}/{uid}.json", _json(doc), 0o644, replace=True)
    return res


def _json(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, indent=2) + "\n"


def _tmpfiles_paths(text: str) -> list[str]:
    return [ln.split()[1] for ln in text.splitlines() if ln.startswith("d ")]


def apply(
    facts: OsFacts,
    ex: Executor,
    person: str,
    person_uid: int,
    *,
    release: str,
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
        _Apply(facts, ex, res).quarantine(p)  # on ANY refusal a kept agent must not keep running or stay recorded
        return res
    # The share's own releases folder is made by the person (root opens nothing below the share), so it need not exist
    # yet: the runner unit retries until the host has copied the release in. Any other release is checked as before.
    own_share = p.person_uid is not None and release == share_releases(p.person)
    problem = None if own_share else release_problem(facts, release, p.person_uid)
    if problem:
        return res.fail(problem)
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
    if not dry:
        p = plan(facts, person, person_uid, release=release)  # now the new users are pinned and adopted
        if p.refusals:
            for r in p.refusals:
                res.fail(r.message)
            return res
    for step in (
        lambda: p.agent and st.agent_dirs(p, p.agent),
        lambda: p.agent and st.share_dir(p, p.agent),
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
class _RootFacts(RealOsFacts):
    """Root's view while setting up. The pin is read with the standard library only: ``orch.host.pins`` would pull
    third-party code into the root process."""

    def pin(self, uid: int) -> dict | None:
        broken: dict = {"class": "agent", "workspaces": "*"}  # unreadable or foreign: never "no pin"
        try:
            fd = os.open(f"{PIN_DIR}/{int(uid)}.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError:
            return broken
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                return broken
            try:
                doc = json.loads(handle.read(65536))
            except ValueError:
                return broken
        return doc if isinstance(doc, dict) else broken


def _running_from_trusted_copy() -> bool:
    """Defence in depth: ``oslauncher`` already checked the tree; this refuses a direct ``python -m`` from anywhere
    else, such as the person's release."""
    here = os.path.realpath(__file__)
    return here.startswith(os.path.realpath(TRUSTED_ROOT) + os.sep)


def main(argv: list[str] | None = None) -> int:
    # The trust checks come FIRST: a copy that is not the root-owned one never reads, parses or answers arguments.
    if sys.platform != "linux":
        print("Agents run as their own user only on Linux for now; nothing was changed.", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("orch-os-setup must run as root (use sudo). Nothing was changed.", file=sys.stderr)
        return 2
    if not _running_from_trusted_copy():
        print(
            f"orch-os-setup runs only from the root-owned copy ({TRUSTED_ROOT}); the installer puts it there. "
            "Nothing was changed.",
            file=sys.stderr,
        )
        return 2
    ap = argparse.ArgumentParser(
        prog="orch-os-setup", description="Create the OS users that agents and add-ons run as."
    )
    ap.add_argument("--person", help="the login name that runs orch (the host user)")
    ap.add_argument(
        "--recheck",
        metavar="PERSON",
        help="ask sudo and sshd again whether the agent user of PERSON is still safe (the runner's start check)",
    )
    ap.add_argument("--release", help="the releases directory the services run (required with --person)")
    ap.add_argument("--dry-run", action="store_true", help="print the exact actions and change nothing")
    ap.add_argument("--front-enable", action="store_true", help="set up the HTTPS front of the remote dashboard")
    ap.add_argument("--front-disable", action="store_true", help="stop and remove the HTTPS front")
    ap.add_argument("--front-enroll", action="store_true", help="print the first passkey link of the remote dashboard")
    ap.add_argument("--ctl", help="with --front-enroll: the person's dashboard control socket")
    ap.add_argument("--name", help="with --front-enable: the public DNS name of the dashboard")
    ap.add_argument(
        "--trusted-proxy",
        action="append",
        default=[],
        help="with --front-enable: a network whose X-Forwarded-For counts",
    )
    args = ap.parse_args(argv)
    if args.front_enable or args.front_disable or args.front_enroll:
        return _front(args)
    if args.name or args.trusted_proxy or args.ctl:
        print("--name, --trusted-proxy and --ctl go with the --front-* options. Nothing was changed.", file=sys.stderr)
        return 2
    if bool(args.person) == bool(args.recheck):
        print(
            "Give exactly one of --person (set up) or --recheck (the start check). Nothing was changed.",
            file=sys.stderr,
        )
        return 2
    if args.person and not args.release:
        print(
            "--release is required with --person (the setup page prints the whole line). Nothing was changed.",
            file=sys.stderr,
        )
        return 2
    if args.recheck:
        res = recheck(_RootFacts(), RealExecutor(), args.recheck, is_root=True)
        if res.problems:
            print(res.report(), file=sys.stderr)
        return 0 if res.ok else 1
    try:
        uid = pwd.getpwnam(args.person).pw_uid
    except KeyError:
        print(f"There is no user named '{args.person}'. Nothing was changed.", file=sys.stderr)
        return 2
    ex: Executor = DryRunExecutor() if args.dry_run else RealExecutor()
    res = apply(_RootFacts(), ex, args.person, uid, release=args.release, is_root=True)
    print(res.report())
    return 0 if res.ok else 1


def _front(args: argparse.Namespace) -> int:
    """``--front-enable`` / ``--front-disable`` (R1-7): the HTTPS front of the remote dashboard, in ``osfront``."""
    if [args.front_enable, args.front_disable, args.front_enroll].count(True) != 1 or args.recheck or args.release:
        print(
            "Give exactly one of --front-enable, --front-disable and --front-enroll, alone. Nothing was changed.",
            file=sys.stderr,
        )
        return 2
    if args.front_enroll:
        if not (args.person and args.ctl) or args.name or args.trusted_proxy or args.dry_run:
            print("--front-enroll takes --person and --ctl only. Nothing was changed.", file=sys.stderr)
            return 2
        from . import osfront  # noqa: PLC0415

        return osfront.enroll(args.person, args.ctl)
    if args.front_enable and (args.ctl or not (args.person and args.name)):
        print("--front-enable needs --person and --name. Nothing was changed.", file=sys.stderr)
        return 2
    if args.front_disable and (args.person or args.name or args.trusted_proxy or args.ctl):
        print("--front-disable takes no other option than --dry-run. Nothing was changed.", file=sys.stderr)
        return 2
    from . import osfront  # noqa: PLC0415  (a trusted file too; only loaded when asked)

    ex: Executor = DryRunExecutor() if args.dry_run else RealExecutor()
    res = osfront.run(args, _RootFacts(), ex)
    print(res.report())
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
