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


def resolve_why(name: str) -> tuple[str | None, str]:
    """(the absolute path of program `name`, ""), or (None, why it is refused). Looked up only in the absolute entries
    of this server's PATH. Trusted means: the file (after links) is a regular file owned by this user or root that no
    group or other can write, and its folder (as found and after links) is owned by this user or root and not
    writable by others (a group-writable folder of yours, such as Homebrew's /opt/homebrew/bin, is fine). This keeps
    out another user's programs and world-writable folders such as /tmp; it is no defence against code running as you
    (an agent can write any folder you own): agent_writable and the session PATH rules are."""
    if not isinstance(name, str) or not name:
        return None, "no program name"
    if "/" in name:
        found = name if os.path.isabs(name) else None
        if found is None:
            return None, f"{name} is a relative path"
    else:
        entries = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and os.path.isabs(d)]
        found = which(name, path=os.pathsep.join(entries)) if entries else None
        if not found:
            return None, f"{name} is not on the dashboard's PATH ({os.pathsep.join(entries) or 'empty'})"
    try:
        st = os.stat(os.path.realpath(found))
    except OSError as e:
        return None, f"{found} cannot be read ({type(e).__name__})"
    if not stat.S_ISREG(st.st_mode):
        return None, f"{found} is not a regular file"
    if st.st_uid not in (os.getuid(), 0):
        return None, f"{found} is owned by someone other than you or root"
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return None, f"{found} is writable by group or others"
    for folder in {os.path.dirname(os.path.abspath(found)), os.path.dirname(os.path.realpath(found))}:
        try:
            ds = os.stat(folder)
        except OSError as e:
            return None, f"its folder {folder} cannot be read ({type(e).__name__})"
        if ds.st_uid not in (os.getuid(), 0):
            return None, f"its folder {folder} is owned by someone other than you or root"
        if ds.st_mode & stat.S_IWOTH:
            return None, f"its folder {folder} is writable by everyone"
    return str(found), ""


def resolve_bin(name: str) -> str | None:
    """The absolute path of trusted program `name` (resolve_why), or None: the runner then does not start anything."""
    return resolve_why(name)[0]


def program_blocker(settings=None) -> str | None:
    """Why the runner cannot start anything for want of a program: tmux, env or the launch command's claude not found
    at a trusted path (with the path it saw and why it was refused), or None. Tests stand in for it."""
    if settings is None:
        from orch.dashboard.launch import load_settings
        settings = load_settings()
    for name in ("tmux", "env", settings["factory_command"][0]):
        if resolve_bin(name) is None:
            return f"{name} was not found at a trusted path: {resolve_why(name)[1]}"
    return None


def runner_blocker(ws, settings=None) -> str | None:
    """The runner's current reason to start nothing, or None: a program it needs (program_blocker), the user-scope
    settings (user_settings_blocker), or the last readiness run's first blocking check (never run here). One function
    for the runner round and every view, so nothing fails silently."""
    if permits.config_enabled(ws) and not permits.enabled(ws):
        return permits.UNSIGNED
    why = program_blocker(settings)
    if why:
        return why
    why = user_settings_blocker()
    if why:
        return why + "; enable the orch-core plugin in your user-scope Claude settings"
    failing = [c for c in (readiness_report(ws) or []) if c["level"] == "block"]
    return failing[0]["why"] if failing else None


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


# Permission modes that let file edits through and still send every other prompt to orch's hook. bypassPermissions is
# not one: under it no permission request reaches the hook at all (Read, Write, WebFetch, MCP, ...). auto is one only
# outside Dark runs (hook_skip_why): its classifier allows actions without asking the hook.
EDIT_MODES = ("acceptEdits", "auto")


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
    if mode == "bypassPermissions":
        return ("permissions.defaultMode is bypassPermissions, under which no permission request reaches orch's hook: "
                "set it to acceptEdits")
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


def hook_skip_why(ws, data) -> str | None:
    """Why the user-scope settings `data` (the only settings a runner session reads: the launch command keeps
    --setting-sources user and --strict-mcp-config, so no project, local or MCP config is loaded) would let a session
    act without orch's permission hook, or None: bypassPermissions or its skip flag; auto while Dark is on (the
    classifier answers in the hook's place); an allow rule for shell commands (it never prompts); outward tools that do
    not prompt (OUTWARD_TOOLS) missing from permissions.deny. Fails closed: settings that cannot be read are a reason."""
    if not isinstance(data, dict):
        return "your user-scope Claude settings cannot be read"
    perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    mode = perms.get("defaultMode")
    if mode == "bypassPermissions" or data.get("skipDangerousModePermissionPrompt") is True:
        return ("your user-scope settings allow bypassPermissions (defaultMode or skipDangerousModePermissionPrompt): "
                "no permission request would reach orch's hook. Use acceptEdits")
    if mode == "auto" and permits.dark_on(ws):
        return ("permissions.defaultMode is auto, whose classifier allows actions without asking orch's hook, and Dark "
                "is on: a Dark run needs acceptEdits")
    allow = perms.get("allow")
    bash = [str(r) for r in (allow if isinstance(allow, list) else []) if str(r).split("(", 1)[0].strip() == "Bash"]
    if bash:
        return (f"your user-scope settings allow shell commands without a prompt ({permits.shown(', '.join(bash[:5]))}"
                "): those never reach orch's permission hook. Remove them from permissions.allow")
    deny = perms.get("deny")
    lacking = [t for t in OUTWARD_TOOLS if not (isinstance(deny, list) and t in deny)]
    if lacking:
        return (f"your user-scope settings do not deny {', '.join(lacking)} (permissions.deny): tools that do not "
                "prompt never reach orch's permission hook, so a session could use them unasked")
    return None


def edits_blocked(environ=None, command=None) -> bool:
    """Whether runner sessions cannot write files (edits_why says why)."""
    return edits_why(environ, command) is not None


# -- readiness: checks that run the session's environment before anything starts ---------------------------------------
# The live run of 5 Oct failed silently in ways no settings file shows: orch's hooks could not run (no uv on the
# session's PATH), `claude` was a wrapper that exited at once, a new folder waited at the trust dialog. These checks run
# the real programs under the session's own environment, write nothing of their own, and while one blocks the runner
# starts nothing. A result is kept for READY_TTL seconds, and only while the programs and hook commands it probed are
# still the ones the runner would use (_fingerprint); it is shown on the run view.
READY_TTL = 60
PROBE_BUDGET = 20.0  # seconds for all the programs one readiness run starts together
PROBE_CAP = 64 * 1024  # bytes of a program's output kept
_READY: dict[str, tuple[float, list, object]] = {}
OUTWARD_TOOLS = ("Artifact", "WebFetch", "WebSearch")
CLAUDE_JSON_CAP = 1 << 28  # a .claude.json larger than this is not read: the trust check says so


def _probe(argv: list[str], stdin: str, cwd: str, timeout: float = PROBE_BUDGET) -> tuple[int, str, str]:
    """Run one check program (an argv, never a shell string) in its own process group; on timeout the group is
    killed. At most PROBE_CAP bytes of each stream are kept. Tests replace it."""
    import signal
    import subprocess
    import tempfile
    with tempfile.TemporaryFile() as o, tempfile.TemporaryFile() as e:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=o, stderr=e, cwd=cwd, start_new_session=True)
        try:
            p.communicate(stdin.encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
            raise
        o.seek(0)
        e.seek(0)
        return p.returncode, o.read(PROBE_CAP).decode("utf-8", "replace"), e.read(PROBE_CAP).decode("utf-8", "replace")


def agent_writable(ws, path) -> bool:
    """Whether `path` (as written or after links) lies in the workspace or in the runner's child clones, which agents
    write: every session's start folder (start_dir) is the workspace root, below it, or a child's clone. Any error is a
    yes."""
    from orch.core import factory_clones
    try:
        clones = factory_clones.root()
        roots = {Path(os.path.abspath(ws.root)), Path(ws.root).resolve(), Path(os.path.abspath(clones)),
                 clones.resolve()}
        forms = {Path(os.path.abspath(path)), Path(os.path.realpath(path))}
    except (OSError, ValueError, TypeError):
        return True
    return any(f == r or r in f.parents for f in forms for r in roots)


def session_bins(ws) -> list[str]:
    """Programs whose folders the sessions' PATH also gets, besides claude's: `orch` (an agent runs it) and `uv` (the
    plugin's bin/orch runs through it), each resolved and trusted as resolve_bin says; one not found, or one inside the
    workspace (agent-writable: readiness blocks then), is left out."""
    return [b for b in (resolve_bin("orch"), resolve_bin("uv")) if b and not agent_writable(ws, b)]


def _plugin_root(ws, environ) -> Path | None:
    """The folder Claude Code installed the orch-core plugin to (an `installPath` of plugins/installed_plugins.json in
    the user config dir), holding bin/orch and outside the workspace; nothing else."""
    from orch.core.fsutil import read_regular_file
    from orch.instructions.settings import is_plugin_id
    raw = read_regular_file(_user_dir(environ) / "plugins" / "installed_plugins.json", 1 << 22)
    try:
        listed = (json.loads(raw.decode("utf-8")) or {}).get("plugins") if raw else None
    except (ValueError, UnicodeDecodeError, AttributeError):
        listed = None
    for pid, entries in (listed.items() if isinstance(listed, dict) else []):
        for e in (entries if isinstance(entries, list) else [entries]):
            if is_plugin_id(pid) and isinstance(e, dict) and isinstance(e.get("installPath"), str):
                p = Path(e["installPath"])
                if p.is_absolute() and (p / "bin" / "orch").is_file() and not agent_writable(ws, p):
                    return p
    return None


_HOOKS = {"guard": ("PreToolUse", ["guard"]), "permission hook": ("PermissionRequest", ["permit", "hook"])}
_PLUGIN_ORCH = "${CLAUDE_PLUGIN_ROOT}/bin/orch"


def _hook_commands(ws, environ, data) -> tuple[list[tuple[str, list[str], list[str]]], list[str]]:
    """([(label, argv, extra env pairs)], labels not found): the guard and permission hook a session runs, parsed
    into words (never run through a shell): from the user-scope hooks (program `orch` or an absolute path ending in
    /orch), else from the installed plugin's hooks.json (program exactly ${CLAUDE_PLUGIN_ROOT}/bin/orch)."""
    import shlex
    from orch.onboarding import _enabled_plugin_id

    def entries(hooks, event):
        for entry in (hooks.get(event) or []) if isinstance(hooks, dict) else []:
            for h in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
                cmd = h.get("command") if isinstance(h, dict) else None
                if isinstance(cmd, str):
                    yield cmd

    out: dict[str, tuple] = {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    for label, (event, words) in _HOOKS.items():
        for cmd in entries(hooks, event):
            w = _orch_words(cmd)
            if label not in out and w[:len(words)] == words:
                out[label] = (label, [shlex.split(cmd)[0], *w], [])
    if len(out) < len(_HOOKS) and _enabled_plugin_id(_user_dir(environ) / "settings.json"):
        root = _plugin_root(ws, environ)
        try:
            plug = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"] if root else {}
        except (OSError, ValueError, KeyError, TypeError):
            plug = {}
        for label, (event, words) in _HOOKS.items():
            for cmd in entries(plug, event):
                try:
                    w = shlex.split(cmd)
                except ValueError:
                    continue
                if label not in out and w[:1] == [_PLUGIN_ORCH] and w[1:1 + len(words)] == words:
                    out[label] = (label, [str(root / "bin" / "orch"), *w[1:]], [f"CLAUDE_PLUGIN_ROOT={root}"])
    return [out[k] for k in _HOOKS if k in out], [k for k in _HOOKS if k not in out]


def _trusted_folder(environ, root: Path, stop: Path | None = None) -> tuple[bool, str]:
    """(trusted, why not): whether Claude Code recorded the trust dialog as accepted for `root` or a folder above it
    (with `stop`, only up to and including `stop`: a child's clone, whose trust Claude does not take from a folder
    above the clone), comparing real paths on both sides (a key may be written /var/... for /private/var/...). Read
    only."""
    from orch.core.fsutil import read_regular_file
    base = environ.get("CLAUDE_CONFIG_DIR")
    path = Path(base) / ".claude.json" if base else Path.home() / ".claude.json"
    try:
        size = os.stat(path).st_size
    except OSError:
        return False, f"{path} does not exist or cannot be read"
    if size > CLAUDE_JSON_CAP:
        return False, f"{path} is {size >> 20} MB, too big for orch to read (limit {CLAUDE_JSON_CAP >> 20} MB)"
    raw = read_regular_file(path, CLAUDE_JSON_CAP)
    try:
        projects = json.loads(raw.decode("utf-8")).get("projects") if raw else None
    except (ValueError, UnicodeDecodeError, AttributeError):
        projects = None
    if not isinstance(projects, dict):
        return False, f"{path} holds no readable `projects`"
    ups = [root, *root.parents]
    if stop is not None:
        top = os.path.realpath(stop)
        ups = ups[:next((i + 1 for i, p in enumerate(ups) if os.path.realpath(p) == top), len(ups))]
    want = {os.path.realpath(p) for p in ups}
    for key, v in projects.items():
        if isinstance(v, dict) and v.get("hasTrustDialogAccepted") is True and isinstance(key, str) \
                and os.path.realpath(key) in want:
            return True, ""
    return False, f"{path} records no accepted trust dialog for {root}" + (
        " or a folder above it" if stop is None else " itself (or a folder above it inside its clone)")


def clone_trust(ws, environ=None) -> list[dict]:
    """Every clone the runner made for this workspace and whether Claude Code recorded its folder-trust answer for
    the folder a session starts in there: [{child, path, trusted}]. Claude asks per clone folder (the live run of
    5 October: trust of the clones folder above did not carry over to a clone, a git repository of its own), so only
    an entry for that folder, or one above it inside the clone, counts. Read only."""
    from orch.core import factory_clones
    environ = os.environ if environ is None else environ
    out = []
    for row in factory_clones.listing(ws):
        rec = factory_clones.record(ws, row["child"])
        if rec is None:
            continue
        start = factory_clones.start_in(ws, rec)
        ok, _ = _trusted_folder(environ, start, stop=Path(rec["path"]))
        out.append({"child": row["child"], "path": str(start), "trusted": ok})
    return out


def _check(name, ok, why="", level="block", tail="") -> dict:
    return {"name": name, "ok": bool(ok), "level": level, "why": why, "tail": escaped_tail(tail, 5) if tail else ""}


def _fingerprint(ws, settings, environ=None):
    """What a readiness result was probed for, cheap to read again (no program is run): the programs the sessions get
    and the hook commands. A cached result counts only while this is unchanged."""
    environ = os.environ if environ is None else environ
    progs = [resolve_bin(settings["factory_command"][0]), resolve_bin("env"), resolve_bin("orch"), resolve_bin("uv")]
    return ([(p, os.path.realpath(p)) if p else None for p in progs],
            _hook_commands(ws, environ, _user_settings(environ)))


def readiness(ws, settings, environ=None) -> list[dict]:
    """Every readiness check: [{name, ok, level ("block" or "warn"), why, tail}]. Runs `claude --version` and the
    hook programs under the session's exact environment (env -i, the session PATH), within PROBE_BUDGET seconds for
    all; reads the user settings, the trust record and the skills folder. Nothing is written by orch."""
    import time
    import uuid
    environ = os.environ if environ is None else environ
    claude, env_bin = resolve_bin(settings["factory_command"][0]), resolve_bin("env")
    if claude is None or env_bin is None:
        return []  # _ready says so for every launch
    out = []
    inside = [f"{n} ({p})" for n, p in (("claude", claude), ("env", env_bin), ("orch", resolve_bin("orch")),
                                        ("uv", resolve_bin("uv"))) if p and agent_writable(ws, p)]
    out.append(_check("programs", not inside,
                      f"{', '.join(inside)} lies inside the workspace, which agents write: the sessions would run "
                      "agent-written code for it. Install it outside the workspace (for example as a tool of your "
                      "user) and start the dashboard from there"))
    if agent_writable(ws, claude) or agent_writable(ws, env_bin):
        return out  # nothing of it is run
    base = environ.get("CLAUDE_CONFIG_DIR")
    if base and not os.path.isabs(base):
        out.append(_check("config dir", False, f"CLAUDE_CONFIG_DIR is the relative path {permits.shown(base)}: each "
                                               "session would read another folder depending on where it starts. "
                                               "Set it to an absolute path"))
        return out
    import tempfile
    from orch.core.ledger import base_dir
    bins = [claude, *session_bins(ws)]
    prefix = env_prefix(env_bin, bins, environ)
    path, root = child_path(*bins), Path(ws.root).resolve()
    data = _user_settings(environ)
    deadline = time.monotonic() + PROBE_BUDGET
    # the programs run in an empty folder of the runner's own, and a plugin's bin/orch keeps its venv in a data folder
    # of the runner's (as Claude Code gives a plugin CLAUDE_PLUGIN_DATA): nothing is written in the workspace or the
    # plugin's install folder
    plugin_data = base_dir() / "permits" / "plugin-data"
    plugin_data.mkdir(mode=0o700, parents=True, exist_ok=True)
    scratch = tempfile.TemporaryDirectory(prefix="orch-probe-")

    def run(argv, stdin=""):
        left = deadline - time.monotonic()
        if left <= 0:
            return 124, "", f"the readiness checks' time budget of {PROBE_BUDGET:.0f} seconds was used up"
        try:
            return _probe(argv, stdin, scratch.name, left)
        except (OSError, ValueError) as e:
            return 127, "", f"{type(e).__name__}: {e}"
        except Exception as e:  # a timeout among them
            return 124, "", f"{type(e).__name__}: it did not finish within the time budget"

    code, so, se = run([*prefix, claude, "--version"])
    out.append(_check("claude", code == 0 and re.search(r"\d+\.\d+", so),
                      f"`{claude} --version` failed under the sessions' environment (exit {code}): the program the "
                      "runner found may be a wrapper that cannot find the real claude. Put the real claude first on "
                      "the dashboard's PATH", tail=se or so))
    has_orch = which("orch", path=path) is not None
    out.append(_check("orch on PATH", has_orch,
                      f"`orch` is not on the sessions' PATH ({path}): install it as a tool of your user (for example "
                      "`uv tool install` of orch-core), outside the workspace, so the dashboard's PATH finds it, then "
                      "restart the dashboard"))
    cmds, missing = _hook_commands(ws, environ, data)
    if missing and user_settings_blocker(environ) is None:
        out.append(_check("hooks", False, f"cannot find the {' and '.join(missing)} command to test (no installed "
                                          "orch-core plugin outside the workspace was found): install the plugin "
                                          "at user scope again"))
    for label, argv, extra in cmds:
        if argv[0].startswith("/") and agent_writable(ws, argv[0]):
            out.append(_check(label, False, f"orch's {label} runs {argv[0]}, inside the workspace, which agents "
                                            "write: point the hook at an orch outside the workspace"))
            continue
        if argv[0].startswith("/") and resolve_bin(argv[0]) != argv[0]:
            out.append(_check(label, False, f"orch's {label} runs {argv[0]}, which is not a trusted program "
                                            f"({resolve_why(argv[0])[1] or 'it resolves elsewhere'}): it is not run"))
            continue
        if extra:  # a plugin's hook: its data folder is the runner's
            extra = [*extra, f"CLAUDE_PLUGIN_DATA={plugin_data}"]
        event = "PreToolUse" if label == "guard" else "PermissionRequest"
        payload = json.dumps({"session_id": str(uuid.uuid4()), "hook_event_name": event, "tool_name": "Bash",
                              "tool_input": {"command": "true"}, "cwd": str(root)})
        code, so, se = run([*prefix, *extra, f"CLAUDE_PROJECT_DIR={root}", *argv], payload)
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
    trusted, why = _trusted_folder(environ, root)
    out.append(_check("trust", trusted,
                      f"Claude Code has not recorded the trust dialog for {root} ({why}): a new session would stop "
                      "at it. Open Claude once in this folder and accept the trust dialog"))
    from orch.core import factory_clones
    from orch.core.factory_release import workspace_repo
    can, cwhy = factory_clones.clonable(ws)
    if can is False:  # predicted here, before a planner makes children that could never start
        out.append(_check("clones", False, f"the runner cannot make the children's clones: {cwhy}. Fix the "
                                           "workspace checkout (or set the release recipe's base), then restart the "
                                           "dashboard"))
    elif can is None and workspace_repo(ws) is not None:
        out.append(_check("clones", False, cwhy, level="warn"))
    if can:  # children get clones of their own: git, and the trust of the clones folder
        git = resolve_bin("git")
        seen = f"{git} lies inside the workspace" if git else resolve_why("git")[1]
        out.append(_check("git", git is not None and not agent_writable(ws, git),
                          f"git was not found at a trusted path outside the workspace ({seen}): a child gets no "
                          "clone of its own and is not started. Install git outside the workspace and restart the "
                          "dashboard"))
        factory_clones.root().mkdir(mode=0o700, parents=True, exist_ok=True)
        rows = clone_trust(ws, environ)
        left = [r for r in rows if not r["trusted"]]
        out.append(_check("clones trust", bool(rows) and not left,
                          "Claude Code asks its folder-trust question once per child's clone folder (trust of a "
                          "folder above a clone is not used for it): "
                          + (f"not yet answered for {', '.join(r['child'] + ' (' + r['path'] + ')' for r in left)}"
                             if left else "no clone has been made yet")
                          + ". A child's session waits at the question until you accept it in its pane (the run view "
                            "says so; the runner never answers it), or run `orch factory clones trust` in your "
                            "terminal for what to add", level="warn"))
    skip = hook_skip_why(ws, data)
    out.append(_check("permission mode", skip is None, skip or ""))
    scratch.cleanup()
    return out


def readiness_blocker(ws, settings) -> str | None:
    """The first blocking readiness failure, or None. A result is reused for at most READY_TTL seconds and only while
    _fingerprint is unchanged; any error is a failure with its reason (never a silent pass)."""
    import time
    key = str(Path(ws.root).resolve())
    try:
        fp = _fingerprint(ws, settings)
        at, checks, seen = _READY.get(key, (0.0, None, None))
        if checks is None or seen != fp or time.monotonic() - at > READY_TTL:
            checks = readiness(ws, settings)
            _READY[key] = (time.monotonic(), checks, fp)
    except Exception as e:
        checks = [_check("readiness", False, f"the readiness checks failed ({type(e).__name__}): nothing starts "
                                             "until they run")]
        _READY[key] = (time.monotonic(), checks, None)
    failed = [c for c in checks if not c["ok"] and c["level"] == "block"]
    return failed[0]["why"] if failed else None


def readiness_report(ws) -> list[dict] | None:
    """The failing checks of the last readiness run (blocking and warnings), without running anything; None when none
    ran yet in this process."""
    checks = _READY.get(str(Path(ws.root).resolve()), (0.0, None, None))[1]
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
    "messages to short plain sentences without line breaks, backticks, dollar signs or backslashes, and put a "
    "revision with ^ or ~ in double quotes (`git show \"HEAD^\"`). Create and change files with your file tools "
    "(Write, Edit), never with a shell heredoc, echo or cat redirect (such a command never runs here), and look at "
    "a file with your Read tool instead of extra commands such as python or jq. Never pipe output into head, grep "
    "or jq: read a ticket with `orch show {key}` alone, or `orch show {key} --section NAME`, `orch show {key} "
    "--lines N` or `orch show {key} --json`, and help with the command followed by --help alone, never piped. Check a JSON or HTML file "
    "by reading it with your Read tool: never start a server (python3 -m http.server), never curl it and never run "
    "python3 -m json.tool on it. Never run mkdir, printf or echo to make a file or folder: the Write tool makes the "
    "folder it needs. File `orch permit request` only for "
    "a command that was actually denied with a request id P-n in the denial message, never for one that was not "
    "denied, and never retry variants of a denied command. If a command is denied, do not wait for approval: run "
    "the next plain step from your instructions, or end your turn with `orch log {key} -m \"...\"` stating exactly "
    "what is missing; the runner tells you when a person answered, and `orch permit show P-n` shows an answer. "
    "Never run `orch instructions sync` or `orch setup`. "
)

# orch's evidence format (orch.core.evidence): criteria are top-level checkbox lines, evidence a top-level Verification
# line citing AC<n>. The examples below are what the tests parse with the real parser.
CRITERION_EXAMPLE = "- [ ] The export writes one row per order to out.csv"
EVIDENCE_EXAMPLE = "- AC1: ran the export on the sample orders and saw 3 rows in out.csv"

# The planner's prompt: built in, like the work prompt, never from the config, a ticket or anything an agent edits. It
# names only commands and options orch has (tests/test_factory_planner.py checks them against the CLI).
PLANNER_PROMPT = (
    "You are the planner of the AI Factory epic {key}. Read it with `orch show {key}` and its limits with "
    "`orch epic show {key}`. " + _PLAIN + "Before you create any child, list every concrete deliverable the epic's "
    "Requirements and Acceptance criteria name: each file by its exact name with its extension (such as elephants.html "
    "or elephants.json), each format and each behaviour, and record that list with `orch log {key} -m \"...\"`. Then "
    "create at least one child per deliverable and one deliverable per child, and write that deliverable's exact file "
    "name into the child's Acceptance criteria. Every file is created by exactly one child: each child works in its "
    "own clone and cannot see the others' files, and two children that add the same file conflict when they are "
    "merged. A child that only reads or links to a file another child creates states in its Requirements the agreed "
    "path and shape of that file and says that it does not create it, and its Acceptance criteria never require that "
    "file in its own commit. When two deliverables depend on each other, make them one child. Create no design, spec, mockup or research child unless the epic asks "
    "for a design, and no placeholder child. Split the work into children within those limits, each created with one "
    "`orch new --epic {key} --title \"...\" --size SIZE --requirements-file FILE --acceptance-file FILE` (SIZE is "
    "xs, s or m, never larger), the Requirements and Acceptance criteria written into files under "
    "orchestrator/temporary first. Write the Acceptance criteria file as top-level checkbox lines, one concrete and "
    "checkable criterion per line and nothing else, like `" + CRITERION_EXAMPLE + "`: orch counts only such lines as "
    "criteria (AC1, AC2, ... from the top), and the workers prove each one. When a child's size needs a Plan (every "
    "size but xs), write it as one paragraph with `orch section set CHILD Plan -m \"...\"`. "
    "`orch ask` is refused in this epic: decide within the epic's text and record why with `orch log CHILD -m "
    "\"...\"`, or leave the item out. Then approve each child with `orch epic auto-approve CHILD`. Do not build "
    "anything and do not change the epic's own text. When every child is refined and approved, stop."
)

# A child's prompt in a factory epic, used instead of the workspace's default work prompt (the agent may not have the
# orch skills at user scope: the live run's agent then invented `orch work-on`), so it carries the command forms.
FACTORY_WORK_PROMPT = (
    "You work on {key}, a child of an AI Factory epic. Follow the orch-work-on-ticket skill if you have it; these "
    "rules come first. " + _PLAIN + "Do not create a file your ticket says another child creates (its own clone "
    "makes it; two children adding one file conflict when they are merged); if you need such data to test, build a "
    "temporary copy outside the repository and never commit it. Start with `orch claim {key}` and read it with `orch show {key}`. Add each task "
    "with `orch task add {key} \"TASK\"`, then for each one run `orch task start {key} TN`, do the work and run "
    "`orch task done {key} TN` with no -m; put notes in `orch log {key} -m \"...\"`. `orch ask` is refused in this "
    "epic: decide within the ticket's text and record why with `orch log`. {commit} When the work is done, prove "
    "each acceptance criterion: the criteria are the checkbox lines of the Acceptance criteria section, AC1, AC2, ... "
    "counted from the top. With your Write tool (never printf, echo or a redirect) write the file "
    "{key}-verification.md under orchestrator/temporary, holding one top-level line per criterion, in order, saying "
    "what you checked and what you saw (a full short sentence), like `" + EVIDENCE_EXAMPLE + "`, and set it with "
    "`orch section set {key} Verification --file FILE`, FILE being that file's path (it replaces the whole "
    "section). Leave the Acceptance criteria as they are: a tick proves nothing, and a criterion counts as "
    "proven only by its Verification line. Read `orch show {key}` to check that every criterion has its line, then "
    "run `orch move {key} testing` and stop; if the move warns that a criterion has no evidence, add its line, set "
    "Verification again and stop."
)
# {commit}: a session in a worktree of its own commits there; one in the shared checkout never commits (its branch may
# be the default branch, and the baseline cannot create one). The permission hook refuses `git commit` on the default
# branch or a detached HEAD whatever the prompt says.
COMMIT_HERE = ("Commit your work on this worktree's branch with two separate plain commands, never chained with && "
               "or ;: first `git add FILES`, then `{form}` (each -m is one paragraph: replace the dots with short "
               "plain sentences).")
NO_COMMIT = ("Do not commit: this session runs in the shared checkout, not in a worktree of its own. Leave your "
             "changes in the working tree and say so with `orch log {key} -m \"...\"`.")
# A session in the child's runner-made clone (factory_clones): its working folder is a separate copy of the repository.
# The workspace's path is never given as a folder to go to: a worker that cds there chains (`cd X && orch ...`), and a
# chain never matches a Dark profile rule (the live run of 5 October).
_HERE = ("Run every orch command exactly as written from your current folder. Never cd, never chain with && or ;. "
         "orch already acts on the workspace's tickets (ORCH_HOME is set for you).")
CLONE_COMMIT = ("Your working folder is a separate clone of the repository that the runner made for {key}, on its own "
                "branch. Commit your work there with two separate plain commands, never chained with && or ;: first "
                "`git add FILES`, then `{form}` (each -m is one paragraph: replace the dots with short plain "
                "sentences). Never push: the runner takes the commits from this clone. "
                + _HERE)
CLONE_NO_COMMIT = ("Your working folder is a separate clone of the repository that the runner made for {key}. Do not "
                   "commit: the workspace's commit format is not plain words. Leave your changes in this clone's "
                   "working tree and say so with `orch log {key} -m \"...\"`. " + _HERE)
_TMP = "under orchestrator/temporary"  # the folder the worker writes its Verification file in (see the prompt)
_SUBJECT_OK = re.compile(r"[A-Za-z0-9 \[\]()#:.,_/-]{1,100}")
_LABEL_OK = re.compile(r"[A-Za-z][A-Za-z0-9 _-]{0,30}")


def commit_form(ws, key: str) -> str | None:
    """The `git commit` a worker runs, from the workspace's commit format (its subject with the key and a summary,
    and one -m per required body line), or None when the config does not give plain words (then the prompt does not
    tell the worker to commit)."""
    from orch.instructions.render import body_names
    try:
        cfg = ws.config
        subject = str(cfg["commit"]["subject"])
        labels = [str(x) for x in body_names(cfg)]
    except (KeyError, TypeError):
        return None
    if "{key}" not in subject or "{summary}" not in subject:
        return None
    subject = subject.replace("{key}", key).replace("{summary}", "short summary")
    if not _SUBJECT_OK.fullmatch(subject) or not all(_LABEL_OK.fullmatch(x) for x in labels):
        return None
    return "git commit " + " ".join(f'-m "{p}"' for p in [subject, *(f"{x}: ..." for x in labels)])


# Sample paragraphs of a worked commit example: plain sentences (no ; $ ` \ or quotes), whatever the labels are.
_WORKED = {"What": "Adds the file this ticket asks for.", "Why": "The ticket asks for it.",
           "Risk": "Low. A new file only.", "Rollback": "Delete the file."}


def commit_worked(ws, key: str) -> str | None:
    """A worked `git commit` for `key` in the workspace's format (commit_form with sample sentences), or None."""
    form = commit_form(ws, key)
    if form is None:
        return None
    from orch.instructions.render import body_names
    out = form.replace('"' + str(ws.config["commit"]["subject"]).replace("{key}", key)
                       .replace("{summary}", "short summary") + '"',
                       '"' + str(ws.config["commit"]["subject"]).replace("{key}", key)
                       .replace("{summary}", "Add the requested file") + '"')
    for x in body_names(ws.config):
        out = out.replace(f'"{x}: ..."', f'"{x}: {_WORKED.get(str(x), "One short plain sentence.")}"')
    return out


def factory_work_prompt(key: str, commit: str | None = None, clone_tmp: str | None = None,
                        worked: str | None = None) -> str | None:
    """The built-in prompt of a child's session (its key validated as a ticket key). `commit`: commit_form for a
    session that starts in a work tree of its own, None for one in the shared checkout (it is told not to commit).
    `clone_tmp`: the workspace's temporary folder (absolute) for a session in the child's clone: the files it hands
    orch must lie in the workspace, so the prompt names that folder by its path."""
    from orch.dashboard.data.agent_start import KEY_RE
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        return None
    if clone_tmp is not None:
        if not isinstance(clone_tmp, str) or not os.path.isabs(clone_tmp) or not clone_tmp.isprintable():
            return None
        part = CLONE_COMMIT.replace("{form}", commit) if commit else CLONE_NO_COMMIT
        if commit and worked:  # the clone runs no commit-msg hook: the format, worked, before the first commit
            part += (" orch checks the message when you commit, as the release does, and refuses one that does not "
                     f"fit: for example `{worked}`.")
        # the one path a clone worker needs: where the file it hands orch goes, as a file path, never a folder to enter
        text = FACTORY_WORK_PROMPT.replace(_TMP, f"in {clone_tmp}, outside this clone (give the Write tool and --file "
                                                 f"that full path, {clone_tmp}/{{key}}-verification.md; never write it "
                                                 "inside your working folder, never commit it, never cd there)")
        return text.replace("{commit}", part).replace("{key}", key)
    part = COMMIT_HERE.replace("{form}", commit) if commit else NO_COMMIT
    return FACTORY_WORK_PROMPT.replace("{commit}", part).replace("{key}", key)


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


_NO_RECIPE = "no release recipe for this workspace"


def default_branches(ws, common: Path) -> set[str] | None:
    """The default branches, casefolded: main, master, the release recipe's base, and the branch every remote's HEAD
    names (refs/remotes/<remote>/HEAD, read from files). None when a recipe exists but cannot be loaded (its base is
    unknown, so nothing is known to be safe)."""
    from orch.core import factory_release
    from orch.core.fsutil import read_regular_file
    out = {"main", "master"}
    rec, why = factory_release.load(ws)
    if rec is None and why != _NO_RECIPE:
        return None
    if rec is not None:
        out.add(str(rec.get("base") or "main").casefold())
    try:
        remotes = sorted((common / "refs" / "remotes").iterdir())
    except OSError:
        remotes = []
    for r in remotes:
        raw = read_regular_file(r / "HEAD", 4096)
        prefix = f"ref: refs/remotes/{r.name}/".encode()
        if raw and raw.startswith(prefix):
            out.add(raw[len(prefix):].decode("utf-8", "replace").strip().casefold())
    return out


def own_work_tree(ws, path, child: str) -> str | None:
    """Why `path` is not the child's own work tree, or None. The one rule for where a session starts outside the
    shared checkout, is told to commit, and may commit (start_dir, the prompt, the guard and the permission hook):
    either the child's runner-made clone (factory_clones.own_clone: the recorded folder, HEAD on the branch the runner
    made for it, not a default branch) or the child's linked worktree of the workspace (_own_worktree)."""
    from orch.core import factory_clones
    try:
        p = Path(str(path)).resolve()
    except (OSError, RuntimeError, ValueError) as e:
        return f"it cannot be read ({type(e).__name__})"
    if factory_clones.in_root(p):
        return factory_clones.own_clone(ws, p, child)
    return _own_worktree(ws, p, child)


def _own_worktree(ws, path, child: str) -> str | None:
    """Why `path` is not the child's own linked worktree, or None: a folder below the workspace root whose `.git` is a
    gitfile naming a gitdir in the common git dir's `worktrees/` folder, no reftable refs, HEAD on a real branch that
    names the child (its id as a word) and is not a default branch (default_branches). Read from files only;
    anything unreadable is a reason."""
    from orch.core.fsutil import read_regular_file
    try:
        root, p = Path(ws.root).resolve(), Path(str(path)).resolve()
        if p == root or root not in p.parents:
            return "it is not a folder below the workspace root"
        dot = p / ".git"
        if dot.is_symlink() or not dot.is_file():
            return "it is not a linked git worktree (no .git file)"
        link = read_regular_file(dot, 4096)
        if link is None or not link.startswith(b"gitdir:"):
            return "it is not a linked git worktree"
        gitdir = (p / link[7:].decode("utf-8", "replace").strip()).resolve()
        common_raw = read_regular_file(gitdir / "commondir", 4096)
        if common_raw is None:
            return "its git dir names no common dir"
        common = (gitdir / common_raw.decode("utf-8", "replace").strip()).resolve()
        if gitdir.parent != common / "worktrees":
            return "its git dir is not one of the repository's worktrees"
        if os.path.lexists(common / "reftable") or os.path.lexists(gitdir / "reftable"):
            return "the repository keeps its refs in reftable, which orch cannot read"
        head = read_regular_file(gitdir / "HEAD", 4096)
        if head is None or not head.startswith(b"ref: refs/heads/"):
            return "its HEAD is detached or cannot be read"
        branch = head[16:].decode("utf-8", "replace").strip()
        if not branch or branch == ".invalid":
            return "its HEAD names no real branch"
        defaults = default_branches(ws, common)
        if defaults is None:
            return "the release recipe cannot be loaded, so the default branch is not known"
        if branch.casefold() in defaults:
            return f"its branch {permits.shown(branch)} is a default branch"
        if not re.search(rf"(?<![a-z0-9]){re.escape(str(child).lower())}(?![a-z0-9])", branch.lower()):
            return f"its branch {permits.shown(branch)} does not name {child}"
    except (OSError, RuntimeError, ValueError) as e:
        return f"it cannot be read ({type(e).__name__})"
    return None


def _harness_ok(root: Path, p: Path) -> bool:
    return not any(os.path.lexists(p / f) and not _same_file(p / f, root / f) for f in _HARNESS_FILES)


def own_worktree_dir(ws, t) -> Path | None:
    """The one linked worktree child `t` names, when it is the child's own (own_work_tree; not a clone), or None."""
    from orch.core import factory_clones
    wts = t.meta.get("worktrees")
    vals = list(wts.values()) if isinstance(wts, dict) else []
    if len(vals) == 1 and isinstance(vals[0], str) and vals[0] and "\x00" not in vals[0]:
        try:
            p = (Path(ws.root).resolve() / vals[0]).resolve()
            if p.is_dir() and not factory_clones.in_root(p) and own_work_tree(ws, p, t.id) is None:
                return p
        except (OSError, RuntimeError, ValueError):
            pass
    return None


def start_dir(ws, t) -> str | None:
    """Where the child's session starts: the one worktree the child names when it is the child's own, else the clone
    the runner recorded for it when it is the child's own (own_work_tree, the same rule the commit checks use); else
    the workspace root (the worktree field is agent-written; the clone record is the runner's). None (refuse to
    launch) when that folder carries a harness settings or MCP file that is not identical to the workspace's own."""
    from orch.core import factory_clones
    root = Path(ws.root).resolve()
    p = own_worktree_dir(ws, t)
    if p is not None:
        return str(p) if _harness_ok(root, p) else None
    rec = factory_clones.record(ws, t.id)
    if rec is not None:
        p = factory_clones.start_in(ws, rec)  # the clone's copy of the workspace folder
        if own_work_tree(ws, p, t.id) is None:
            p = p.resolve()
            return str(p) if _harness_ok(root, p) else None
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


ROUND_CLONE_SECONDS = 120  # what one runner round may spend making a clone (at most one new clone per round)


def _ready(ws, settings, epic, d, t, lines, planner: bool = False, actor=None,
           new_clones: list | None = None) -> tuple | None:
    """Everything a launch needs, checked before a launch is counted (a missing program or a refused worktree must not
    use up a child's or the planner's launches): (prompt, cwd, claude, env), or None. `planner`: `t` is the epic
    itself, the planner's prompt is used and the session starts in the workspace root. A child with no worktree of its
    own in a workspace that is a git checkout gets its own clone (factory_clones.ensure; `actor`: the runner)."""
    from orch.core import factory_clones
    root = str(Path(ws.root).resolve())
    cwd = root if planner else start_dir(ws, t)
    if cwd == root and not planner and actor is not None and factory_clones.clonable(ws)[0] is not None:
        new = factory_clones.record(ws, t.id) is None
        if new and new_clones is not None:  # one new clone per round, within a time budget: the round goes on
            if new_clones:
                lines.append(f"{t.id} waits: the runner makes one clone per round")
                return None
            new_clones.append(t.id)
        path, why = factory_clones.ensure(ws, actor, t.id,
                                          ROUND_CLONE_SECONDS if new_clones is not None else factory_clones.CLONE_TIMEOUT)
        if path is None:
            lines.append(f"{t.id} not started: its clone could not be prepared: {why}")
            return None
        cwd = start_dir(ws, t)
        if cwd == root:  # made, but it does not pass the rule the commit checks use: never a silent shared start
            why = f"its clone is not its own work tree: {own_work_tree(ws, path, t.id)}"
            factory_clones._note_failure(ws, t.id, why)
            lines.append(f"{t.id} not started: {why}")
            return None
    if cwd is None:
        lines.append(f"{t.id} not started: its work tree carries harness settings the workspace does not")
        return None
    clone = factory_clones.in_root(Path(cwd).resolve())
    prompt = planner_prompt(t.id) if planner else factory_work_prompt(
        t.id, commit_form(ws, t.id) if str(Path(cwd).resolve()) != root else None,
        clone_tmp=str(Path(ws.temporary_dir).resolve()) if clone else None,
        worked=commit_worked(ws, t.id) if clone else None)
    claude, env_bin = resolve_bin(settings["factory_command"][0]), resolve_bin("env")
    if claude is None or env_bin is None:
        lines.append(f"{t.id} not started: claude or env was not found at a trusted path (owned by you or root, not "
                     "writable by others)")
        return None
    if agent_writable(ws, claude) or agent_writable(ws, env_bin):
        lines.append(f"{t.id} not started: claude or env lies inside the workspace, which agents write")
        return None
    if prompt is None or not _gate(ws, epic.id, d["id"]):
        return None
    return prompt, cwd, claude, env_bin, [claude, *session_bins(ws)]


def _start(ws, actor, launcher, settings, epic, d, t, token, lines, ready: tuple,
           start_answers: dict | None = None) -> dict | None:
    """Start one session (`ready`: what _ready returned). The command is the user's launch setting with the generated
    id and the built-in prompt put in as whole argv elements (never a shell, never ticket text), the program resolved
    to a trusted absolute path, under `env -i` with a fixed PATH and the allowlisted variables."""
    prompt, cwd, claude, env_bin, bins = ready
    if not _gate(ws, epic.id, d["id"]):  # once more, right before the start
        return None
    command = settings["factory_command"]
    if t.id == epic.id:  # the planner: the human's planner_model, when factory-command.json names one
        from orch.dashboard.launch import with_model
        command = with_model(command, settings.get("planner_model"))
    sid = fs.new_session_id()
    name = f"fx-{t.id}-{secrets.token_hex(3)}"  # unrelated to the session id
    # ORCH_HOME: the agent's orch commands and the hooks act on this workspace's ticket store wherever the session runs
    # (a child's clone or worktree holds a copy of orchestrator/ of its own)
    home = str(Path(ws.home).resolve())
    if "\n" in home or "\x00" in home:
        lines.append(f"{t.id} not started: the workspace path cannot be passed to the session")
        return None
    argv = [*env_prefix(env_bin, bins), f"ORCH_HOME={home}", claude,
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
# After the human answered a card this session's own denial filed, the nudge names each answer instead (fixed template,
# request ids validated by their own shape, nothing an agent wrote): the live run's agent waited for approvals that
# had long been answered, because "the human answered your permission requests" did not say what came of them.
_RID = re.compile(r"P-[0-9A-F]{8}")
OUTCOME = ("A person answered your permission request: {answers}. Retry a granted command now, exactly as before; "
           "for a denied one do the work without it, or end your turn with orch log saying what is missing.")
_ANSWER = r"P-[0-9A-F]{8} (?:granted|denied)"
_OUTCOME_RE = re.compile(re.escape(OUTCOME).replace(re.escape("{answers}"), _ANSWER + r"(?:, " + _ANSWER + r"){0,4}"))
MAX_OUTCOMES = 5


def outcome_nudge(outcomes) -> str | None:
    """The outcome nudge for [(request id, "granted"|"denied")], or None (nothing to say, or an id of another shape)."""
    outcomes = list(outcomes)[:MAX_OUTCOMES]
    if not outcomes or not all(_RID.fullmatch(str(r)) and how in ("granted", "denied") for r, how in outcomes):
        return None
    return OUTCOME.replace("{answers}", ", ".join(f"{r} {how}" for r, how in outcomes))


def nudge_ok(text) -> bool:
    """Whether `text` is one of the runner's own nudges: a fixed line, or the outcome template with valid ids."""
    return isinstance(text, str) and (text in NUDGES.values() or bool(_OUTCOME_RE.fullmatch(text)))


def outcomes(ws, b: dict, signed=None) -> list[tuple[str, str]]:
    """The answers the human signed, since session `b` started, to the requests filed for its ticket: [(id,
    "granted"|"denied")], newest first. From the event log and the signed ledger only."""
    from orch import clock
    signed = ledger.entries(ws) if signed is None else signed
    dec = permits.decisions(ws, signed)
    try:
        start = clock.parse_stamp(str(b.get("at")))
    except (ValueError, TypeError):
        return []
    out = []
    for r in permits.requests(ws).values():
        e = dec.get((r["id"], r["sha"]))
        if e is None or str(r["ticket"]).upper() != str(b.get("child")).upper():
            continue
        try:
            if clock.parse_stamp(str(e.get("at"))) < start:
                continue
        except (ValueError, TypeError):
            continue
        out.append((str(e.get("at")), r["id"], "granted" if e.get("kind") == "grant" else "denied"))
    return [(rid, how) for _, rid, how in sorted(out, reverse=True)][:MAX_OUTCOMES]


MAX_NUDGES = 3  # per session
NUDGE_GAP = 300  # seconds between two nudges of one session
IDLE_SECONDS = 45  # the pane must show the same idle prompt this long
_BUSY = ("esc to interrupt", "do you want", "don't ask again", "❯ 1.", "> 1.", "(y/n)", "yes, proceed",
         "trust the files", "press enter", "interrupted")
_IDLE = ("? for shortcuts", "shift+tab to cycle")
_PROMPT = re.compile(r"^[\s\u2502|]*[>\u276f](?=\s|$)")
IDLE_VIEW_SECONDS = 180  # the run view says the sessions wait at their prompt after this long


_MENU_LINE = re.compile(r"^[\s\u2502|]*(?:[\u276f\u203a>]|\d+\.\s)")  # a picker's or menu's option line
MAX_WRAP = 10  # lines a typed input may wrap over inside the box


def input_line(text) -> str | None:
    """The text in Claude Code's input box: the last prompt line (`>` or `\u276f`) of the pane whose line above is the
    box's top border (a line of \u2500), with the lines it wraps onto inside the box, up to the box's bottom border,
    side bars stripped and joined by single spaces. None when there is no such box (a prompt echoed in the transcript
    has no border above it), no bottom border, or a picker or menu drawn under the box (an option line). Anything
    else under the box (Claude's footer, a status bar, "Update available!", a clock) is not the input and is ignored:
    it changes by itself."""
    if not isinstance(text, str):
        return None
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        m = _PROMPT.match(lines[i])
        if not m:
            continue
        above = [ln for ln in lines[max(0, i - 2):i] if ln.strip()]
        if not above or "\u2500" not in above[-1]:
            return None
        parts = [lines[i][m.end():]]
        bottom = next((j for j in range(i + 1, min(len(lines), i + 2 + MAX_WRAP)) if "\u2500" in lines[j]), None)
        if bottom is None:
            return None
        parts += lines[i + 1:bottom]
        if any(_MENU_LINE.match(ln) for ln in lines[bottom + 1:] if ln.strip()):
            return None
        return " ".join(" ".join(p.strip().strip("\u2502|").split()) for p in parts if p.strip("\u2502| ")).strip()
    return None


def _norm(s: str) -> str:
    return " ".join(str(s).split())


def leftover(text) -> str | None:
    """The runner's own nudge left in the input box by an attempt that was not submitted (its text, or what of it the
    box shows), or None: the input line starts with the first words of one of the runner's lines."""
    line = input_line(text)
    if not line:
        return None
    heads = [*NUDGES.values(), OUTCOME.split("{answers}")[0]]
    return line if any(_norm(line)[:30] == _norm(h)[:30] for h in heads if len(_norm(line)) >= 30) else None


# Claude Code's folder-trust question, as it draws it. The runner never answers it: trusting a folder is the human's.
_TRUST_QUESTION = ("is this a project you trust", "do you trust the files in this folder")
_TRUST_YES = re.compile(r"^[\s\u2502|]*(?:[>\u276f]\s*)?1\.\s+yes\b.*\btrust\b", re.I)
_TRUST_NO = re.compile(r"^[\s\u2502|]*(?:[>\u276f]\s*)?2\.\s+no\b", re.I)


def trust_question(text) -> bool:
    """Whether the pane shows Claude Code's folder-trust dialog itself, by its structure in the screen's last lines:
    the question, then its numbered options (1. Yes, I trust this folder; 2. No ...), then "Enter to confirm", and
    no Claude input box or footer under it. The words alone (in a transcript, in docs a session prints) never count."""
    if not isinstance(text, str):
        return False
    lines = [ln for ln in text.splitlines() if ln.strip()][-30:]
    low = [ln.casefold() for ln in lines]
    q = next((i for i, ln in enumerate(low) if any(m in ln for m in _TRUST_QUESTION)), None)
    if q is None:
        return False
    yes = next((i for i in range(q + 1, len(low)) if _TRUST_YES.match(low[i])), None)
    no = next((i for i in range((yes or q) + 1, len(low)) if _TRUST_NO.match(low[i])), None)
    enter = next((i for i in range((no or q) + 1, len(low)) if "enter to confirm" in low[i]), None)
    if yes is None or no is None or enter is None:
        return False
    rest = lines[enter + 1:]
    return input_line(text) is None and not any("\u2500" in ln or any(m in ln.casefold() for m in _IDLE)
                                                for ln in rest)


def trust_line(b: dict) -> str:
    """What the run view and the log say, once per session, about a session at the folder-trust question."""
    return (f"{b['child']} waits at Claude's folder-trust question for {b.get('start') or 'its folder'}; accept it "
            "once or trust the folder; the runner cannot answer it")


def _busy(text: str) -> bool:
    low = "\n".join([ln for ln in text.splitlines() if ln.strip()][-12:]).casefold()
    return any(m in low for m in _BUSY)


def typed_ok(text, nudge: str) -> bool:
    """Whether a pane read after typing `nudge` shows it on the input line itself (never on an earlier echo of it in
    the transcript) and nothing that Enter would answer instead (a menu, a permission or trust prompt, a running
    command): only then is Enter pressed."""
    if not isinstance(text, str) or _busy(text):
        return False
    line = input_line(text)
    return bool(line) and _norm(line).startswith(_norm(nudge)[:40])


def pane_idle(text) -> bool:
    """Whether a pane's text shows Claude Code idle at an empty input line, conservatively: its footer hint, the input
    box's line empty, and no sign of a running command, a permission or trust prompt or a menu in its last lines.
    Anything else (including text it does not recognise) is not idle."""
    if not isinstance(text, str) or not text.strip() or _busy(text):
        return False
    low = text.casefold()
    return any(m in low for m in _IDLE) and input_line(text) == ""


def answers(epic, signed, dark_adds: int) -> dict:
    """What the human answered in this epic so far, as counts: grants, denials, and for a Dark charter the rules added
    to this checkout's Dark profile (`dark_adds`). Revocations and removals take a permission away: they answer
    nothing a waiting session could retry."""
    mine = [e for e in signed if e.get("ticket") == epic.id]
    charter = next((e for e in reversed(mine) if e.get("kind") == "charter"), None)
    dark = bool(charter and isinstance(charter.get("delegate"), dict) and charter["delegate"].get("dark"))
    return {"grant": sum(1 for e in mine if e.get("kind") == "grant"),
            "deny": sum(1 for e in mine if e.get("kind") == "permit_deny"),
            "profile": dark_adds if dark else 0}


def _nudge_base(b: dict, now_answers: dict) -> dict:
    return {"session": b["session"], "epic": b["epic"], "delegation": b["delegation"], "answers": now_answers,
            "count": 0, "last": "", "pane": "", "pane_at": "", "idle": ""}


def _observe(actor, capture, b: dict, rec: dict) -> tuple[dict, str | None]:
    """Read the session's pane and keep in its record what it shows: a hash of the screen, since when it is unchanged
    (`pane_at`), and whether it is idle at its prompt (`idle`). Written only when that changes."""
    import hashlib as _h
    from orch import clock
    text = capture(b["name"])
    if not isinstance(text, str):
        return rec, None
    pane = _h.sha256(text.encode("utf-8", "replace")).hexdigest()
    # "trust": waits at that question; a nudge left in the box by an attempt that was not sent counts as idle (the
    # next attempt clears it first), or the session would wait behind it forever (the fourth live run)
    idle = "trust" if trust_question(text) else "1" if pane_idle(text) or (leftover(text) and not _busy(text)) else ""
    if pane != rec["pane"] or idle != rec["idle"]:
        rec = {**rec, "pane": pane, "pane_at": clock.stamp_s(), "idle": idle}
        fs.write_nudge_record(actor, rec)
    return rec, text


def _nudge(ws, actor, launcher, b: dict, now_answers: dict, lines: list) -> None:
    """Watch the pane of session `b` (every round: the run view tells a waiting session from a working one by it), and
    after an answer in its epic type one built-in nudge when it has been idle at its prompt for IDLE_SECONDS: at most
    MAX_NUDGES, at least NUDGE_GAP apart. Fails closed: a missing or damaged record, a pane that cannot be read or
    anything unexpected types nothing."""
    from orch import clock
    capture, type_ = getattr(launcher, "capture", None), getattr(launcher, "type", None)
    rec = fs.nudge_record(b["session"])
    if capture is None or rec is None:
        return
    try:
        was = rec["idle"]
        rec, text = _observe(actor, capture, b, rec)
        if rec["idle"] == "trust" and was != "trust":  # said once, when the session reaches the question
            lines.append(trust_line(b))
        typed = getattr(launcher, "human_typed", None)
        if typed is not None and typed(b["name"]):
            return  # the human typed into it from the browser just now: never type over them
        if text is None or type_ is None or rec["count"] >= MAX_NUDGES or rec["answers"] == now_answers:
            return
        now = clock.now()
        if rec["last"] and (now - clock.parse_stamp(rec["last"])).total_seconds() < NUDGE_GAP:
            return
        if rec["idle"] != "1" or (now - clock.parse_stamp(rec["pane_at"])).total_seconds() < IDLE_SECONDS:
            return
        old = rec["answers"]
        only_denied = (now_answers.get("deny", 0) > old.get("deny", 0)
                       and all(now_answers.get(k, 0) == old.get(k, 0) for k in ("grant", "profile")))
        kind = "planner" if fs.is_planner(b) else "denied" if only_denied else "answered"
        text = outcome_nudge(outcomes(ws, b)) or NUDGES[kind]  # the answers themselves, when they were to its asks
        # counted before typing: a failure to type is not retried in a loop
        fs.write_nudge_record(actor, {**rec, "answers": now_answers, "count": rec["count"] + 1,
                                      "last": clock.stamp_s(), "pane": "", "pane_at": "", "idle": ""})
        sent = type_(b["name"], text)
        if sent is True:
            lines.append(f"{b['name']}: nudged after your answer ({rec['count'] + 1} of {MAX_NUDGES})")
        elif sent == "cleaned":  # typed, not sent, and cleared again: the same answer is tried in the next round
            fs.write_nudge_record(actor, {**rec, "answers": old, "count": rec["count"] + 1, "last": "",
                                          "pane": "", "pane_at": "", "idle": ""})
            lines.append(f"{b['name']}: nudge cleaned up, the pane changed while typing; tried again next round "
                         f"({rec['count'] + 1} of {MAX_NUDGES})")
        else:  # the launcher typed nothing, or cleared the input line again and pressed nothing: an attempt
            lines.append(f"{b['name']}: nudge not sent, the pane changed while typing ({rec['count'] + 1} of "
                         f"{MAX_NUDGES})")
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
    dark_adds = sum(1 for e in signed if e.get("kind") == "dark_profile" and e.get("checkout") == cid
                    and e.get("op") == "add")
    checked: list = []
    new_clones: list = []  # at most one clone is made per round

    def not_ready() -> bool:  # the readiness checks, once per round and only when something would start
        if not checked:
            checked.append(readiness_blocker(ws, settings))
            if checked[0]:
                lines.append(f"not starting anything: {checked[0]}")
        return bool(checked[0])

    for b in keep:  # a session that waits at its prompt after the human answered: one built-in line wakes it
        epic = _ticket(ws, b["epic"])
        if epic is not None:
            _nudge(ws, actor, launcher, b, answers(epic, signed, dark_adds), lines)
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
                           answers(epic, signed, dark_adds))
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
            if fs.runs(ws, d["id"], t.id) == 0 and fs.runs(ws, d["id"]) >= d["max_children"]:
                continue  # checked again under the lock; here so no clone is made for a child that cannot start
            ready = _ready(ws, settings, epic, d, t, lines, actor=actor, new_clones=new_clones)
            if ready is None:
                continue  # checked before its marker: a refused launch uses up none of its launches
            with epics.delegation_lock(d["id"]):
                if any(x["child"] == t.id for x in fs.bindings(ws)):
                    continue  # another dashboard on this config dir started it meanwhile
                if ready[1] != str(Path(ws.root).resolve()) and own_work_tree(ws, ready[1], t.id) is not None:
                    continue  # its clone or worktree changed since (`orch factory clones clean` holds this lock)
                if fs.runs(ws, d["id"], t.id) == 0 and fs.runs(ws, d["id"]) >= d["max_children"]:
                    continue
                if not fs.mark_run(ws, d["id"], t.id):
                    continue
                b = _start(ws, actor, launcher, settings, epic, d, t, token, lines, ready,
                           answers(epic, signed, dark_adds))
            if b is not None:
                keep.append(b)
    return lines
