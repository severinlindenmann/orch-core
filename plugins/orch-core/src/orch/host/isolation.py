"""Isolation detection (H1 L11, spec #1239 13.6): are agents running as their own OS user (tier B)? From OS facts only.

Never a flag, a setting or a file the person could write: the answer is read from the system on each call, in-process.
The click signer calls ``detect()`` itself and never takes the dashboard server's word (13.7). The caller may cache a
result for at most 60 s.

``status``:
- ``isolated``: every fact holds (Linux; agent user ``orch-agent-<person>`` whose gids are EXACTLY its own group's (an
  allowlist, not a deny-list); UID and name agree both ways; a root pin of class agent naming this host UID; the runner
  unit active, running as the agent's UID with the same exact gids, NoNewPrivs=1 and no capabilities; the run tree
  ``/run/orch/<person uid>`` walked to a fixed depth (setgid 2750 person:agent, only sockets <= 0660 and the dirs
  leading to them group-accessible, every other file 0600 and dir 0700, no ACL anywhere); no protected path
  (state, keys) readable by the agent (r or x on every real ancestor, ACLs evaluated); no root-equivalent socket
  (docker, podman, lxd, incus) connectable by the agent; no sudoers rule naming it (when readable); SSH logins
  denied for it).
- ``same_user``: no agent user, or a platform without the separation (macOS, Windows): tier A.
- ``broken``: the agent user exists but a fact fails. Each failed fact is a ``Reason`` of kind ``exposure`` (an agent
  could reach the key or act as the person: callers pause click signing) or ``availability`` (nothing exposed,
  something is down: One click stays on with a red line).
At least one protected path must exist (always include the ``<state>`` directory): none present is a failure, nothing
is proven. Fails closed.

The host runs unprivileged, so it normally cannot read /etc/sudoers: that is no fact here (the root-time setup plan
refuses a sudo rule); a sudoers file this process can read and that names the agent is an exposure fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Final, Literal

from ..setup.osfacts import OsFacts
from ..setup.osusers import AGENT_HOME_ROOT, AGENT_PREFIX, AGENT_SHELL, PRIVILEGED_GROUPS, sudoers_names

__all__ = ["EXPOSURE", "AVAILABILITY", "IsolationResult", "Reason", "detect"]

RUN_ROOT: Final = "/run/orch"
RUN_DIR_MODE: Final = 0o2750
MAX_DEPTH: Final = 4
MAX_ENTRIES: Final = 2000
# Root-equivalent sockets: whoever can connect can take the machine (container daemons). Checked when present.
ROOT_SOCKETS: Final = (
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/run/podman/podman.sock",
    "/var/lib/lxd/unix.socket",
    "/var/snap/lxd/common/lxd/unix.socket",
    "/run/lxd.socket",
    "/var/lib/incus/unix.socket",
    "/run/incus/unix.socket",
)
SSHD_CONFIG: Final = "/etc/ssh/sshd_config"
SSHD_DIR: Final = "/etc/ssh/sshd_config.d"
EXPOSURE: Final = "exposure"
AVAILABILITY: Final = "availability"
_SYSTEM_UID_MAX = 999


@dataclass(frozen=True)
class Reason:
    code: str
    message: str  # plain words, shown on the dashboard status line
    kind: Literal["exposure", "availability"] = EXPOSURE


@dataclass(frozen=True)
class IsolationResult:
    status: Literal["isolated", "same_user", "broken"]
    platform: str
    reasons: tuple[Reason, ...]  # the failed facts; empty when isolated

    @property
    def isolated(self) -> bool:
        return self.status == "isolated"

    @property
    def tier(self) -> str:
        return "B" if self.isolated else "A"

    @property
    def exposure(self) -> tuple[Reason, ...]:
        return tuple(r for r in self.reasons if r.kind == EXPOSURE)

    @property
    def availability(self) -> tuple[Reason, ...]:
        return tuple(r for r in self.reasons if r.kind == AVAILABILITY)

    @property
    def pause_click_signing(self) -> bool:
        """True on any exposure fact. Availability facts never pause signing."""
        return bool(self.exposure)

    @property
    def summary(self) -> str:
        if self.isolated:
            return "Agents run as their own user."
        return self.reasons[0].message if self.reasons else "Agents run as you."


def _result(platform: str, reasons: list[Reason]) -> IsolationResult:
    return IsolationResult("broken" if reasons else "isolated", platform, tuple(reasons))


def _same_user(platform: str, reason: Reason) -> IsolationResult:
    return IsolationResult("same_user", platform, (reason,))


def detect(
    facts: OsFacts, person: str, person_uid: int, protected_paths: list[str], *, run_root: str = RUN_ROOT
) -> IsolationResult:
    """``protected_paths``: the person's key and state files/directories the agent must not read."""
    if facts.platform != "linux":
        word = {"darwin": "a Mac", "windows": "Windows"}.get(facts.platform, "this system")
        return _same_user(
            facts.platform,
            Reason(
                "unsupported_platform", f"On {word} agents still run as you, so they can reach your keys.", EXPOSURE
            ),
        )
    name = AGENT_PREFIX + person
    agent = facts.user(name)
    if agent is None:
        return _same_user(
            "linux", Reason("no_agent_user", "Agents run as you: the separate agent user is not set up.", EXPOSURE)
        )
    reasons: list[Reason] = []
    if agent.uid == person_uid or agent.uid == 0:
        reasons.append(Reason("agent_is_person", f"'{name}' has your user id (or root's)."))
    if agent.uid > _SYSTEM_UID_MAX:
        reasons.append(Reason("agent_not_system", f"'{name}' is not a system user."))
    back = facts.user_by_uid(agent.uid)  # UID -> name must agree with the name -> UID we started from
    if back is None or back.name != name:
        reasons.append(Reason("name_uid_mismatch", f"The user id of '{name}' belongs to another name."))
    if agent.home != f"{AGENT_HOME_ROOT}/{name}/home" or agent.shell != AGENT_SHELL:
        reasons.append(Reason("agent_properties", f"'{name}' does not have the expected home or shell."))
    own = facts.group(name)
    if agent.gids != {agent.gid} or own is None or own.gid != agent.gid:
        named = sorted(set(PRIVILEGED_GROUPS) & agent.groups)
        what = ", ".join(named) if named else "other groups"
        reasons.append(Reason("agent_privileged", f"'{name}' is in {what} besides its own group."))
    pin = facts.pin(agent.uid)
    if not (isinstance(pin, dict) and pin.get("class") == "agent" and pin.get("host_uid") == person_uid):
        reasons.append(Reason("no_pin", f"'{name}' has no valid root-owned pin."))
    reasons += _run_tree(facts, f"{run_root}/{person_uid}", person_uid, agent.gid)
    existing = [path for path in protected_paths if facts.stat(path) is not None]
    if not existing:
        reasons.append(
            Reason("protected_missing", "None of the key or state paths to check exists, so nothing is proven.")
        )
    for path in existing:
        if facts.can_read(agent.uid, path):
            reasons.append(Reason("readable_by_agent", f"The agent user can read {path}."))
    for sock in ROOT_SOCKETS:
        if facts.stat(sock) is not None and facts.can_connect(agent.uid, sock):
            reasons.append(Reason("root_socket", f"The agent user can reach {sock}, which gives root power."))
    reasons += _sudo_and_ssh(facts, name)
    reasons += _runner(facts, name, agent.uid, agent.gid, person_uid)
    return _result("linux", reasons)


def _runner(facts: OsFacts, name: str, agent_uid: int, agent_gid: int, person_uid: int) -> list[Reason]:
    unit = facts.unit_state(f"orch-runner@{name}.service")
    if unit.state == "inactive":
        return [Reason("runner_down", "The agent service is not running.", AVAILABILITY)]
    if unit.state == "transitional":  # starting, stopping, restarting: an agent crash-looping its runner pauses nothing
        return [Reason("runner_restarting", "The agent service is restarting.", AVAILABILITY)]
    if unit.state != "active" or unit.uid is None:
        return [Reason("runner_unknown", "The agent service state could not be read, so it is not trusted.")]
    out: list[Reason] = []
    if unit.uid == person_uid:
        out.append(Reason("runner_is_person", "The agent service runs as you."))
    elif unit.uid != agent_uid:
        out.append(Reason("runner_wrong_user", "The agent service runs as the wrong user."))
    if unit.gids != {agent_gid}:
        out.append(Reason("runner_groups", "The agent service runs with groups beyond its own."))
    if unit.no_new_privs is not True or unit.cap_eff != 0 or unit.cap_amb != 0:
        out.append(Reason("runner_privileged", "The agent service can gain privileges."))
    return out


def _sudo_and_ssh(facts: OsFacts, name: str) -> list[Reason]:
    out: list[Reason] = []
    lines = facts.sudoers_lines()  # None: unreadable to this process (normal); the setup plan checks as root
    if lines and sudoers_names(lines, name):
        out.append(Reason("sudoers_entry", f"'{name}' has a sudo rule."))
    try:
        main = facts.read_text(SSHD_CONFIG)
        if main is not None:
            texts = [main]
            if "sshd_config.d" in main:
                for entry in sorted(facts.listdir(SSHD_DIR)):
                    if entry.endswith(".conf"):
                        text = facts.read_text(f"{SSHD_DIR}/{entry}")
                        if text is not None:
                            texts.append(text)
            if not _ssh_denied(texts, name):
                out.append(Reason("ssh_not_denied", f"SSH logins are not forbidden for '{name}'."))
    except OSError:
        out.append(Reason("ssh_unknown", "The SSH configuration could not be read, so it is not trusted."))
    return out


def _ssh_denied(texts: list[str], name: str) -> bool:
    for text in texts:
        for line in text.splitlines():
            parts = line.split("#", 1)[0].split()
            if parts and parts[0].lower() == "denyusers" and any(fnmatch(name, pat) for pat in parts[1:]):
                return True
    return False


def _run_tree(facts: OsFacts, root: str, person_uid: int, agent_gid: int) -> list[Reason]:
    """Walk ``root`` to MAX_DEPTH: owner/group/mode of every entry per 13.3, no symlink, no ACL, no oversize tree."""
    info = facts.stat(root)
    if info is None or not info.is_dir or info.is_symlink:
        return [Reason("run_dir_missing", "The run directory for agents is missing or not a directory.")]
    out: list[Reason] = []
    if info.uid != person_uid or info.gid != agent_gid or (info.mode & 0o7777) != RUN_DIR_MODE:
        out.append(Reason("run_dir_wrong", "The run directory has the wrong owner, group or mode."))
    if info.has_acl:
        out.append(Reason("run_tree_acl", "The run directory has an ACL."))
    budget = [MAX_ENTRIES]

    def walk(path: str, depth: int, inside_control: bool) -> bool:
        """Returns whether a group-shared socket lives at or under ``path``."""
        shared = False
        for entry in facts.listdir(path):
            budget[0] -= 1
            if budget[0] < 0:
                out.append(Reason("run_tree_too_big", "The run directory holds too many entries to check."))
                return shared
            full = f"{path}/{entry}"
            st = facts.stat(full)
            if st is None:
                continue
            if st.is_symlink:
                out.append(Reason("run_tree_symlink", f"{entry} in the run directory is a symbolic link."))
                continue
            if st.has_acl:
                out.append(Reason("run_tree_acl", f"{entry} in the run directory has an ACL."))
            perm = st.mode & 0o7777
            if st.is_socket:
                bad_owner = st.uid != person_uid
                if inside_control:  # control sockets are person-only
                    wrong = bad_owner or perm & 0o077
                else:
                    wrong = bad_owner or st.gid != agent_gid or perm & ~0o660
                    shared = shared or bool(perm & 0o070)
                if wrong:
                    out.append(Reason("socket_wrong", f"The socket {entry} has the wrong owner, group or mode."))
            elif st.is_dir:
                if depth + 1 >= MAX_DEPTH:
                    out.append(Reason("run_tree_too_deep", f"{entry} is nested too deep in the run directory."))
                    continue
                control = inside_control or (depth == 0 and entry == "control")
                below = walk(full, depth + 1, control)
                shared = shared or below
                if control:
                    if st.uid != person_uid or perm != 0o700:
                        code = "control_dir_wrong" if depth == 0 else "run_tree_wrong"
                        out.append(Reason(code, f"{entry} in the control area is open to more than you."))
                elif below:  # a directory leading to a shared socket: group may read and search, nothing more
                    if st.uid != person_uid or st.gid != agent_gid or perm & ~0o2750:
                        out.append(
                            Reason("run_tree_wrong", f"The directory {entry} has the wrong owner, group or mode.")
                        )
                elif st.uid != person_uid or perm != 0o700:
                    out.append(Reason("run_tree_wrong", f"The directory {entry} must be private (0700)."))
            else:  # pid files, launch codes, state: private
                if st.uid != person_uid or perm & ~0o600:
                    out.append(
                        Reason("run_tree_wrong", f"The file {entry} in the run directory must be private (0600).")
                    )
        return shared

    walk(root, 0, False)
    return out
