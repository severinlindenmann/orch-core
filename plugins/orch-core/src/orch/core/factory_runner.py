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


def _user_dir(environ) -> Path:
    base = environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) if base else Path.home() / ".claude"


def _user_settings(environ) -> dict | None:
    """The user-scope Claude settings (CLAUDE_CONFIG_DIR, else ~/.claude, settings.json), read only; None when
    missing or not a JSON object."""
    from orch.core.fsutil import read_regular_file
    raw = read_regular_file(_user_dir(environ) / "settings.json", 1 << 20)
    try:
        data = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (ValueError, UnicodeDecodeError):
        data = None
    return data if isinstance(data, dict) else None


def _model(command, data) -> str:
    """The model the sessions run: the launch command's --model, else the user settings' `model`; "" when unset."""
    words = list(command or [])
    if "--model" in words[:-1]:
        return str(words[words.index("--model") + 1])
    return str((data or {}).get("model") or "")


def edits_why(environ=None, command=None) -> str | None:
    """Why runner sessions cannot write files, or None. The user-scope settings (the file user_settings_blocker reads)
    must set permissions.defaultMode to one of EDIT_MODES; `auto` does not count for a model that cannot use auto mode
    (Haiku: Claude Code then offers no auto mode, and file edits prompt). A runner session's file-edit prompt is denied
    without a card (only shell commands are answered), so its agent cannot write a file, and the planner cannot write
    the files its children's text comes from. Read only. `command`: the launch command (default: the user's)."""
    environ = os.environ if environ is None else environ
    data = _user_settings(environ)
    perms = data.get("permissions") if isinstance(data, dict) else None
    mode = perms.get("defaultMode") if isinstance(perms, dict) else None
    if mode not in EDIT_MODES:
        return "permissions.defaultMode in your user-scope Claude settings is not acceptEdits"
    if mode == "auto":
        if command is None:
            from orch.dashboard.launch import load_settings
            command = load_settings()["factory_command"]
        model = _model(command, data)
        if "haiku" in model.casefold():
            return (f"permissions.defaultMode is auto, which the model {permits.shown(model)} cannot use, so file "
                    "edits prompt: set it to acceptEdits")
    return None


def edits_blocked(environ=None, command=None) -> bool:
    """Whether runner sessions cannot write files (edits_why says why)."""
    return edits_why(environ, command) is not None


# -- readiness: checks that run the session's environment before anything starts ---------------------------------------
# The live run of 5 Oct failed silently in ways no settings file shows: orch's hooks could not run (no uv on the
# session's PATH), `claude` was a wrapper that exited at once, a new folder waited at the trust dialog. These checks run
# the real programs under the session's own environment, write nothing of their own, and while one blocks the runner
# starts nothing. Their result is kept for READY_TTL seconds (FAIL_TTL after a failure) and shown on the run view.
READY_TTL, FAIL_TTL = 300, 60
_READY: dict[str, tuple[float, list]] = {}
OUTWARD_TOOLS = ("Artifact", "WebFetch", "WebSearch")


def _probe(argv: list[str], stdin: str, cwd: str, timeout: float = 60) -> tuple[int, str, str]:
    """Run one check program (an argv, never a shell string of ours). Tests replace it."""
    import subprocess
    r = subprocess.run(argv, input=stdin, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def session_bins() -> list[str]:
    """Programs whose folders the sessions' PATH also gets, besides claude's: `orch` (an agent runs it) and `uv` (the
    plugin's bin/orch runs through it), each resolved and trusted as resolve_bin says; those not found are left out."""
    return [b for b in (resolve_bin("orch"), resolve_bin("uv")) if b]


def _plugin_root(environ) -> Path | None:
    """The folder of the orch-core plugin Claude Code installed at user scope (plugins/installed_plugins.json), else
    the plugin this orch runs from; only one holding bin/orch."""
    from orch.core.fsutil import read_regular_file
    from orch.instructions.settings import is_plugin_id
    from orch.onboarding import _package_plugin_root
    found = []
    raw = read_regular_file(_user_dir(environ) / "plugins" / "installed_plugins.json", 1 << 22)
    try:
        listed = (json.loads(raw.decode("utf-8")) or {}).get("plugins") if raw else None
    except (ValueError, UnicodeDecodeError, AttributeError):
        listed = None
    for pid, entries in (listed.items() if isinstance(listed, dict) else []):
        for e in (entries if isinstance(entries, list) else [entries]):
            if is_plugin_id(pid) and isinstance(e, dict) and isinstance(e.get("installPath"), str):
                found.append(Path(e["installPath"]))
    found.append(_package_plugin_root())
    return next((p for p in found if p is not None and (p / "bin" / "orch").is_file()), None)


def _hook_commands(environ, data) -> tuple[list[tuple[str, str, list[str]]], list[str]]:
    """([(label, shell command, extra env pairs)], labels not found): the guard and permission hook a session runs,
    from the user-scope hooks, else from the enabled plugin's hooks.json."""
    from orch.onboarding import _enabled_plugin_id
    want = {"guard": ("PreToolUse", ["guard"]), "permission hook": ("PermissionRequest", ["permit", "hook"])}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    out: dict[str, tuple] = {}
    for label, (event, words) in want.items():
        for entry in (hooks.get(event) or []) if isinstance(hooks, dict) else []:
            for h in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
                cmd = h.get("command") if isinstance(h, dict) else None
                if label not in out and _orch_words(cmd)[:len(words)] == words:
                    out[label] = (label, cmd, [])
    if len(out) < len(want) and _enabled_plugin_id(_user_dir(environ) / "settings.json"):
        root = _plugin_root(environ)
        try:
            plug = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"] if root else {}
        except (OSError, ValueError, KeyError, TypeError):
            plug = {}
        for label, (event, words) in want.items():
            for entry in (plug.get(event) or []) if isinstance(plug, dict) else []:
                for h in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
                    cmd = h.get("command") if isinstance(h, dict) else None
                    if label not in out and isinstance(cmd, str) and " ".join(words) in cmd:
                        out[label] = (label, cmd, [f"CLAUDE_PLUGIN_ROOT={root}"])
    return [out[k] for k in want if k in out], [k for k in want if k not in out]


def _trusted_folder(environ, root: Path) -> bool:
    """Whether Claude Code recorded the trust dialog as accepted for `root` or a folder above it (read only)."""
    from orch.core.fsutil import read_regular_file
    base = environ.get("CLAUDE_CONFIG_DIR")
    raw = read_regular_file(Path(base) / ".claude.json" if base else Path.home() / ".claude.json", 1 << 26)
    try:
        projects = json.loads(raw.decode("utf-8")).get("projects") if raw else None
    except (ValueError, UnicodeDecodeError, AttributeError):
        projects = None
    if not isinstance(projects, dict):
        return False
    return any(isinstance(projects.get(str(p)), dict) and projects[str(p)].get("hasTrustDialogAccepted") is True
               for p in (root, *root.parents))


def _check(name, ok, why="", level="block", tail="") -> dict:
    return {"name": name, "ok": bool(ok), "level": level, "why": why, "tail": escaped_tail(tail, 5) if tail else ""}


def readiness(ws, settings, environ=None) -> list[dict]:
    """Every readiness check: [{name, ok, level ("block" or "warn"), why, tail}]. Runs `claude --version` and the
    hook commands under the session's exact environment (env -i, the session PATH); reads the user settings, the
    trust record and the skills folder. Nothing is written by orch."""
    import uuid
    environ = os.environ if environ is None else environ
    claude, env_bin = resolve_bin(settings["factory_command"][0]), resolve_bin("env")
    if claude is None or env_bin is None:
        return []  # _ready says so for every launch
    bins = [claude, *session_bins()]
    prefix = env_prefix(env_bin, bins, environ)
    path, root = child_path(*bins), Path(ws.root).resolve()
    data = _user_settings(environ)
    out = []

    def run(argv, stdin=""):
        try:
            return _probe(argv, stdin, str(root))
        except (OSError, ValueError) as e:
            return 127, "", f"{type(e).__name__}: {e}"
        except Exception as e:  # a timeout among them
            return 124, "", f"{type(e).__name__}"

    code, so, se = run([*prefix, claude, "--version"])
    out.append(_check("claude", code == 0 and re.search(r"\d+\.\d+", so),
                      f"`{claude} --version` failed under the sessions' environment (exit {code}): the program the "
                      "runner found may be a wrapper that cannot find the real claude. Put the real claude first on "
                      "the dashboard's PATH", tail=se or so))
    has_orch = which("orch", path=path) is not None
    out.append(_check("orch on PATH", has_orch,
                      f"`orch` is not on the sessions' PATH ({path}): install it as a tool of your user (for example "
                      "`uv tool install` of orch-core) so the dashboard's PATH finds it, then restart the dashboard"))
    cmds, missing = _hook_commands(environ, data)
    if missing and user_settings_blocker(environ) is None:
        out.append(_check("hooks", False, f"cannot find the {' and '.join(missing)} command to test (the orch-core "
                                          "plugin's folder was not found): enable the plugin at user scope again"))
    for label, cmd, extra in cmds:
        event = "PreToolUse" if label == "guard" else "PermissionRequest"
        payload = json.dumps({"session_id": str(uuid.uuid4()), "hook_event_name": event, "tool_name": "Bash",
                              "tool_input": {"command": "true"}, "cwd": str(root)})
        code, so, se = run([*prefix, *extra, f"CLAUDE_PROJECT_DIR={root}", "/bin/sh", "-c", cmd], payload)
        try:
            sane = not so.strip() or isinstance(json.loads(so), dict)
        except ValueError:
            sane = False
        out.append(_check(label, code == 0 and sane,
                          f"orch's {label} does not run under the sessions' environment (exit {code}): sessions would "
                          "run without it. A plugin's bin/orch needs uv on the sessions' PATH (the folders of claude, "
                          "orch and uv, and the system's)", tail=se or so))
    from orch.onboarding import _enabled_plugin_id
    skills = (_enabled_plugin_id(_user_dir(environ) / "settings.json") is not None
              or (_user_dir(environ) / "skills" / "orch-work-on-ticket" / "SKILL.md").is_file())
    out.append(_check("skills", skills, "the orch skills are not available at user scope: sessions follow the "
                                        "built-in prompts, which name the commands they need", level="warn"))
    out.append(_check("trust", _trusted_folder(environ, root),
                      f"Claude Code has not recorded the trust dialog for {root}: a new session would stop at it. "
                      "Open Claude once in this folder and accept the trust dialog"))
    deny = ((data or {}).get("permissions") or {}).get("deny") if isinstance((data or {}).get("permissions"), dict) \
        else None
    lacking = [t for t in OUTWARD_TOOLS if not (isinstance(deny, list) and t in deny)]
    out.append(_check("outward tools", not lacking,
                      f"your user-scope settings do not deny {', '.join(lacking)} (permissions.deny): tools that do "
                      "not prompt never reach orch's permission hook, so a session can use them unasked",
                      level="warn"))
    return out


def readiness_blocker(ws, settings) -> str | None:
    """The first blocking readiness failure (with its output tail), or None; computed at most every READY_TTL seconds
    (FAIL_TTL while one fails)."""
    import time
    key = str(Path(ws.root).resolve())
    at, checks = _READY.get(key, (0.0, None))
    failed = [c for c in (checks or []) if not c["ok"] and c["level"] == "block"]
    if checks is None or time.monotonic() - at > (FAIL_TTL if failed else READY_TTL):
        checks = readiness(ws, settings)
        _READY[key] = (time.monotonic(), checks)
        failed = [c for c in checks if not c["ok"] and c["level"] == "block"]
    return failed[0]["why"] if failed else None


def readiness_report(ws) -> list[dict] | None:
    """The failing checks of the last readiness run (blocking and warnings), without running anything; None when none
    ran yet in this process."""
    checks = _READY.get(str(Path(ws.root).resolve()), (0.0, None))[1]
    return None if checks is None else [c for c in checks if not c["ok"]]


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
    return prompt, cwd, claude, env_bin, [claude, *session_bins()]


def _start(ws, actor, launcher, settings, epic, d, t, token, lines, ready: tuple,
           start_answers: dict | None = None) -> dict | None:
    """Start one session (`ready`: what _ready returned). The command is the user's launch setting with the generated
    id and the built-in prompt put in as whole argv elements (never a shell, never ticket text), the program resolved
    to a trusted absolute path, under `env -i` with a fixed PATH and the allowlisted variables."""
    prompt, cwd, claude, env_bin, bins = ready
    if not _gate(ws, epic.id, d["id"]):  # once more, right before the start
        return None
    command = settings["factory_command"]
    sid = fs.new_session_id()
    name = f"fx-{t.id}-{secrets.token_hex(3)}"  # unrelated to the session id
    argv = [*env_prefix(env_bin, bins), claude,
            *(a.replace("{session}", sid).replace("{prompt}", prompt) for a in command[1:])]
    b = fs.bind(ws, actor, session=sid, epic=epic.id, delegation=d["id"], child=t.id, name=name, wake=token,
                start=str(cwd))
    if start_answers is not None:  # what the human had answered at the start: a later answer may nudge it
        try:
            fs.write_nudge_record(actor, _nudge_base(b, start_answers))
        except (OrchError, OSError):
            pass  # no record: the session is never nudged (fail closed)
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


# -- the idle nudge -----------------------------------------------------------------------------------------------------
# An interactive session that waits does not end, so the wake above never reaches it. After the human answers something
# in its epic, the runner types ONE of these built-in lines into its pane, when the pane shows Claude's empty input
# prompt and nothing else changed on it for IDLE_SECONDS. Never text from a ticket, the config or an agent.
NUDGES = {
    "answered": "The human answered your permission requests. Retry the blocked commands, then finish your ticket "
                "and move it to testing.",
    "denied": "The human denied a permission you asked for. Do the work without that command and record why with "
              "orch log, then finish your ticket and move it to testing.",
    "planner": "The human answered your permission requests. Retry the blocked commands, then finish splitting the "
               "epic and approving its children.",
}
MAX_NUDGES = 3  # per session
NUDGE_GAP = 300  # seconds between two nudges of one session
IDLE_SECONDS = 45  # the pane must show the same idle prompt this long
_BUSY = ("esc to interrupt", "do you want", "don't ask again", "❯ 1.", "> 1.", "(y/n)", "yes, proceed",
         "trust the files", "press enter", "interrupted")
_IDLE = ("? for shortcuts", "shift+tab to cycle")
_EMPTY_INPUT = re.compile(r"[│|\s]*[>❯]\s*[│|\s]*")


def pane_idle(text) -> bool:
    """Whether a pane's text shows Claude Code idle at an empty input prompt, conservatively: its footer hint, an empty
    input line, and no sign of a running command, a permission or trust prompt or a menu in its last lines. Anything
    else (including text it does not recognise) is not idle."""
    if not isinstance(text, str):
        return False
    tail = [ln for ln in text.splitlines() if ln.strip()][-12:]
    low = "\n".join(tail).casefold()
    if not tail or any(m in low for m in _BUSY) or not any(m in low for m in _IDLE):
        return False
    return any(_EMPTY_INPUT.fullmatch(ln) for ln in tail)


def answers(epic, signed, dark_marks) -> dict:
    """What the human answered in this epic so far, as counts: grants, denials, revocations, and for a Dark charter
    the changes to this checkout's Dark profile."""
    mine = [e for e in signed if e.get("ticket") == epic.id]
    charter = next((e for e in reversed(mine) if e.get("kind") == "charter"), None)
    dark = bool(charter and isinstance(charter.get("delegate"), dict) and charter["delegate"].get("dark"))
    return {"grant": sum(1 for e in mine if e.get("kind") == "grant"),
            "deny": sum(1 for e in mine if e.get("kind") == "permit_deny"),
            "revoke": sum(1 for e in mine if e.get("kind") == "permit_revoke"),
            "profile": len(dark_marks) if dark else 0}


def _nudge_base(b: dict, now_answers: dict) -> dict:
    return {"session": b["session"], "epic": b["epic"], "delegation": b["delegation"], "answers": now_answers,
            "count": 0, "last": "", "pane": "", "pane_at": ""}


def _nudge(ws, actor, launcher, b: dict, now_answers: dict, lines: list) -> None:
    """Type one built-in nudge into the idle pane of session `b` after an answer in its epic: at most MAX_NUDGES, at
    least NUDGE_GAP apart, only while pane_idle holds for IDLE_SECONDS. Fails closed: a missing or damaged record, a
    pane that cannot be read or anything unexpected types nothing."""
    import hashlib as _h
    from orch import clock
    capture, type_ = getattr(launcher, "capture", None), getattr(launcher, "type", None)
    rec = fs.nudge_record(b["session"])
    if capture is None or type_ is None or rec is None or rec["count"] >= MAX_NUDGES or rec["answers"] == now_answers:
        return
    now = clock.now()
    try:
        if rec["last"] and (now - clock.parse_stamp(rec["last"])).total_seconds() < NUDGE_GAP:
            return
        text = capture(b["name"])
        if not isinstance(text, str):
            return
        pane = _h.sha256(text.encode("utf-8", "replace")).hexdigest()
        if pane != rec["pane"]:
            fs.write_nudge_record(actor, {**rec, "pane": pane, "pane_at": clock.stamp_s()})
            return
        if (now - clock.parse_stamp(rec["pane_at"])).total_seconds() < IDLE_SECONDS or not pane_idle(text):
            return
        old = rec["answers"]
        only_denied = (now_answers.get("deny", 0) > old.get("deny", 0)
                       and all(now_answers.get(k, 0) == old.get(k, 0) for k in ("grant", "revoke", "profile")))
        kind = "planner" if fs.is_planner(b) else "denied" if only_denied else "answered"
        # counted before typing: a failure to type is not retried in a loop
        fs.write_nudge_record(actor, {**rec, "answers": now_answers, "count": rec["count"] + 1,
                                      "last": clock.stamp_s(), "pane": "", "pane_at": ""})
        type_(b["name"], NUDGES[kind])
        lines.append(f"{b['name']}: nudged after your answer ({rec['count'] + 1} of {MAX_NUDGES})")
    except (OrchError, OSError, ValueError, TypeError, KeyError):
        return


EARLY_SECONDS = 90  # a session that ends this soon after its start most likely never got going
TAIL_LINES = 15


def escaped_tail(text: str, n: int = TAIL_LINES) -> str:
    """The last `n` non-empty lines of pane text, each cut to 200 characters, everything outside printable ASCII
    escaped: what a record may keep of a screen."""
    lines = [ln.rstrip() for ln in str(text).splitlines() if ln.strip()][-n:]
    return "\n".join(permits.shown(ln[:200]) for ln in lines)


def _ended(ws, actor, launcher, b: dict) -> str | None:
    """A session that is no longer running: read how it ended (when the launcher can, and the pane stayed), end the
    tmux session, and record it when it ended within EARLY_SECONDS of its start. The reason line, or None."""
    from orch import clock
    reap = getattr(launcher, "reap", None)
    try:
        info = reap(b["name"]) if reap else None
    except (OrchError, OSError, ValueError):
        info = None
    if info is None:
        return None
    status, text = info
    try:
        early = (clock.now() - clock.parse_stamp(b["at"])).total_seconds() < EARLY_SECONDS
    except (ValueError, TypeError):
        early = False
    if not early:
        return f"its session ended (exit {status or 'unknown'})"
    try:
        fs.record_early_end(ws, actor, b, status or "unknown", escaped_tail(text))
    except (OrchError, OSError):
        pass
    return f"its session ended right after it started (exit {status or 'unknown'})"


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
        try:
            if b["name"] in names:
                launcher.stop(b["name"])
            elif getattr(launcher, "reap", None):
                launcher.reap(b["name"])  # a pane left after its process ended: end that session too
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
        if why is not None:
            why = _ended(ws, actor, launcher, b) or why
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
    checked: list = []

    def not_ready() -> bool:  # the readiness checks, once per round and only when something would start
        if not checked:
            checked.append(readiness_blocker(ws, settings))
            if checked[0]:
                lines.append(f"not starting anything: {checked[0]}")
        return bool(checked[0])

    for b in keep:  # a session that waits at its prompt after the human answered: one built-in line wakes it
        epic = _ticket(ws, b["epic"])
        if epic is not None:
            _nudge(ws, actor, launcher, b, answers(epic, signed, dark_marks), lines)
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
            if not_ready():
                return lines
            ready = _ready(ws, settings, epic, d, epic, lines, planner=True)
            if ready is None:
                continue  # checked before its marker: a missing program must not use up its two launches
            # check, count and bind under the delegation's lock: two dashboards on one config dir start one planner
            with epics.delegation_lock(d["id"]):
                if any(fs.is_planner(x) and x["epic"] == epic.id for x in fs.bindings(ws)):
                    continue
                if not fs.mark_planner_run(ws, d["id"]):
                    continue
                b = _start(ws, actor, launcher, settings, epic, d, epic, token, lines, ready,
                           answers(epic, signed, dark_marks))
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
            if not_ready():
                return lines
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
                b = _start(ws, actor, launcher, settings, epic, d, t, token, lines, ready,
                           answers(epic, signed, dark_marks))
            if b is not None:
                keep.append(b)
    return lines
