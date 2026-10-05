"""Dark AI Factory phase 6: the release recipe and the runner's release step (docs/factory.md, "Release recipe").

A Ready Dark epic whose signed charter carries `release: merge|dev` is taken through the stages of the human's release
recipe, up to the signed stage, by the runner (the dashboard process the human started), never by an agent.

- The **recipe** is a JSON document in the guarded permits folder of the orch config dir (`factory-release.json`,
  `{"workspaces": {"<workspace id>": recipe}}`), written only by the human's terminal command (`orch factory release
  set`). Never workspace config, ticket text or charter text: an agent can edit those. Its commands are argv lists
  (no shell strings); placeholders are filled in as whole values that were validated first.
- **Diff classification**: before the first merge command of an epic, the runner lists each child branch's changed
  paths against the recipe's base (git, by argv) and stops on any `sensitive_paths` match; nothing is merged then.
- One workspace-wide **release lock** (an exclusive file with the holder's process and an expiry) is held across all
  stages of one epic.
- **Crash safety**: an intent record is written exclusively before a stage's commands run, the outcome after. An
  intent without an outcome (and no live holder) is "unknown": never run again automatically. At most one automatic
  attempt per stage and unit; the human's Retry allows one more.
- Production stages, automatic closing, release windows and rollback are not built.

Every reader fails closed: a missing, unreadable, malformed or foreign record counts as "not proven".
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
from pathlib import Path

from orch.core import factory_sessions as fs
from orch.errors import UsageError, ValidationError

STAGES = ("merge", "dev")  # in this order; production is not built yet
DEFAULT_PER = {"merge": "child", "dev": "epic"}
RELEASE_CODES = ("sensitive", "release-failed", "release-unknown")  # the Stopped reasons this module adds
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
PLACEHOLDERS = ("epic", "child", "branch", "sha", "workspace")
_CHILD_ONLY = ("child", "branch", "sha")
_PH = re.compile(r"\{([a-z]+)\}")
KEY = re.compile(r"[A-Z][A-Z0-9]*-\d+")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_WSID = re.compile(r"[0-9a-f]{16}")
# programs that take a shell string: the recipe is argv lists, so these are refused as a command's program
_SHELLS = re.compile(r"(?:ba|z|k|c|tc|da|fi|pw)?sh[\d.]*|env|eval|exec|sudo|su|doas|xargs|busybox", re.I)


def valid_branch(name) -> bool:
    """A strict git branch name: starts with a letter or digit (never an option), no `..`, `//`, `/.`, `@{`, and
    no trailing `/`, `.` or `.lock`."""
    return (isinstance(name, str) and bool(_BRANCH.fullmatch(name)) and ".." not in name and "//" not in name
            and "/." not in name and not name.endswith(("/", ".", ".lock")))


def _printable(s: str) -> bool:
    return all(32 <= ord(c) < 127 for c in s)


# -- the recipe ---------------------------------------------------------------------------------------------------------

def path() -> Path:
    from orch.core.ledger import base_dir
    return base_dir() / "permits" / FILE


def _program_error(word: str, ws) -> str | None:
    from orch.core import factory_runner
    if _PH.search(word) or "{" in word or "}" in word:
        return f"the program {word!r} may not hold a placeholder"
    if _SHELLS.fullmatch(os.path.basename(word)):
        return f"the program {word!r} runs shell strings: give the command as an argv list, or a script of yours"
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


def _argv(value, what: str, per: str, ws, *, check_program: bool) -> list[str]:
    if isinstance(value, str):
        raise ValidationError(f"{what} is a shell string: give it as a list of words, e.g. [\"make\", \"deploy-dev\"]")
    if not isinstance(value, list) or not value or len(value) > MAX_ARGS:
        raise ValidationError(f"{what} must be a list of 1 to {MAX_ARGS} words")
    for w in value:
        if not isinstance(w, str) or not w or len(w) > MAX_ARG or not _printable(w):
            raise ValidationError(f"{what}: every word is printable ASCII text of 1 to {MAX_ARG} characters")
        names = _PH.findall(w)
        if "{" in _PH.sub("", w) or "}" in _PH.sub("", w):
            raise ValidationError(f"{what}: {w!r} holds a brace that is not one of the placeholders "
                                  + ", ".join("{" + p + "}" for p in PLACEHOLDERS))
        for n in names:
            if n not in PLACEHOLDERS:
                raise ValidationError(f"{what}: {{{n}}} is not a placeholder (allowed: "
                                      + ", ".join("{" + p + "}" for p in PLACEHOLDERS) + ")")
            if n in _CHILD_ONLY and per != "child":
                raise ValidationError(f"{what}: {{{n}}} is only filled in for a stage that runs per child")
    why = _program_error(value[0], ws) if check_program else (
        "the program may not hold a placeholder" if _PH.search(value[0]) else None)
    if why:
        raise ValidationError(f"{what}: {why}")
    return list(value)


def check_recipe(data, ws, *, check_programs: bool = True) -> dict:
    """The recipe `data` validated and normalised: {stages: [{name, per, commands, check: {argv, expect}, timeout}],
    sensitive_paths, base}. Raises ValidationError with the first reason it is refused."""
    if not isinstance(data, dict) or not set(data) <= {"stages", "sensitive_paths", "base"} or "stages" not in data:
        raise ValidationError('a recipe is {"stages": [...], "sensitive_paths": [...], "base": "main"} and nothing else')
    raw = data["stages"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= len(STAGES):
        raise ValidationError(f"stages must be a list of 1 to {len(STAGES)} stages")
    stages, seen = [], []
    for i, s in enumerate(raw):
        if not isinstance(s, dict) or not set(s) <= {"name", "commands", "check", "timeout", "per"}:
            raise ValidationError(f"stage {i + 1} must be an object with name, commands, check, timeout and per")
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
        if per not in ("child", "epic"):
            raise ValidationError(f"stage {name}: per is child or epic")
        if name == "dev" and per != "epic":
            raise ValidationError("stage dev runs once per epic (per: epic)")
        cmds = s.get("commands")
        if not isinstance(cmds, list) or not 1 <= len(cmds) <= MAX_COMMANDS:
            raise ValidationError(f"stage {name}: commands must be a list of 1 to {MAX_COMMANDS} commands")
        commands = [_argv(c, f"stage {name}, command {k + 1}", per, ws, check_program=check_programs)
                    for k, c in enumerate(cmds)]
        chk = s.get("check")
        if not isinstance(chk, dict) or not set(chk) <= {"argv", "expect"} or "argv" not in chk:
            raise ValidationError(f'stage {name}: check is {{"argv": [...], "expect": "..."}} (expect optional): a '
                                  "stage is proven only by its check")
        expect = chk.get("expect")
        if expect is not None and (not isinstance(expect, str) or len(expect) > MAX_EXPECT or not _printable(expect)):
            raise ValidationError(f"stage {name}: expect is printable ASCII text of at most {MAX_EXPECT} characters")
        check = {"argv": _argv(chk["argv"], f"stage {name}, check", per, ws, check_program=check_programs),
                 "expect": expect.strip() if isinstance(expect, str) else None}
        timeout = s.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT:
            raise ValidationError(f"stage {name}: timeout is a whole number of seconds from 1 to {MAX_TIMEOUT}")
        stages.append({"name": name, "per": per, "commands": commands, "check": check, "timeout": timeout})
    pats = data.get("sensitive_paths", [])
    if not isinstance(pats, list) or len(pats) > MAX_PATTERNS or not all(
            isinstance(p, str) and p and len(p) <= MAX_PATTERN and _printable(p) for p in pats):
        raise ValidationError(f"sensitive_paths is a list of at most {MAX_PATTERNS} glob patterns (printable ASCII, "
                              f"at most {MAX_PATTERN} characters each)")
    base = data.get("base", "main")
    if not valid_branch(base):
        raise ValidationError("base must be a plain branch name, such as main")
    return {"stages": stages, "sensitive_paths": list(pats), "base": base}


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
    """(the validated recipe of this workspace, None) or (None, why there is none). Never raises."""
    from orch.core.ledger import workspace_id
    try:
        data = _read_all()
        if data is None:
            return None, f"{path()} is damaged or not yours alone: no release runs"
        raw = data["workspaces"].get(workspace_id(ws))
        if raw is None:
            return None, "no release recipe for this workspace"
        return check_recipe(raw, ws), None
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


def _write_all(data: dict) -> None:
    p = path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = p.with_name(f".{FILE}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=1)
    os.chmod(tmp, 0o600)
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
        _write_all(data)


def set_recipe(ws, actor, data) -> dict:
    """Human only: validate and store this workspace's recipe (the other workspaces' are kept)."""
    fs.human_check(actor, "setting the release recipe")
    rec = check_recipe(data, ws)
    _edit(ws, lambda all_, wid: all_.__setitem__(wid, data))
    return rec


def clear_recipe(ws, actor) -> bool:
    """Human only: remove this workspace's recipe. True when there was one."""
    fs.human_check(actor, "clearing the release recipe")
    had = []
    _edit(ws, lambda all_, wid: had.append(all_.pop(wid, None) is not None))
    return bool(had and had[0])


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
    """{state: waiting|running|proven|failed|unknown, attempt, codes, tail, why, ended} of one stage and unit (a
    child, or the epic for a stage that runs per epic). `holder`: the live release lock holder, if any."""
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
            "ended": out.get("ended"), "check": out.get("check")}
    if out.get("proven") is True:
        return {**info, "state": "proven"}
    if os.path.lexists(d / _name(stage, unit, n, "retry")) and n < MAX_ATTEMPTS:
        return {**info, "state": "waiting"}  # the human allowed one more attempt
    return {**info, "state": "failed"}


def _sensitive(ws, epic_id: str) -> dict | None:
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
    {target, recipe: bool, why, stages: [{name, per, state, units: [...]}], sensitive, reasons}. Reads only."""
    target = (d or {}).get("release")
    if target not in STAGES:
        return None
    rec, why = load(ws)
    holder = lock_holder(ws)
    kids = _units(ws, epic, entries)
    stages = []
    for name in STAGES[:STAGES.index(target) + 1]:
        conf = next((s for s in (rec or {}).get("stages", []) if s["name"] == name), None)
        per = conf["per"] if conf else DEFAULT_PER[name]
        units = kids if per == "child" else [epic.id]
        rows = [{"unit": u, **unit_state(ws, epic.id, name, u, holder)} for u in units]
        states = [r["state"] for r in rows]
        agg = ("proven" if rows and all(s == "proven" for s in states) else
               next((s for s in ("unknown", "failed", "running") if s in states), "waiting"))
        stages.append({"name": name, "per": per, "state": agg, "units": rows})
    sens = _sensitive(ws, epic.id)
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
                                    + (f"exit code {code}" if isinstance(code, int) else _text(u.get("why") or
                                                                                              "no exit code", 200))
                                    + ")"})
            elif u["state"] == "unknown":
                out.append({"code": "release-unknown", "label": "Release outcome unknown",
                            "text": f"the {s['name']} stage of {_text(u['unit'], 40)} started and has no recorded "
                                    "outcome (the runner stopped while it ran)"})
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
    """The live holder of the workspace's release lock ({pid, epic, until}), or None. A holder whose process is gone
    or whose expiry passed holds nothing; a lock file that cannot be read holds until it is older than the longest
    command could run."""
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
        if float(body.get("until")) < time.time():
            return None
        pid = int(body.get("pid"))
    except (TypeError, ValueError):
        return None
    if fs.proc_start(pid) != body.get("pid_start"):
        return None
    return body


def _lock_body(ws, epic_id: str, seconds: int) -> dict:
    import time
    return {"pid": os.getpid(), "pid_start": fs.proc_start(os.getpid()), "epic": str(epic_id).upper(),
            "until": time.time() + seconds + 120}


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


def _refresh(ws, epic_id: str, seconds: int) -> None:
    p = _lock_path(ws)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(_lock_body(ws, epic_id, seconds)), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


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

def _tail_of(f) -> tuple[str, int]:
    size = f.seek(0, os.SEEK_END)
    f.seek(max(0, size - TAIL))
    return f.read().decode("utf-8", "replace"), size


def run_command(argv: list[str], cwd: str, env: dict, timeout: int) -> dict:
    """Run one command: no shell, stdin closed, its own process group (killed whole on timeout), output to unlinked
    temporary files of which only the last TAIL bytes are kept. {code, out, out_size, err, timed_out}."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                 start_new_session=True)
        except OSError as e:
            return {"code": None, "out": "", "out_size": 0, "err": f"could not start: {e.strerror or e}",
                    "timed_out": False}
        timed = False
        try:
            code = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed, code = True, None
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
        o, size = _tail_of(out)
        e, _ = _tail_of(err)
        return {"code": code, "out": o, "out_size": size, "err": e, "timed_out": timed}


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
        if m.group(1) not in ctx:
            raise ValidationError(f"{{{m.group(1)}}} has no value here")
        return ctx[m.group(1)]
    return [_PH.sub(one, w) for w in argv]


def _context(epic_id: str, wsid: str, child: str | None = None, branch: str | None = None,
             sha: str | None = None) -> dict:
    """The placeholder values, each validated (ids by the ticket id form, branch by a strict git ref form, sha as
    hex); anything else raises."""
    ctx = {"epic": epic_id, "workspace": wsid}
    if child is not None:
        ctx.update(child=child, branch=branch, sha=sha)
    for k, rx in (("epic", KEY), ("child", KEY), ("sha", _SHA), ("workspace", _WSID)):
        if k in ctx and not (isinstance(ctx[k], str) and rx.fullmatch(ctx[k])):
            raise ValidationError(f"the {k} value is not valid")
    if "branch" in ctx and not valid_branch(ctx["branch"]):
        raise ValidationError("the branch name is not valid")
    return ctx


def _git(run, git: str, root: str, *args: str) -> dict:
    return run([git, "-c", "core.fsmonitor=false", *args], root, _env(git), 60)


def child_branch(ws, t) -> tuple[str | None, str]:
    """(branch, "") of child `t`: the one branch it names (`orch link --branch`), else the branch of its one worktree;
    the name must be a valid branch name that names the child (its id as a word). (None, why) otherwise. Agent
    written, so it is only a name the runner then resolves itself."""
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


def _resolve_sha(run, git: str, root: str, branch: str) -> str | None:
    r = _git(run, git, root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
    sha = (r.get("out") or "").strip()
    return sha if r.get("code") == 0 and _SHA.fullmatch(sha) else None


def classify(ws, rec: dict, kids: list, run) -> tuple[dict, dict, dict]:
    """({child: (branch, sha)}, {child: [sensitive paths]}, {child: why it could not be checked}) for every child
    ticket in `kids`: the changed paths of its branch against the recipe's base (`git diff --name-only --no-renames
    <base>...<sha>`, by argv), matched against the recipe's sensitive_paths."""
    from orch.core import factory_runner
    git = factory_runner.resolve_bin("git")
    root = str(Path(ws.root).resolve())
    found, hits, errors = {}, {}, {}
    for t in kids:
        if git is None:
            errors[t.id] = "git was not found at a trusted path"
            continue
        branch, why = child_branch(ws, t)
        if branch is None:
            errors[t.id] = why
            continue
        if branch == rec["base"]:
            errors[t.id] = f"{t.id}'s branch is the base branch"
            continue
        sha = _resolve_sha(run, git, root, branch)
        if sha is None:
            errors[t.id] = f"{t.id}'s branch was not found in this repository"
            continue
        r = _git(run, git, root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z",
                 f"{rec['base']}...{sha}")
        if r.get("code") != 0 or r.get("out_size", 0) > TAIL * 64:
            errors[t.id] = f"the changes of {t.id}'s branch could not be listed"
            continue
        paths = [p for p in (r.get("out") or "").split("\x00") if p]
        if r.get("out_size", 0) > len((r.get("out") or "").encode("utf-8")):
            errors[t.id] = f"{t.id}'s branch changes too many paths to check"
            continue
        bad = [p for p in paths if any(fnmatch.fnmatchcase(p, pat) for pat in rec["sensitive_paths"])]
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
    the human started) runs it. `run`: run_command, or a stand-in in tests."""
    from orch.core import epics, permits, store
    fs.human_check(actor, "running a factory release")
    if not permits.enabled(ws):
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
    if st["reasons"] or all(s["state"] == "proven" for s in st["stages"]):
        return []  # stopped (the Stopped card says why) or done: nothing to run
    if not acquire(ws, epic.id, max(s["timeout"] for s in stages)):
        return [f"{epic.id}: the release waits: another release holds the lock"]
    try:
        return _run_stages(ws, actor, epic, d, rec, stages, kids, workspace_id(ws), run)
    finally:
        release_lock(ws)


def _run_stages(ws, actor, epic, d, rec, stages, kids, wsid, run) -> list[str]:
    lines: list[str] = []
    ddir = _dir(ws, epic.id)
    holder = lock_holder(ws)
    root = str(Path(ws.root).resolve())
    if any(unit_state(ws, epic.id, s["name"], u, holder)["state"] not in ("waiting", "proven")
           for s in stages for u in (kids if s["per"] == "child" else [epic.id])) or _sensitive(ws, epic.id):
        return lines
    # diff classification, before the first merge command: every child not yet merged, all at once
    merge = next((s for s in stages if s["name"] == "merge"), None)
    open_kids = [k for k in kids if merge is None or merge["per"] == "epic"
                 or unit_state(ws, epic.id, "merge", k, holder)["state"] != "proven"]
    todo_merge = merge is not None and any(
        unit_state(ws, epic.id, "merge", u, holder)["state"] != "proven"
        for u in (kids if merge["per"] == "child" else [epic.id]))
    found: dict = {}
    if todo_merge:
        tickets = [t for t in (_ticket(ws, k) for k in open_kids) if t is not None]
        if len(tickets) != len(open_kids):
            return lines + [f"{epic.id}: a child cannot be read; no release this round"]
        found, hits, errors = classify(ws, rec, tickets, run)
        if hits:
            _write(ddir / "sensitive.json", {"hits": hits, "at": _now()})
            return lines + [f"{epic.id}: release stopped: a sensitive path is touched; nothing was merged"]
        if errors:
            k = sorted(errors)[0]
            unit = k if merge["per"] == "child" else epic.id
            n = unit_state(ws, epic.id, "merge", unit, holder)["attempt"] + 1
            body = {"stage": "merge", "unit": unit, "attempt": n, "started": _now()}
            if n <= MAX_ATTEMPTS and _write(ddir / _name("merge", unit, n, "intent"), body):
                _write(ddir / _name("merge", unit, n, "outcome"),
                       {**body, "codes": [], "check": None, "proven": False, "ended": _now(), "tail": "",
                        "why": errors[k]})
                _event(ws, actor, epic.id, "release.stage", {"stage": "merge", "child": unit, "proven": False,
                                                             "exit": None})
            return lines + [f"{epic.id}: merge not started: {errors[k]}"]
    for s in stages:
        for unit in (kids if s["per"] == "child" else [epic.id]):
            us = unit_state(ws, epic.id, s["name"], unit, lock_holder(ws))
            if us["state"] == "proven":
                continue
            if us["state"] != "waiting":
                return lines
            line, proven = _attempt(ws, actor, epic, d, s, unit, us["attempt"] + 1, found, wsid, root, run)
            lines.append(line)
            if not proven:
                return lines
    return lines


def _now() -> str:
    from orch.clock import stamp_s
    return stamp_s()


def _attempt(ws, actor, epic, d, s, unit, n, found, wsid, root, run) -> tuple[str, bool]:
    """One attempt at stage `s` for `unit`: the intent first, then the commands and the check, then the outcome."""
    from orch.core import factory_runner
    from orch.core.factory_report import _full
    name = s["name"]
    try:
        if s["per"] == "child":
            if unit not in found:
                return f"{epic.id}: {name} of {unit} not started: its branch was not checked this round", False
            branch, sha = found[unit]
            ctx = _context(epic.id, wsid, unit, branch, sha)
        else:
            ctx = _context(epic.id, wsid)
        argvs = [expand(c, ctx) for c in s["commands"]]
        check = expand(s["check"]["argv"], ctx)
    except ValidationError as e:
        return f"{epic.id}: {name} of {unit} not started: {e}", False
    progs = [factory_runner.resolve_bin(a[0]) for a in [*argvs, check]]
    if any(p is None for p in progs):
        return f"{epic.id}: {name} of {unit} not started: a program was not found at a trusted path", False
    argvs = [[p, *a[1:]] for p, a in zip(progs, argvs)]
    check = [progs[-1], *check[1:]]
    if n > MAX_ATTEMPTS or gate(ws, epic.id, d["id"]) is None:
        return f"{epic.id}: {name} of {unit} not started: the epic may not release now", False
    if s["per"] == "child":  # the branch must still be the commit that was checked
        git = factory_runner.resolve_bin("git")
        if git is None or _resolve_sha(run, git, root, ctx["branch"]) != ctx["sha"]:
            return f"{epic.id}: {name} of {unit} not started: its branch moved since it was checked", False
    ddir = _dir(ws, epic.id)
    digest = "sha256:" + hashlib.sha256(json.dumps([argvs, check]).encode("utf-8")).hexdigest()
    intent = {"stage": name, "unit": unit, "attempt": n, "argv_sha": digest, "started": _now()}
    if not _write(ddir / _name(name, unit, n, "intent"), intent):
        return f"{epic.id}: {name} of {unit}: another runner started it", False
    env = _env(*progs)
    codes, tail, why, check_code, proven = [], "", "", None, False
    for k, argv in enumerate([*argvs, check]):
        is_check = k == len(argvs)
        if gate(ws, epic.id, d["id"]) is None:
            why = "stopped before the next command: the epic may no longer release (paused, edited, out of budget, " \
                  "switched off or the ledger cut)"
            break
        _refresh(ws, epic.id, s["timeout"])
        r = run(argv, root, env, s["timeout"])
        tail += f"$ {' '.join(argv)}\n{r.get('out') or ''}{r.get('err') or ''}"
        if r.get("timed_out"):
            why = f"timed out after {s['timeout']} seconds"
        if is_check:
            check_code = r.get("code")
            exp = s["check"]["expect"]
            proven = check_code == 0 and not r.get("timed_out") and (
                exp is None or (r.get("out_size", 0) <= TAIL and (r.get("out") or "").strip() == exp))
            if check_code == 0 and not proven and not why:
                why = "the check's output is not what the recipe expects"
        else:
            codes.append(r.get("code"))
            if r.get("code") != 0:
                why = why or f"exit code {r.get('code')}"
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
    """Human only: allow one more attempt at a failed or unknown stage of one unit (or, for the merge stage after a
    sensitive-path stop, check the branches again). Runs nothing itself; the runner's next round does."""
    fs.human_check(actor, "retrying a release stage")
    epic_id, unit = str(epic_id).upper(), str(unit).upper()
    if stage not in STAGES or not KEY.fullmatch(epic_id) or not KEY.fullmatch(unit):
        raise UsageError("give a stage (merge or dev), an epic and a child or the epic itself")
    ddir = _dir(ws, epic_id)
    if stage == "merge" and _sensitive(ws, epic_id) is not None:
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
    if us["state"] not in ("failed", "unknown"):
        raise ValidationError(f"the {stage} stage of {unit} is {us['state']}: only a failed or unknown stage is "
                              "retried")
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
