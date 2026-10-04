"""AI Factory phase 4 (#2, #31): the runner. One `tick` keeps agent sessions going for the children of the factory
epics the human started, and stops them when the factory must stop. It runs in the dashboard server the human started,
and refuses any process with an agent harness in its ancestry.

It never approves, grants, signs or starts anything by itself. It works only while the factory is switched on, the
epic's signed charter is a factory one and still active (not paused, the epic text unchanged, the time budget not
used up, the epic not done), the ledger is whole, and the human armed that delegation by starting it from the
dashboard (orch.core.factory_sessions). The limits are the signed charter's, plus a concurrency cap (3, which the
workspace config may only lower with `factory.max_concurrency`).

- Launch: one session per eligible child (auto-approved or covered by the human's charter, size within the limit,
  open, in progress or waiting), at most `max_children` children per delegation and a few launches per child, both
  counted in markers beside the ledger. The runner generates the session id, writes the binding the permission hook
  trusts, and only then starts the agent.
- Wake: a child whose session ended is started again when something it waits for changed (a grant, a denial, a
  revocation, its own text or status), never otherwise, within the launch cap.
- Stop: when the epic is paused, edited, done, approved again, out of time, the ledger is cut, the factory is off, or
  the child is done. A stopped session loses its binding at once, whether or not tmux obeys.

The launcher (list, start, stop) is a parameter, so tests never start a real agent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from pathlib import Path
from typing import Protocol

from orch.core import epics, factory_sessions as fs, ledger, permits, store
from orch.errors import OrchError

DEFAULT_CONCURRENCY = 3
RUNNABLE = ("open", "in-progress", "waiting")
# The only variables an agent session starts with, besides the fixed PATH below (no dashboard token, no key, nothing
# else the server holds). The config-dir variables are not secret: without them the agent's own orch and Claude Code
# would read other folders than the ones the runner checked.
ENV_ALLOW = ("HOME", "LANG", "LC_ALL", "TERM", "USER", "SHELL", "TMPDIR", "ORCH_STATE_DIR", "XDG_CONFIG_HOME",
             "CLAUDE_CONFIG_DIR")
SYSTEM_PATH = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
_HARNESS_FILES = (".claude/settings.json", ".claude/settings.local.json", ".mcp.json")
which = shutil.which  # tests stand in for it


class Launcher(Protocol):
    def alive(self) -> set[str] | None:
        """The names of the live sessions, or None when tmux did not answer (then nothing is concluded)."""
    def start(self, name: str, cwd: str, argv: list[str]) -> int:
        """Start the session; returns the pid of its first process (an ancestor of everything the agent runs)."""
    def stop(self, name: str) -> None: ...


def resolve_bin(name: str) -> str | None:
    """The absolute path of program `name`, looked up only in the absolute entries of this server's PATH, and only when
    the file (after links) is a regular file owned by this user or root that no group or other can write. None
    otherwise: the runner then does not start anything."""
    if not isinstance(name, str) or not name:
        return None
    if "/" in name:
        found = name if os.path.isabs(name) else None
    else:
        entries = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and os.path.isabs(d)]
        found = which(name, path=os.pathsep.join(entries)) if entries else None
    if not found:
        return None
    try:
        st = os.stat(os.path.realpath(found))
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_uid not in (os.getuid(), 0) or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return None
    return str(found)


def child_path(*bins: str) -> str:
    """The PATH an agent session gets: the folders of the resolved programs, then the system's."""
    dirs = []
    for d in [*(os.path.dirname(b) for b in bins), *SYSTEM_PATH]:
        if d and os.path.isabs(d) and d not in dirs:
            dirs.append(d)
    return os.pathsep.join(dirs)


def env_prefix(env_bin: str, bins: list[str], environ=None) -> list[str]:
    """`env -i` (the resolved program) plus the fixed PATH and the allowlisted variables, as an argv prefix: the agent
    starts with nothing else."""
    environ = os.environ if environ is None else environ
    pairs = [f"{k}={environ[k]}" for k in ENV_ALLOW if isinstance(environ.get(k), str) and environ[k]
             and "\n" not in environ[k] and "\x00" not in environ[k]]
    return [env_bin, "-i", f"PATH={child_path(*bins)}", *pairs]


def user_settings_blocker(environ=None) -> str | None:
    """Why a session started without project settings would run without orch's guard and permission hook, or None.
    The launched session reads only the user-scope settings of CLAUDE_CONFIG_DIR (else ~/.claude): they must enable
    orch-core, or carry both the guard (PreToolUse) and the permission (PermissionRequest) hooks."""
    from orch.core.fsutil import read_regular_file
    from orch.onboarding import _enabled_plugin_id
    environ = os.environ if environ is None else environ
    base = environ.get("CLAUDE_CONFIG_DIR")
    path = (Path(base) if base else Path.home() / ".claude") / "settings.json"
    why = (f"the user-scope Claude settings ({path}) neither enable orch-core nor carry the orch guard and permission "
           "hooks: a session that ignores project settings would run without them")
    raw = read_regular_file(path, 1 << 20)
    if raw is None:
        return why
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return why
    if not isinstance(data, dict) or data.get("disableAllHooks") is True:
        return why
    if _enabled_plugin_id(path):
        return None
    hooks = data.get("hooks")

    def has(event: str, *words: str) -> bool:
        for entry in (hooks.get(event) or []) if isinstance(hooks, dict) else []:
            for h in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
                cmd = h.get("command") if isinstance(h, dict) else None
                if _orch_words(cmd)[:len(words)] == list(words):
                    return True
        return False

    return None if has("PreToolUse", "guard") and has("PermissionRequest", "permit", "hook") else why


def _orch_words(cmd) -> list[str]:
    """The words after the program of hook command `cmd`, when that program is `orch` or an absolute path ending in
    /orch; else an empty list. Parsed as a shell would split it, not matched as text."""
    import shlex
    try:
        words = shlex.split(cmd) if isinstance(cmd, str) else []
    except ValueError:
        return []
    if not words or not (words[0] == "orch" or (os.path.isabs(words[0]) and os.path.basename(words[0]) == "orch")):
        return []
    return words[1:]


def work_prompt(key: str) -> str | None:
    """The built-in work prompt for `key`. Never the workspace config's prompt or any ticket text: an agent can edit
    those, and this text starts another agent."""
    from orch.config.load import DEFAULTS
    from orch.dashboard.data.agent_start import KEY_RE
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        return None
    return DEFAULTS["agents"]["prompts"]["work"].replace("{key}", key)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.is_file() and b.is_file() and not a.is_symlink() and not b.is_symlink() \
            and a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _branch_of(p: Path) -> str | None:
    """The branch checked out in git worktree `p` (read from its git files, no subprocess), or None."""
    from orch.core.fsutil import read_regular_file
    link = read_regular_file(p / ".git", 4096)
    if link is None or not link.startswith(b"gitdir:"):
        return None
    gitdir = (p / link[7:].decode("utf-8", "replace").strip()).resolve()
    head = read_regular_file(gitdir / "HEAD", 4096)
    if head is None or not head.startswith(b"ref: refs/heads/"):
        return None
    return head[16:].decode("utf-8", "replace").strip()


def start_dir(ws, t) -> str | None:
    """Where the child's session starts. The child's own worktree when it names exactly one that lies below the
    workspace's `.claude/worktrees` folder, or is a git worktree inside the workspace whose branch names the child; else
    the workspace root (the field is agent-written). None (refuse to launch) when that worktree carries a harness
    settings or MCP file that is not identical to the workspace's own."""
    root = Path(ws.root).resolve()
    wts = t.meta.get("worktrees")
    vals = list(wts.values()) if isinstance(wts, dict) else []
    if len(vals) == 1 and isinstance(vals[0], str) and vals[0] and "\x00" not in vals[0]:
        try:
            p = (root / vals[0]).resolve()
            if p != root and root in p.parents and p.is_dir():
                branch = _branch_of(p)
                named = bool(branch) and re.search(rf"(?<![a-z0-9]){re.escape(t.id.lower())}(?![a-z0-9])", branch.lower())
                if (root / ".claude" / "worktrees").resolve() in p.parents or named:
                    if any(os.path.lexists(p / f) and not _same_file(p / f, root / f) for f in _HARNESS_FILES):
                        return None
                    return str(p)
        except (OSError, RuntimeError, ValueError):
            pass
    return str(root)


def _gate(ws, epic_id: str, did: str) -> bool:
    """Everything a launch needs, read fresh right before it: the factory on, the ledger whole, the signed charter live
    and current, not paused, not edited, not out of time, the epic not done, and the human's Start."""
    try:
        if not permits.enabled(ws) or not ledger.head_ok():
            return False
        epic = _ticket(ws, epic_id)
        if epic is None or epic.status == "done":
            return False
        d = permits.factory_delegation(ws, epic, ledger.entries(ws))
        return d is not None and d["id"] == did and d["active"] and fs.armed(ws, did)
    except Exception:
        return False


def concurrency(ws) -> int:
    """The cap: 3, or lower when the workspace config says so (a config value can only restrict)."""
    v = (ws.config.get("factory") or {}).get("max_concurrency")
    ok = isinstance(v, int) and not isinstance(v, bool) and v >= 0
    return min(DEFAULT_CONCURRENCY, v) if ok else DEFAULT_CONCURRENCY


def wake_token(epic, child, signed) -> str:
    """What a parked child waits for: the human's answers in this epic (grants, denials, revocations) and what its
    approvals bind (its text and gates), and in a Dark epic every change to the workspace's Dark profile. Not its
    status: the agent moves that itself."""
    mine = [e for e in signed if e.get("ticket") == epic.id]
    gates = {k: bool((v or {}).get("approved")) for k, v in sorted((child.meta.get("gates") or {}).items())}
    body = [epics.child_hashes(child), gates, sorted(str(e.get("grant")) for e in mine if e.get("kind") == "grant"),
            sum(1 for e in mine if e.get("kind") == "permit_deny"),
            sum(1 for e in mine if e.get("kind") == "permit_revoke")]
    charter = next((e for e in reversed(mine) if e.get("kind") == "charter"), None)
    if charter and isinstance(charter.get("delegate"), dict) and charter["delegate"].get("dark"):  # only Dark epics: other tokens stay as they were
        body.append([e.get("mac") for e in signed if e.get("kind") == "dark_profile"])
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _ticket(ws, ref):
    try:
        return store.read_ticket(store.resolve(ws, ref).path)
    except Exception:
        return None


def stop_reason(ws, b: dict, signed, cut: bool, blocker: str | None = None) -> str | None:
    """Why the session bound by `b` must stop now, or None."""
    if blocker:
        return blocker
    if not permits.enabled(ws):
        return "the factory is switched off"
    if cut:
        return "the approval ledger on this machine was cut"
    epic = _ticket(ws, b["epic"])
    if epic is None:
        return "its epic is gone"
    if epic.status == "done":
        return "the epic is done"
    d = permits.factory_delegation(ws, epic, signed)
    if d is None or d["id"] != b["delegation"]:
        return "the epic was approved again or is no longer a factory epic"
    if not fs.armed(ws, d["id"]):
        return "the human has not started it from the dashboard"
    if d["paused"]:
        return "the delegation is paused"
    if d["epic_changed"]:
        return "the epic text changed"
    if d["expired"]:
        return "the time budget is used up"
    child = _ticket(ws, b["child"])
    if child is None:
        return "its child is gone"
    if child.status == "done":
        return "the child is done"
    return None


def _launchable(ws, epic, d, t, signed) -> bool:
    try:
        permits.require_budget(ws, t)  # the same budget refusal an agent's claim meets
    except OrchError:
        return False
    return (t.status in RUNNABLE and not epics.is_epic(t) and epics.within_limits(t, d) is None
            and not epics.hidden_in(t) and epics.child_state(ws, epic, t, signed) in ("delegated", "covered"))


def _launch(ws, actor, launcher, settings, epic, d, t, token, lines) -> dict | None:
    """Start one session. The command is the user's launch setting with the generated id and the built-in prompt put
    in as whole argv elements (never a shell, never ticket text), the program resolved to a trusted absolute path, under
    `env -i` with a fixed PATH and the allowlisted variables."""
    prompt = work_prompt(t.id)
    cwd = start_dir(ws, t)
    if cwd is None:
        lines.append(f"{t.id} not started: its worktree carries harness settings the workspace does not")
        return None
    command = settings["factory_command"]
    claude, env_bin = resolve_bin(command[0]), resolve_bin("env")
    if claude is None or env_bin is None:
        lines.append(f"{t.id} not started: claude or env was not found at a trusted path (owned by you or root, not "
                     "writable by others)")
        return None
    if prompt is None or not _gate(ws, epic.id, d["id"]):
        return None
    sid = fs.new_session_id()
    name = f"fx-{t.id}-{secrets.token_hex(3)}"  # unrelated to the session id
    argv = [*env_prefix(env_bin, [claude]), claude,
            *(a.replace("{session}", sid).replace("{prompt}", prompt) for a in command[1:])]
    b = fs.bind(ws, actor, session=sid, epic=epic.id, delegation=d["id"], child=t.id, name=name, wake=token)
    try:
        pid = launcher.start(name, cwd, argv)
        fs.set_pid(ws, actor, sid, pid)
    except (OrchError, OSError, ValueError) as e:
        try:
            launcher.stop(name)
        except (OrchError, OSError):
            pass
        fs.end(ws, sid)
        lines.append(f"could not start {name}: {e}")
        return None
    lines.append(f"started {name}")
    return b


def sweep(ws, actor, launcher: Launcher, *, stop_all: bool = False) -> list[str]:
    """End the bindings of sessions that are not alive (the dashboard just started), or, with `stop_all`, stop every
    session and end every binding (the dashboard is shutting down; each child may start again next time)."""
    fs.human_check(actor, "running the AI Factory")
    live = fs.bindings(ws)
    if not live:
        return []
    names = launcher.alive()
    if names is None and not stop_all:
        return []
    names = names or set()
    lines = []
    for b in live:
        if not stop_all and b["name"] in names:
            continue
        if b["name"] in names:
            try:
                launcher.stop(b["name"])
            except (OrchError, OSError):
                pass
        fs.end(ws, b["session"], wake="" if stop_all else None)
        lines.append(f"{b['name']}: " + ("the dashboard stopped" if stop_all else "its session is not running"))
    return lines


def tick(ws, actor, launcher: Launcher, *, settings: dict) -> list[str]:
    """One round; returns what it did, one line each. `settings`: orch.dashboard.launch.load_settings()."""
    fs.human_check(actor, "running the AI Factory")
    lines: list[str] = []
    on = permits.enabled(ws)
    live = fs.bindings(ws)
    if not on and not live:
        return lines
    cut = not ledger.head_ok()
    signed = [] if cut else ledger.entries(ws)
    names = launcher.alive()
    if names is None:
        return lines  # tmux did not answer: conclude nothing (no binding ends, nothing starts), try again next round
    blocker = user_settings_blocker()
    keep = []
    for b in live:
        why = None if b["name"] in names else "its session ended"
        if why is None:
            why = stop_reason(ws, b, signed, cut, blocker)
        if why is None:
            keep.append(b)
            continue
        if b["name"] in names:
            try:
                launcher.stop(b["name"])
            except (OrchError, OSError):
                pass  # the binding ends anyway: what keeps running is an ordinary session the harness asks about
        fs.end(ws, b["session"])
        lines.append(f"{b['name']}: {why}")
    if not on or cut or blocker:
        if on and not cut and blocker:
            lines.append(f"not starting anything: {blocker}")
        return lines
    cap = concurrency(ws)
    gone = fs.ended(ws)
    for entry in store.scan(ws):
        if entry.meta is None or not epics.is_epic(entry.meta) or entry.status == "done":
            continue
        epic = _ticket(ws, entry.id)
        d = permits.factory_delegation(ws, epic, signed) if epic is not None else None
        if d is None or not d["active"] or not fs.armed(ws, d["id"]):
            continue
        for ce in epics.children(ws, epic.id):
            if len(keep) >= cap:
                return lines
            t = _ticket(ws, ce.id)
            if t is None or not _launchable(ws, epic, d, t, signed):
                continue
            if any(b["child"] == t.id for b in keep):
                continue
            token = wake_token(epic, t, signed)
            if any(g["child"] == t.id and g["delegation"] == d["id"] and g["wake"] == token for g in gone):
                continue  # parked: nothing it waits for changed since it last started
            with epics.delegation_lock(d["id"]):
                if fs.runs(ws, d["id"], t.id) == 0 and fs.runs(ws, d["id"]) >= d["max_children"]:
                    continue
                if not fs.mark_run(ws, d["id"], t.id):
                    continue
            b = _launch(ws, actor, launcher, settings, epic, d, t, token, lines)
            if b is not None:
                keep.append(b)
    return lines
