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
- Plan: an epic with no child at all gets one planner session instead (its binding's `child` is the epic itself), which
  splits it into children, refines them and auto-approves them; at most PLANNER_LAUNCHES per delegation, outside the
  child count. Once a child exists the runner never starts a planner again; one running is stopped once every child
  is approved, or PLANNER_MINUTES after its start (planner_stop).
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
PLANNER_MINUTES = 30  # a planner session is stopped this long after its start
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


EDIT_MODES = ("acceptEdits", "auto", "bypassPermissions")  # permission modes that let file edits through


def edits_blocked(environ=None) -> bool:
    """Whether the user-scope Claude settings (the file user_settings_blocker reads: CLAUDE_CONFIG_DIR, else
    ~/.claude, settings.json) leave file edits to a prompt: permissions.defaultMode is not one of EDIT_MODES. A
    runner session's file-edit prompt is denied without a card (only shell commands are answered), so then its agent
    cannot write a file, and the planner cannot write the files its children's text comes from. Read only."""
    from orch.core.fsutil import read_regular_file
    environ = os.environ if environ is None else environ
    base = environ.get("CLAUDE_CONFIG_DIR")
    raw = read_regular_file((Path(base) if base else Path.home() / ".claude") / "settings.json", 1 << 20)
    try:
        data = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (ValueError, UnicodeDecodeError):
        data = None
    perms = data.get("permissions") if isinstance(data, dict) else None
    return not (isinstance(perms, dict) and perms.get("defaultMode") in EDIT_MODES)


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


# What every runner session is told about command shapes (the live run of 5 Oct: chains, pipes and redirects each
# became a card, and agents filed requests for commands that were never denied). Built in, never from config.
_PLAIN = (
    "Run exactly one plain command per tool call: no `&&`, `;`, `|`, `2>&1`, `|| true`, other redirects or command "
    "substitution, because a Dark run stops any such command for the human. Keep titles, -m texts and commit "
    "messages to short plain sentences without line breaks, backticks, dollar signs or backslashes. File "
    "`orch permit request` only for a command that was actually denied with a request id P-n in the denial message, "
    "never for one that was not denied, and never retry variants of a denied command. Never run "
    "`orch instructions sync` or `orch setup`. "
)

# The planner's prompt: built in, like the work prompt, never from the config, a ticket or anything an agent edits. It
# names only commands and options orch has (tests/test_factory_planner.py checks them against the CLI).
PLANNER_PROMPT = (
    "You are the planner of the AI Factory epic {key}. Read it with `orch show {key}` and its limits with "
    "`orch epic show {key}`. " + _PLAIN + "Split the work into children within those limits, each created with one "
    "`orch new --epic {key} --title \"...\" --size SIZE --requirements-file FILE --acceptance-file FILE` (SIZE is xs, "
    "s or m unless the limits say otherwise), the Requirements and Acceptance criteria written into files under "
    "orchestrator/temporary first. When a child's size needs a Plan (every size but xs), write it as one paragraph "
    "with `orch section set CHILD Plan -m \"...\"`. "
    "`orch ask` is refused in this epic: decide within the epic's text and record why with `orch log CHILD -m "
    "\"...\"`, or leave the item out. Then approve each child with `orch epic auto-approve CHILD`. Do not build "
    "anything and do not change the epic's own text. When every child is refined and approved, stop."
)

# A child's prompt in a factory epic, used instead of the workspace's default work prompt (the agent may not have the
# orch skills at user scope: the live run's agent then invented `orch work-on`), so it carries the command forms.
FACTORY_WORK_PROMPT = (
    "You work on {key}, a child of an AI Factory epic. Follow the orch-work-on-ticket skill if you have it; these "
    "rules come first. " + _PLAIN + "Start with `orch claim {key}` and read it with `orch show {key}`. Add each task "
    "with `orch task add {key} \"TASK\"`, then for each one run `orch task start {key} TN`, do the work and run "
    "`orch task done {key} TN` with no -m; put notes in `orch log {key} -m \"...\"`. `orch ask` is refused in this "
    "epic: decide within the ticket's text and record why with `orch log`. Commit your work on your own branch or "
    "worktree with `git add FILES` and `git commit -m \"{key} short text\"`. Write one Verification line per "
    "acceptance criterion into a file under orchestrator/temporary and set it with `orch section set {key} "
    "Verification --file FILE`. When every task is done, run `orch move {key} testing` and stop. If a command was "
    "denied with a request id, do other work or wait for the human with `orch wait {key}`."
)


def factory_work_prompt(key: str) -> str | None:
    """The built-in prompt of a child's session (its key validated as a ticket key)."""
    from orch.dashboard.data.agent_start import KEY_RE
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        return None
    return FACTORY_WORK_PROMPT.replace("{key}", key)


def planner_prompt(key: str) -> str | None:
    """The built-in planner prompt for epic `key` (its key validated as a ticket key)."""
    from orch.dashboard.data.agent_start import KEY_RE
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        return None
    return PLANNER_PROMPT.replace("{key}", key)


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


def wake_token(epic, child, signed, dark_marks: list | None = None) -> str:
    """What a parked child waits for: the human's answers in this epic (grants, denials, revocations) and what its
    approvals bind (its text and gates), and in a Dark epic every change to this checkout's Dark profile (`dark_marks`:
    the MACs of its signed entries, a history, so removing a rule wakes too). The marks count whether the Dark switch
    is on or off, so flipping the switch alone wakes nothing (no relaunch storm); a profile change wakes a parked
    Dark child either way, which grants nothing by itself. Not its status: the agent moves that itself."""
    mine = [e for e in signed if e.get("ticket") == epic.id]
    gates = {k: bool((v or {}).get("approved")) for k, v in sorted((child.meta.get("gates") or {}).items())}
    body = [epics.child_hashes(child), gates, sorted(str(e.get("grant")) for e in mine if e.get("kind") == "grant"),
            sum(1 for e in mine if e.get("kind") == "permit_deny"),
            sum(1 for e in mine if e.get("kind") == "permit_revoke")]
    charter = next((e for e in reversed(mine) if e.get("kind") == "charter"), None)
    if (dark_marks is not None and charter and isinstance(charter.get("delegate"), dict)
            and charter["delegate"].get("dark")):  # only Dark epics: other tokens stay as they were
        body.append(list(dark_marks))
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


def _ready(ws, settings, epic, d, t, lines, planner: bool = False) -> tuple | None:
    """Everything a launch needs, checked before a launch is counted (a missing program or a refused worktree must not
    use up a child's or the planner's launches): (prompt, cwd, claude, env), or None. `planner`: `t` is the epic
    itself, the planner's prompt is used and the session starts in the workspace root."""
    prompt = planner_prompt(t.id) if planner else factory_work_prompt(t.id)
    cwd = str(Path(ws.root).resolve()) if planner else start_dir(ws, t)
    if cwd is None:
        lines.append(f"{t.id} not started: its worktree carries harness settings the workspace does not")
        return None
    claude, env_bin = resolve_bin(settings["factory_command"][0]), resolve_bin("env")
    if claude is None or env_bin is None:
        lines.append(f"{t.id} not started: claude or env was not found at a trusted path (owned by you or root, not "
                     "writable by others)")
        return None
    if prompt is None or not _gate(ws, epic.id, d["id"]):
        return None
    return prompt, cwd, claude, env_bin


def _start(ws, actor, launcher, settings, epic, d, t, token, lines, ready: tuple) -> dict | None:
    """Start one session (`ready`: what _ready returned). The command is the user's launch setting with the generated
    id and the built-in prompt put in as whole argv elements (never a shell, never ticket text), the program resolved
    to a trusted absolute path, under `env -i` with a fixed PATH and the allowlisted variables."""
    prompt, cwd, claude, env_bin = ready
    if not _gate(ws, epic.id, d["id"]):  # once more, right before the start
        return None
    command = settings["factory_command"]
    sid = fs.new_session_id()
    name = f"fx-{t.id}-{secrets.token_hex(3)}"  # unrelated to the session id
    argv = [*env_prefix(env_bin, [claude]), claude,
            *(a.replace("{session}", sid).replace("{prompt}", prompt) for a in command[1:])]
    b = fs.bind(ws, actor, session=sid, epic=epic.id, delegation=d["id"], child=t.id, name=name, wake=token,
                start=str(cwd))
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


_APPROVED = ("covered", "delegated", "approved", "done")  # epics.child_state of a child that needs no planner


def planner_stop(ws, b: dict, signed) -> str | None:
    """Why a planner session must stop now although its epic goes on, or None: every child of the epic is approved
    (its work is done), or PLANNER_MINUTES have passed since it was bound (children or not: an interactive session
    does not end by itself, and it holds a concurrency slot)."""
    from orch import clock
    epic = _ticket(ws, b["epic"])
    kids = [_ticket(ws, e.id) for e in epics.children(ws, b["epic"])]
    if epic is not None and kids and all(
            k is not None and epics.child_state(ws, epic, k, signed) in _APPROVED for k in kids):
        return "every child of the epic is approved: the planner is done"
    try:
        late = (clock.now() - clock.parse_stamp(b["at"])).total_seconds() >= PLANNER_MINUTES * 60
    except (ValueError, TypeError):
        late = True  # no readable start: fail closed
    return f"the planner's time limit of {PLANNER_MINUTES} minutes is used up" if late else None


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
        if stop_all and fs.is_planner(b):
            fs.unmark_planner_run(ws, b["delegation"])  # the dashboard stopping is not one of its two launches
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
        if why is None and fs.is_planner(b):
            why = planner_stop(ws, b, signed)
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
    cid = ledger.checkout_id(ws)
    dark_marks = [e.get("mac") for e in signed if e.get("kind") == "dark_profile" and e.get("checkout") == cid]
    for entry in store.scan(ws):
        if entry.meta is None or not epics.is_epic(entry.meta) or entry.status == "done":
            continue
        epic = _ticket(ws, entry.id)
        d = permits.factory_delegation(ws, epic, signed) if epic is not None else None
        if d is None or not d["active"] or not fs.armed(ws, d["id"]):
            continue
        kids = epics.children(ws, epic.id)
        if not kids:  # nothing splits the epic yet: one planner session does, within its own launch cap
            if len(keep) >= cap:
                return lines
            if any(fs.is_planner(b) and b["epic"] == epic.id for b in keep) or permits.budget_reason(ws, epic, d):
                continue
            token = wake_token(epic, epic, signed, dark_marks)
            if any(fs.is_planner(g) and g["epic"] == epic.id and g["delegation"] == d["id"] and g["wake"] == token
                   for g in gone):
                continue  # it ended without children and nothing it waits for changed since
            ready = _ready(ws, settings, epic, d, epic, lines, planner=True)
            if ready is None:
                continue  # checked before its marker: a missing program must not use up its two launches
            # check, count and bind under the delegation's lock: two dashboards on one config dir start one planner
            with epics.delegation_lock(d["id"]):
                if any(fs.is_planner(x) and x["epic"] == epic.id for x in fs.bindings(ws)):
                    continue
                if not fs.mark_planner_run(ws, d["id"]):
                    continue
                b = _start(ws, actor, launcher, settings, epic, d, epic, token, lines, ready)
            if b is not None:
                keep.append(b)
            continue
        for ce in kids:
            if len(keep) >= cap:
                return lines
            t = _ticket(ws, ce.id)
            if t is None or not _launchable(ws, epic, d, t, signed):
                continue
            if any(b["child"] == t.id for b in keep):
                continue
            token = wake_token(epic, t, signed, dark_marks)
            if any(g["child"] == t.id and g["delegation"] == d["id"] and g["wake"] == token for g in gone):
                continue  # parked: nothing it waits for changed since it last started
            ready = _ready(ws, settings, epic, d, t, lines)
            if ready is None:
                continue  # checked before its marker: a refused launch uses up none of its launches
            with epics.delegation_lock(d["id"]):
                if any(x["child"] == t.id for x in fs.bindings(ws)):
                    continue  # another dashboard on this config dir started it meanwhile
                if fs.runs(ws, d["id"], t.id) == 0 and fs.runs(ws, d["id"]) >= d["max_children"]:
                    continue
                if not fs.mark_run(ws, d["id"], t.id):
                    continue
                b = _start(ws, actor, launcher, settings, epic, d, t, token, lines, ready)
            if b is not None:
                keep.append(b)
    return lines
