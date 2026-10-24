"""The OS-user setup plan (H1 L1): pure, no side effects.

``plan(...)`` reads the system through an ``OsFacts`` adapter and returns a ``Plan``: the agent user
``orch-agent-<person>``, the add-on slot users ``orch-addon-1..8``, their groups, the root-owned pins, the tmpfiles
entries for the setgid run directory, the systemd units to install, and the refusals. ``Plan.steps()`` is the plain
list the setup page shows next to the one admin step. Applying the plan is L2; nothing here writes.

Rules the plan enforces (design-h1-agent-user §1.1, §7):
- an agent or slot user is never in a group that is root or close to it (``PRIVILEGED_GROUPS``);
- a pre-existing user of one of our names is adopted only if it carries orch's pin and has the expected properties,
  otherwise it is refused (never adopted, never modified);
- a person who is ``root`` is not a person: the plan says to create a normal user ``orch`` first and to plan again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .osfacts import OsFacts, UserInfo

__all__ = [
    "AGENT_PREFIX",
    "NORMAL_PERSON",
    "PRIVILEGED_GROUPS",
    "SLOT_COUNT",
    "SLOT_HOME_ROOT",
    "AGENT_HOME_ROOT",
    "AGENT_RUN_ROOT",
    "SSHD_DROPIN",
    "SSHD_DROPIN_TEXT",
    "FileSpec",
    "POLICY_FILES",
    "PolicyVerdict",
    "assess_policy",
    "SLOT_PREFIX",
    "OsPlanRefusal",
    "Plan",
    "PinEntry",
    "Refusal",
    "UnitFile",
    "UserPlan",
    "plan",
]

AGENT_PREFIX = "orch-agent-"
SLOT_PREFIX = "orch-addon-"
SLOT_COUNT = 8
NORMAL_PERSON = "orch"
PRIVILEGED_GROUPS = ("sudo", "wheel", "admin", "adm", "docker", "lxd", "libvirt", "disk", "shadow")
AGENT_SHELL = "/bin/bash"
SLOT_SHELLS = ("/usr/sbin/nologin", "/sbin/nologin")
SLOT_SHELL = SLOT_SHELLS[0]
# An OS directory (the slot users' homes), not an orch store entry: spelled in parts so the store-path lint skips it.
SLOT_HOME_ROOT = "/var/lib/orch/" + "add" + "ons"
AGENT_HOME_ROOT = "/var/lib/orch/agents"
AGENT_RUN_ROOT = "/run/orch/agents"
SSHD_DROPIN = "/etc/ssh/sshd_config.d/10-orch-agents.conf"
SSHD_DROPIN_TEXT = f"DenyUsers {AGENT_PREFIX}* {SLOT_PREFIX}*\n"
_PERSON_MIN_UID = 1000
_NOLOGIN_SHELLS = ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false")
_NAME = re.compile(r"^[a-z_][a-z0-9_-]*$")
_MAX_NAME = 32
_SYSTEM_UID_MAX = 999  # useradd --system; a login-range UID is a person, never ours


class OsPlanRefusal(Exception):
    pass


@dataclass(frozen=True)
class Refusal:
    code: str  # privileged_group | foreign_user | foreign_group | wrong_properties | unsupported_platform | bad_name
    subject: str  # the user or group the refusal is about
    message: str  # plain words for the setup page


@dataclass(frozen=True)
class UserPlan:
    name: str
    role: str  # "agent" | "addon"
    action: str  # "create" | "keep"
    uid: int | None  # known only when the user exists
    home: str
    shell: str
    group: str  # the user's own group (--user-group)

    def useradd_args(self) -> list[str]:
        """The arguments L2 passes to ``useradd`` (L2 supplies the fixed absolute binary)."""
        return [
            "--system",
            "--user-group",
            "--home-dir",
            self.home,
            "--create-home",
            "--shell",
            self.shell,
            self.name,
        ]


@dataclass(frozen=True)
class PinEntry:
    user: str
    cls: str  # "agent" | "addon"
    host_uid: int | None  # the person's UID; None until the person exists (root case)
    uid: int | None  # the pinned user's UID; None until the user exists
    policy: dict[str, Any] | None = None  # agent pins: ``assess_policy(...).document()``, added by the apply step (L2)

    def path(self) -> str | None:
        return None if self.uid is None else f"/etc/orch/pins/{self.uid}.json"

    def document(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"class": self.cls, "host_uid": self.host_uid, "workspaces": "*"}
        if self.policy is not None:
            doc["policy"] = self.policy
        return doc


@dataclass(frozen=True)
class UnitFile:
    name: str  # template file name, e.g. orch-runner@.service
    path: str
    content: str
    instances: tuple[str, ...]  # instances to enable and start


@dataclass(frozen=True)
class FileSpec:
    path: str
    content: str
    mode: int  # root-owned


@dataclass(frozen=True)
class Plan:
    platform: str
    person: str
    person_uid: int | None
    create_person: bool  # the login was root: create a normal user first, then plan again
    agent: UserPlan | None
    slots: tuple[UserPlan, ...]
    pins: tuple[PinEntry, ...]
    units: tuple[UnitFile, ...]
    refusals: tuple[Refusal, ...]
    release: str
    run_dir: str | None = None
    notes: tuple[str, ...] = field(default=())
    files: tuple[FileSpec, ...] = field(default=())  # root-owned config files besides pins/units (sshd deny)

    @property
    def ok(self) -> bool:
        return not self.refusals

    @property
    def complete(self) -> bool:
        """Everything the tmpfiles and pin files need is known (the person and the agent exist)."""
        return self.ok and not self.create_person

    def groups(self) -> list[str]:
        """The groups the plan creates (user groups), in order; existing ones are kept."""
        return [u.group for u in self.all_users() if u.action == "create"]

    def all_users(self) -> list[UserPlan]:
        return ([self.agent] if self.agent else []) + list(self.slots)

    def tmpfiles(self) -> str:
        """The ``/etc/tmpfiles.d/orch.conf`` text. Keyed by user name, so it needs no agent UID (13.1 naming key)."""
        if self.create_person or self.person_uid is None or self.agent is None:
            raise OsPlanRefusal("the person does not exist yet: plan again after it is created")
        a, p = self.agent.name, self.person
        lines = [
            "d /run/orch 0755 root root -",
            f"d /run/orch/{self.person_uid} 2750 {p} {a} -",
            f"d {AGENT_RUN_ROOT} 0755 root root -",
            f"d {AGENT_RUN_ROOT}/{a} 0750 {a} {a} -",
            f"a+ {AGENT_RUN_ROOT}/{a} - - - - u:{p}:rwx,d:u:{p}:rw",
        ]
        return "\n".join(lines) + "\n"

    def steps(self) -> list[str]:
        """Plain words for the setup page: what the one admin step will do (or why it cannot)."""
        if self.refusals:
            return [r.message for r in self.refusals]
        out: list[str] = []
        if self.create_person:
            out.append(f"Create a normal user '{NORMAL_PERSON}' to run orch (orch does not run as root).")
        if self.agent:
            verb = "Create" if self.agent.action == "create" else "Keep"
            out.append(f"{verb} the user '{self.agent.name}' that your agents run as. It cannot read your keys.")
        new = [s.name for s in self.slots if s.action == "create"]
        if new:
            out.append(f"Create {len(new)} restricted user(s) for add-ons ({new[0]} to {new[-1]}).")
        if self.units:
            out.append("Install the background services that run agents and add-ons as those users.")
        if self.files:
            out.append("Forbid logging in over SSH as those users.")
        if not out:
            out.append("Nothing to do: agents already run as their own user.")
        return out

    def admin_command(self) -> str:
        return f"sudo {self.release}/bin/orch-os-setup --person {self.person}"


def _expected_agent(person: str) -> UserPlan:
    name = AGENT_PREFIX + person
    return UserPlan(name, "agent", "create", None, f"{AGENT_HOME_ROOT}/{name}/home", AGENT_SHELL, name)


def _expected_slot(n: int) -> UserPlan:
    name = f"{SLOT_PREFIX}{n}"
    return UserPlan(name, "addon", "create", None, f"{SLOT_HOME_ROOT}/{name}", SLOT_SHELL, name)


def _check_existing(facts: OsFacts, want: UserPlan, person_uid: int | None, refusals: list[Refusal]) -> UserPlan:
    info = facts.user(want.name)
    if info is None:
        grp_ = facts.group(want.name)
        if grp_ is not None:
            refusals.append(
                Refusal(
                    "foreign_group",
                    want.name,
                    f"A group named '{want.name}' already exists and orch did not create it, so setup stops here.",
                )
            )
        return want
    bad = _wrong_properties(facts, want, info)
    pin = facts.pin(info.uid)
    pin_ok = isinstance(pin, dict) and pin.get("class") == want.role and pin.get("workspaces") == "*"
    pinned = pin_ok and person_uid is not None and pin.get("host_uid") == person_uid
    if pin_ok and want.role == "addon" and not pinned and person_uid is not None:
        refusals.append(
            Refusal(
                "slot_other_person",
                want.name,
                f"'{want.name}' was set up for another person on this machine (user id {pin.get('host_uid')}). "
                "Add-on users are shared per machine and can be set up for one person only.",
            )
        )
        return want
    if not pinned:
        refusals.append(
            Refusal(
                "foreign_user",
                want.name,
                f"A user named '{want.name}' already exists and orch did not create it. Setup will not use it.",
            )
        )
        return want
    # Exactly its own group: no root group, no person's group, nothing else (an allowlist, not a deny-list).
    if info.gids != {info.gid}:
        named = sorted(set(PRIVILEGED_GROUPS) & info.groups)
        what = ", ".join(named) if named else "other groups"
        refusals.append(
            Refusal(
                "privileged_group",
                want.name,
                f"'{want.name}' is in {what} besides its own group; that can give it near-root power. Remove it.",
            )
        )
    if bad:
        refusals.append(
            Refusal("wrong_properties", want.name, f"'{want.name}' exists but {bad}. Setup will not fix it.")
        )
    sudo = facts.sudo_allows(want.name)  # sudo's own answer (the plan runs as root); never our own parsing
    if sudo is not False:
        what = "has a sudo rule" if sudo else "could not be checked for sudo rules"
        refusals.append(Refusal("sudoers_entry" if sudo else "policy_unknown", want.name, f"'{want.name}' {what}."))
    return UserPlan(want.name, want.role, "keep", info.uid, want.home, want.shell, want.group)


# Root-written policy files whose stat signature the pin records (no content is ever parsed by the unprivileged host):
POLICY_FILES = ("/etc/sudoers", "/etc/sudoers.d", "/etc/doas.conf", "/etc/ssh/sshd_config", "/etc/ssh/sshd_config.d")
_POLICY_DIRS = ("/etc/sudoers.d", "/etc/ssh/sshd_config.d")
# Representative sshd connection contexts for ``sshd -T -C``; the user must be denied in every one.
# (host, addr, laddr); each is tried on every port sshd reports.
SSH_CONTEXTS = (
    ("localhost", "127.0.0.1", "127.0.0.1"),
    ("example.com", "203.0.113.9", "10.0.0.5"),
    ("host.internal", "10.0.0.5", "192.0.2.1"),
)


@dataclass(frozen=True)
class PolicyVerdict:
    """What root learned from the authoritative tools, to be recorded in the agent's root-owned pin (L2)."""

    sudo: str  # "none" | "allowed" | "unknown"
    ssh: str  # "denied" | "allowed" | "unknown"
    ssh_dropin: bool  # sshd is not installed, or orch's own drop-in is present with the expected content
    files: dict[str, list[int] | None]  # path -> [size, mtime_ns, ctime_ns, inode], or None while absent

    @property
    def ok(self) -> bool:
        return self.sudo == "none" and self.ssh == "denied" and self.ssh_dropin

    def document(self) -> dict[str, Any]:
        return {"sudo": self.sudo, "ssh": self.ssh, "ssh_dropin": self.ssh_dropin, "files": self.files}


def assess_policy(facts: OsFacts, user: str, *, dropin_text: str | None = None) -> PolicyVerdict:
    """Run as root (the setup apply step, after the user and the sshd drop-in exist): ask sudo and sshd themselves for
    ``user`` and take the stat signatures of the policy files. Never parses policy text."""
    sudo = facts.sudo_allows(user)
    ports = facts.sshd_ports()
    answers = (
        [None]
        if ports is None
        else [facts.sshd_denies(user, h, a, la, port) for h, a, la in SSH_CONTEXTS for port in (ports or [22])]
    )
    ssh = "unknown" if any(a is None for a in answers) else "denied" if all(answers) else "allowed"
    files: dict[str, list[int] | None] = {}
    for path in POLICY_FILES:
        sig = facts.file_sig(path)
        files[path] = None if sig is None else list(sig)
    for folder in _POLICY_DIRS:
        for entry in facts.listdir(folder):
            sig = facts.file_sig(f"{folder}/{entry}")
            files[f"{folder}/{entry}"] = None if sig is None else list(sig)
    return PolicyVerdict(
        "unknown" if sudo is None else "allowed" if sudo else "none",
        ssh,
        not facts.sshd_installed()
        or (facts.file_sig(SSHD_DROPIN) is not None and (dropin_text is None or dropin_text == SSHD_DROPIN_TEXT)),
        files,
    )


def _wrong_properties(facts: OsFacts, want: UserPlan, info: UserInfo) -> str:
    shells = (AGENT_SHELL,) if want.role == "agent" else SLOT_SHELLS
    own = facts.group(want.group)
    if info.uid == 0 or info.uid > _SYSTEM_UID_MAX:
        return "is not a system user"
    if info.home != want.home:
        return f"has the home directory {info.home} instead of {want.home}"
    if info.shell not in shells:
        return f"has the shell {info.shell} instead of {shells[0]}"
    if own is None or own.gid != info.gid:
        return "does not have its own group as primary group"
    return ""


def _bad_person(facts: OsFacts, info: UserInfo) -> str:
    """A person must be a normal login user: not root, a login-range UID, a real shell, its own home directory."""
    if info.uid == 0 or info.uid < _PERSON_MIN_UID:
        return "is not a normal login user (its user id is root's or a system one)"
    if info.shell in _NOLOGIN_SHELLS or not info.shell:
        return "cannot log in (no real shell)"
    home = facts.stat(info.home)
    if home is None or not home.is_dir or home.uid != info.uid:
        return "does not have its own home directory"
    return ""


def unit_runner(release: str) -> UnitFile:
    content = f"""[Unit]
Description=orch runner for %i
After=network.target

[Service]
User=%i
Delegate=yes
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={AGENT_HOME_ROOT}/%i {AGENT_RUN_ROOT}/%i
RestrictSUIDSGID=yes
MemoryMax=80%
TasksMax=4096
ExecStart={release}/current/bin/orch runner serve
Restart=always

[Install]
WantedBy=multi-user.target
"""
    return UnitFile("orch-runner@.service", "/etc/systemd/system/orch-runner@.service", content, ())


def unit_addon(release: str) -> UnitFile:
    content = f"""[Unit]
Description=orch add-on slot %i
After=network.target

[Service]
User=%i
Delegate=yes
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths={SLOT_HOME_ROOT}/%i
RestrictSUIDSGID=yes
MemoryMax=2G
TasksMax=512
ExecStart={release}/current/bin/orch addon-slot serve
Restart=always

[Install]
WantedBy=multi-user.target
"""
    return UnitFile("orch-addon@.service", "/etc/systemd/system/orch-addon@.service", content, ())


def plan(facts: OsFacts, person: str, person_uid: int, *, release: str = "/opt/orch/release") -> Plan:
    """Compute the setup plan for ``person`` (login name and UID). Pure: reads ``facts``, writes nothing."""
    if facts.platform != "linux":
        reason = {
            "darwin": "On a Mac, agents run as you for now; their own user comes in a later step.",
            "windows": "On Windows, agents run as you for now; their own account comes in a later step.",
        }.get(facts.platform, "This system is not supported for separate agent users yet.")
        return Plan(
            facts.platform,
            person,
            person_uid,
            False,
            None,
            (),
            (),
            (),
            (Refusal("unsupported_platform", person, reason),),
            release,
        )
    refusals: list[Refusal] = []
    create_person = person_uid == 0 or person == "root"
    if create_person:
        person, person_uid_known = NORMAL_PERSON, None
        existing = facts.user(NORMAL_PERSON)
        if existing is not None:
            bad = _bad_person(facts, existing)
            if bad:
                refusals.append(
                    Refusal("bad_person", NORMAL_PERSON, f"A user 'orch' exists but {bad}. Setup stops here.")
                )
            person_uid_known = existing.uid  # an earlier run created it: plan for it
            create_person = False
    else:
        person_uid_known = person_uid
    if not _NAME.match(person) or len(AGENT_PREFIX + person) > _MAX_NAME:
        refusals.append(Refusal("bad_name", person, f"The user name '{person}' cannot be used to name an agent user."))
    if person.startswith((AGENT_PREFIX, SLOT_PREFIX)):
        refusals.append(Refusal("bad_name", person, f"'{person}' is an orch service user, not a person."))
    if refusals:
        return Plan("linux", person, person_uid_known, False, None, (), (), (), tuple(refusals), release)
    agent = _check_existing(facts, _expected_agent(person), person_uid_known, refusals)
    slots = tuple(
        _check_existing(facts, _expected_slot(n), person_uid_known, refusals) for n in range(1, SLOT_COUNT + 1)
    )
    for u in [agent, *slots]:
        if u.uid is not None and u.uid == person_uid_known:
            refusals.append(Refusal("foreign_user", u.name, f"'{u.name}' has the same UID as you; setup stops here."))
    if agent.uid is not None:
        for u in slots:
            if u.uid == agent.uid:
                refusals.append(Refusal("foreign_user", u.name, f"'{u.name}' has the same UID as '{agent.name}'."))
    pins = tuple(PinEntry(u.name, u.role, person_uid_known, u.uid) for u in [agent, *slots])
    runner, addon = unit_runner(release), unit_addon(release)
    runner = UnitFile(runner.name, runner.path, runner.content, (agent.name,))
    addon = UnitFile(
        addon.name, addon.path, addon.content, tuple(f"{SLOT_PREFIX}{n}" for n in range(1, SLOT_COUNT + 1))
    )
    run_dir = None if person_uid_known is None else f"/run/orch/{person_uid_known}"
    notes = ("The login was root: orch will run as a new normal user 'orch'.",) if create_person else ()
    return Plan(
        "linux",
        person,
        person_uid_known,
        create_person,
        agent,
        slots,
        pins,
        (runner, addon),
        tuple(refusals),
        release,
        run_dir,
        notes,
        (FileSpec(SSHD_DROPIN, SSHD_DROPIN_TEXT, 0o644),),
    )
