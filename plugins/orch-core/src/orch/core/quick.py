"""Quick tasks: one-line jobs too small for a ticket (a typo, a version bump, a dead import).

A quick task has no requirements, plan, task list or verdict: someone adds one line, an agent claims it, does it and
closes it with a one-line note (and, if it likes, a few artifacts). What keeps it small is a size limit checked at
`done`: more commits or files than the workspace allows mark the task "outgrew it" and the agent stops; the human
then makes it a ticket, lets it finish anyway, or drops it.

Storage: one JSON file per task under `orchestrator/.state/quick/` (`Q-12.json`), so parallel branches never edit the
same file and the guard already keeps agents from writing there by hand. Artifacts go to `orchestrator/artifacts/Q-12/`
like a ticket's. The counter is `.state/quick-counter.json`.

Who decides: quick tasks are on while the `quick-tasks` default addon is enabled in the workspace (Workspace &
addons, or `orch addon enable quick-tasks` in the human's own terminal), like Terminals and Graph. Its settings
(whether agents may add tasks, the size limit) live with it in the user's orch config dir, outside the repository,
where agents cannot write. Reopening, letting an outgrown task finish and dropping are human-only too.

How agents get one (in order of preference): the human starts them on it; `orch next` falls through to quick tasks
when no ticket is ready (`quick.next`: idle, first or never); or, once the agent's own ticket is in testing,
`orch quick near` lists quick tasks in the files that ticket changed. An agent never takes a quick task while it holds
a ticket that is still in progress or waiting, holds one quick task at a time, and never picks up one it filed itself
in the same session.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from orch.clock import now, parse_stamp, stamp_s
from orch.core.events import append_event
from orch.core.fsutil import atomic_write_text
from orch.core.locks import lock
from orch.errors import ClaimError, HumanOnlyError, NotFoundError, TransitionError, UsageError, ValidationError

ADDON = "quick-tasks"  # the default addon that switches quick tasks on (Workspace & addons), like `terminals`
STATUSES = ("open", "done", "moved", "dropped")
NEXT_MODES = ("idle", "first", "never")
DEFAULTS = {"prefix": "Q", "next": "idle", "max_artifacts": 5, "claim_minutes": 30}
# The addon's settings (its manifest's settings_schema; stored per user in the orch config dir, outside the repo
# and out of an agent's reach): what gives agents room is the human's.
ADDON_DEFAULTS = {"agents_add": False, "max_commits": "1", "max_files": "3"}
MAX_TITLE = 200
MAX_NOTE = 500
MAX_AREA = 200
_SEP_COMMIT, _SEP_FIELD = "\x1e", "\x1f"
_HINT_ON = "quick tasks are an addon: the human enables `quick-tasks` in Workspace & addons (or runs " \
           "`orch addon enable quick-tasks` in their own terminal)"


# -- settings ---------------------------------------------------------------------------------------------------------

def config(ws) -> dict:
    """The `quick` block of the workspace config with defaults: the key prefix, `orch next`, artifacts, claims."""
    return config_of(getattr(ws, "config", None) or {})


def config_of(cfg: dict) -> dict:
    raw = cfg.get("quick")
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        out.update({k: v for k, v in raw.items() if k in DEFAULTS})
    if out["next"] not in NEXT_MODES:
        out["next"] = "idle"
    for k in ("max_artifacts", "claim_minutes"):
        v = out[k]
        out[k] = v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else DEFAULTS[k]
    p = out["prefix"]
    if not isinstance(p, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{0,5}", p):
        out["prefix"] = DEFAULTS["prefix"]
    if out["prefix"].upper() == str((cfg.get("id") or {}).get("prefix", "")).upper():
        out["prefix"] = out["prefix"] + "T"  # never the ticket prefix: Q-12 and a ticket Q-0012 would collide
    return out


def addon_state(root) -> tuple[bool, dict]:
    """(enabled, settings) of the `quick-tasks` addon for the workspace at `root`, read from the user's own files
    without importing addon code (the CLI and the hooks may not); never raises."""
    try:
        from orch.addons.userfiles import workspace_addons
        item = workspace_addons(root).get(ADDON) or {}
    except Exception:  # an unreadable user file counts as off
        return False, dict(ADDON_DEFAULTS)
    saved = item.get("config") or {}
    return item.get("enabled") is True, {**ADDON_DEFAULTS, **{k: v for k, v in saved.items() if k in ADDON_DEFAULTS}}


def _limit(value, default: int) -> int:
    try:
        n = int(str(value).strip())
    except ValueError:
        return default
    return n if 0 <= n <= 50 else default


def settings(ws) -> dict:
    """The settings in force: on while the `quick-tasks` addon is enabled in this workspace, with its settings (agents
    may add, the size limit) over the workspace config. `state` is "on" or "off"."""
    on, addon = addon_state(ws.root)
    out = dict(config(ws))
    out.update(state="on" if on else "off", enabled=on, agents_add=on and addon["agents_add"] is True,
               max_commits=_limit(addon["max_commits"], 1), max_files=_limit(addon["max_files"], 3))
    return out


def enabled(ws) -> bool:
    return settings(ws)["enabled"]


# -- ids and files ----------------------------------------------------------------------------------------------------

def prefix(ws) -> str:
    return config(ws)["prefix"]


def key_pattern(ws) -> str:
    """A regex (no anchors) for this workspace's quick-task keys, for commit subjects."""
    return key_pattern_of(ws.config)


def key_pattern_of(cfg: dict) -> str:
    return rf"{re.escape(config_of(cfg)['prefix'])}-\d+"


def normalize(ws, ref: str) -> str:
    ref = str(ref or "").strip()
    m = re.fullmatch(rf"(?:{re.escape(prefix(ws))}-)?0*(\d+)", ref, re.IGNORECASE)
    if not m:
        raise NotFoundError(f"no quick task {ref!r}", hint=f"quick tasks are named {prefix(ws)}-<number>; see `orch quick`")
    return f"{prefix(ws)}-{int(m.group(1))}"


def is_key(ws, ref: str) -> bool:
    return bool(re.fullmatch(key_pattern(ws), str(ref or "").strip(), re.IGNORECASE))


def directory(ws) -> Path:
    return ws.state_dir / "quick"


def _path(ws, qid: str) -> Path:
    return directory(ws) / f"{qid}.json"


def _read(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("id"), str) and d.get("status") in STATUSES else None


def load(ws, ref: str) -> dict:
    qid = normalize(ws, ref)
    task = _read(_path(ws, qid))
    if task is None or task["id"] != qid:
        raise NotFoundError(f"no quick task {qid}", hint="`orch quick --all` lists them")
    return task


def _num(qid: str) -> int:
    try:
        return int(qid.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def all_tasks(ws) -> list[dict]:
    """Every readable quick task, newest first."""
    d = directory(ws)
    if not d.is_dir():
        return []
    out = [t for p in d.glob("*.json") if (t := _read(p)) is not None and p.stem == t["id"]]
    return sorted(out, key=lambda t: _num(t["id"]), reverse=True)


def _save(ws, task: dict) -> None:
    atomic_write_text(_path(ws, task["id"]), json.dumps(task, indent=2, ensure_ascii=False) + "\n")


def _next_id(ws) -> str:
    counter = ws.state_dir / "quick-counter.json"
    try:
        n = int(json.loads(counter.read_text(encoding="utf-8"))["next"])
    except (OSError, ValueError, KeyError, TypeError):
        n = 1
    existing = [_num(p.stem) for p in directory(ws).glob("*.json")] if directory(ws).is_dir() else []
    n = max([n, *[x + 1 for x in existing]])
    atomic_write_text(counter, json.dumps({"next": n + 1}) + "\n")
    return f"{prefix(ws)}-{n}"


# -- facts ------------------------------------------------------------------------------------------------------------

def claim_expired(task: dict, minutes: int) -> bool:
    c = task.get("claim") or {}
    try:
        return (now() - parse_stamp(str(c.get("at")))).total_seconds() > minutes * 60
    except ValueError:
        return True


def active_claim(task: dict, minutes: int) -> dict | None:
    c = task.get("claim")
    return c if isinstance(c, dict) and c.get("session") and not claim_expired(task, minutes) else None


def view(ws, task: dict, cfg: dict | None = None) -> dict:
    """The task with what readers derive: who holds it, whether that claim went stale, and its area as a list."""
    cfg = cfg or settings(ws)
    c = task.get("claim") if isinstance(task.get("claim"), dict) else None
    return {**task,
            "claimed_by": c.get("harness") if c else None,
            "stale": bool(c and claim_expired(task, cfg["claim_minutes"])),
            "outgrew": task.get("outgrew") if task["status"] == "open" else None}


def _one_line(text, what: str, limit: int, *, required: bool = True) -> str | None:
    from orch.textsafe import decodes_to_hidden
    text = " ".join(str(text or "").split())
    if not text:
        if required:
            raise UsageError(f"{what} must not be empty")
        return None
    if decodes_to_hidden(text):
        raise ValidationError(f"{what} holds hidden or control characters")
    if len(text) > limit:
        raise ValidationError(f"{what} is longer than {limit} characters",
                              hint="a quick task is one line; anything longer belongs in a ticket (`orch new`)")
    return text


def _area(raw) -> str | None:
    area = _one_line(raw, "the area", MAX_AREA, required=False)
    if area is None:
        return None
    area = area.replace("\\", "/").strip("/")
    if area.startswith("../") or "/../" in f"/{area}/" or area.startswith("~"):
        raise UsageError("the area is a path inside the workspace, e.g. src/orch/cli.py or docs/")
    return area or None


# -- git: the size of a task and the files of a ticket ----------------------------------------------------------------

def _git(directory: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


def _home_rel(ws) -> str | None:
    try:
        return Path(ws.home).resolve().relative_to(Path(ws.root).resolve()).as_posix().rstrip("/") + "/"
    except ValueError:
        return None


def _keep(ws, repo: str, path: str) -> bool:
    """orch's own records (tickets, events, quick tasks) never count towards a task's size."""
    home = _home_rel(ws)
    return bool(path) and not (repo == "" and home and path.startswith(home))


def commits_naming(ws, key: str, max_commits: int = 2000) -> list[dict]:
    """Commits (any local branch) whose subject names `key`, newest first: {repo, sha, files}."""
    from orch.core.related import _qualify, repos
    rx = re.compile(rf"(?<![A-Za-z0-9]){re.escape(key)}(?![0-9])", re.IGNORECASE)
    out = []
    for repo, d in repos(ws):
        log = _git(d, "log", "--all", "--no-merges", f"-n{max_commits}", "--name-only",
                   f"--format={_SEP_COMMIT}%H{_SEP_FIELD}%s")
        for chunk in log.split(_SEP_COMMIT)[1:]:
            head, _, names = chunk.partition("\n")
            sha, _, subject = head.partition(_SEP_FIELD)
            if rx.search(subject):
                files = [_qualify(repo, n) for n in names.splitlines() if _keep(ws, repo, n.strip())]
                out.append({"repo": repo, "sha": sha, "files": files})
    return out


def uncommitted(ws) -> list[str]:
    """Files changed but not committed in every repo (orch's records left out)."""
    from orch.core.related import _qualify, repos
    out = []
    for repo, d in repos(ws):
        for entry in filter(None, _git(d, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames").split("\0")):
            rel = entry[3:]
            if _keep(ws, repo, rel):
                out.append(_qualify(repo, rel))
    return out


def size(ws, qid: str) -> dict:
    """What the task changed: the commits naming it, and the files of those commits plus what is not committed yet
    (an agent that may not commit leaves its work in the tree)."""
    commits = commits_naming(ws, qid)
    files = sorted({f for c in commits for f in c["files"]} | set(uncommitted(ws)))
    return {"commits": [c["sha"][:12] for c in commits], "files": files}


def ticket_files(ws, ref: str) -> list[str]:
    """The files the commits of ticket `ref` changed."""
    from orch.core import store
    tid = store.resolve(ws, ref).id
    return sorted({f for c in commits_naming(ws, tid) for f in c["files"]})


def _matches(area: str, files: list[str]) -> bool:
    a = area.rstrip("/")
    return any(f == a or f.startswith(a + "/") or f.endswith("/" + a) or ("/" not in a and f.rsplit("/", 1)[-1] == a)
               for f in files)


# -- picking ----------------------------------------------------------------------------------------------------------

def _filed_by_this_agent(task: dict, actor) -> bool:
    added = task.get("added") or {}
    return (not actor.is_human and str(added.get("by", "")).startswith("agent:")
            and added.get("session") == (actor.session or "local"))


def pickable(ws, actor=None) -> list[dict]:
    """Open quick tasks an agent may take now, in the order to take them: unclaimed (or stale), not outgrown, the
    human's before agents' and oldest first, leaving out the ones this agent session filed itself."""
    cfg = settings(ws)
    if not cfg["enabled"]:
        return []
    out = [t for t in all_tasks(ws) if t["status"] == "open" and not t.get("outgrew")
           and active_claim(t, cfg["claim_minutes"]) is None and not (actor and _filed_by_this_agent(t, actor))]
    return sorted(out, key=lambda t: (str((t.get("added") or {}).get("by", "")).startswith("agent:"), _num(t["id"])))


def near(ws, ref: str | None = None, paths: list[str] | None = None, actor=None) -> dict:
    """Quick tasks whose area lies in the files ticket `ref` changed (and/or `paths`): what an agent may pick up once
    its ticket is in testing, because it is already in that code."""
    from orch.core.related import normalize_paths
    files = ticket_files(ws, ref) if ref else []
    files += normalize_paths(ws, list(paths or []))
    tasks = [t for t in pickable(ws, actor) if t.get("area") and _matches(t["area"], files)]
    return {"ticket": ref, "files": files, "tasks": tasks}


def _held_ticket(ws, session: str) -> str | None:
    """A ticket this session still works on (in progress or waiting, claim not expired)."""
    from orch.core import query
    for e in query.list_tickets(ws, session=session):
        claim = (e.meta or {}).get("claim") or {}
        if e.status in ("in-progress", "waiting") and not query.claim_is_expired(ws, e.id, e.status, claim):
            return e.id
    return None


# -- changes ----------------------------------------------------------------------------------------------------------

class QuickOps:
    """Every change to a quick task. Each runs under the workspace's quick lock and writes one event."""

    def __init__(self, ws, actor):
        self.ws, self.actor = ws, actor

    @property
    def _session(self) -> str:
        return self.actor.session or "local"

    def _require_on(self, cfg: dict) -> None:
        if not cfg["enabled"]:
            raise TransitionError("quick tasks are off in this workspace", hint=_HINT_ON)

    def _human(self, what: str) -> None:
        from orch.core.lifecycle import require_human
        require_human(self.actor, what)

    def _emit(self, kind: str, task: dict, data: dict | None = None, ticket: str | None = None) -> None:
        append_event(self.ws, ticket, kind, self.actor, {"quick": task["id"], **(data or {})})

    def _change(self, ref: str, fn, kind: str) -> dict:
        with lock(self.ws, "quick"):
            task = load(self.ws, ref)
            extra = fn(task)
            _save(self.ws, task)
        self._emit(kind, task, extra if isinstance(extra, dict) else None)
        return task

    def add(self, title: str, area: str | None = None) -> dict:
        cfg = settings(self.ws)
        self._require_on(cfg)
        if not self.actor.is_human and not cfg["agents_add"]:
            raise HumanOnlyError("agents do not add quick tasks in this workspace",
                                 hint="file a backlog ticket with `orch new`, or ask the human to add the quick task")
        title = _one_line(title, "the title", MAX_TITLE)
        area = _area(area)
        with lock(self.ws, "quick"):
            task = {"id": _next_id(self.ws), "title": title, "area": area, "status": "open",
                    "added": {"at": stamp_s(), "by": self.actor.to_str(), "session": self._session},
                    "claim": None, "done": None, "outgrew": None, "waived": False, "ticket": None,
                    "artifacts": [], "notes": []}
            _save(self.ws, task)
        self._emit("quick.added", task, {"title": title})
        return task

    def claim(self, ref: str) -> dict:
        cfg = settings(self.ws)
        self._require_on(cfg)
        if not self.actor.is_human:
            held = _held_ticket(self.ws, self._session)
            if held:
                raise ClaimError(f"this session still works on {held}: finish it first",
                                 hint=f"a quick task comes after the ticket: once {held} is in testing, "
                                      "`orch quick near` lists the ones in the same files")

        def fn(t: dict):
            if t["status"] != "open":
                raise TransitionError(f"{t['id']} is {t['status']}; only open quick tasks can be claimed")
            if t.get("outgrew"):
                raise TransitionError(f"{t['id']} outgrew a quick task; the human decides what happens to it",
                                      hint=f"the human can make it a ticket (`orch quick promote {t['id']}`)")
            current = active_claim(t, cfg["claim_minutes"])
            if current and current.get("session") != self._session:
                raise ClaimError(f"{t['id']} is claimed by {current.get('harness')} since {current.get('at')}",
                                 hint=f"a quick-task claim goes stale after {cfg['claim_minutes']} min")
            if not self.actor.is_human:
                if _filed_by_this_agent(t, self.actor):
                    raise ClaimError(f"{t['id']} was filed by this session; an agent does not pick up its own quick task",
                                     hint="leave it for the human or another session")
                for other in all_tasks(self.ws):
                    oc = active_claim(other, cfg["claim_minutes"])
                    if other["id"] != t["id"] and other["status"] == "open" and oc and oc.get("session") == self._session:
                        raise ClaimError(f"this session already holds {other['id']}: one quick task at a time",
                                         hint=f"close it with `orch quick done {other['id']} -m …` or release it")
            t["claim"] = {"session": self._session, "harness": self.actor.name if not self.actor.is_human else "you",
                          "at": stamp_s()}
        return self._change(ref, fn, "quick.claimed")

    def release(self, ref: str) -> dict:
        def fn(t: dict):
            c = t.get("claim") or {}
            if not c:
                raise TransitionError(f"{t['id']} is not claimed")
            if not self.actor.is_human and c.get("session") != self._session:
                raise ClaimError(f"{t['id']} is claimed by another session")
            t["claim"] = None
        return self._change(ref, fn, "quick.released")

    def done(self, ref: str, note: str | None) -> dict:
        cfg = settings(self.ws)
        self._require_on(cfg)
        note = _one_line(note, "the note", MAX_NOTE, required=not self.actor.is_human)
        outgrown: dict = {}

        def fn(t: dict):
            if t["status"] != "open":
                raise TransitionError(f"{t['id']} is {t['status']}")
            if t.get("outgrew"):
                raise TransitionError(f"{t['id']} outgrew a quick task; the human decides what happens to it")
            if not self.actor.is_human:
                c = active_claim(t, cfg["claim_minutes"])
                if not c or c.get("session") != self._session:
                    raise ClaimError(f"claim {t['id']} first (`orch quick claim {t['id']}`)")
                if not t.get("waived"):
                    s = size(self.ws, t["id"])
                    if len(s["commits"]) > cfg["max_commits"] or len(s["files"]) > cfg["max_files"]:
                        t["outgrew"] = {"at": stamp_s(), "commits": len(s["commits"]), "files": s["files"][:50],
                                        "files_n": len(s["files"])}
                        t["claim"] = None
                        outgrown.update(t["outgrew"])
                        return {"commits": len(s["commits"]), "files": len(s["files"])}
            t["status"] = "done"
            t["claim"] = None
            t["done"] = {"at": stamp_s(), "by": self.actor.to_str(), "note": note}
            return None

        with lock(self.ws, "quick"):
            task = load(self.ws, ref)
            extra = fn(task)
            _save(self.ws, task)
        self._emit("quick.outgrew" if outgrown else "quick.done", task, extra or ({"note": note} if note else None))
        if outgrown:
            # the task is saved as outgrown and the event recorded; say plainly why nothing closed
            raise ValidationError(
                f"{task['id']} outgrew a quick task: {outgrown['commits']} commit(s), {outgrown['files_n']} file(s) "
                f"(the limit is {cfg['max_commits']} commit(s), {cfg['max_files']} file(s)); stop here",
                hint="the human decides: make it a ticket, let it finish, or drop the change")
        return task

    def reopen(self, ref: str, note: str | None = None) -> dict:
        """Human only: a done task back to open (with why), or an outgrown one let through ("let it finish")."""
        self._human("reopening a quick task")
        note = _one_line(note, "the note", MAX_NOTE, required=False)

        def fn(t: dict):
            if t["status"] == "done":
                t["status"], t["done"] = "open", None
            elif t["status"] == "open" and t.get("outgrew"):
                t["outgrew"], t["waived"] = None, True
            else:
                raise TransitionError(f"{t['id']} is {t['status']}: only a done or outgrown quick task can be reopened")
            if note:
                t.setdefault("notes", []).append({"at": stamp_s(), "by": self.actor.to_str(), "text": note})
            return {"note": note} if note else None
        return self._change(ref, fn, "quick.reopened")

    def drop(self, ref: str) -> dict:
        self._human("dropping a quick task")

        def fn(t: dict):
            if t["status"] not in ("open", "done"):
                raise TransitionError(f"{t['id']} is {t['status']}")
            t["status"], t["claim"] = "dropped", None
        return self._change(ref, fn, "quick.dropped")

    def promote(self, ref: str) -> tuple[dict, object]:
        """Make it a ticket: a backlog chore with the line as its title; its requirements still need the human."""
        from orch.core.ops import Ops
        task = load(self.ws, ref)
        if task["status"] != "open":
            raise TransitionError(f"{task['id']} is {task['status']}; only an open quick task can become a ticket")
        c = active_claim(task, settings(self.ws)["claim_minutes"])
        if not self.actor.is_human and c and c.get("session") != self._session:
            raise ClaimError(f"{task['id']} is claimed by another session")
        ask = f"From quick task {task['id']}: {task['title']}"
        if task.get("area"):
            ask += f"\n\nArea: `{task['area']}`"
        if task.get("outgrew"):
            o = task["outgrew"]
            ask += f"\n\nIt outgrew a quick task: {o.get('commits', 0)} commit(s), {o.get('files_n', 0)} file(s)."
        ticket = Ops(self.ws, self.actor).new(task["title"], type="chore", size="s", ask=ask)

        def fn(t: dict):
            if t["status"] != "open":
                raise TransitionError(f"{t['id']} is {t['status']}")
            t["status"], t["ticket"], t["claim"] = "moved", ticket.id, None
            return {"ticket": ticket.id}
        return self._change(ref, fn, "quick.promoted"), ticket

    def artifact_add(self, ref: str, file: Path | None = None, *, url: str | None = None, name: str | None = None,
                     label: str | None = None, kind: str | None = None) -> dict:
        from orch.core import artifacts as art
        from orch.core.ops import _artifact_kind, _artifact_label, _artifact_name, _copy_capped
        cfg = settings(self.ws)
        if (file is None) == (url is None):
            raise UsageError("pass a file or --url")
        label = _artifact_label(label)
        item: dict
        if url is not None:
            url = url.strip()
            if not re.match(r"^https?://[^\s]+$", url):
                raise UsageError("--url takes an http(s) link")
            item = {"url": url, "kind": _artifact_kind(kind, art.guess_kind(url=url))}
            fname = None
        else:
            src = Path(file)
            if src.is_symlink() or not src.is_file():
                raise UsageError(f"not a file: {file}")
            fname = _artifact_name(name or src.name)
            if not art.safe_name(fname):
                raise UsageError(f"invalid artifact name {name or src.name!r}")
            limit = art.max_bytes(self.ws)
            if src.stat().st_size > limit:
                raise ValidationError(f"{src.name} is larger than the artifact limit of {limit} bytes")
            item = {"name": fname, "kind": _artifact_kind(kind, art.guess_kind(fname))}

        def fn(t: dict):
            if t["status"] not in ("open", "done"):
                raise TransitionError(f"{t['id']} is {t['status']}")
            if not self.actor.is_human:
                c = active_claim(t, cfg["claim_minutes"])
                if t["status"] != "open" or not c or c.get("session") != self._session:
                    raise ClaimError(f"claim {t['id']} first: artifacts come with the work")
            items = t.setdefault("artifacts", [])
            if len(items) >= cfg["max_artifacts"]:
                raise ValidationError(f"{t['id']} already has {len(items)} artifacts, the most a quick task takes")
            if fname is not None:
                dest = self.ws.artifacts_dir / t["id"] / fname
                if dest.exists() or any(a.get("name") == fname for a in items):
                    raise ValidationError(f"artifact {t['id']}/{fname} already exists", hint="pass --name")
                _copy_capped(Path(file), None, dest, art.max_bytes(self.ws))
                item.update(sha256=art.file_sha256(dest), size=dest.stat().st_size)
            if label:
                item["label"] = label
            item.update(added=stamp_s(), by=self.actor.to_str())
            items.append(item)
            return {"name": fname or item.get("url")}
        return self._change(ref, fn, "quick.artifact")


def commit_problem(ws, key: str) -> str | None:
    """The commit-msg hook's check for a quick-task key in a subject: it must name an open quick task."""
    try:
        task = load(ws, key)
    except NotFoundError:
        return f"no quick task {key} (`orch quick` lists them)"
    if not enabled(ws):
        return "quick tasks are off in this workspace; use a ticket key"
    if task["status"] != "open":
        return f"{task['id']} is {task['status']}; commit under an open quick task or a ticket"
    if task.get("outgrew"):
        return f"{task['id']} outgrew a quick task; the human decides what happens to it"
    return None
