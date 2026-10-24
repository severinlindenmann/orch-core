"""Isolation detection (H1 L11): is this host running agents as their own OS user (tier B)? From OS facts only.

Never a flag or a setting: the answer is read from the system every time. Tier B needs all of
1. Linux (macOS and Windows answer "not isolated" with a plain reason until their steps exist);
2. the agent user ``orch-agent-<person>`` exists, is a system user, is not the person and not root, has the expected
   home and shell, and is in no privileged group;
3. a root-owned agent pin for its UID that names this person as host;
4. the run directory ``/run/orch/<person uid>`` is a real directory owned by the person, group = the agent's group,
   mode 2750, and every socket in it is owned by the person, group = the agent's group, with no "other" access;
5. none of the person's protected paths (keys, state) is readable by the agent user. An empty list of paths is a
   failure: nothing was proven.
The check fails closed: anything missing or odd is a reason, and the result is "not isolated".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..setup.osfacts import OsFacts
from ..setup.osusers import AGENT_PREFIX, AGENT_SHELL, PRIVILEGED_GROUPS

__all__ = ["IsolationResult", "Reason", "detect"]

RUN_ROOT: Final = "/run/orch"
RUN_DIR_MODE: Final = 0o2750
_SYSTEM_UID_MAX = 999


@dataclass(frozen=True)
class Reason:
    code: str
    message: str  # plain words, shown on the dashboard status line


@dataclass(frozen=True)
class IsolationResult:
    isolated: bool
    tier: str  # "B" when isolated, "A" otherwise
    platform: str
    reasons: tuple[Reason, ...]  # empty when isolated; otherwise why not

    @property
    def summary(self) -> str:
        return (
            "Agents run as their own user."
            if self.isolated
            else (self.reasons[0].message if self.reasons else "Agents run as you.")
        )


def _result(platform: str, reasons: list[Reason]) -> IsolationResult:
    ok = not reasons
    return IsolationResult(ok, "B" if ok else "A", platform, tuple(reasons))


def detect(
    facts: OsFacts, person: str, person_uid: int, protected_paths: list[str], *, run_root: str = RUN_ROOT
) -> IsolationResult:
    """``protected_paths``: the person's key and state files/directories the agent must not read."""
    if facts.platform != "linux":
        word = {"darwin": "a Mac", "windows": "Windows"}.get(facts.platform, "this system")
        return _result(
            facts.platform,
            [Reason("unsupported_platform", f"On {word} agents still run as you, so they can reach your keys.")],
        )
    reasons: list[Reason] = []
    name = AGENT_PREFIX + person
    agent = facts.user(name)
    if agent is None:
        return _result("linux", [Reason("no_agent_user", "Agents run as you: the separate agent user is not set up.")])
    if agent.uid == person_uid or agent.uid == 0:
        reasons.append(Reason("agent_is_person", f"'{name}' has your user id (or root's)."))
    if agent.uid > _SYSTEM_UID_MAX:
        reasons.append(Reason("agent_not_system", f"'{name}' is not a system user."))
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
    return _result("linux", reasons)


def _run_dir(facts: OsFacts, path: str, person_uid: int, agent_gid: int) -> list[Reason]:
    info = facts.stat(path)
    if info is None or not info.is_dir or info.is_symlink:
        return [Reason("run_dir_missing", "The run directory for agents is missing or not a directory.")]
    out: list[Reason] = []
    if info.uid != person_uid or info.gid != agent_gid or (info.mode & 0o7777) != RUN_DIR_MODE:
        out.append(Reason("run_dir_wrong", "The run directory has the wrong owner, group or mode."))
    for entry in facts.listdir(path):
        st = facts.stat(f"{path}/{entry}")
        if st is None or not st.is_socket:
            continue
        if st.uid != person_uid or st.gid != agent_gid or st.mode & 0o007:
            out.append(Reason("socket_wrong", f"The socket {entry} has the wrong owner, group or mode."))
    return out
