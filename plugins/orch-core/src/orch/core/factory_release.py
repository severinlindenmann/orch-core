"""Dark AI Factory phase 6: the release recipe and the runner's release step (docs/factory.md, "Release recipe").

A Ready Dark epic whose signed charter carries `release: merge|dev|prod` is taken through the stages of the human's
release recipe, up to the signed stage (prod: the recipe's production stage), by the runner (the dashboard process the
human started), never by an agent.

- The **recipe** is a JSON document in the guarded permits folder of the orch config dir (`factory-release.json`,
  `{"workspaces": {"<workspace id>": {"recipe": ..., "programs": ...}}}`), written only by the human's terminal
  command (`orch factory release set`), which also pins each program (real path and sha256). Never workspace config,
  ticket text or charter text: an agent can edit those. Commands are argv lists (no shell strings); placeholders are
  filled in with values that were validated first.
- **The runner's own release repository.** The workspace checkout is agent-written (its `.git/config`, hooks, refs and objects
  included), so nothing is classified or run there. The runner keeps a repository of its own under the guarded
  permits folder (`release-repos/<workspace id>/repo`), with a config only it writes. It fetches each child branch
  from the workspace (objects only, pinned by commit) and the base from the recipe's remote (never from the
  workspace), classifies there, and runs the recipe's commands in that repository's work tree.
- **Diff classification**: before the first merge command of an epic, every child branch not yet merged is compared
  with the remote base (the net diff and every commit it brings in, renames as both paths, submodules included, no
  replace objects); any `sensitive_paths` match stops the release; nothing is merged then.
- One workspace-wide **release lock** (an exclusive file naming the dashboard process and the running command's
  process group, with an expiry) is held across all stages of one epic.
- **Crash safety**: an intent record is written exclusively before a stage's commands run, the outcome after. An
  intent without an outcome (and no live holder) is "unknown": never run again automatically. At most one automatic
  attempt per stage and unit; the human's Retry allows one more.
- **Production** runs after dev is proven and not out of date, on the commit dev was proven on, and never before the
  stage's release window opens (hours since the last production attempt of the workspace, begun or ended, from the
  runner's own records).
  When its live check fails and the charter signs `rollback`, the recipe's rollback runs once, then its own check.

The records, outcomes and the lock live beside the ledger and rest on same-user trust (docs/factory.md). Every reader
fails closed: a missing, unreadable, malformed or foreign record counts as "not proven".
"""
from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import tempfile
import threading
import unicodedata
from pathlib import Path

from orch.core import factory_sessions as fs
from orch.errors import UsageError, ValidationError

STAGES = ("merge", "dev", "production")  # in this order
DEFAULT_PER = {"merge": "child", "dev": "epic", "production": "epic"}
TARGET_STAGE = {"merge": "merge", "dev": "dev", "prod": "production"}  # a charter's `release` -> the recipe's stage
# the Stopped reasons this module adds (orch.dashboard.data.factory._CAN says what the human can do for each)
RELEASE_CODES = ("sensitive", "release-failed", "release-unknown", "release-stale", "production-failed",
                 "rolled-back", "rollback-failed", "release-blocked", "rollback-missing", "release-conflict")
FUTURE_SKEW = 300  # seconds a record's time may lie ahead of this clock before it counts as unreadable
DEFAULT_WINDOW = 20  # hours after any production attempt of a workspace (begun or ended) before the next may begin
MAX_WINDOW = 720
FILE = "factory-release.json"
MAX_FILE = 64 * 1024
MAX_WORKSPACES = 50
MAX_COMMANDS = 10
MAX_ARGS = 64
MAX_ARG = 512
MAX_EXPECT = 1024
MAX_TIMEOUT = 1800
DEFAULT_TIMEOUT = 600
MAX_PATTERNS = 100
MAX_PATTERN = 200
MAX_ATTEMPTS = 10  # files per stage and unit: one automatic attempt, then one per human retry
TAIL = 4096
GIT_OUT = 1 << 20  # the most path-list output classification reads; more is refused
GRACE = 5.0  # seconds a release command gets between SIGTERM and SIGKILL when the dashboard stops
PLACEHOLDERS = ("epic", "child", "branch", "sha", "workspace", "base", "repo", "remote")
_CHILD_ONLY = ("child", "branch")  # {sha}: the child's commit for merge, the base commit dev and production run on
_PH = re.compile(r"\{([a-z]+)\}")
KEY = re.compile(r"[A-Z][A-Z0-9]*-\d+")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_WSID = re.compile(r"[0-9a-f]{16}")
_SLUG = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_SCP = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._/~-]+")
GIT_CONFIG_KEYS = ("credential.helper", "core.sshCommand")  # what a recipe's git_config may set for the runner's git
# programs that run a string as code, or another program: the recipe is argv lists, so these are refused (the same
# idea as the Dark profile's broad programs); interpreters only with a -c/-e style argument
_SHELLS = re.compile(r"(?:ba|z|k|c|tc|da|fi|pw)?sh[\d.]*|env|eval|exec|sudo|su|doas|xargs|busybox|nohup|timeout|nice"
                     r"|command|builtin|script|osascript|time|stdbuf|setsid|caffeinate|unbuffer|watch|ionice|flock",
                     re.I)
_INTERP = re.compile(r"python[\d.]*w?(?:-\S+)?|pypy[\d.]*|node[\d]*|nodejs|perl[\d.]*|ruby[\d.]*|php[\d.]*|deno|bun|lua"
                     r"|tclsh|Rscript|julia|swift|osascript", re.I)
_CODE_FLAGS = ("-c", "-e", "-E", "--eval", "-p", "--print", "-r", "-x")


class ReleaseError(ValidationError):
    """The runner's release repository or a git step failed: nothing runs this round (fail closed)."""


def valid_branch(name) -> bool:
    """A strict git branch name: starts with a letter or digit (never an option), no `..`, `//`, `/.`, `@{`, and
    no trailing `/`, `.` or `.lock`."""
    return (isinstance(name, str) and bool(_BRANCH.fullmatch(name)) and ".." not in name and "//" not in name
            and "/." not in name and not name.endswith(("/", ".", ".lock")))


def _printable(s: str) -> bool:
    return all(32 <= ord(c) < 127 for c in s)


def remote_url(remote: str) -> str | None:
    """The fetch URL of a recipe's `remote`: an https, ssh or file URL, an scp-style `user@host:path`, an absolute
    path, or `owner/name` (GitHub over https). None for anything else: no `ext::` or other transport helpers."""
    if not isinstance(remote, str) or not remote or len(remote) > 512 or not _printable(remote) or " " in remote \
            or remote.startswith("-") or "::" in remote:
        return None
    if remote.startswith(("https://", "ssh://", "file:///")) or (remote.startswith("/") and ".." not in remote):
        return remote
    if _SCP.fullmatch(remote):
        return remote
    if _SLUG.fullmatch(remote) and ".." not in remote:
        return f"https://github.com/{remote}.git"
    return None


# -- the recipe ---------------------------------------------------------------------------------------------------------

def path() -> Path:
    from orch.core.ledger import base_dir
    return base_dir() / "permits" / FILE


def _program_error(word: str, ws) -> str | None:
    from orch.core import factory_runner
    if _PH.search(word) or "{" in word or "}" in word:
        return f"the program {word!r} may not hold a placeholder"
    if _SHELLS.fullmatch(os.path.basename(word)):
        return (f"the program {word!r} runs a string or another program: give the command as an argv list, or a "
                "script of yours")
    found = factory_runner.resolve_bin(word)
    if found is None:
        return (f"the program {word!r} was not found at a trusted path (an absolute PATH entry, owned by you or root, "
                "not writable by group or others)")
    try:
        real, root = Path(os.path.realpath(found)), Path(ws.root).resolve()
    except (OSError, RuntimeError, ValueError):
        return f"the program {word!r} cannot be resolved"
    if real == root or root in real.parents:
        return f"the program {word!r} lies inside the workspace, which agents write"
    return None


def _code_error(argv: list[str]) -> str | None:
    """Arguments that make a program run code given as text, or git run an alias."""
    prog = os.path.basename(argv[0])
    if _INTERP.fullmatch(prog) and any(w in _CODE_FLAGS or (w[:2] in ("-c", "-e") and len(w) > 2 and w[1] != "-")
                                       for w in argv[1:]):
        return f"{prog} with {'/'.join(_CODE_FLAGS[:2])} runs code given as text"
    if prog.lower() in ("git", "git.exe"):
        for k, w in enumerate(argv[1:], 1):
            nxt = argv[k + 1] if k + 1 < len(argv) else ""
            low = w.lower()
            if (w == "-c" and nxt.lower().startswith("alias.")) or (low.startswith("-c") and "alias." in low) \
                    or low.startswith("--config-env") or low.startswith("--exec-path"):
                return "git with an alias, --config-env or --exec-path runs other code"
    return None


def _check_words(value, what: str, per: str, has_repo: bool) -> None:
    for w in value:
        names = _PH.findall(w)
        rest = _PH.sub("", w)
        if "{" in rest or "}" in rest:
            raise ValidationError(f"{what}: {w!r} holds a brace that is not one of the placeholders "
                                  + ", ".join("{" + p + "}" for p in PLACEHOLDERS))
        for n in names:
            if n not in PLACEHOLDERS:
                raise ValidationError(f"{what}: {{{n}}} is not a placeholder (allowed: "
                                      + ", ".join("{" + p + "}" for p in PLACEHOLDERS) + ")")
            if n in _CHILD_ONLY and per != "child":
                raise ValidationError(f"{what}: {{{n}}} is only filled in for a stage that runs per child")
            if n == "repo" and not has_repo:
                raise ValidationError(f"{what}: {{repo}} needs the recipe's repo (owner/name)")


def _argv(value, what: str, per: str, ws, *, check_program: bool, has_repo: bool) -> list[str]:
    if isinstance(value, str):
        raise ValidationError(f"{what} is a shell string: give it as a list of words, e.g. [\"make\", \"deploy-dev\"]")
    if not isinstance(value, list) or not value or len(value) > MAX_ARGS:
        raise ValidationError(f"{what} must be a list of 1 to {MAX_ARGS} words")
    for w in value:
        if not isinstance(w, str) or not w or len(w) > MAX_ARG or not _printable(w):
            raise ValidationError(f"{what}: every word is printable ASCII text of 1 to {MAX_ARG} characters")
    _check_words(value, what, per, has_repo)
    why = _program_error(value[0], ws) if check_program else (
        "the program may not hold a placeholder" if _PH.search(value[0]) else None)
    why = why or _code_error(value)
    if why:
        raise ValidationError(f"{what}: {why}")
    return list(value)


def _check(chk, what: str, per: str, ws, check_programs: bool, has_repo: bool) -> dict:
    if not isinstance(chk, dict) or not set(chk) <= {"argv", "expect"} or "argv" not in chk:
        raise ValidationError(f'{what} is {{"argv": [...], "expect": "..."}} (expect optional)')
    expect = chk.get("expect")
    if expect is not None:
        if not isinstance(expect, str) or len(expect) > MAX_EXPECT or not _printable(expect):
            raise ValidationError(f"{what}: expect is printable ASCII text of at most {MAX_EXPECT} characters")
        _check_words([expect], f"{what} expect", per, has_repo)
    return {"argv": _argv(chk["argv"], what, per, ws, check_program=check_programs, has_repo=has_repo),
            "expect": expect.strip() if isinstance(expect, str) else None}


def check_recipe(data, ws, *, check_programs: bool = True) -> dict:
    """The recipe `data` validated and normalised: {stages: [{name, per, commands, check, precheck, timeout}],
    sensitive_paths, base, remote, remote_url, repo, git_config}. Raises ValidationError with the first reason it is
    refused."""
    keys = {"stages", "sensitive_paths", "base", "remote", "repo", "git_config"}
    if not isinstance(data, dict) or not set(data) <= keys or "stages" not in data or "remote" not in data:
        raise ValidationError('a recipe is {"stages": [...], "remote": "...", "repo": "owner/name", "base": "main", '
                              '"sensitive_paths": [...], "git_config": {...}} and nothing else; stages and remote are '
                              "required")
    url = remote_url(data["remote"])
    if url is None:
        raise ValidationError("remote is an https, ssh or file URL, user@host:path, an absolute path or owner/name "
                              "(it is never read from the workspace)")
    repo = data.get("repo")
    if repo is None and _SLUG.fullmatch(data["remote"]):
        repo = data["remote"]
    if repo is not None and not (isinstance(repo, str) and _SLUG.fullmatch(repo) and ".." not in repo):
        raise ValidationError("repo is owner/name")
    gc = data.get("git_config", {})
    if not isinstance(gc, dict) or not set(gc) <= set(GIT_CONFIG_KEYS) or not all(
            isinstance(v, str) and len(v) <= 512 and _printable(v) for v in gc.values()):
        raise ValidationError(f"git_config may set only {', '.join(GIT_CONFIG_KEYS)} (printable text)")
    raw = data["stages"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= len(STAGES):
        raise ValidationError(f"stages must be a list of 1 to {len(STAGES)} stages")
    stages, seen = [], []
    for i, s in enumerate(raw):
        if not isinstance(s, dict) or not set(s) <= {"name", "commands", "check", "timeout", "per", "precheck",
                                                     "window", "rollback"}:
            raise ValidationError(f"stage {i + 1} must be an object with name, commands, check, timeout, per, "
                                  "(merge only) precheck and (production only) window and rollback")
        name = s.get("name")
        if name not in STAGES:
            raise ValidationError(f"stage {name!r} is not a stage: a recipe has merge, dev and production stages "
                                  "only")
        if name in seen:
            raise ValidationError(f"stage {name} is given twice")
        if seen and STAGES.index(name) < STAGES.index(seen[-1]):
            raise ValidationError("the stages come in this order: merge, dev, production")
        if name == "production" and "dev" not in seen:
            raise ValidationError("the production stage comes after a dev stage: production runs only on what dev "
                                  "was proven on")
        seen.append(name)
        per = s.get("per", DEFAULT_PER[name])
        if per != DEFAULT_PER[name]:
            raise ValidationError("stage merge runs per child, pinned to the commit that was checked (per: child)"
                                  if name == "merge" else f"stage {name} runs once per epic (per: epic)")
        cmds = s.get("commands")
        if not isinstance(cmds, list) or not 1 <= len(cmds) <= MAX_COMMANDS:
            raise ValidationError(f"stage {name}: commands must be a list of 1 to {MAX_COMMANDS} commands")
        has_repo = repo is not None
        commands = [_argv(c, f"stage {name}, command {k + 1}", per, ws, check_program=check_programs,
                          has_repo=has_repo) for k, c in enumerate(cmds)]
        if "check" not in s:
            raise ValidationError(f"stage {name}: a stage is proven only by its check: give one")
        check = _check(s["check"], f"stage {name}, check", per, ws, check_programs, has_repo)
        if name != "production" and ("window" in s or "rollback" in s):
            raise ValidationError("only the production stage has a window and a rollback")
        window = s.get("window", {"min_hours_since_last": DEFAULT_WINDOW})
        if not isinstance(window, dict) or set(window) != {"min_hours_since_last"}:
            raise ValidationError('stage production: window is {"min_hours_since_last": <hours>}')
        hours = window["min_hours_since_last"]
        if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= MAX_WINDOW:
            raise ValidationError(f"stage production: min_hours_since_last is a whole number of hours from 1 to "
                                  f"{MAX_WINDOW} (the window is never skipped)")
        rollback = None
        if "rollback" in s:
            rb = s["rollback"]
            if not isinstance(rb, dict) or set(rb) != {"commands", "check"}:
                raise ValidationError('stage production: rollback is {"commands": [[...]], "check": {"argv": [...], '
                                      '"expect": "..."}}, both required')
            if not isinstance(rb["commands"], list) or not 1 <= len(rb["commands"]) <= MAX_COMMANDS:
                raise ValidationError(f"stage production: rollback commands must be a list of 1 to {MAX_COMMANDS} "
                                      "commands")
            rollback = {"commands": [_argv(c, f"stage production, rollback command {k + 1}", per, ws,
                                           check_program=check_programs, has_repo=has_repo)
                                     for k, c in enumerate(rb["commands"])],
                        "check": _check(rb["check"], "stage production, rollback check", per, ws, check_programs,
                                        has_repo)}
        precheck = None
        if "precheck" in s:
            if name != "merge":
                raise ValidationError("only the merge stage has a precheck")
            precheck = _check(s["precheck"], f"stage {name}, precheck", per, ws, check_programs, has_repo)
        if name == "merge":
            if not any("{sha}" in w for c in commands for w in c):
                raise ValidationError("stage merge: a command must name {sha}, so the merge is pinned to the commit "
                                      "that was checked (e.g. --match-head-commit {sha})")
            if not any("{base}" in w for c in commands for w in c) and not (
                    precheck and any("{base}" in w for w in [*precheck["argv"], precheck["expect"] or ""])):
                raise ValidationError("stage merge: a command (the one that opens the pull request) or the precheck "
                                      "must name {base}, so the work goes to the recipe's base")
        timeout = s.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT:
            raise ValidationError(f"stage {name}: timeout is a whole number of seconds from 1 to {MAX_TIMEOUT}")
        stages.append({"name": name, "per": per, "commands": commands, "check": check, "precheck": precheck,
                       "timeout": timeout,
                       **({"window_hours": hours, "rollback": rollback} if name == "production" else {})})
    pats = data.get("sensitive_paths", [])
    if not isinstance(pats, list) or len(pats) > MAX_PATTERNS or not all(
            isinstance(p, str) and p and len(p) <= MAX_PATTERN and _printable(p) for p in pats):
        raise ValidationError(f"sensitive_paths is a list of at most {MAX_PATTERNS} glob patterns (printable ASCII, "
                              f"at most {MAX_PATTERN} characters each)")
    for p in pats:
        if p.endswith("/") or p.startswith("/"):
            raise ValidationError(f"sensitive path {p!r}: no leading or trailing / (a name without a glob character "
                                  "already covers everything below it)")
    base = data.get("base", "main")
    if not valid_branch(base):
        raise ValidationError("base must be a plain branch name, such as main")
    return {"stages": stages, "sensitive_paths": list(pats), "base": base, "remote": data["remote"],
            "remote_url": url, "repo": repo, "git_config": dict(gc)}


def programs_of(rec: dict) -> list[str]:
    """Every program word of the recipe (commands, checks, prechecks), each once."""
    out = []
    for s in rec["stages"]:
        rb = s.get("rollback") or {}
        for argv in [*s["commands"], s["check"]["argv"], *([s["precheck"]["argv"]] if s.get("precheck") else []),
                     *rb.get("commands", []), *([rb["check"]["argv"]] if rb else [])]:
            if argv[0] not in out:
                out.append(argv[0])
    return out


def _sha256(p: str) -> str | None:
    h = hashlib.sha256()
    try:
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def pin_programs(rec: dict) -> dict:
    """{program word: {path, sha256}}: each program resolved now (this terminal's PATH, the runner's trust checks),
    by its real path and content hash. Raises when one cannot be resolved."""
    from orch.core import factory_runner
    pins = {}
    for w in programs_of(rec):
        found = factory_runner.resolve_bin(w)
        real = os.path.realpath(found) if found else None
        digest = _sha256(real) if real else None
        if digest is None:
            raise ValidationError(f"the program {w!r} cannot be pinned: not found at a trusted path")
        pins[w] = {"path": real, "sha256": digest}
    return pins


def pinned(rec: dict, word: str) -> tuple[str | None, str]:
    """(the real path to run, "") when program `word` still resolves to the file the human pinned with the same
    content; (None, why) otherwise."""
    from orch.core import factory_runner
    pin = (rec.get("programs") or {}).get(word)
    found = factory_runner.resolve_bin(word)
    real = os.path.realpath(found) if found else None
    if not pin or real != pin["path"] or _sha256(real) != pin["sha256"]:
        return None, (f"the program {word!r} is not the one you pinned with `orch factory release set` (moved, "
                      "changed or not found): set the recipe again")
    return real, ""


def _read_all() -> dict | None:
    """The whole file as {"workspaces": {...}}: {} when it does not exist, None when it is damaged or not the human's
    own (not a regular file, owned by someone else, writable by group or others)."""
    from orch.core.artifacts import read_regular
    p = path()
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return {"workspaces": {}}
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return None
    raw = read_regular(p, MAX_FILE, root=p.parent)
    try:
        data = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (ValueError, UnicodeDecodeError):
        return None
    if (not isinstance(data, dict) or set(data) != {"workspaces"} or not isinstance(data["workspaces"], dict)
            or len(data["workspaces"]) > MAX_WORKSPACES
            or not all(isinstance(k, str) and _WSID.fullmatch(k) for k in data["workspaces"])):
        return None
    return data


def load(ws) -> tuple[dict | None, str | None]:
    """(the validated recipe of this workspace with its program pins, None) or (None, why there is none). Never
    raises."""
    from orch.core.ledger import workspace_id
    try:
        data = _read_all()
        if data is None:
            return None, f"{path()} is damaged or not yours alone: no release runs"
        entry = data["workspaces"].get(workspace_id(ws))
        if entry is None:
            return None, "no release recipe for this workspace"
        if not isinstance(entry, dict) or set(entry) != {"recipe", "programs"} or not isinstance(entry["programs"],
                                                                                                  dict):
            return None, "the release recipe of this workspace is damaged: set it again"
        rec = check_recipe(entry["recipe"], ws, check_programs=False)
        pins = entry["programs"]
        for w in programs_of(rec):
            pin = pins.get(w)
            if not (isinstance(pin, dict) and isinstance(pin.get("path"), str) and os.path.isabs(pin["path"])
                    and isinstance(pin.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", pin["sha256"])):
                return None, "the release recipe's program pins are damaged: set it again"
        return {**rec, "programs": {w: pins[w] for w in programs_of(rec)}}, None
    except ValidationError as e:
        return None, f"the release recipe is refused: {e}"
    except Exception as e:  # fail closed
        return None, f"the release recipe cannot be read ({type(e).__name__})"


def recipe(ws) -> dict | None:
    return load(ws)[0]


def stage_of(target) -> str | None:
    """The recipe stage a charter's `release` names (prod is the production stage), or None."""
    return TARGET_STAGE.get(target) if isinstance(target, str) else None


def target_stages(target) -> tuple[str, ...]:
    """The stage names a charter's `release` signs, in order (empty for none)."""
    s = stage_of(target)
    return STAGES[:STAGES.index(s) + 1] if s else ()


def up_to(rec: dict | None, target: str) -> list[dict] | None:
    """The recipe's stages up to `target` (merge; merge and dev; or all three for prod), or None when it lacks one."""
    need = target_stages(target) or (STAGES[:STAGES.index(target) + 1] if target in STAGES else ())
    if rec is None or not need:
        return None
    have = {s["name"]: s for s in rec["stages"]}
    return [have[n] for n in need] if all(n in have for n in need) else None


def release_blocker(ws, target: str, rollback: bool = False, max_hours=None) -> str | None:
    """Why a charter cannot sign `release: target` (and `rollback`) in this workspace now, or None. `max_hours`: the
    charter's time budget; a production window as long or longer can use it all up waiting, so that is refused."""
    rec, why = load(ws)
    if rec is None:
        return why + ": run `orch factory release set --file recipe.json` in your own terminal"
    stages = up_to(rec, target)
    if stages is None:
        return (f"the release recipe has no stage(s) up to {stage_of(target) or target} ("
                + ", ".join(target_stages(target) or ("merge",)) + ", in that order)")
    if rollback and not stages[-1].get("rollback"):
        return "the release recipe's production stage has no rollback: add one to the recipe first"
    hours = stages[-1].get("window_hours") if stages[-1]["name"] == "production" else None
    if hours and isinstance(max_hours, int) and hours >= max_hours:
        return (f"the production window ({hours} hours) is as long as or longer than the charter's time budget "
                f"({max_hours} hours): waiting for the window would use the whole budget, so production might never "
                "run. Shorten the window in the recipe")
    return None


def _atomic(p: Path, text: str) -> None:
    """Replace `p` with `text`: a random temporary name created exclusively, never through a link."""
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)


def _edit(ws, change) -> None:
    from filelock import FileLock
    from orch.core.ledger import workspace_id
    path().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with FileLock(str(path().with_name(FILE + ".lock")), timeout=10):
        data = _read_all()
        if data is None:
            raise ValidationError(f"{path()} is damaged or not yours alone; nothing was changed",
                                  hint="look at it and remove it yourself, then set the recipe again")
        change(data["workspaces"], workspace_id(ws))
        _atomic(path(), json.dumps(data, ensure_ascii=True, indent=1))


def set_recipe(ws, actor, data, shown: dict | None = None) -> dict:
    """Human only: validate this workspace's recipe, pin its programs and store both (the other workspaces' are kept).
    `shown`: the pins the human was shown; a program that changed since refuses."""
    fs.human_check(actor, "setting the release recipe")
    rec = check_recipe(data, ws)
    pins = pin_programs(rec)
    if shown is not None and shown != pins:
        raise ValidationError("a program changed since it was shown; nothing was set")
    _edit(ws, lambda all_, wid: all_.__setitem__(wid, {"recipe": data, "programs": pins}))
    return {**rec, "programs": pins}


def clear_recipe(ws, actor) -> bool:
    """Human only: remove this workspace's recipe. True when there was one."""
    fs.human_check(actor, "clearing the release recipe")
    had = []
    _edit(ws, lambda all_, wid: had.append(all_.pop(wid, None) is not None))
    return bool(had and had[0])


# -- sensitive paths --------------------------------------------------------------------------------------------------

def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s).casefold()


def sensitive(p: str, patterns) -> bool:
    """Whether path `p` matches one of `patterns`, case-insensitively. A pattern without a glob character is that file
    or folder and everything below it; one with a glob is matched with fnmatch (`*` also crosses `/`); a leading `**/`
    matches at any depth, the top level included."""
    q = _norm(p)
    for pat in patterns:
        r = _norm(pat)
        anyd = r.startswith("**/")
        r = r[3:] if anyd else r
        if not any(c in r for c in "*?["):
            if q == r or q.startswith(r + "/") or (anyd and f"/{r}/" in f"/{q}/"):
                return True
        elif fnmatch.fnmatchcase(q, r) or (anyd and fnmatch.fnmatchcase(q, "*/" + r)):
            return True
    return False


# -- records beside the ledger ---------------------------------------------------------------------------------------

def _dir(ws, epic_id: str) -> Path:
    """The epic's release records (keyed by epic, not by delegation: a proven merge stays proven after the human
    approves the epic again, so nothing is merged twice)."""
    return fs._root() / "release-records" / fs._key(ws, "release", str(epic_id).upper())


def _read(p: Path, limit: int = 3 * TAIL) -> dict | None:
    from orch.core.artifacts import read_regular
    raw = read_regular(p, limit, root=fs._root())
    try:
        body = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (ValueError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _name(stage: str, unit: str, n: int, kind: str) -> str:
    return f"{stage}.{fs._safe(unit)}.{n}.{kind}"


def unit_state(ws, epic_id: str, stage: str, unit: str, holder: dict | None = None) -> dict:
    """{state: waiting|running|proven|stale|failed|unknown, attempt, codes, tail, why, ended, sha, children} of one
    stage and unit (a child, or the epic for a stage that runs per epic). `holder`: the live release lock holder."""
    d = _dir(ws, epic_id)
    n = 0
    while n < MAX_ATTEMPTS and os.path.lexists(d / _name(stage, unit, n + 1, "intent")):
        n += 1
    if any(os.path.lexists(d / _name(stage, unit, k, "intent")) for k in range(n + 2, MAX_ATTEMPTS + 1)):
        # an attempt's intent was removed while a later one remains: the records cannot be trusted (fail closed)
        return {"state": "unknown", "attempt": n, "why": "its attempt records have a gap"}
    j = max(_journal_attempts(ws, epic_id, stage, unit), default=0)
    if j > n:  # the journal remembers an attempt whose records are gone (removed, or a crash right after the line)
        return {"state": "unknown", "attempt": n, "journal": j,
                "why": f"the runner's journal records attempt {j}, but its records are missing"}
    if n == 0:
        return {"state": "waiting", "attempt": 0}
    out = _read(d / _name(stage, unit, n, "outcome"))
    base = {"attempt": n}
    if not os.path.lexists(d / _name(stage, unit, n, "outcome")):
        live = holder is not None and holder.get("epic") == str(epic_id).upper()
        return {**base, "state": "running" if live else "unknown"}
    if out is None:
        return {**base, "state": "unknown", "why": "its outcome record cannot be read"}
    info = {**base, "codes": out.get("codes"), "tail": str(out.get("tail") or ""), "why": str(out.get("why") or ""),
            "ended": out.get("ended"), "check": out.get("check"), "sha": out.get("sha"),
            "base_sha": out.get("base_sha"), "failed_at": out.get("failed_at"),
            "conflicts": out.get("conflicts") if isinstance(out.get("conflicts"), list) else [],
            "doubles": out.get("doubles") if isinstance(out.get("doubles"), list) else [],
            "children": out.get("children") if isinstance(out.get("children"), dict) else None}
    if os.path.lexists(d / _name(stage, unit, n, "rollback-intent")):  # production only: the signed rollback ran
        rb = _read(d / _name(stage, unit, n, "rollback"))
        if rb is not None:
            info["rollback"] = {"state": "rolled back" if rb.get("rolled_back") is True else "rollback failed",
                                "tail": str(rb.get("tail") or ""), "why": str(rb.get("why") or ""),
                                "ended": rb.get("ended")}
        elif os.path.lexists(d / _name(stage, unit, n, "rollback")):
            info["rollback"] = {"state": "rollback failed", "tail": "", "why": "its record cannot be read"}
        else:
            live = holder is not None and holder.get("epic") == str(epic_id).upper()
            info["rollback"] = {"state": "rolling back" if live else "rollback failed", "tail": "",
                                "why": "" if live else "the rollback started and has no recorded outcome"}
    retried = os.path.lexists(d / _name(stage, unit, n, "retry")) and n < MAX_ATTEMPTS
    if out.get("proven") is True:
        if retried:
            return {**info, "state": "waiting"}  # the human asked for it to run again (it went stale)
        if stage == "merge" and not own_merge(info):  # for every reader, not only the close
            return {**info, "state": "unknown", "why": "its record names no commit of its own (no base it was checked "
                                                       "against, or the base itself)"}
        if os.path.lexists(d / _name(stage, unit, n, "stale")):
            why = (_read(d / _name(stage, unit, n, "stale")) or {}).get("why")
            return {**info, "state": "stale", "why": str(why) if why else "its branch changed after it was merged"}
        return {**info, "state": "proven"}
    if retried:
        return {**info, "state": "waiting"}  # the human allowed one more attempt
    if out.get("interrupted") is True:  # killed or timed out midway: what it changed is not known
        return {**info, "state": "unknown", "why": info["why"] or "it was stopped while a command ran"}
    return {**info, "state": "failed"}


def _sensitive_record(ws, epic_id: str) -> dict | None:
    p = _dir(ws, epic_id) / "sensitive.json"
    if not os.path.lexists(p):
        return None
    return _read(p) or {"hits": {}, "damaged": True}


JOURNAL_STAGE = "journal"  # the pseudo stage of a block on a missing or damaged journal (retry acknowledges it)


def _blocked_record(ws, epic_id: str) -> dict | None:
    """The runner's record that a stage could not start (a program not the one pinned, the recipe lacking a stage,
    the base not fetchable, a signed rollback missing ...): a Stopped reason until the human's Retry clears it. The
    block is in two places, the epic's `blocked.json` and the runner's append-only journal; removing the file does not
    lift it, and a journal that is missing (while release records exist) or damaged blocks every epic (fail closed)."""
    eid = str(epic_id).upper()
    p = _dir(ws, eid) / "blocked.json"
    if os.path.lexists(p):
        return _read(p) or {"code": "release-blocked", "stage": "?", "unit": eid, "why": "its record cannot be read"}
    lines, damaged = _journal(ws)
    if damaged:
        return {"code": "release-blocked", "stage": JOURNAL_STAGE, "unit": eid,
                "why": "the runner's release journal is missing or damaged while release records exist; check the "
                       "release records, then Retry release to acknowledge it"}
    last = next((x for x in reversed(lines) if x.get("epic") == eid and x.get("kind") in ("block", "clear")), None)
    if last is not None and last["kind"] == "block":
        return {"code": str(last.get("code") or "release-blocked"), "stage": str(last.get("stage") or "?"),
                "unit": str(last.get("unit") or eid),
                "why": str(last.get("why") or "") + " (the runner's journal still holds it; its file is missing: "
                       "removed, or never written because the runner stopped right after the journal line)"}
    return None


def _block(ws, actor, epic_id: str, stage: str, unit: str, why: str, code: str = "release-blocked") -> str:
    """Record that `stage` of `unit` could not start, and why (once; the first reason is kept). Nothing ran. The
    journal line comes first: if writing the file fails afterwards, the block still holds."""
    body = {"code": code, "stage": stage, "unit": unit, "why": why[:400], "at": _now(), "epic": str(epic_id).upper()}
    if _blocked_record(ws, epic_id) is None:
        _journal_add(ws, {"kind": "block", **body})
    if _write(_dir(ws, epic_id) / "blocked.json", body):
        _event(ws, actor, epic_id, "release.stage", {"stage": stage, "child": unit, "proven": False, "exit": None})
    return f"{epic_id}: {stage} of {unit} not started: {why}"


SKIP_TEXT = "The release has not run; closing now skips it"


def unreleased(ws, epic) -> list[str] | None:
    """The release stages the epic's live factory charter signs that are not proven yet (as status() reads them;
    unreadable records count as not proven), or None when no release is signed. A human verdict on such an epic must
    say it closes without releasing (Ops.verdict skip_release)."""
    from orch.core import permits
    d = permits.factory_delegation(ws, epic)
    if not d or not d.get("release"):
        return None
    try:
        st = status(ws, epic, d)
    except Exception:
        st = None
    if not st or not st.get("stages"):
        return list(target_stages(d.get("release")))
    return [s["name"] for s in st["stages"] if s["state"] != "proven"]


def skip_fields(ws, epic, skip_release, how: str = "orch verdict <epic> done --skip-release REASON") -> dict:
    """{release_skipped, skipped_stages} for a human verdict or close of `epic` whose signed release has not run all
    its stages, {} when nothing is skipped. Refused without a reason (SKIP_TEXT)."""
    from orch.errors import ValidationError
    left = unreleased(ws, epic)
    if not left:
        return {}
    why = " ".join((skip_release or "").split())[:300]
    if not why:
        raise ValidationError(f"{SKIP_TEXT} ({', '.join(left)} not proven yet)",
                              hint=f"choose Close without releasing and say why ({how})")
    return {"release_skipped": why, "skipped_stages": left}


def signs_release(ws, epic) -> bool:
    """Whether the epic's live factory charter signs a release (then a human close or verdict holds the lock)."""
    from orch.core import permits
    d = permits.factory_delegation(ws, epic)
    return bool(d and d.get("release"))


@contextlib.contextmanager
def quiet(ws, epic_id: str, needed: bool):
    """Hold the workspace's release lock while a human closes `epic_id` without its release (`needed`), so no stage
    runs meanwhile; refused while a release holds it."""
    from orch.errors import ValidationError
    if not needed:
        yield
        return
    if not acquire(ws, epic_id, 120):
        raise ValidationError("a release is running in this workspace now: wait until it ends, then decide again")
    try:
        yield
    finally:
        release_lock(ws)


_CONFLICT = re.compile(r"^CONFLICT \(([^)\n]{1,40})\): (?:Merge conflict in )?(\S[^\n]{0,300})$", re.M)


def conflicts(output: str) -> list[str]:
    """The paths git names in the CONFLICT lines of a merge's output (the runner's own capture of the recipe's
    commands; at most 20), as `<path> (<kind>)`."""
    out = []
    for kind, rest in _CONFLICT.findall(str(output or "")):
        item = f"{rest.strip()} ({kind})"
        if item not in out:
            out.append(item)
    return out[:20]


def own_merge(us: dict) -> bool:
    """Whether a proven merge record names a commit of its own: a full commit id, recorded with the base it was
    classified against, and not that base."""
    sha, base = us.get("sha"), us.get("base_sha")
    return (isinstance(sha, str) and isinstance(base, str) and bool(_SHA.fullmatch(sha))
            and bool(_SHA.fullmatch(base)) and sha != base)


def _units(ws, epic, entries=None) -> list[str]:
    """The children a per-child stage runs for: those in testing (Ready's open children), and those done since with
    a proven merge (the verdict closed them after their release), by id."""
    from orch.core import epics
    return sorted(e.id for e in epics.children(ws, epic.id, entries) if e.status == "testing" or (
        e.status == "done" and unit_state(ws, epic.id, "merge", e.id)["state"] == "proven"))


def _uncounted(ws, epic_id: str, kids: list[str], holder) -> list[dict]:
    """The merge units of children no longer counted (sent back, closed alone, moved, retyped or deleted) whose merge
    failed or whose outcome is unknown, found from the epic's own merge records and the journal, never from tickets:
    such a failure stays a reason until the human retries it (the child going away does not undo a half merge)."""
    eid = str(epic_id).upper()
    d = _dir(ws, eid)
    units = set()
    try:
        names = [x.name for x in d.iterdir() if x.name.startswith("merge.") and x.name.endswith(".intent")]
    except OSError:
        names = []
    for name in names[:MAX_ATTEMPTS * 64]:
        u = (_read(d / name) or {}).get("unit")
        if isinstance(u, str) and KEY.fullmatch(u.upper()):
            units.add(u.upper())
    for x in (_journal_read(ws) or ([], False))[0]:
        if x.get("kind") == "intent" and x.get("epic") == eid and x.get("stage") == "merge" \
                and isinstance(x.get("unit"), str) and KEY.fullmatch(x["unit"].upper()):
            units.add(x["unit"].upper())
    rows = []
    for u in sorted(units - {k.upper() for k in kids}):
        us = unit_state(ws, eid, "merge", u, holder)
        if us["state"] in ("failed", "unknown"):
            why = us.get("why") or "not proven"
            rows.append({"unit": u, **us, "uncounted": True,
                         "why": f"{why}; {u} is no longer in the epic's release (sent back, closed alone, moved or "
                                "deleted), so check by hand what its merge did"})
    return rows


def _window_path(ws) -> Path:
    """The runner's record of the last production attempt of this workspace, written when its commands begin,
    whatever comes of them (guarded, written only by the runner)."""
    return fs._root() / "release-records" / f"production-last-{fs._key(ws, 'production-last')}.json"


def _epic_ids(ws) -> list[str]:
    from orch.core import epics, store
    return [e.id for e in store.scan(ws) if e.meta is not None and epics.is_epic(e.meta)]


def _index_path(ws) -> Path:
    """The runner's append-only release journal of this workspace (guarded, written only by the runner): one line per
    production intent ({kind: production, epic}), per block and per clear of a block, and the human's acknowledgement
    of a damaged journal ({kind: reset}). The hold, the window and blocks read it, so deleting, retyping or breaking an
    epic's ticket, or removing a block's file, changes nothing."""
    return fs._root() / "release-records" / f"production-index-{fs._key(ws, 'production-index')}.jsonl"


def _journal_add(ws, line: dict) -> None:
    """Append one line (raises when it cannot: then nothing that depends on it happens)."""
    p = _index_path(ws)
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps({**line, "at": line.get("at") or _now()}) + "\n")


def _record_epics(ws, production: bool = True) -> set[str]:
    """The epics of this workspace that have production records (or, with production=False, any release records),
    found from the records themselves, whatever the journal says: each intent names its epic (`epic`, or the unit of
    an epic stage), and its folder must be that epic's folder of this workspace."""
    root = fs._root() / "release-records"
    out = set()
    try:
        folders = [x for x in root.iterdir() if x.is_dir() and not x.is_symlink()]
    except OSError:
        return out
    for d in folders:
        try:
            names = [x.name for x in d.iterdir() if x.name.endswith(".intent")
                     and (x.name.startswith("production.") or not production)]
        except OSError:
            continue
        for name in names[:MAX_ATTEMPTS * 64]:
            body = _read(d / name) or {}
            for eid in (body.get("epic"), body.get("unit")):
                if isinstance(eid, str) and KEY.fullmatch(eid) and _dir(ws, eid) == d:
                    out.add(eid)
                    break
    return out


def _journal_attempts(ws, epic_id: str, stage: str, unit: str) -> dict[int, str]:
    """{attempt: when} of every attempt of `stage` for `unit` the journal records (lines that name both; a missing or
    damaged journal is a block of its own, _blocked_record)."""
    eid, u = str(epic_id).upper(), str(unit).upper()
    lines = _journal_read(ws)
    out = {}
    for x in (lines[0] if lines else []):
        if (x.get("kind") in ("intent", "production") and x.get("epic") == eid and x.get("stage") == stage
                and str(x.get("unit") or "").upper() == u and isinstance(x.get("attempt"), int)):
            out[x["attempt"]] = str(x.get("at") or "")
    return out


def _journal_read(ws) -> tuple[list[dict], bool] | None:
    """(the journal's lines, whether a line cannot be read after the human's last acknowledgement), None when there is
    no journal file."""
    from orch.core.artifacts import read_regular
    p = _index_path(ws)
    if not os.path.lexists(p):
        return None
    raw = read_regular(p, 4 << 20, root=fs._root())
    if raw is None:
        return [], True
    lines, bad_after = [], False
    for text in raw.decode("utf-8", "replace").splitlines():
        try:
            line = json.loads(text)
        except ValueError:
            line = None
        ok = isinstance(line, dict) and (line.get("kind") == "reset" or (
            isinstance(line.get("epic"), str) and KEY.fullmatch(line["epic"])))
        if not ok:
            bad_after = True
            continue
        if line.get("kind") == "reset":
            bad_after = False  # the human acknowledged everything before it
        lines.append(line)
    return lines, bad_after


def _journal(ws) -> tuple[list[dict], bool]:
    """(the journal's lines, whether it is missing while production records exist, or holds a line that cannot be read
    after the human's last acknowledgement)."""
    got = _journal_read(ws)
    return got if got is not None else ([], bool(_record_epics(ws, production=False)))


def production_epics(ws) -> tuple[list[str], bool]:
    """(the epics whose production records count for the hold and the window, whether the runner's journal is missing
    or damaged). The journal, the production records themselves, and every epic ticket that parses. Every id is
    checked against the ticket id form before it names a path."""
    lines, damaged = _journal(ws)
    ids = [x["epic"] for x in lines if x.get("kind", "production") == "production" and x.get("epic")]
    ids += sorted(_record_epics(ws))
    ids += [e.upper() for e in _epic_ids(ws) if KEY.fullmatch(e.upper())]
    return list(dict.fromkeys(ids)), damaged


def _reset_path(ws) -> Path:
    return fs._root() / "release-records" / f"window-reset-{fs._key(ws, 'window-reset')}.json"


def prod_charters(ws) -> list[tuple[str, int]]:
    """(epic, the charter's max hours) of every live charter of this workspace that signs release prod."""
    from orch.core import permits
    out = []
    for eid in _epic_ids(ws):
        t = _ticket(ws, eid)
        d = permits.factory_delegation(ws, t) if t is not None else None
        if d and d.get("release") == "prod" and d["active"] and isinstance(d.get("max_hours"), int):
            out.append((eid, d["max_hours"]))
    return out


def _voided(ws) -> tuple[set[str], bool]:
    """(the production times the human's clear-window voided, whether the reset record cannot be read)."""
    p = _reset_path(ws)
    if not os.path.lexists(p):
        return set(), False
    body = _read(p, 64 * 1024)
    got = (body or {}).get("voided")
    if not isinstance(got, list) or not all(isinstance(x, str) for x in got):
        return set(), True
    return set(got), False


def clear_window(ws, actor) -> str:
    """Human only: record that the production times the records hold beyond now (future-dated ones) no longer count
    for the window: exactly those times, listed in the reset record (with the ones voided before). Times up to now,
    and every production recorded later, still count; nothing is deleted from the guarded records."""
    from orch import clock
    fs.human_check(actor, "clearing a future-dated release window")
    times, _ = _production_times(ws, voiding=False)
    before, _ = _voided(ws)
    now = clock.now()
    voided = sorted(before | {clock.stamp_s(t) for t in times if t > now})
    _atomic(_reset_path(ws), json.dumps({"at": _now(), "by": actor.to_str(), "voided": voided}))
    return "production times recorded beyond now no longer keep the window shut; earlier and later ones still count"


def _production_times(ws, voiding: bool = True) -> tuple[list, str]:
    """(every time a production attempt of this workspace began or ended, as the runner's records say, why one could
    not be read). Sources: the runner's window record, the journal's production lines, and every epic's production
    intent and outcome records, so a deleted window record or attempt record does not open the window while another
    source remains. `voiding`: leave out the times the human's clear-window voided."""
    from orch import clock
    times, bad = [], ""

    def add(stamp, what):
        nonlocal bad
        try:
            times.append(clock.parse_stamp(str(stamp)))
        except (ValueError, OverflowError):
            bad = bad or what
    p = _window_path(ws)
    if os.path.lexists(p):
        add((_read(p, 1024) or {}).get("at"), f"the runner's window record ({p.name})")
    ids, damaged = production_epics(ws)
    if damaged:
        bad = "the runner's release journal"
    for x in _journal(ws)[0]:
        if x.get("kind", "production") == "production":
            add(x.get("at"), "a production line of the runner's journal")
    for eid in ids:
        d = _dir(ws, eid)
        for n in range(1, MAX_ATTEMPTS + 1):
            ip = d / _name("production", eid, n, "intent")
            if not os.path.lexists(ip):
                break
            add((_read(ip) or {}).get("started"), f"a production intent record of {eid}")
            op = d / _name("production", eid, n, "outcome")
            if os.path.lexists(op):
                out = _read(op)
                if out is None or out.get("ended") is not None:
                    add((out or {}).get("ended"), f"a production outcome record of {eid}")
    if voiding:  # the human cleared future-dated times (clear_window): exactly those do not count
        voided, unreadable = _voided(ws)
        if unreadable:
            bad = bad or "the window reset record"
        times = [t for t in times if clock.stamp_s(t) not in voided]
    return times, bad


def window(ws, hours: int) -> dict:
    """The production release window of this workspace: {open, opens (a stamp, or None), last, why}. It opens
    `hours` after the last production attempt of the workspace began or ended (open when there was none), as the
    runner's own records say. A record that cannot be read, or whose time lies in the future, keeps it shut (fail
    closed): the human looks at it."""
    from datetime import timedelta
    from orch import clock
    times, bad = _production_times(ws)
    shut = {"open": False, "opens": None, "last": None}
    if bad:
        return {**shut, "why": f"{bad} cannot be read, so the window stays shut until you look at it"}
    if not times:
        return {"open": True, "opens": None, "last": None, "why": ""}
    now, last = clock.now(), max(times)
    if last > now + timedelta(seconds=FUTURE_SKEW):
        return {**shut, "why": "the runner's record of the last production release lies in the future, so the window "
                               "stays shut until you look at it (orch factory release clear-window, in your terminal, "
                               "stops future-dated times from counting)"}
    try:
        opens = last + timedelta(hours=hours)
    except OverflowError:
        return {**shut, "why": "the release window cannot be worked out"}
    return {"open": now >= opens, "opens": clock.stamp_s(opens), "last": clock.stamp_s(last), "why": ""}


def _resolved_path(ws, epic_id: str, attempt: int) -> Path:
    return _dir(ws, epic_id) / _name("production", epic_id, attempt, "resolved")


def unresolved_production(ws, epic_id: str) -> list[str]:
    """The other epics of this workspace (from the runner's index, not only ticket files) whose production attempt is
    failed, unknown or rolling back (a failed or unknown rollback included), or whose ticket is gone or cannot be read
    while it has production records: no production runs anywhere in the workspace until the human retries it or
    resolves it (resolve). A damaged index holds everything."""
    from orch.core import epics
    holder = lock_holder(ws)
    ids, damaged = production_epics(ws)
    out = ["the runner's release journal (missing or damaged)"] if damaged else []
    for eid in ids:
        if eid.upper() == str(epic_id).upper():
            continue
        us = unit_state(ws, eid, "production", eid, holder)
        if (us.get("rollback") or {}).get("state") == "rolling back":
            out.append(eid)
            continue
        if os.path.lexists(_resolved_path(ws, eid, us["attempt"])):
            continue
        t = _ticket(ws, eid)
        if us["state"] in ("failed", "unknown") or t is None or not epics.is_epic(t):
            out.append(eid)
    return out


def resolve(ws, actor, epic_id: str, reason: str) -> str:
    """Human only: lift the workspace-wide production hold that epic `epic_id`'s unresolved production puts on every
    other epic, WITHOUT letting its own production run again (that stays Retry's). Records who and why."""
    from orch.core import epics
    fs.human_check(actor, "resolving a production hold")
    epic_id = str(epic_id).upper()
    reason = " ".join((reason or "").split())
    if not KEY.fullmatch(epic_id):
        raise UsageError("give the epic whose production holds the others")
    if not reason:
        raise UsageError("say why the hold can be lifted (for example: production checked by hand)")
    us = unit_state(ws, epic_id, "production", epic_id, lock_holder(ws))
    if (us.get("rollback") or {}).get("state") == "rolling back":
        raise ValidationError("the runner is rolling production back right now: wait for its outcome")
    t = _ticket(ws, epic_id)
    if us["state"] not in ("failed", "unknown") and t is not None and epics.is_epic(t):
        raise ValidationError(f"the production of {epic_id} holds nothing (it is {us['state']})")
    if not fs._create(_resolved_path(ws, epic_id, us["attempt"]),
                      {"at": _now(), "by": actor.to_str(), "why": reason[:300], "attempt": us["attempt"]}):
        raise ValidationError("this production attempt was resolved already")
    _event(ws, actor, epic_id, "release.resolved", {"stage": "production", "child": epic_id})
    return (f"the production of {epic_id} no longer holds the other epics; it does not run again unless you retry "
            "it")


def _record_production(ws, epic_id: str, sha: str | None = None) -> None:
    _atomic(_window_path(ws), json.dumps({"at": _now(), "epic": str(epic_id).upper(), "sha": sha}))


def last_released(ws) -> str | None:
    """The commit the workspace's last production attempt released (begun, whatever came of it), from the runner's
    journal, else its window record; None when none names one."""
    for x in reversed(_journal(ws)[0]):
        if x.get("kind", "production") == "production" and isinstance(x.get("sha"), str) and _SHA.fullmatch(x["sha"]):
            return x["sha"]
    body = _read(_window_path(ws), 1024) if os.path.lexists(_window_path(ws)) else None
    sha = (body or {}).get("sha")
    return sha if isinstance(sha, str) and _SHA.fullmatch(sha) else None


BASE_MOVED = ("the base moved since dev was proven: an earlier production released a commit this dev commit does not "
              "contain; run dev again")


def status(ws, epic, d: dict | None, entries=None) -> dict | None:
    """The release of factory epic `epic` (delegation `d`) as records show it, or None when its charter signs none:
    {target, recipe: bool, why, stages: [{name, per, state, units: [...], window}], sensitive, reasons}. Reads only
    (no git). A production stage waiting only for its release window carries `window` (not a reason to stop)."""
    names = target_stages((d or {}).get("release"))
    if not names:
        return None
    rec, why = load(ws)
    holder = lock_holder(ws)
    kids = _units(ws, epic, entries)
    stages = []
    for name in names:
        units = kids if DEFAULT_PER[name] == "child" else [epic.id]
        rows = [{"unit": u, **unit_state(ws, epic.id, name, u, holder)} for u in units]
        if name == "merge":
            rows += _uncounted(ws, epic.id, kids, holder)
        stages.append({"name": name, "per": DEFAULT_PER[name], "units": rows})
    merged = {r["unit"]: r.get("sha") for r in stages[0]["units"] if r["state"] == "proven"}
    for k, s in enumerate(stages[1:], 1):
        prev = stages[k - 1]["units"][0] if stages[k - 1]["per"] == "epic" and stages[k - 1]["units"] else None
        for r in s["units"]:
            if r["state"] != "proven":
                continue
            # dev (and production) went stale when the children it was proven for are not the ones merged now
            if (r.get("children") or {}) != {c: merged.get(c) for c in kids}:
                r.update(state="stale", why=f"the children changed after {s['name']} was proven")
            # production went stale when dev is no longer proven on the commit production ran on
            elif prev is not None and (prev["state"] != "proven" or prev.get("base_sha") != r.get("base_sha")):
                r.update(state="stale", why="dev changed after production was proven")
    for s in stages:
        states = [r["state"] for r in s["units"]]
        s["state"] = ("proven" if states and all(x == "proven" for x in states) else
                      next((x for x in ("unknown", "failed", "stale", "running") if x in states), "waiting"))
    for k, s in enumerate(stages):
        if s["name"] == "production" and s["state"] == "waiting" and all(x["state"] == "proven" for x in stages[:k]):
            spec = next((x for x in (rec or {}).get("stages", []) if x["name"] == "production"), None)
            if spec is not None:
                w = window(ws, spec["window_hours"])
                s["window"] = {**w, "hours": spec["window_hours"]}
            s["held"] = unresolved_production(ws, epic.id)
        if s["name"] == "production" and s["units"]:
            u = s["units"][0]
            s["resolved"] = os.path.lexists(_resolved_path(ws, epic.id, u.get("attempt") or 0))
    waits = _read(_waits_path(ws, epic.id)) if os.path.lexists(_waits_path(ws, epic.id)) else None
    for s in stages:  # the runner's last word on why the next stage waits (stage_hold), while it still waits
        if waits and waits.get("stage") == s["name"] and s["state"] == "waiting":
            s["waits"] = [str(x) for x in (waits.get("why") or [])][:10] or ["its record cannot be read"]
    sens = _sensitive_record(ws, epic.id)
    blocked = _blocked_record(ws, epic.id)
    return {"target": stages[-1]["name"], "recipe": rec is not None, "why": why, "stages": stages, "sensitive": sens,
            "blocked": blocked, "reasons": _reasons(stages, sens, blocked)}


def _reasons(stages, sens, blocked=None) -> list[dict]:
    from orch.core.factory_report import _text
    out = []
    if blocked is not None:
        what = f"the {_text(blocked.get('stage'), 20)} stage of {_text(blocked.get('unit'), 40)}"
        if blocked.get("code") == "rollback-missing":
            out.append({"code": "rollback-missing", "label": "Signed rollback missing",
                        "text": f"{what} did not start: your charter signs a rollback, but the recipe's production stage "
                                "has none now; nothing ran"})
        else:
            out.append({"code": "release-blocked", "label": "Release could not start",
                        "text": f"{what} did not start: {_text(blocked.get('why'), 300)}; nothing ran"})
    if sens is not None:
        hits = sens.get("hits") if isinstance(sens.get("hits"), dict) else {}
        named = "; ".join(f"{_text(c, 40)}: " + ", ".join(_text(p, 120) for p in (ps or [])[:10])
                          for c, ps in list(hits.items())[:10]) or "the record cannot be read"
        out.append({"code": "sensitive", "label": "Sensitive path touched",
                    "text": f"a child branch changes a path the release recipe marks sensitive, or a submodule "
                            f"({named}); nothing was merged"})
    for s in stages:
        for u in s["units"]:
            rb = u.get("rollback") if u["state"] == "failed" else None
            if rb and rb["state"] == "rolled back":
                out.append({"code": "rolled-back", "label": "Production rolled back",
                            "text": "the production check failed, and the recipe's rollback ran and its check passed "
                                    "(as you signed): production is back where it was, as that check says"})
            elif rb and rb["state"] == "rolling back":
                continue  # the runner is rolling back right now (it holds the lock): the outcome comes next
            elif rb and rb["state"] == "rollback failed":
                out.append({"code": "rollback-failed", "label": "Rollback failed",
                            "text": "the production check failed and the recipe's rollback did not prove itself ("
                                    + _text(rb.get("why") or "its check did not pass", 200) + "): production may be "
                                    "broken"})
            elif u["state"] == "failed" and s["name"] == "production" and u.get("failed_at") == "check":
                out.append({"code": "production-failed", "label": "Production check failed",
                            "text": "the production stage's commands ran, but its live check did not pass ("
                                    + _text(u.get("why") or "not proven", 200) + "); nothing was rolled back"})
            elif u["state"] == "failed" and s["name"] == "production":
                out.append({"code": "production-failed", "label": "Production stage failed",
                            "text": "the production stage's commands did not all succeed ("
                                    + _text(u.get("why") or "not proven", 200) + "): look at production now; it may be "
                                    "half-deployed; nothing was rolled back"})
            elif u["state"] == "failed" and s["name"] == "merge" and u.get("conflicts"):
                dbl = [x for x in u.get("doubles") or [] if isinstance(x, dict)]
                who = ("; ".join(f"{' and '.join(_text(c, 40) for c in (x.get('children') or []))} both add "
                                 f"{_text(x.get('path'), 120)}" for x in dbl[:5])
                       + ": two children changed the same file; send one child back") if dbl else (
                    "two changes touched the same file (this child's and one that reached the base since, such as "
                    "another child's merge); send the child back to bring its branch up to date, or merge by hand")
                out.append({"code": "release-conflict", "label": "Merge conflict",
                            "text": f"the merge of {_text(u['unit'], 40)} conflicts in "
                                    + ", ".join(_text(c, 120) for c in u["conflicts"][:10])
                                    + f": {who}; this will not go away on Retry"})
            elif u["state"] == "failed":
                codes = [c for c in (u.get("codes") or []) if c != 0] or [u.get("check")]
                code = codes[0] if codes else None
                out.append({"code": "release-failed", "label": "Release stage failed",
                            "text": f"the {s['name']} stage of {_text(u['unit'], 40)} failed ("
                                    + (f"exit code {code}" if isinstance(code, int) and not u.get("why")
                                       else _text(u.get("why") or f"exit code {code}", 200)) + ")"})
            elif u["state"] == "unknown":
                out.append({"code": "release-unknown", "label": "Release outcome unknown",
                            "text": f"the {s['name']} stage of {_text(u['unit'], 40)} started and its outcome is unknown "
                                    f"({_text(u.get('why') or 'the runner stopped while it ran', 200)})"
                                    + ("; look at production now: it may be half-deployed"
                                       if s["name"] == "production" else "")})
            elif u["state"] == "stale":
                out.append({"code": "release-stale", "label": "Release out of date",
                            "text": f"the {s['name']} stage of {_text(u['unit'], 40)} was proven, but "
                                    f"{_text(u.get('why'), 200)}"})
    return out


def reasons(ws, epic, d) -> list[dict]:
    """The Stopped reasons of the release (factory_report.stopped); a read error is a reason too (fail closed)."""
    try:
        st = status(ws, epic, d)
    except Exception:
        return [{"code": "release-unknown", "label": "Release outcome unknown",
                 "text": "the release records cannot be read"}]
    return st["reasons"] if st else []


# -- the release lock ---------------------------------------------------------------------------------------------------

def _lock_path(ws) -> Path:
    return fs._root() / "release-records" / f"lock-{fs._key(ws, 'release-lock')}"


def lock_holder(ws) -> dict | None:
    """The live holder of the workspace's release lock ({pid, epic, until, pgid}), or None. It holds while the
    dashboard process that took it is alive and its expiry has not passed, or while the process group of the command
    it was running is alive (a dashboard that died does not let another release start beside a running command). A
    lock file that cannot be read holds until it is older than the longest command could run."""
    import time
    p = _lock_path(ws)
    if not os.path.lexists(p):
        return None
    body = _read(p, 1024)
    if body is None:
        try:
            old = time.time() - os.lstat(p).st_mtime > MAX_TIMEOUT + 300
        except OSError:
            return None
        return None if old else {"pid": None, "epic": None, "until": None}
    try:
        until, pid = float(body.get("until")), int(body.get("pid"))
    except (TypeError, ValueError):
        return None
    if until >= time.time() and fs.proc_start(pid) == body.get("pid_start"):
        return body
    pgid = body.get("pgid")
    if isinstance(pgid, int) and pgid > 1 and body.get("pgid_start") and fs.proc_start(pgid) == body["pgid_start"]:
        return body
    return None


def _lock_body(ws, epic_id: str, seconds: int, pgid: int | None = None) -> dict:
    import time
    body = {"pid": os.getpid(), "pid_start": fs.proc_start(os.getpid()), "epic": str(epic_id).upper(),
            "until": time.time() + seconds + 120}
    if pgid:
        body.update(pgid=pgid, pgid_start=fs.proc_start(pgid))
    return body


def _mutex(ws):
    from filelock import FileLock
    p = _lock_path(ws)
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return FileLock(str(p) + ".mx", timeout=10)


def acquire(ws, epic_id: str, seconds: int) -> bool:
    """Take the workspace's release lock for `epic_id`; False while another live holder has it."""
    from filelock import Timeout
    try:
        with _mutex(ws):
            if lock_holder(ws) is not None:
                return False
            try:
                _lock_path(ws).unlink()
            except FileNotFoundError:
                pass
            return fs._create(_lock_path(ws), _lock_body(ws, epic_id, seconds))
    except Timeout:
        return False


def _refresh(ws, epic_id: str, seconds: int, pgid: int | None = None) -> None:
    _atomic(_lock_path(ws), json.dumps(_lock_body(ws, epic_id, seconds, pgid)))


def release_lock(ws) -> None:
    from filelock import Timeout
    try:
        with _mutex(ws):
            h = _read(_lock_path(ws), 1024)
            if h is not None and h.get("pid") == os.getpid():
                _lock_path(ws).unlink()
    except (Timeout, OSError):
        pass


# -- running commands ---------------------------------------------------------------------------------------------------

_RUNNING: dict[int, subprocess.Popen] = {}
_RUNNING_LOCK = threading.Lock()
_STOPPING = threading.Event()


def _tail_of(f, limit: int) -> tuple[str, int]:
    size = f.seek(0, os.SEEK_END)
    f.seek(max(0, size - limit))
    return f.read().decode("utf-8", "replace"), size


def run_command(argv: list[str], cwd: str, env: dict, timeout: int, *, limit: int = TAIL, started=None) -> dict:
    """Run one command: no shell, stdin closed, its own process group (killed whole on timeout, or terminated when
    the dashboard stops: terminate_all), output to unlinked temporary files of which only the last `limit` bytes are
    kept. `started(pgid)` is told the process group once it runs. {code, out, out_size, err, timed_out}."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        if _STOPPING.is_set():
            return {"code": None, "out": "", "out_size": 0, "err": "the dashboard is stopping", "timed_out": False}
        try:
            p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                 start_new_session=True)
        except OSError as e:
            return {"code": None, "out": "", "out_size": 0, "err": f"could not start: {e.strerror or e}",
                    "timed_out": False}
        with _RUNNING_LOCK:
            _RUNNING[p.pid] = p
        timed = False
        try:
            if started is not None:
                try:
                    started(p.pid)
                except Exception:
                    pass
            try:
                code = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed, code = True, None
                _kill(p, signal.SIGKILL)
                p.wait()
        finally:
            with _RUNNING_LOCK:
                _RUNNING.pop(p.pid, None)
        o, size = _tail_of(out, limit)
        e, _ = _tail_of(err, TAIL)
        return {"code": code, "out": o, "out_size": size, "err": e, "timed_out": timed}


def _kill(p, sig) -> None:
    try:
        os.killpg(p.pid, sig)
    except OSError:
        pass


def terminate_all(grace: float = GRACE) -> int:
    """The dashboard is stopping: every running release command's process group gets SIGTERM, then SIGKILL after
    `grace` seconds; no new command starts. Returns how many were running. Their outcomes are recorded as failed."""
    import time
    _STOPPING.set()
    with _RUNNING_LOCK:
        procs = list(_RUNNING.values())
    for p in procs:
        _kill(p, signal.SIGTERM)
    end = time.monotonic() + grace
    for p in procs:
        try:
            p.wait(timeout=max(0.0, end - time.monotonic()))
        except subprocess.TimeoutExpired:
            _kill(p, signal.SIGKILL)
    return len(procs)


def _env(*programs: str) -> dict:
    """The minimal environment of a release command: the runner's allowlist (HOME among it) and a fixed PATH."""
    from orch.core import factory_runner
    env = {k: os.environ[k] for k in factory_runner.ENV_ALLOW if isinstance(os.environ.get(k), str)
           and os.environ[k] and "\n" not in os.environ[k] and "\x00" not in os.environ[k]}
    env["PATH"] = factory_runner.child_path(*programs)
    return env


def expand(argv: list[str], ctx: dict) -> list[str]:
    """`argv` with each placeholder filled in from `ctx` (values validated by _context). A placeholder without a
    value raises: nothing runs."""
    def one(m):
        if ctx.get(m.group(1)) is None:
            raise ValidationError(f"{{{m.group(1)}}} has no value here")
        return ctx[m.group(1)]
    return [_PH.sub(one, w) for w in argv]


def _context(epic_id: str, wsid: str, rec: dict | None = None, child: str | None = None, branch: str | None = None,
             sha: str | None = None) -> dict:
    """The placeholder values, each validated (ids by the ticket id form, branch and base by a strict git ref form,
    sha as hex, the recipe's remote and repo as the recipe validated them); anything else raises."""
    rec = rec or {}
    ctx = {"epic": epic_id, "workspace": wsid, "base": rec.get("base"), "remote": rec.get("remote_url"),
           "repo": rec.get("repo"), "sha": sha}  # sha: the child's commit, or the base commit an epic stage runs on
    if child is not None:
        ctx.update(child=child, branch=branch)
    for k, rx in (("epic", KEY), ("child", KEY), ("sha", _SHA), ("workspace", _WSID), ("repo", _SLUG)):
        if ctx.get(k) is not None and not (isinstance(ctx[k], str) and rx.fullmatch(ctx[k])):
            raise ValidationError(f"the {k} value is not valid")
    for k in ("branch", "base"):
        if ctx.get(k) is not None and not valid_branch(ctx[k]):
            raise ValidationError(f"the {k} name is not valid")
    if ctx.get("remote") is not None and remote_url(ctx["remote"]) is None:
        raise ValidationError("the remote is not valid")
    if child is not None and branch is None:
        raise ValidationError("the branch name is not valid")
    return ctx


# -- the runner's own release repository ----------------------------------------------------------------------------------

# Written by the runner, every time it uses the release repository: no includes, aliases, hooks, fsmonitor, filters or remotes.
REPO_CONFIG = ("[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n"
                 "\thooksPath = /dev/null\n\tfsmonitor = false\n\tsymlinks = true\n")


def repo_dir(ws) -> Path:
    from orch.core.ledger import workspace_id
    return fs._root() / "release-repos" / workspace_id(ws) / "repo"


def _git_flags(rec: dict) -> list[str]:
    """What every executor git call carries: no hooks, no fsmonitor, plain ssh, no credential helper unless the
    recipe's (human-owned) git_config names one, and no replace objects."""
    flags = ["--no-replace-objects", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-c", "core.attributesFile=/dev/null", "-c", "core.sshCommand=ssh", "-c", "credential.helper="]
    for k in GIT_CONFIG_KEYS:
        v = (rec.get("git_config") or {}).get(k)
        if v is not None:
            flags += ["-c", f"{k}={v}"]
    return flags


def _isolation(ws) -> tuple[dict, Path]:
    """No user or system git config at all: the user's global files (~/.gitconfig, ~/.config/git/*) are writable by
    the agents' processes (same user), and `-c` cannot undo what they add (url.*.insteadOf, filters, attributes).
    ({GIT_CONFIG_GLOBAL: an empty file the runner writes now, GIT_CONFIG_NOSYSTEM, GIT_ATTR_NOSYSTEM}, an empty home
    folder the runner owns), both beside the release repository."""
    import shutil
    d = repo_dir(ws).parent
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    home = d / "git-home"
    if os.path.islink(home) or (os.path.lexists(home) and (not home.is_dir() or any(os.scandir(home)))):
        if home.is_dir() and not os.path.islink(home):
            shutil.rmtree(home)
        else:
            home.unlink()
    home.mkdir(mode=0o700, exist_ok=True)
    empty = d / "git-global"
    _atomic(empty, "")
    return {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1"}, home


def _git_env(ws, git: str) -> dict:
    """The executor's git environment: the allowlist without XDG_CONFIG_HOME, HOME set to the runner's empty home,
    no user or system config (_isolation), no replace objects, no prompt. Never the human's own git config."""
    iso, home = _isolation(ws)
    env = {k: v for k, v in _env(git).items() if k != "XDG_CONFIG_HOME"}
    env.update(iso, HOME=str(home), GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0")
    return env


def command_env(ws, *programs: str) -> dict:
    """The recipe commands' environment: the allowlist (HOME and XDG_CONFIG_HOME kept, so gh finds its own config)
    and no user or system git config for any git they run (attributes file off too, through git's own environment
    config: GIT_CONFIG_COUNT/KEY/VALUE are set by the runner, never forwarded)."""
    iso, _ = _isolation(ws)
    return {**_env(*programs), **iso, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.attributesFile",
            "GIT_CONFIG_VALUE_0": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}


def _git(ws, rec: dict, *args: str, timeout: int = 600, limit: int = TAIL) -> dict:
    """One git command in the release repository, by argv; raises ReleaseError when git cannot be found."""
    from orch.core import factory_runner
    git = factory_runner.resolve_bin("git")
    if git is None:
        raise ReleaseError("git was not found at a trusted path")
    repo = repo_dir(ws)
    return run_command([git, *_git_flags(rec), "-C", str(repo), *args], str(repo), _git_env(ws, git), timeout,
                       limit=limit)


def ensure_repo(ws, rec: dict) -> Path:
    """The runner's repository, created on first use (`git init` without templates) and its config written by the
    runner every time. Raises ReleaseError on any failure."""
    from orch.core import factory_runner
    repo = repo_dir(ws)
    try:
        if not os.path.isdir(repo / ".git") or os.path.islink(repo) or os.path.islink(repo / ".git"):
            git = factory_runner.resolve_bin("git")
            if git is None:
                raise ReleaseError("git was not found at a trusted path")
            repo.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            r = run_command([git, "-c", "core.hooksPath=/dev/null", "init", "-q", "--template=", str(repo)],
                            str(repo.parent), _git_env(ws, git), 60)
            if r["code"] != 0:
                raise ReleaseError("the runner's repository could not be created")
        _atomic(repo / ".git" / "config", REPO_CONFIG)
        for extra in ("hooks", "info"):
            import shutil
            shutil.rmtree(repo / ".git" / extra, ignore_errors=True)
    except OSError as e:
        raise ReleaseError(f"the runner's repository cannot be prepared ({type(e).__name__})") from None
    return repo


def workspace_repo(ws) -> Path | None:
    """The top folder of the workspace's git checkout (the one holding `.git`), or None. Only fetched from."""
    root = Path(ws.root).resolve()
    for p in (root, *root.parents):
        if os.path.lexists(p / ".git"):
            return p
    return None


def _rev(ws, rec, ref: str) -> str | None:
    r = _git(ws, rec, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    sha = (r.get("out") or "").strip()
    return sha if r.get("code") == 0 and _SHA.fullmatch(sha) else None


def fetch_child(ws, rec, branch: str, src: Path | None = None, child: str | None = None,
                ns: str = "release-heads") -> str | None:
    """Fetch `branch` from `src` (the child's runner-made clone, see child_source; else the workspace checkout) into
    the release repository (objects and that one ref, nothing else), and return its commit, or None. From a clone
    (anything but the workspace checkout), only for `child` and through factory_clones.fetch_from: under the clone's
    lock, the clone checked again and its config written again first. `ns`: the ref folder it lands in (the Ready
    report's check of what was built uses its own, so it never touches a ref a release stage compares)."""
    from orch.core import factory_clones
    top = workspace_repo(ws)
    src = src or top
    if src is None or not valid_branch(branch):
        return None

    def fetch(where: Path) -> str | None:
        r = _git(ws, rec, "fetch", "-q", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
                 str(where), f"+refs/heads/{branch}:refs/{ns}/{branch}")
        return _rev(ws, rec, f"refs/{ns}/{branch}") if r.get("code") == 0 else None
    if top is not None and Path(src) == top:
        return fetch(top)
    if child is None:
        return None
    return factory_clones.fetch_from(ws, child, lambda p: fetch(p) if p == Path(src) else None)


def fetch_base(ws, rec) -> str | None:
    """Fetch the recipe's base from the recipe's remote (never from the workspace) into a ref only the runner writes,
    and return its commit, or None."""
    base = rec["base"]
    r = _git(ws, rec, "fetch", "-q", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
             rec["remote_url"], f"+refs/heads/{base}:refs/remotes/release/{base}")
    return _rev(ws, rec, f"refs/remotes/release/{base}") if r.get("code") == 0 else None


def checkout(ws, rec, sha: str) -> bool:
    """The release repository's work tree at exactly `sha`, clean (nothing left from an earlier stage)."""
    if not _SHA.fullmatch(sha or ""):
        return False
    for args in (("checkout", "-q", "--force", "--detach", sha), ("clean", "-q", "-ffdx")):
        if _git(ws, rec, *args).get("code") != 0:
            return False
    return True


def child_source(ws, t) -> tuple[str | None, Path | None, str]:
    """(branch, the repository to fetch it from, "") of child `t`: for a child the runner made a clone for, the
    branch and clone its record names (the runner's record, never a ticket field); else child_branch from the
    workspace checkout, but only for a child the runner never gave a clone and never would: a child with a linked
    worktree of its own, or one in a workspace that is not a checkout clones are made from. A clone record that is
    gone, damaged or was cleaned (or a clone that failed) never falls back to the agent-written ticket fields.
    (None, None, why) otherwise."""
    from orch.core import factory_clones, factory_runner
    rec = factory_clones.record(ws, t.id)
    if rec is not None:  # checked as the commit gate checks it (no link, the pinned folder and .git) before a fetch
        path, why = factory_clones.verify(ws, t.id)
        return (rec["branch"], path, "") if path is not None else (None, None, why)
    if (factory_clones.ever_had_clone(ws, t.id)
            or (factory_clones.clonable(ws)[0] is not None and factory_runner.own_worktree_dir(ws, t) is None)):
        return None, None, f"the runner's clone record of {t.id} is missing"
    branch, why = child_branch(ws, t)
    return branch, workspace_repo(ws) if branch else None, why


def child_branch(ws, t) -> tuple[str | None, str]:
    """(branch, "") of child `t`: the one branch it names (`orch link --branch`), else the branch of its one worktree;
    the name must be a valid branch name that names the child (its id as a word). (None, why) otherwise. Agent
    written, so it is only a name the runner then fetches and pins by commit itself."""
    from orch.core import factory_runner
    names = {v for v in (t.meta.get("branches") or {}).values() if isinstance(v, str)} if isinstance(
        t.meta.get("branches"), dict) else set()
    if not names:
        start = factory_runner.start_dir(ws, t)
        if start and Path(start) != Path(ws.root).resolve():
            b = factory_runner._branch_of(Path(start))
            if b:
                names = {b}
    if len(names) != 1:
        return None, f"{t.id} names {'no' if not names else 'more than one'} branch"
    (b,) = names
    if not valid_branch(b):
        return None, f"{t.id} names a branch that is not a plain branch name"
    if not re.search(rf"(?<![a-z0-9]){re.escape(t.id.lower())}(?![a-z0-9])", b.lower()):
        return None, f"{t.id}'s branch does not name {t.id}"
    return b, ""


def changed_paths(ws, rec, sha: str) -> tuple[list[str], list[str]] | None:
    """(every path the commit brings in against the remote base, the gitlinks among them), in the release repository:
    the net diff and each commit's own changes (merges against each parent), renames as both paths, submodules
    included. A gitlink is a path whose mode is 160000 before or after any of those changes (a submodule added,
    changed or removed). None when git fails or the list is too long to check."""
    base = f"refs/remotes/release/{rec['base']}"
    common = ("--no-renames", "--no-ext-diff", "--no-textconv", "--ignore-submodules=none", "--raw", "-z")
    out: list[str] = []
    links: list[str] = []
    for args in (("diff", *common, f"{base}...{sha}"),
                 ("log", *common, "-m", "--format=", f"{base}..{sha}")):
        r = _git(ws, rec, *args, limit=GIT_OUT)
        if r.get("code") != 0 or r.get("out_size", 0) > GIT_OUT:
            return None
        words = [w.strip("\n") for w in (r.get("out") or "").split("\x00")]
        i = 0
        while i < len(words):  # ":<old mode> <new mode> <old> <new> <status>", then the path (--no-renames: one)
            meta = words[i]
            if not meta.startswith(":"):
                i += 1
                continue
            path = words[i + 1] if i + 1 < len(words) else ""
            if not path:
                return None
            out.append(path)
            if "160000" in meta[1:].split()[:2]:
                links.append(path)
            i += 2
    return sorted(set(out)), sorted(set(links))


# What the release treats as sensitive in every repository, whatever the recipe says (patterns as in sensitive_paths):
# the harness's settings, hooks, skills and MCP servers, the instructions agents read, and CI. A child that changes
# any of them changes what runs agents or code later, so a human merges that by hand.
HARNESS_SENSITIVE = ("**/.claude", "**/.mcp.json", "**/CLAUDE.md", "**/CLAUDE.local.md", "**/AGENTS.md", ".github",
                     "**/.gitmodules")


def always_sensitive(ws) -> list[str]:
    """What the release treats as sensitive whatever the recipe says: first the workspace's orch folder (its tickets,
    state and config) as a path of the repository (normally `orchestrator`; a child's clone carries a copy of it that
    the session's file tools can write; tickets change only through orch, never through a merge), then
    HARNESS_SENSITIVE."""
    top = workspace_repo(ws)
    try:
        rel = Path(ws.home).resolve().relative_to(top.resolve()).as_posix() if top else None
    except ValueError:
        rel = None
    return [rel or Path(ws.home).name, *HARNESS_SENSITIVE]


MAX_MESSAGES = 500  # commits a child branch may bring in for the message check; more is refused


def message_refusal(ws, rec: dict, sha: str) -> str | None:
    """Why a commit the branch brings in (`base..sha`, in the release repository) has a message orch's commit-msg
    check refuses (the workspace's commit format, no AI attribution), or None. A child's clone runs no hooks, so
    this is where its messages are checked. Each commit is listed by its id (`rev-list`) and its message read on its
    own from the raw commit object (`cat-file commit`, everything after the header's blank line): no separator a
    message could contain decides where one message ends. Unreadable, or more than MAX_MESSAGES, is a refusal."""
    from orch.hooks.commit_msg import check_message
    r = _git(ws, rec, "rev-list", f"refs/remotes/release/{rec['base']}..{sha}", limit=GIT_OUT)
    if r.get("code") != 0 or r.get("out_size", 0) > GIT_OUT:
        return "its commits could not be listed"
    shas = (r.get("out") or "").split()
    if len(shas) > MAX_MESSAGES or not all(_SHA.fullmatch(c) for c in shas):
        return f"it brings in more than {MAX_MESSAGES} commits, or an unreadable list of them"
    for commit in shas:
        c = _git(ws, rec, "cat-file", "commit", commit, limit=GIT_OUT)
        raw = c.get("out") or ""
        if c.get("code") != 0 or c.get("out_size", 0) > GIT_OUT or "\n\n" not in raw:
            return f"the message of commit {commit[:12]} could not be read"
        problems = check_message(ws, raw.split("\n\n", 1)[1])
        if problems:
            return f"commit {commit[:12]} has a message orch's commit-msg check refuses ({problems[0]})"
    return None


def classify(ws, rec: dict, kids: list) -> tuple[dict, dict, dict]:
    """({child: (branch, sha, source, base sha)}, {child: [sensitive paths]}, {child: why it could not be checked})
    for every child ticket in `kids`, in the runner's release repository against the base fetched from the recipe's
    remote. A branch that brings in no commit of its own (its commit is on the base already) is refused."""
    found, hits, errors = {}, {}, {}
    try:
        ensure_repo(ws, rec)
        base_sha = fetch_base(ws, rec)
    except ReleaseError as e:
        return {}, {}, {t.id: str(e) for t in kids}
    if base_sha is None:
        return {}, {}, {t.id: "the base could not be fetched from the recipe's remote" for t in kids}
    for t in kids:
        branch, src, why = child_source(ws, t)
        if branch is None:
            errors[t.id] = why
            continue
        if branch == rec["base"]:
            errors[t.id] = f"{t.id}'s branch is the base branch"
            continue
        sha = fetch_child(ws, rec, branch, src, t.id)
        if sha is None:
            errors[t.id] = f"{t.id}'s branch could not be fetched from {src}"
            continue
        own = _git(ws, rec, "rev-list", "--count", f"refs/remotes/release/{rec['base']}..{sha}")
        if own.get("code") != 0 or (own.get("out") or "").strip() in ("", "0"):
            errors[t.id] = f"{t.id}: the child's branch has no commits of its own"
            continue
        got = changed_paths(ws, rec, sha)
        if got is None:
            errors[t.id] = f"the changes of {t.id}'s branch could not be listed"
            continue
        paths, links = got
        # a submodule (a gitlink, or .gitmodules, always sensitive) is never merged by itself: its content is another
        # repository's, which the release never inspects
        bad = [f"{p} (a submodule)" for p in links]
        bad += [p for p in paths if p not in links and sensitive(p, [*rec["sensitive_paths"], *always_sensitive(ws)])]
        if bad:
            hits[t.id] = bad[:20]
        why = None if bad else message_refusal(ws, rec, sha)  # a sensitive hit stops the release anyway
        if why:
            errors[t.id] = f"{t.id}'s branch: {why}"
            continue
        found[t.id] = (branch, sha, src, base_sha)
    return found, hits, errors


# -- the gate and the tick ---------------------------------------------------------------------------------------------

def _ticket(ws, ref):
    from orch.core import store
    try:
        return store.read_ticket(store.resolve(ws, ref).path)
    except Exception:
        return None


def gate(ws, epic_id: str, did: str | None = None):
    """(epic, delegation, ready report) when the release of `epic_id` may run its next command now, else None:
    the factory and Dark on, the ledger whole, a live armed Dark charter that signs a release (the same delegation
    as `did`), the budget not used up, no open request, no Stopped reason other than the release's own, and Ready.
    Read fresh; any error is a no."""
    from orch.core import factory_report, ledger, permits
    try:
        if not permits.enabled(ws) or not ledger.head_ok() or not permits.dark_on(ws):
            return None
        epic = _ticket(ws, epic_id)
        if epic is None or epic.status != "open":
            return None
        signed = ledger.entries(ws)
        d = permits.factory_delegation(ws, epic, signed)
        if (d is None or (did is not None and d["id"] != did) or not d.get("dark") or not stage_of(d.get("release"))
                or not d["active"] or not fs.armed(ws, d["id"]) or permits.budget_reason(ws, epic, d)):
            return None
        if any(str(r["epic"]).upper() == epic.id.upper() for r in permits.open_requests(ws, signed)):
            return None
        if any(r["code"] not in RELEASE_CODES for r in factory_report.stopped(ws, epic, signed=signed)):
            return None
        rep = factory_report.ready(ws, epic, signed=signed)
        return (epic, d, rep) if rep is not None else None
    except Exception:
        return None


def _write(p: Path, body: dict) -> bool:
    return fs._create(p, body)


def stage_hold(ws, epic, stage: str) -> list[str]:
    """Why release stage `stage` (merge, dev or production) may not start now though the epic is Ready: what an
    unattended close refuses too, read fresh. For every child in testing: evidence that does not meet the strict rules
    (evidence.strict_missing); before the merge also work not committed in its clone, a submodule in its clone, or a
    clone whose state cannot be read (after the merge the merged commit is fixed: dev and production never read a
    clone). [] when nothing holds. (Two children adding the same file is checked before the merge, release_doubles.)"""
    from orch.core import epics, evidence, factory_built
    from orch.core.factory_report import _text
    out = []
    for e in epics.children(ws, epic.id):
        if e.status != "testing":
            continue
        t = _ticket(ws, e.id)
        if t is None:
            out.append(f"{e.id} cannot be read")
            continue
        for n, why in evidence.strict_missing(t):
            out.append(f"the evidence of {t.id}" + (f" for AC{n}" if n else "") + " does not meet the close rules: "
                       + _text(why, 160))
        st = factory_built.uncommitted(ws, t.id) if stage == "merge" else None
        if st is not None and not st["ok"]:
            out.append(f"the state of {t.id}'s clone could not be read")
        elif st is not None:
            if st["lines"]:
                out.append(f"{t.id} has uncommitted work in its clone")
            if st["submodules"]:
                out.append(f"{t.id}'s clone holds a submodule ({', '.join(st['submodules'][:5])}), which the release "
                           "does not merge")
    return out


def _waits_path(ws, epic_id: str) -> Path:
    return _dir(ws, epic_id) / "waits.json"


def _set_waits(ws, epic_id: str, stage: str | None, why: list[str]) -> None:
    """The runner's record of why the next stage waits (stage_hold), or none (stage None)."""
    p = _waits_path(ws, epic_id)
    if stage is None:
        with contextlib.suppress(FileNotFoundError):
            p.unlink()
        return
    _atomic(p, json.dumps({"stage": stage, "why": why[:10], "at": _now()}))


def _event(ws, actor, epic_id, kind, data) -> None:
    from orch.core.events import append_event
    append_event(ws, epic_id, kind, actor, data)


def tick(ws, actor, run=None) -> list[str]:
    """One release round over every epic; returns what it did, one line each. Only a human process (the dashboard
    the human started) runs it. `run`: run_command, or a stand-in in tests (for the recipe's commands only: the
    runner's own git always runs for real)."""
    from orch.core import epics, permits, store
    fs.human_check(actor, "running a factory release")
    if not permits.enabled(ws) or _STOPPING.is_set():
        return []
    run = run or run_command
    lines: list[str] = []
    for entry in store.scan(ws):
        if entry.meta is None or not epics.is_epic(entry.meta) or entry.status != "open":
            continue
        g = gate(ws, entry.id)
        if g is None:
            continue
        lines += _release(ws, actor, g, run)
    return lines


def _release(ws, actor, g, run) -> list[str]:
    from orch.core.ledger import workspace_id
    epic, d, rep = g
    rec, why = load(ws)
    st = status(ws, epic, d)
    if any(r["code"] != "release-stale" for r in st["reasons"]):
        return []  # stopped: the Stopped card says why (an out-of-date stage waits for the human's retry by itself)
    stages = up_to(rec, d["release"])
    if stages is None:
        have = {s["name"] for s in (rec or {}).get("stages", [])}
        missing = next((n for n in target_stages(d["release"]) if n not in have), "merge")
        return [_block(ws, actor, epic.id, missing, epic.id,
                       why or f"the release recipe has no {missing} stage, which your charter signs")]
    # the children as the release records count them: in testing, and done since with a proven merge (a verdict
    # given after the merge must not make dev out of date)
    testing = {r["id"] for r in rep["children"] if r["status"] == "testing"}
    kids = _units(ws, epic)
    if not acquire(ws, epic.id, max(s["timeout"] for s in stages)):
        return [f"{epic.id}: the release waits: another release holds the lock"]
    try:
        stale = _mark_stale(ws, rec, epic, [k for k in kids if k in testing])
        if stale or all(s["state"] == "proven" for s in st["stages"]):
            return stale
        return _run_stages(ws, actor, epic, d, rec, stages, kids, workspace_id(ws), run)
    except ReleaseError as e:
        return [f"{epic.id}: no release this round: {e}"]
    finally:
        release_lock(ws)


def _mark_stale(ws, rec, epic, kids) -> list[str]:
    """A child whose merge was proven but whose branch now points elsewhere (it came back with more work), read where
    child_source says (its clone, or its own worktree's branch in the workspace; never a ticket field in place of a
    missing clone record), gets a stale record: the release is out of date and the human decides."""
    lines = []
    for k in kids:
        us = unit_state(ws, epic.id, "merge", k)
        if us["state"] != "proven" or not us.get("sha"):
            continue
        t = _ticket(ws, k)
        branch, src, _ = child_source(ws, t) if t is not None else (None, None, "")
        if branch is None:
            continue
        ensure_repo(ws, rec)
        tip = fetch_child(ws, rec, branch, src, k)
        if tip is not None and tip != us["sha"]:
            _write(_dir(ws, epic.id) / _name("merge", k, us["attempt"], "stale"), {"was": us["sha"], "now": tip})
            lines.append(f"{epic.id}: the merge of {k} is out of date: its branch changed after it was merged")
    return lines


def _run_stages(ws, actor, epic, d, rec, stages, kids, wsid, run) -> list[str]:
    lines: list[str] = []
    ddir = _dir(ws, epic.id)
    holder = lock_holder(ws)
    if any(unit_state(ws, epic.id, s["name"], u, holder)["state"] not in ("waiting", "proven")
           for s in stages for u in (kids if s["per"] == "child" else [epic.id])) or _sensitive_record(ws, epic.id):
        return lines
    # diff classification, before the first merge command: every child not yet merged, all at once
    open_kids = [k for k in kids if unit_state(ws, epic.id, "merge", k, holder)["state"] != "proven"]
    found: dict = {}
    if open_kids:
        tickets = [t for t in (_ticket(ws, k) for k in open_kids) if t is not None]
        if len(tickets) != len(open_kids):
            return lines + [f"{epic.id}: a child cannot be read; no release this round"]
        found, hits, errors = classify(ws, rec, tickets)
        if hits:
            _write(ddir / "sensitive.json", {"hits": hits, "at": _now()})
            return lines + [f"{epic.id}: release stopped: a sensitive path is touched; nothing was merged"]
        if not errors:  # every file the epic names must be in a child's commit before anything is merged
            from orch.core import factory_built
            refused = factory_built.merge_refusal(ws, rec, epic, kids, found)
            if refused:
                return lines + [_block(ws, actor, epic.id, "merge", refused[0], refused[1])]
            dups, why = factory_built.release_doubles(ws, rec, epic, kids, found)
            if why or dups:  # two children add the same file: the second merge would conflict
                unit = next((c for c in reversed(dups[0]["children"]) if c in found), open_kids[0]) if dups \
                    else open_kids[0]
                text = (f"{factory_built.double_text(dups)}: the release will conflict; remove it from one child "
                        "(send it back) and Retry" if dups else why)
                return lines + [_block(ws, actor, epic.id, "merge", unit, text)]
        if errors:
            k = sorted(errors)[0]
            n = unit_state(ws, epic.id, "merge", k, holder)["attempt"] + 1
            body = {"stage": "merge", "unit": k, "epic": epic.id, "attempt": n, "started": _now()}
            if n <= MAX_ATTEMPTS:
                _journal_add(ws, {"kind": "intent", "epic": epic.id, "stage": "merge", "unit": k, "attempt": n})
            if n <= MAX_ATTEMPTS and _write(ddir / _name("merge", k, n, "intent"), body):
                _write(ddir / _name("merge", k, n, "outcome"),
                       {**body, "codes": [], "check": None, "proven": False, "ended": _now(), "tail": "",
                        "why": errors[k]})
                _event(ws, actor, epic.id, "release.stage", {"stage": "merge", "child": k, "proven": False,
                                                             "exit": None})
            return lines + [f"{epic.id}: merge not started: {errors[k]}"]
    for s in stages:
        for unit in (kids if s["per"] == "child" else [epic.id]):
            us = unit_state(ws, epic.id, s["name"], unit, lock_holder(ws))
            if us["state"] == "proven":
                continue
            if us["state"] != "waiting":
                return lines
            if s["per"] == "epic":
                # every stage before it proven as the records show it now, out-of-date ones not (dev out of date
                # never lets production run), and production never before its release window opens
                now = {x["name"]: x for x in status(ws, epic, d)["stages"]}
                if any(now[n]["state"] != "proven" for n in STAGES[:STAGES.index(s["name"])]):
                    return lines
                if s["name"] == "production" and (not window(ws, s["window_hours"])["open"]
                                                  or unresolved_production(ws, epic.id)):
                    return lines  # waiting, not stopped: the run view shows when it opens or what holds it
            # an irreversible stage starts only on what an unattended close would accept (stage_hold)
            hold = stage_hold(ws, epic, s["name"])
            _set_waits(ws, epic.id, s["name"] if hold else None, hold)
            if hold:
                return lines + [f"{epic.id}: {s['name']} waits: {hold[0]}"]
            line, proven = _attempt(ws, actor, epic, d, rec, s, unit, us["attempt"] + 1, found, kids, wsid, run)
            lines.append(line)
            if not proven:
                return lines
    return lines


def _now() -> str:
    from orch.clock import stamp_s
    return stamp_s()


def _steps_of(s: dict, ctx: dict) -> list:
    """[(kind, argv, expect)] of a stage (or a rollback): the precheck, the commands, the check, placeholders filled."""
    def exp(chk):
        return expand([chk["expect"]], ctx)[0] if chk["expect"] is not None else None
    steps = [("precheck", expand(s["precheck"]["argv"], ctx), exp(s["precheck"]))] if s.get("precheck") else []
    steps += [("command", expand(c, ctx), None) for c in s["commands"]]
    return steps + [("check", expand(s["check"]["argv"], ctx), exp(s["check"]))]


def _pin_steps(rec, steps) -> tuple[list | None, dict, str]:
    """The steps with each program replaced by its pinned real path, ({word: path}), or (None, {}, why)."""
    progs = {}
    for _, argv, _ in steps:
        real, why = pinned(rec, argv[0])
        if real is None:
            return None, {}, why
        progs[argv[0]] = real
    return [(k, [progs[a[0]], *a[1:]], e) for k, a, e in steps], progs, ""


def _run_steps(ws, epic, d, s, steps, run, env, cwd, moved=None) -> dict:
    """Run `steps` in order, each only while the gate still holds (and `moved()` says nothing moved). {codes, tail,
    why, check, proven, failed_at}: failed_at is "check" only when every command ran and exited 0 and the check alone
    did not pass (the one case a signed rollback answers)."""
    codes, tail, why, check_code, proven, failed_at, interrupted = [], "", "", None, False, None, False
    for kind, argv, expect in steps:
        if gate(ws, epic.id, d["id"]) is None:
            why = "stopped before the next command: the epic may no longer release (paused, edited, out of budget, " \
                  "switched off or the ledger cut)"
            break
        if moved is not None and moved():
            why = "stopped before the next command: the branch moved since it was checked"
            break
        _refresh(ws, epic.id, s["timeout"])
        r = run(argv, cwd, env, s["timeout"],
                started=lambda pgid, t=s["timeout"]: _refresh(ws, epic.id, t, pgid))
        tail += f"$ {' '.join(argv)}\n{r.get('out') or ''}{r.get('err') or ''}"
        stopping = _STOPPING.is_set()
        if stopping:
            why = "the dashboard stopped while it ran"
        if r.get("timed_out"):
            why = f"timed out after {s['timeout']} seconds"
        # a command killed midway may have done part of its work (a check only reads): its outcome is unknown
        interrupted = interrupted or stopping or (bool(r.get("timed_out")) and kind != "check")
        out_ok = expect is None or (r.get("out_size", 0) <= TAIL and (r.get("out") or "").strip() == expect)
        if kind == "check":
            check_code = r.get("code")
            proven = check_code == 0 and not r.get("timed_out") and out_ok and not why
            if check_code == 0 and not out_ok and not why:
                why = "the check's output is not what the recipe expects"
            if not proven and not stopping:
                failed_at = "check"
                why = why or f"the check exited with {check_code}"
        elif kind == "precheck":
            codes.append(r.get("code"))
            if r.get("code") != 0 or not out_ok:
                why = why or "the precheck refused (for example a pull request open against another base)"
                failed_at = "precheck"
                break
        else:
            codes.append(r.get("code"))
            if r.get("code") != 0:
                why = why or f"exit code {r.get('code')}"
                failed_at = "command"
                break
        if why:
            break
    return {"codes": codes, "tail": tail, "why": why, "check": check_code, "proven": proven, "failed_at": failed_at,
            "interrupted": interrupted and not proven}


def _attempt(ws, actor, epic, d, rec, s, unit, n, found, kids, wsid, run) -> tuple[str, bool]:
    """One attempt at stage `s` for `unit`, in the runner's release repository: the intent first, then the precheck,
    the commands and the check, then the outcome; for production whose live check failed under a charter that signs
    `rollback`, then the recipe's rollback and its check, with records of their own."""
    from orch.core.factory_report import _full
    name = s["name"]
    child = s["per"] == "child"

    def block(why, code="release-blocked"):  # a cause the runner cannot get past by itself: a Stopped reason
        return _block(ws, actor, epic.id, name, unit, why, code), False
    if name == "production" and d.get("rollback") and not s.get("rollback"):
        return block("your charter signs a rollback, but the recipe's production stage has none", "rollback-missing")
    # the work area: the checked commit for a child stage; for dev, the remote base as it is now (the merged work);
    # for production, exactly the commit dev was proven on
    extra: dict = {}
    src = None
    try:
        if child:
            if unit not in found:
                return f"{epic.id}: {name} of {unit} not started: its branch was not checked this round", False
            branch, sha, src, base_of = found[unit]
            ctx = _context(epic.id, wsid, rec, unit, branch, sha)
            extra["base_sha"] = base_of  # what the child was classified against (factory_close: never the base)
        else:
            if name == "production":
                dev = unit_state(ws, epic.id, "dev", epic.id)
                base_sha = dev.get("base_sha") if dev["state"] == "proven" else None
                if not isinstance(base_sha, str) or not _SHA.fullmatch(base_sha):
                    return block("no proven dev commit to release")
                ensure_repo(ws, rec)
                if fetch_base(ws, rec) is None:
                    return block("the base could not be fetched from the recipe's remote")
                last = last_released(ws)  # never release a commit older than what production already has
                if last and last != base_sha and _git(ws, rec, "merge-base", "--is-ancestor", last,
                                                       base_sha).get("code") != 0:
                    _write(_dir(ws, epic.id) / _name("dev", epic.id, dev["attempt"], "stale"),
                           {"why": BASE_MOVED, "was": base_sha, "released": last})
                    return f"{epic.id}: production of {unit} not started: {BASE_MOVED}", False
                extra.update(children=dict(dev.get("children") or {}))
            else:
                base_sha = fetch_base(ws, rec)
                if base_sha is None:
                    return block("the base could not be fetched from the recipe's remote")
                extra.update(children={k: unit_state(ws, epic.id, "merge", k).get("sha") for k in kids})
            extra["base_sha"] = base_sha
            ctx = _context(epic.id, wsid, rec, sha=base_sha)
        steps = _steps_of(s, ctx)
        rollback = s.get("rollback") if name == "production" and d.get("rollback") else None
        rb_steps = _steps_of(rollback, ctx) if rollback else []
    except ValidationError as e:
        return block(str(e))
    steps, progs, why = _pin_steps(rec, steps)
    if steps is None:
        return block(why)
    if rollback:  # a signed rollback must be runnable before production starts at all
        rb_steps, rb_progs, why = _pin_steps(rec, rb_steps)
        if rb_steps is None:
            return block(f"the rollback cannot run: {why}")
        progs = {**progs, **rb_progs}
    if n > MAX_ATTEMPTS or gate(ws, epic.id, d["id"]) is None:
        return f"{epic.id}: {name} of {unit} not started: the epic may not release now", False
    if child:
        if fetch_child(ws, rec, ctx["branch"], src, unit) != ctx["sha"] or not checkout(ws, rec, ctx["sha"]):
            return f"{epic.id}: {name} of {unit} not started: its branch moved since it was checked", False
        extra["sha"] = ctx["sha"]
    elif not checkout(ws, rec, extra["base_sha"]):
        return block("the commit to release cannot be checked out")
    ddir = _dir(ws, epic.id)
    digest = "sha256:" + hashlib.sha256(json.dumps([a for _, a, _ in steps]).encode("utf-8")).hexdigest()
    intent = {"stage": name, "unit": unit, "epic": epic.id, "attempt": n, "argv_sha": digest, "started": _now(),
              **extra}
    # with the intent, in the runner's journal: the hold, the window and the blocks never depend on ticket files, and a
    # journal that goes missing while records exist blocks every release
    _journal_add(ws, {"kind": "production" if name == "production" else "intent", "epic": epic.id, "stage": name,
                      "unit": unit, "attempt": n, **({"sha": extra["base_sha"]} if name == "production" else {})})
    if not _write(ddir / _name(name, unit, n, "intent"), intent):
        return f"{epic.id}: {name} of {unit}: another runner started it", False
    if name == "production":  # the window's clock starts when production's commands begin, whatever comes of them
        _record_production(ws, epic.id, extra["base_sha"])
    env = command_env(ws, *progs.values())
    cwd = str(repo_dir(ws))
    moved = (lambda: fetch_child(ws, rec, ctx["branch"], src, unit) != ctx["sha"]) if child else None
    r = _run_steps(ws, epic, d, s, steps, run, env, cwd, moved)
    proven = r["proven"]
    conf = conflicts(r["tail"]) if not proven and name == "merge" else []
    outcome = {**intent, "codes": r["codes"], "check": r["check"], "proven": proven, "ended": _now(),
               "tail": _full(r["tail"])[-TAIL:], "why": r["why"], "failed_at": r["failed_at"],
               "interrupted": r["interrupted"], **({"conflicts": conf} if not proven and name == "merge" else {}),
               **({"doubles": _conflict_doubles(ws, rec, epic, kids, found, conf)} if conf else {})}
    _write(ddir / _name(name, unit, n, "outcome"), outcome)
    bad = next((c for c in r["codes"] if c != 0), r["check"])
    _event(ws, actor, epic.id, "release.stage", {"stage": name, "child": unit, "proven": proven,
                                                 "exit": bad if isinstance(bad, int) else None})
    line = f"{epic.id}: {name} of {unit} " + ("proven" if proven else f"failed ({r['why'] or 'not proven'})")
    if rollback and not proven and r["failed_at"] == "check":
        line += "; " + _rollback(ws, actor, epic, d, s, unit, n, rb_steps, run, env, cwd)
    return line, proven


def _conflict_doubles(ws, rec, epic, kids, found, conf: list[str]) -> list[dict]:
    """The conflicting paths that two children add ([{path, children}], factory_built.double_adds): only then does
    the reason say two children changed the same file. [] when none, or when it cannot be told."""
    from orch.core import factory_built
    try:
        dups, why = factory_built.release_doubles(ws, rec, epic, kids, found)
    except Exception:
        return []
    paths = {c.rsplit(" (", 1)[0] for c in conf}
    return [] if why else [x for x in dups if x["path"] in paths][:10]


def _rollback(ws, actor, epic, d, s, unit, n, steps, run, env, cwd) -> str:
    """The recipe's rollback, once, after a failed production check under a charter that signs it: an intent record
    first, then its commands and check, then its outcome. Never retried by itself."""
    from orch.core.factory_report import _full
    ddir = _dir(ws, epic.id)
    if not _write(ddir / _name("production", unit, n, "rollback-intent"), {"attempt": n, "started": _now()}):
        return "the rollback was started already"
    r = _run_steps(ws, epic, d, s, steps, run, env, cwd)
    _write(ddir / _name("production", unit, n, "rollback"),
           {"attempt": n, "codes": r["codes"], "check": r["check"], "rolled_back": r["proven"], "ended": _now(),
            "tail": _full(r["tail"])[-TAIL:], "why": r["why"]})
    bad = next((c for c in r["codes"] if c != 0), r["check"])
    _event(ws, actor, epic.id, "release.stage", {"stage": "rollback", "child": unit, "proven": r["proven"],
                                                 "exit": bad if isinstance(bad, int) else None})
    return "rolled back" if r["proven"] else f"rollback failed ({r['why'] or 'not proven'})"


# -- the human's retry ------------------------------------------------------------------------------------------------

def retry(ws, actor, epic_id: str, stage: str, unit: str) -> str:
    """Human only: allow one more attempt at a failed, unknown or out-of-date stage of one unit (or, for the merge
    stage after a sensitive-path stop, check the branches again). Runs nothing itself; the runner's next round does."""
    from orch.core import permits
    fs.human_check(actor, "retrying a release stage")
    epic_id, unit = str(epic_id).upper(), str(unit).upper()
    if stage not in (*STAGES, JOURNAL_STAGE) or not KEY.fullmatch(epic_id) or not KEY.fullmatch(unit):
        raise UsageError("give a stage (merge, dev or production), an epic and a child or the epic itself")
    ddir = _dir(ws, epic_id)
    blocked = _blocked_record(ws, epic_id)
    if blocked is not None and blocked.get("stage") == JOURNAL_STAGE:  # the human acknowledges a damaged journal
        _journal_add(ws, {"kind": "reset", "by": actor.to_str()})
        _event(ws, actor, epic_id, "release.retry", {"stage": stage, "child": unit})
        return "the runner's journal is acknowledged; the release records decide again from the next round"
    if blocked is not None:  # a stage that could not start: only a retry of that stage and unit clears it
        if (blocked.get("stage"), str(blocked.get("unit") or "").upper()) != (stage, unit):
            raise ValidationError(f"the release is blocked on the {blocked.get('stage')} stage of "
                                  f"{blocked.get('unit')}: retry that (fix what kept it from starting first)")
        _journal_add(ws, {"kind": "clear", "epic": epic_id, "by": actor.to_str()})
        if os.path.lexists(ddir / "blocked.json"):
            os.replace(ddir / "blocked.json", ddir / f"blocked.{_now()}.{secrets.token_hex(4)}.cleared")
        _event(ws, actor, epic_id, "release.retry", {"stage": stage, "child": unit})
        return "the release starts again in the runner's next round (fix what kept it from starting first)"
    if stage == "merge" and _sensitive_record(ws, epic_id) is not None:
        os.replace(ddir / "sensitive.json", ddir / f"sensitive.{_now()}.{secrets.token_hex(4)}.cleared")
        _event(ws, actor, epic_id, "release.retry", {"stage": stage, "child": unit})
        return "the branches are checked again in the runner's next round"
    us = unit_state(ws, epic_id, stage, unit, lock_holder(ws))
    if (us.get("rollback") or {}).get("state") == "rolling back":
        raise ValidationError("the runner is rolling production back right now: wait for its outcome")
    if us["state"] == "proven" and stage in ("dev", "production"):  # stale through the children or dev (status)
        epic = _ticket(ws, epic_id)
        st = status(ws, epic, permits.factory_delegation(ws, epic)) if epic is not None else None
        row = next((u for s in (st or {}).get("stages", []) if s["name"] == stage for u in s["units"]
                    if u["unit"] == unit), None)
        us = {**us, "state": row["state"]} if row else us
    if us["state"] not in ("failed", "unknown", "stale"):
        raise ValidationError(f"the {stage} stage of {unit} is {us['state']}: only a failed, unknown or out-of-date "
                              "stage is retried")
    if us["attempt"] >= MAX_ATTEMPTS:
        raise ValidationError("this stage was retried too often; release it by hand")
    if us.get("journal"):  # the journal remembers attempts whose records are gone: put back what it says, as unknown
        for k, at in sorted(_journal_attempts(ws, epic_id, stage, unit).items()):
            if us["attempt"] < k <= us["journal"]:
                _write(ddir / _name(stage, unit, k, "intent"), {"stage": stage, "unit": unit, "epic": epic_id,
                                                                "attempt": k, "started": at or _now(),
                                                                "why": "restored from the runner's journal"})
        us = {**us, "attempt": us["journal"]}
    if us["state"] == "unknown":  # close the open attempt so the record reads as failed, then allow the next one
        _write(ddir / _name(stage, unit, us["attempt"], "outcome"),
               {"stage": stage, "unit": unit, "attempt": us["attempt"], "codes": [], "check": None, "proven": False,
                "ended": _now(), "tail": "", "why": "outcome unknown; the human retried it"})
    if not _write(ddir / _name(stage, unit, us["attempt"], "retry"), {"at": _now()}):
        raise ValidationError("this attempt was retried already")
    _event(ws, actor, epic_id, "release.retry", {"stage": stage, "child": unit})
    return f"the {stage} stage of {unit} runs once more in the runner's next round"
