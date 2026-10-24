"""Isolation detection (H1 L11, spec #1239 13.6): are agents running as their own OS user (tier B)? From OS facts only.

Never a flag, a setting or a file the person could write: the answer is read from the system on each call, in-process.
The click signer calls ``detect()`` itself and never takes the dashboard server's word (13.7). The caller may cache a
result for at most 60 s.

``status``:
- ``isolated``: every fact holds (Linux; agent user ``orch-agent-<person>`` in no privileged group, UID and name agree
  both ways; a root pin of class agent naming this host UID; the runner unit active and running as the agent's UID;
  ``/run/orch/<person uid>`` setgid 2750 person:agent with only person-owned 0660-or-tighter sockets, no "other" bits;
  the control directory inside it 0700; no protected path (state, keys) readable by the agent, ACLs included).
- ``same_user``: no agent user, or a platform without the separation (macOS, Windows): tier A.
- ``broken``: the agent user exists but a fact fails. Each failed fact is a ``Reason`` of kind ``exposure`` (an agent
  could reach the key or act as the person: callers pause click signing) or ``availability`` (nothing exposed,
  something is down: One click stays on with a red line).
An empty ``protected_paths`` list is a failure (nothing proven). Fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from ..setup.osfacts import OsFacts
from ..setup.osusers import AGENT_PREFIX, AGENT_SHELL, PRIVILEGED_GROUPS

__all__ = ["EXPOSURE", "AVAILABILITY", "IsolationResult", "Reason", "detect"]

RUN_ROOT: Final = "/run/orch"
RUN_DIR_MODE: Final = 0o2750
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
    if agent.home != f"/var/lib/orch/agents/{person}/home" or agent.shell != AGENT_SHELL:
        reasons.append(Reason("agent_properties", f"'{name}' does not have the expected home or shell."))
    priv = sorted(set(PRIVILEGED_GROUPS) & agent.groups)
    if priv:
        reasons.append(Reason("agent_privileged", f"'{name}' is in {', '.join(priv)}, which give near-root power."))
    pin = facts.pin(agent.uid)
    if not (isinstance(pin, dict) and pin.get("class") == "agent" and pin.get("host_uid") == person_uid):
        reasons.append(Reason("no_pin", f"'{name}' has no valid root-owned pin."))
    reasons += _run_dir(facts, f"{run_root}/{person_uid}", person_uid, agent.gid)
    if not protected_paths:
        reasons.append(Reason("nothing_checked", "No key or state path was given to check, so nothing is proven."))
    for path in protected_paths:
        if facts.stat(path) is None:
            continue  # absent means nothing to read (a key not created yet)
        if facts.can_read(agent.uid, path):
            reasons.append(Reason("readable_by_agent", f"The agent user can read {path}."))
    unit = facts.unit_state(f"orch-runner@{name}.service")
    if unit.state == "inactive":
        reasons.append(Reason("runner_down", "The agent service is not running.", AVAILABILITY))
    elif unit.state != "active" or unit.uid is None:
        reasons.append(Reason("runner_unknown", "The agent service state could not be read, so it is not trusted."))
    elif unit.uid == person_uid:
        reasons.append(Reason("runner_is_person", "The agent service runs as you."))
    elif unit.uid != agent.uid:
        reasons.append(Reason("runner_wrong_user", "The agent service runs as the wrong user."))
    return _result("linux", reasons)


def _run_dir(facts: OsFacts, path: str, person_uid: int, agent_gid: int) -> list[Reason]:
    info = facts.stat(path)
    if info is None or not info.is_dir or info.is_symlink:
        return [Reason("run_dir_missing", "The run directory for agents is missing or not a directory.")]
    out: list[Reason] = []
    if info.uid != person_uid or info.gid != agent_gid or (info.mode & 0o7777) != RUN_DIR_MODE:
        out.append(Reason("run_dir_wrong", "The run directory has the wrong owner, group or mode."))
    ctl = facts.stat(f"{path}/control")
    if ctl is not None and (not ctl.is_dir or ctl.uid != person_uid or (ctl.mode & 0o7777) != 0o700):
        out.append(Reason("control_dir_wrong", "The control directory is open to more than you."))
    for entry in facts.listdir(path):
        st = facts.stat(f"{path}/{entry}")
        if st is None or not st.is_socket:
            continue
        if st.uid != person_uid or st.gid != agent_gid or st.mode & 0o007:
            out.append(Reason("socket_wrong", f"The socket {entry} has the wrong owner, group or mode."))
    return out
