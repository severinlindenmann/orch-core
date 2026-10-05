"""Dark AI Factory phase 6: the release recipe and the runner's release step (docs/factory.md, "Release recipe").

A Ready Dark epic whose signed charter carries `release: merge|dev` is taken through the stages of the human's release
recipe, up to the signed stage, by the runner (the dashboard process the human started), never by an agent.

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
- Production stages, automatic closing, release windows and rollback are not built.

The records, outcomes and the lock live beside the ledger and rest on same-user trust (docs/factory.md). Every reader
fails closed: a missing, unreadable, malformed or foreign record counts as "not proven".
"""
from __future__ import annotations

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

STAGES = ("merge", "dev")  # in this order; production is not built yet
DEFAULT_PER = {"merge": "child", "dev": "epic"}
# the Stopped reasons this module adds (orch.dashboard.data.factory._CAN says what the human can do for each)
RELEASE_CODES = ("sensitive", "release-failed", "release-unknown", "release-stale")
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
_CHILD_ONLY = ("child", "branch", "sha")
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
        if not isinstance(s, dict) or not set(s) <= {"name", "commands", "check", "timeout", "per", "precheck"}:
            raise ValidationError(f"stage {i + 1} must be an object with name, commands, check, timeout, per and "
                                  "(merge only) precheck")
        name = s.get("name")
        if name not in STAGES:
            raise ValidationError(f"stage {name!r} is not built yet: a recipe has merge and dev stages only "
                                  "(production comes later)")
        if name in seen:
            raise ValidationError(f"stage {name} is given twice")
        if seen and STAGES.index(name) < STAGES.index(seen[-1]):
            raise ValidationError("the merge stage comes before the dev stage")
        seen.append(name)
        per = s.get("per", DEFAULT_PER[name])
        if per != DEFAULT_PER[name]:
            raise ValidationError("stage merge runs per child, pinned to the commit that was checked (per: child)"
                                  if name == "merge" else "stage dev runs once per epic (per: epic)")
        cmds = s.get("commands")
        if not isinstance(cmds, list) or not 1 <= len(cmds) <= MAX_COMMANDS:
            raise ValidationError(f"stage {name}: commands must be a list of 1 to {MAX_COMMANDS} commands")
        has_repo = repo is not None
        commands = [_argv(c, f"stage {name}, command {k + 1}", per, ws, check_program=check_programs,
                          has_repo=has_repo) for k, c in enumerate(cmds)]
        if "check" not in s:
            raise ValidationError(f"stage {name}: a stage is proven only by its check: give one")
        check = _check(s["check"], f"stage {name}, check", per, ws, check_programs, has_repo)
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
                       "timeout": timeout})
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
        for argv in [*s["commands"], s["check"]["argv"], *([s["precheck"]["argv"]] if s.get("precheck") else [])]:
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


def up_to(rec: dict | None, target: str) -> list[dict] | None:
    """The recipe's stages up to `target` (merge, or merge and dev), or None when it lacks one of them."""
    if rec is None or target not in STAGES:
        return None
    need = STAGES[:STAGES.index(target) + 1]
    have = {s["name"]: s for s in rec["stages"]}
    return [have[n] for n in need] if all(n in have for n in need) else None


def release_blocker(ws, target: str) -> str | None:
    """Why a charter cannot sign `release: target` in this workspace now, or None."""
    rec, why = load(ws)
    if rec is None:
        return why + ": run `orch factory release set --file recipe.json` in your own terminal"
    if up_to(rec, target) is None:
        return f"the release recipe has no stage(s) up to {target} (merge comes first)"
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
            "children": out.get("children") if isinstance(out.get("children"), dict) else None}
    retried = os.path.lexists(d / _name(stage, unit, n, "retry")) and n < MAX_ATTEMPTS
    if out.get("proven") is True:
        if retried:
            return {**info, "state": "waiting"}  # the human asked for it to run again (it went stale)
        if os.path.lexists(d / _name(stage, unit, n, "stale")):
            return {**info, "state": "stale", "why": "its branch changed after it was merged"}
        return {**info, "state": "proven"}
    if retried:
        return {**info, "state": "waiting"}  # the human allowed one more attempt
    return {**info, "state": "failed"}


def _sensitive_record(ws, epic_id: str) -> dict | None:
    p = _dir(ws, epic_id) / "sensitive.json"
    if not os.path.lexists(p):
        return None
    return _read(p) or {"hits": {}, "damaged": True}


def _units(ws, epic, entries=None) -> list[str]:
    """The children a per-child stage runs for: those in testing (Ready's open children), by id."""
    from orch.core import epics
    return sorted(e.id for e in epics.children(ws, epic.id, entries) if e.status == "testing")


def status(ws, epic, d: dict | None, entries=None) -> dict | None:
    """The release of factory epic `epic` (delegation `d`) as records show it, or None when its charter signs none:
    {target, recipe: bool, why, stages: [{name, per, state, units: [...]}], sensitive, reasons}. Reads only (no git)."""
    target = (d or {}).get("release")
    if target not in STAGES:
        return None
    rec, why = load(ws)
    holder = lock_holder(ws)
    kids = _units(ws, epic, entries)
    stages = []
    for name in STAGES[:STAGES.index(target) + 1]:
        units = kids if DEFAULT_PER[name] == "child" else [epic.id]
        rows = [{"unit": u, **unit_state(ws, epic.id, name, u, holder)} for u in units]
        stages.append({"name": name, "per": DEFAULT_PER[name], "units": rows})
    merged = {r["unit"]: r.get("sha") for r in stages[0]["units"] if r["state"] == "proven"}
    for s in stages[1:]:  # dev went stale when the children it was proven for are not the ones merged now
        for r in s["units"]:
            if r["state"] == "proven" and (r.get("children") or {}) != {k: merged.get(k) for k in kids}:
                r.update(state="stale", why="the children changed after dev was proven")
    for s in stages:
        states = [r["state"] for r in s["units"]]
        s["state"] = ("proven" if states and all(x == "proven" for x in states) else
                      next((x for x in ("unknown", "failed", "stale", "running") if x in states), "waiting"))
    sens = _sensitive_record(ws, epic.id)
    return {"target": target, "recipe": rec is not None, "why": why, "stages": stages, "sensitive": sens,
            "reasons": _reasons(stages, sens)}


def _reasons(stages, sens) -> list[dict]:
    from orch.core.factory_report import _text
    out = []
    if sens is not None:
        hits = sens.get("hits") if isinstance(sens.get("hits"), dict) else {}
        named = "; ".join(f"{_text(c, 40)}: " + ", ".join(_text(p, 120) for p in (ps or [])[:10])
                          for c, ps in list(hits.items())[:10]) or "the record cannot be read"
        out.append({"code": "sensitive", "label": "Sensitive path touched",
                    "text": f"a child branch changes a path the release recipe marks sensitive ({named}); nothing was "
                            "merged"})
    for s in stages:
        for u in s["units"]:
            if u["state"] == "failed":
                codes = [c for c in (u.get("codes") or []) if c != 0] or [u.get("check")]
                code = codes[0] if codes else None
                out.append({"code": "release-failed", "label": "Release stage failed",
                            "text": f"the {s['name']} stage of {_text(u['unit'], 40)} failed ("
                                    + (f"exit code {code}" if isinstance(code, int) and not u.get("why")
                                       else _text(u.get("why") or f"exit code {code}", 200)) + ")"})
            elif u["state"] == "unknown":
                out.append({"code": "release-unknown", "label": "Release outcome unknown",
                            "text": f"the {s['name']} stage of {_text(u['unit'], 40)} started and has no recorded "
                                    "outcome (the runner stopped while it ran)"})
            elif u["state"] == "stale":
                out.append({"code": "release-stale", "label": "Release out of date",
                            "text": f"the {s['name']} stage of {_text(u['unit'], 40)} was proven, but "
                                    f"{_text(u.get('why'), 120)}: children changed after the release stage was proven"})
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
           "repo": rec.get("repo")}
    if child is not None:
        ctx.update(child=child, branch=branch, sha=sha)
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


def fetch_child(ws, rec, branch: str) -> str | None:
    """Fetch `branch` from the workspace checkout into the release repository (objects and that one ref, nothing else), and return
    its commit, or None."""
    src = workspace_repo(ws)
    if src is None or not valid_branch(branch):
        return None
    r = _git(ws, rec, "fetch", "-q", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", str(src),
             f"+refs/heads/{branch}:refs/release-heads/{branch}")
    return _rev(ws, rec, f"refs/release-heads/{branch}") if r.get("code") == 0 else None


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


def changed_paths(ws, rec, sha: str) -> list[str] | None:
    """Every path the commit brings in against the remote base, in the release repository: the net diff and each commit's own
    changes (merges against each parent), renames as both paths, submodules included. None when git fails or the
    list is too long to check."""
    base = f"refs/remotes/release/{rec['base']}"
    common = ("--no-renames", "--no-ext-diff", "--no-textconv", "--ignore-submodules=none", "--name-only", "-z")
    out: list[str] = []
    for args in (("diff", *common, f"{base}...{sha}"),
                 ("log", *common, "-m", "--format=", f"{base}..{sha}")):
        r = _git(ws, rec, *args, limit=GIT_OUT)
        if r.get("code") != 0 or r.get("out_size", 0) > GIT_OUT:
            return None
        out += [p for p in (r.get("out") or "").split("\x00") if p.strip()]
    return sorted(set(out))


def classify(ws, rec: dict, kids: list) -> tuple[dict, dict, dict]:
    """({child: (branch, sha)}, {child: [sensitive paths]}, {child: why it could not be checked}) for every child
    ticket in `kids`, in the runner's release repository against the base fetched from the recipe's remote."""
    found, hits, errors = {}, {}, {}
    try:
        ensure_repo(ws, rec)
        base_sha = fetch_base(ws, rec)
    except ReleaseError as e:
        return {}, {}, {t.id: str(e) for t in kids}
    if base_sha is None:
        return {}, {}, {t.id: "the base could not be fetched from the recipe's remote" for t in kids}
    for t in kids:
        branch, why = child_branch(ws, t)
        if branch is None:
            errors[t.id] = why
            continue
        if branch == rec["base"]:
            errors[t.id] = f"{t.id}'s branch is the base branch"
            continue
        sha = fetch_child(ws, rec, branch)
        if sha is None:
            errors[t.id] = f"{t.id}'s branch could not be fetched from the workspace"
            continue
        paths = changed_paths(ws, rec, sha)
        if paths is None:
            errors[t.id] = f"the changes of {t.id}'s branch could not be listed"
            continue
        bad = [p for p in paths if sensitive(p, rec["sensitive_paths"])]
        if bad:
            hits[t.id] = bad[:20]
        found[t.id] = (branch, sha)
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
        if (d is None or (did is not None and d["id"] != did) or not d.get("dark") or d.get("release") not in STAGES
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
    stages = up_to(rec, d["release"])
    if stages is None:
        return [f"{epic.id}: no release: {why or 'the recipe lacks a stage the charter signs'}"]
    kids = sorted(r["id"] for r in rep["children"] if r["status"] == "testing")
    st = status(ws, epic, d)
    if any(r["code"] != "release-stale" for r in st["reasons"]):
        return []  # stopped: the Stopped card says why (an out-of-date stage waits for the human's retry by itself)
    if not acquire(ws, epic.id, max(s["timeout"] for s in stages)):
        return [f"{epic.id}: the release waits: another release holds the lock"]
    try:
        stale = _mark_stale(ws, rec, epic, kids)
        if stale or all(s["state"] == "proven" for s in st["stages"]):
            return stale
        return _run_stages(ws, actor, epic, d, rec, stages, kids, workspace_id(ws), run)
    except ReleaseError as e:
        return [f"{epic.id}: no release this round: {e}"]
    finally:
        release_lock(ws)


def _mark_stale(ws, rec, epic, kids) -> list[str]:
    """A child whose merge was proven but whose branch in the workspace now points elsewhere (it came back with more
    work) gets a stale record: the release is out of date and the human decides."""
    lines = []
    for k in kids:
        us = unit_state(ws, epic.id, "merge", k)
        if us["state"] != "proven" or not us.get("sha"):
            continue
        t = _ticket(ws, k)
        branch = child_branch(ws, t)[0] if t is not None else None
        if branch is None:
            continue
        ensure_repo(ws, rec)
        tip = fetch_child(ws, rec, branch)
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
        if errors:
            k = sorted(errors)[0]
            n = unit_state(ws, epic.id, "merge", k, holder)["attempt"] + 1
            body = {"stage": "merge", "unit": k, "attempt": n, "started": _now()}
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
            line, proven = _attempt(ws, actor, epic, d, rec, s, unit, us["attempt"] + 1, found, kids, wsid, run)
            lines.append(line)
            if not proven:
                return lines
    return lines


def _now() -> str:
    from orch.clock import stamp_s
    return stamp_s()


def _attempt(ws, actor, epic, d, rec, s, unit, n, found, kids, wsid, run) -> tuple[str, bool]:
    """One attempt at stage `s` for `unit`, in the runner's release repository: the intent first, then the precheck, the commands
    and the check, then the outcome."""
    from orch.core.factory_report import _full
    name = s["name"]
    child = s["per"] == "child"
    try:
        if child:
            if unit not in found:
                return f"{epic.id}: {name} of {unit} not started: its branch was not checked this round", False
            branch, sha = found[unit]
            ctx = _context(epic.id, wsid, rec, unit, branch, sha)
        else:
            ctx = _context(epic.id, wsid, rec)
        steps = ([("precheck", expand(s["precheck"]["argv"], ctx),
                   expand([s["precheck"]["expect"]], ctx)[0] if s["precheck"]["expect"] is not None else None)]
                 if s.get("precheck") else [])
        steps += [("command", expand(c, ctx), None) for c in s["commands"]]
        steps.append(("check", expand(s["check"]["argv"], ctx),
                      expand([s["check"]["expect"]], ctx)[0] if s["check"]["expect"] is not None else None))
    except ValidationError as e:
        return f"{epic.id}: {name} of {unit} not started: {e}", False
    progs = {}
    for _, argv, _ in steps:
        real, why = pinned(rec, argv[0])
        if real is None:
            return f"{epic.id}: {name} of {unit} not started: {why}", False
        progs[argv[0]] = real
    steps = [(k, [progs[a[0]], *a[1:]], e) for k, a, e in steps]
    if n > MAX_ATTEMPTS or gate(ws, epic.id, d["id"]) is None:
        return f"{epic.id}: {name} of {unit} not started: the epic may not release now", False
    # the work area: the checked commit for a child stage; for dev, the remote base as it is now (the merged work)
    extra = {}
    if child:
        if fetch_child(ws, rec, ctx["branch"]) != ctx["sha"] or not checkout(ws, rec, ctx["sha"]):
            return f"{epic.id}: {name} of {unit} not started: its branch moved since it was checked", False
        extra["sha"] = ctx["sha"]
    else:
        base_sha = fetch_base(ws, rec)
        if base_sha is None or not checkout(ws, rec, base_sha):
            return f"{epic.id}: {name} not started: the base could not be fetched from the recipe's remote", False
        extra.update(base_sha=base_sha, children={k: unit_state(ws, epic.id, "merge", k).get("sha") for k in kids})
    ddir = _dir(ws, epic.id)
    digest = "sha256:" + hashlib.sha256(json.dumps([a for _, a, _ in steps]).encode("utf-8")).hexdigest()
    intent = {"stage": name, "unit": unit, "attempt": n, "argv_sha": digest, "started": _now(), **extra}
    if not _write(ddir / _name(name, unit, n, "intent"), intent):
        return f"{epic.id}: {name} of {unit}: another runner started it", False
    env = command_env(ws, *progs.values())
    cwd = str(repo_dir(ws))
    codes, tail, why, check_code, proven = [], "", "", None, False
    for kind, argv, expect in steps:
        if gate(ws, epic.id, d["id"]) is None:
            why = "stopped before the next command: the epic may no longer release (paused, edited, out of budget, " \
                  "switched off or the ledger cut)"
            break
        if child and fetch_child(ws, rec, ctx["branch"]) != ctx["sha"]:
            why = "stopped before the next command: the branch moved since it was checked"
            break
        _refresh(ws, epic.id, s["timeout"])
        r = run(argv, cwd, env, s["timeout"],
                started=lambda pgid, t=s["timeout"]: _refresh(ws, epic.id, t, pgid))
        tail += f"$ {' '.join(argv)}\n{r.get('out') or ''}{r.get('err') or ''}"
        if _STOPPING.is_set():
            why = "the dashboard stopped while it ran"
        if r.get("timed_out"):
            why = f"timed out after {s['timeout']} seconds"
        out_ok = expect is None or (r.get("out_size", 0) <= TAIL and (r.get("out") or "").strip() == expect)
        if kind == "check":
            check_code = r.get("code")
            proven = check_code == 0 and not r.get("timed_out") and out_ok and not why
            if check_code == 0 and not out_ok and not why:
                why = "the check's output is not what the recipe expects"
        elif kind == "precheck":
            codes.append(r.get("code"))
            if r.get("code") != 0 or not out_ok:
                why = why or "the precheck refused (for example a pull request open against another base)"
                break
        else:
            codes.append(r.get("code"))
            if r.get("code") != 0:
                why = why or f"exit code {r.get('code')}"
                break
        if why:
            break
    outcome = {**intent, "codes": codes, "check": check_code, "proven": proven, "ended": _now(),
               "tail": _full(tail)[-TAIL:], "why": why}
    _write(ddir / _name(name, unit, n, "outcome"), outcome)
    bad = next((c for c in codes if c != 0), check_code)
    _event(ws, actor, epic.id, "release.stage", {"stage": name, "child": unit, "proven": proven,
                                                 "exit": bad if isinstance(bad, int) else None})
    return f"{epic.id}: {name} of {unit} " + ("proven" if proven else f"failed ({why or 'not proven'})"), proven


# -- the human's retry ------------------------------------------------------------------------------------------------

def retry(ws, actor, epic_id: str, stage: str, unit: str) -> str:
    """Human only: allow one more attempt at a failed, unknown or out-of-date stage of one unit (or, for the merge
    stage after a sensitive-path stop, check the branches again). Runs nothing itself; the runner's next round does."""
    from orch.core import permits
    fs.human_check(actor, "retrying a release stage")
    epic_id, unit = str(epic_id).upper(), str(unit).upper()
    if stage not in STAGES or not KEY.fullmatch(epic_id) or not KEY.fullmatch(unit):
        raise UsageError("give a stage (merge or dev), an epic and a child or the epic itself")
    ddir = _dir(ws, epic_id)
    if stage == "merge" and _sensitive_record(ws, epic_id) is not None:
        for k in range(1, MAX_ATTEMPTS + 1):
            dst = ddir / f"sensitive.{k}.cleared"
            if not os.path.lexists(dst):
                os.replace(ddir / "sensitive.json", dst)
                break
        else:
            raise ValidationError("the release was retried too often; approve the epic again for a new start")
        _event(ws, actor, epic_id, "release.retry", {"stage": stage, "child": unit})
        return "the branches are checked again in the runner's next round"
    us = unit_state(ws, epic_id, stage, unit, lock_holder(ws))
    if us["state"] == "proven" and stage == "dev":  # dev goes stale through the children (status), not a record
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
    if us["state"] == "unknown":  # close the open attempt so the record reads as failed, then allow the next one
        _write(ddir / _name(stage, unit, us["attempt"], "outcome"),
               {"stage": stage, "unit": unit, "attempt": us["attempt"], "codes": [], "check": None, "proven": False,
                "ended": _now(), "tail": "", "why": "outcome unknown; the human retried it"})
    if not _write(ddir / _name(stage, unit, us["attempt"], "retry"), {"at": _now()}):
        raise ValidationError("this attempt was retried already")
    _event(ws, actor, epic_id, "release.retry", {"stage": stage, "child": unit})
    return f"the {stage} stage of {unit} runs once more in the runner's next round"
