"""Schedules (docs/schedules.md): agent work that starts without anyone typing. Three kinds, one file each in
`orchestrator/schedules/<id>.yaml`, committed with the tickets:

- **schedule**: runs a workspace skill (`.claude/skills/<name>/`) on a clock (`every` within active hours, `at` given
  times, or a `cron` line), as one headless agent session;
- **listener**: runs a skill when an orch event matches (a ticket moved to testing, a question asked, ...), with a
  cooldown;
- **recurring**: puts a ticket from a template on Today on a cadence; the human files it with one click.

A run never decides for the human. It ends with a report: quiet, or findings the human sees on Today, each with an
optional proposed ticket that only the human files (`file_finding`, human only).

Being armed is not in the file, which agents can edit. It is a signed ledger setting (`schedule:<id>`, chained per
checkout, orch.core.ledger.record_setting) holding the sha256 of the definition and of every file of its skill.
`state()` compares those with the files as they are now: any change reads as "changed" and nothing runs until the
human arms it again. Pausing takes power away, so anyone may pause (a signed off, like `widgets.html`); arming and
resuming are the human's.

Runs, budget markers and cursors live beside the ledger (orch config dir, `schedules/<checkout id>/`), outside the
repository, so editing the workspace cannot reset a budget. Every reader fails closed.
"""
from __future__ import annotations

import calendar
import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from orch.errors import NotFoundError, OrchError, UsageError, ValidationError

ID_RE = re.compile(r"[a-z][a-z0-9-]{0,39}")
SKILL_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
MODEL_RE = re.compile(r"[A-Za-z0-9._:/\[\]-]{1,64}")
MCP_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
RUN_RE = re.compile(r"r-\d{8}-\d{4}-[0-9a-f]{6}")
KINDS = ("schedule", "listener", "recurring")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# The orch events a listener may wait for (orch.core.events), and the one filter each knows.
LISTEN = {"ticket.moved": "to", "ticket.created": None, "question.asked": None, "verdict.given": None,
          "artifact.added": None, "gate.approved": "gate"}
MAY = ("report",)  # what a run may do besides reading; proposals become tickets only through the human
MAX_DEF_BYTES = 16 * 1024
MAX_SKILL_FILES = 64
MAX_SKILL_BYTES = 1 << 20
MAX_FINDINGS = 10
MAX_RUNS_KEPT = 200  # per workspace checkout; the oldest finished ones go first
MIN_EVERY = 5  # minutes
DEFAULTS = {"runs_per_day": 24, "minutes_per_run": 10}
LIMITS = {"runs_per_day": (1, 96), "minutes_per_run": (1, 60), "cooldown_minutes": (0, 1440)}
OPEN, FILED, DISMISSED = "open", "filed", "dismissed"


# -- definitions ------------------------------------------------------------------------------------------------------

@dataclass
class Definition:
    id: str
    path: Path
    raw: bytes
    kind: str = "schedule"
    name: str = ""
    skill: str | None = None
    when: dict = field(default_factory=dict)
    on: dict = field(default_factory=dict)
    ticket: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)
    model: str | None = None
    mcp: tuple = ()
    problems: list = field(default_factory=list)

    @property
    def sha(self) -> str:
        return "sha256:" + hashlib.sha256(self.raw).hexdigest()

    @property
    def ok(self) -> bool:
        return not self.problems


def folder(ws) -> Path:
    return ws.home / "schedules"


def _int(v, key, problems, default):
    lo, hi = LIMITS[key]
    if v is None:
        return default
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        problems.append(f"limits.{key} must be a whole number from {lo} to {hi}")
        return default
    return v


def _minutes(text) -> int | None:
    """`15m`, `1h`, `90m` -> minutes; None when it is not such a duration."""
    m = re.fullmatch(r"\s*(\d{1,4})\s*([mh])\s*", str(text)) if isinstance(text, (str, int)) else None
    if not m:
        return None
    n = int(m.group(1)) * (60 if m.group(2) == "h" else 1)
    return n or None


def _clock(text) -> int | None:
    """`08:00` -> minutes after midnight."""
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", text) if isinstance(text, str) else None
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _parse_when(when, problems: list) -> dict:
    """Normalise `when` to {every, between, at, days, cron} or report what is wrong."""
    if not isinstance(when, dict):
        problems.append("`when` must be a mapping (every, at or cron)")
        return {}
    out: dict = {}
    days = when.get("days")
    if days is not None:
        if isinstance(days, str):
            days = [days]
        if not isinstance(days, list) or not days or any(d not in DAYS for d in days):
            problems.append("`when.days` must list days as mon, tue, wed, thu, fri, sat, sun")
        else:
            out["days"] = sorted({DAYS.index(d) for d in days})
    given = [k for k in ("every", "cron") if k in when] + (["at"] if "at" in when or "weekly" in when else [])
    if len(given) != 1:  # `weekly` goes with `at` (default 09:00)
        problems.append("`when` needs exactly one of every, at, weekly or cron")
        return out
    if "every" in when:
        n = _minutes(when["every"])
        if n is None or n < MIN_EVERY or n > 720:
            problems.append(f"`when.every` must be a duration such as 15m or 2h, from {MIN_EVERY}m to 12h")
        else:
            out["every"] = n
        between = when.get("between", "00:00-23:59")
        parts = between.split("-") if isinstance(between, str) else []
        lo, hi = (_clock(parts[0].strip()), _clock(parts[1].strip())) if len(parts) == 2 else (None, None)
        if lo is None or hi is None or hi <= lo:
            problems.append("`when.between` must look like 08:00-19:00, start before end")
        else:
            out["between"] = (lo, hi)
    elif "at" in when or "weekly" in when:
        at = when.get("at", "09:00")
        ats = [at] if isinstance(at, str) else at
        mins = [_clock(a) for a in ats] if isinstance(ats, list) and ats else [None]
        if any(m is None for m in mins) or len(mins) > 24:
            problems.append("`when.at` must be a time such as 07:00, or a list of up to 24")
        else:
            out["at"] = sorted(set(mins))
        if "weekly" in when:
            d = when["weekly"]
            if d not in DAYS:
                problems.append("`when.weekly` must be a day: mon, tue, wed, thu, fri, sat or sun")
            else:
                out["days"] = [DAYS.index(d)]
    else:
        try:
            out["cron"] = Cron(when["cron"])
        except ValueError as e:
            problems.append(f"`when.cron`: {e}")
    return out


def _parse_ticket(t, problems: list) -> dict:
    from orch.core.constants import PRIORITIES, TYPES
    if not isinstance(t, dict):
        problems.append("`ticket` must be a mapping with a title and an ask")
        return {}
    title, ask = t.get("title"), t.get("ask", "")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        problems.append("`ticket.title` must be text of 1 to 200 characters")
    if not isinstance(ask, str) or len(ask) > 20000:
        problems.append("`ticket.ask` must be text of at most 20000 characters")
    typ, prio = t.get("type", "chore"), t.get("priority", "normal")
    if typ not in TYPES or typ == "epic":
        problems.append("`ticket.type` must be a ticket type other than epic")
    if prio not in PRIORITIES:
        problems.append(f"`ticket.priority` must be one of {', '.join(PRIORITIES)}")
    return {"title": title if isinstance(title, str) else "", "ask": ask if isinstance(ask, str) else "",
            "type": typ, "priority": prio}


def parse(sid: str, path: Path, raw: bytes) -> Definition:
    """Read one definition. Never raises: what is wrong lands in `problems`, and a definition with problems never
    runs and cannot be armed."""
    from orch.core.model import yaml_load
    d = Definition(id=sid, path=path, raw=raw)
    p = d.problems
    if not ID_RE.fullmatch(sid):
        p.append("the file name must be a lower-case id such as check-inbox.yaml")
    try:
        data = yaml_load(raw.decode("utf-8"))
    except Exception:
        p.append("the file is not valid YAML")
        return d
    if not isinstance(data, dict):
        p.append("the file must hold a mapping")
        return d
    if True in data and "on" not in data:  # YAML 1.1 reads a bare `on:` key as true
        data["on"] = data.pop(True)
    known = {"name", "kind", "skill", "when", "on", "cooldown", "ticket", "limits", "model", "mcp", "may"}
    for k in sorted(set(data) - known):
        p.append(f"unknown key `{k}`")
    d.kind = data.get("kind", "schedule")
    if d.kind not in KINDS:
        p.append("`kind` must be schedule, listener or recurring")
        return d
    name = data.get("name", sid)
    d.name = " ".join(name.split()) if isinstance(name, str) else ""
    if not d.name or len(d.name) > 80:
        p.append("`name` must be text of 1 to 80 characters")
    may = data.get("may", ["report"])
    if not isinstance(may, list) or any(m not in MAY for m in may):
        p.append("`may` only knows report: a run proposes, the human files tickets on Today")
    if d.kind in ("schedule", "listener"):
        skill = data.get("skill")
        if not isinstance(skill, str) or not SKILL_RE.fullmatch(skill):
            p.append("`skill` must name a folder in .claude/skills/ (lower-case letters, digits, - and _)")
        else:
            d.skill = skill
        model = data.get("model")
        if model is not None and (not isinstance(model, str) or not MODEL_RE.fullmatch(model)):
            p.append("`model` must be a model name (letters, digits and . _ : / [ ] -)")
        else:
            d.model = model
        mcp = data.get("mcp", [])
        if not isinstance(mcp, list) or len(mcp) > 8 or any(not isinstance(m, str) or not MCP_RE.fullmatch(m) for m in mcp):
            p.append("`mcp` must list up to 8 server names from your schedule-mcp.json")
        else:
            d.mcp = tuple(dict.fromkeys(mcp))
        if "ticket" in data:
            p.append("`ticket` belongs to a recurring schedule")
    if d.kind == "recurring":
        d.ticket = _parse_ticket(data.get("ticket"), p)
        for k in ("skill", "model", "mcp"):
            if k in data:
                p.append(f"`{k}` belongs to a schedule or listener: a recurring schedule files a ticket, it runs nothing")
    if d.kind == "listener":
        on = data.get("on")
        if not isinstance(on, dict) or on.get("event") not in LISTEN:
            p.append("`on.event` must be one of " + ", ".join(LISTEN))
        else:
            d.on = {"event": on["event"]}
            filt = LISTEN[on["event"]]
            extra = set(on) - {"event"} - ({filt} if filt else set())
            if extra:
                p.append(f"`on` knows only event{' and ' + filt if filt else ''} for {on['event']}")
            if filt and filt in on:
                v = on[filt]
                vals = v if isinstance(v, list) else [v]
                if not vals or any(not isinstance(x, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", x) for x in vals):
                    p.append(f"`on.{filt}` must be a word or a list of words")
                else:
                    d.on[filt] = sorted(set(vals))
        cd = _minutes(data.get("cooldown", "30m"))
        if cd is None and data.get("cooldown") not in (0, "0", "0m"):
            p.append("`cooldown` must be a duration such as 30m")
        d.limits["cooldown_minutes"] = cd or 0
        if "when" in data:
            p.append("a listener starts on `on`, not on `when`")
    else:
        d.when = _parse_when(data.get("when"), p)
        if "on" in data or "cooldown" in data:
            p.append("`on` and `cooldown` belong to a listener")
    limits = data.get("limits") or {}
    if not isinstance(limits, dict) or set(limits) - {"runs_per_day", "minutes_per_run"}:
        p.append("`limits` knows runs_per_day and minutes_per_run")
        limits = {}
    for k in ("runs_per_day", "minutes_per_run"):
        d.limits[k] = _int(limits.get(k), k, p, DEFAULTS[k])
    return d


def definitions(ws) -> list[Definition]:
    """Every `*.yaml` in orchestrator/schedules, sorted by id. A link, a folder or an oversized file is a definition
    with a problem, never followed."""
    out = []
    base = folder(ws)
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    for n in names:
        if not n.endswith(".yaml") or n.startswith("."):
            continue
        path = base / n
        sid = n[:-5]
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_DEF_BYTES:
            d = Definition(id=sid, path=path, raw=b"")
            d.problems.append("not a plain file of at most 16 KiB")
            out.append(d)
            continue
        from orch.core.fsutil import read_regular_file
        raw = read_regular_file(path, MAX_DEF_BYTES)
        if raw is None:
            continue
        out.append(parse(sid, path, raw))
    return out


def get(ws, sid: str) -> Definition:
    for d in definitions(ws):
        if d.id == sid:
            return d
    raise NotFoundError(f"no schedule {sid!r} in {folder(ws).relative_to(ws.root)}",
                        hint="`orch schedule list` shows them")


# -- skills -----------------------------------------------------------------------------------------------------------

def skill_dir(ws, skill: str) -> Path:
    return ws.root / ".claude" / "skills" / skill


def skill_hash(ws, skill: str | None) -> tuple[str | None, str | None]:
    """(sha256 over every file of the skill folder, why not) for a schedule or listener; (None, None) when the kind runs
    no skill. The hash covers relative paths and contents, sorted. A link anywhere, more than 64 files, more than
    1 MiB or no SKILL.md is a problem, and such a skill cannot be armed."""
    if skill is None:
        return None, None
    base = skill_dir(ws, skill)
    try:
        if os.path.islink(base) or not base.is_dir():
            return None, f"no skill folder .claude/skills/{skill}"
    except OSError:
        return None, f"no skill folder .claude/skills/{skill}"
    h, total, files = hashlib.sha256(), 0, []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for dn in dirnames:
            if os.path.islink(os.path.join(dirpath, dn)):
                return None, f"the skill folder holds a link ({dn}); links are not followed"
        for fn in sorted(filenames):
            p = os.path.join(dirpath, fn)
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode):
                return None, f"the skill folder holds something that is not a plain file ({fn})"
            files.append(p)
    if not any(os.path.relpath(p, base) == "SKILL.md" for p in files):
        return None, f".claude/skills/{skill} has no SKILL.md"
    if len(files) > MAX_SKILL_FILES:
        return None, f"the skill folder holds more than {MAX_SKILL_FILES} files"
    for p in sorted(files, key=lambda x: os.path.relpath(x, base)):
        rel = os.path.relpath(p, base).replace(os.sep, "/")
        data = Path(p).read_bytes()
        total += len(data)
        if total > MAX_SKILL_BYTES:
            return None, "the skill folder is larger than 1 MiB"
        h.update(rel.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
    return "sha256:" + h.hexdigest(), None


def skill_summary(ws, skill: str | None) -> str:
    """The skill's `description` from the SKILL.md frontmatter, for the page; empty when there is none."""
    if not skill:
        return ""
    from orch.core.fsutil import read_regular_file
    raw = read_regular_file(skill_dir(ws, skill) / "SKILL.md", 64 * 1024)
    text = raw.decode("utf-8", "replace") if raw else ""
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if m:
        d = re.search(r"^description:\s*(.+)$", m.group(1), re.M)
        if d:
            return d.group(1).strip().strip("\"'")[:400]
    return ""


# -- the human's charter ----------------------------------------------------------------------------------------------

def setting_name(sid: str) -> str:
    return f"schedule:{sid}"


def _human(actor, what: str) -> None:
    from orch.core.lifecycle import require_human
    require_human(actor, what)


@dataclass
class State:
    code: str  # invalid | draft | armed | paused | changed
    label: str
    why: str = ""
    def_sha: str = ""
    skill_sha: str | None = None
    signed: dict | None = None

    @property
    def armed(self) -> bool:
        return self.code == "armed"


def state(ws, d: Definition, signed: list[dict] | None = None) -> State:
    """Where a schedule stands. Only "armed" runs."""
    from orch.core import ledger
    sk, why = skill_hash(ws, d.skill)
    if not d.ok or why:
        return State("invalid", "needs a fix", "; ".join([*d.problems, *([why] if why else [])]), d.sha, sk)
    v = ledger.signed_setting(ws, setting_name(d.id), signed)
    if v is False:
        return State("paused", "paused", "paused; resume to arm it again with the files as they are now", d.sha, sk)
    if not isinstance(v, dict) or v.get("state") != "armed":
        return State("draft", "not armed", "not armed yet: arm it after you read the definition and the skill",
                     d.sha, sk)
    changed = []
    if v.get("def") != d.sha:
        changed.append("the definition")
    if v.get("skill") != sk:
        changed.append(f"the skill {d.skill}")
    if changed:
        return State("changed", "changed", " and ".join(changed) + " changed since you armed it: review and arm it "
                     "again", d.sha, sk, v)
    return State("armed", "armed", "", d.sha, sk, v)


def arm(ws, actor, sid: str, *, expected_def: str | None = None, expected_skill: str | None = None) -> dict:
    """Human only: sign the schedule as it stands. With `expected_*` (the hashes the human saw), refused when the
    files changed since."""
    from orch.actor import process_evidence
    from orch.core import ledger
    from orch.core.locks import lock
    _human(actor, "arming a schedule")
    d = get(ws, sid)
    st = state(ws, d)
    if st.code == "invalid":
        raise ValidationError(f"{sid} cannot be armed: {st.why}")
    if expected_def is not None and expected_def != st.def_sha or \
            expected_skill is not None and expected_skill != (st.skill_sha or ""):
        raise ValidationError(f"{sid} changed since you looked at it; nothing was signed", hint="read it again")
    value = {"state": "armed", "def": st.def_sha, "skill": st.skill_sha, "kind": d.kind}
    with lock(ws, "config"):
        ledger.record_setting(ws, setting_name(sid), value, actor, process_evidence())
    reset_cursor(ws, sid)  # an armed schedule starts from now: nothing missed before it was armed runs
    return value


def pause(ws, actor, sid: str) -> None:
    """Anyone: pausing takes power away (signed off, like `widgets.html` off). Resuming is `arm` again."""
    from orch.actor import process_evidence
    from orch.core import ledger
    from orch.core.locks import lock
    get(ws, sid)
    with lock(ws, "config"):
        ledger.record_setting(ws, setting_name(sid), False, actor, process_evidence() if actor.is_human else None)


# -- cadence ----------------------------------------------------------------------------------------------------------

class Cron:
    """Five fields (minute hour day-of-month month day-of-week), each `*`, a number, a range `a-b`, a list and an
    optional step `/n`. Day of week 0-7 (0 and 7 are Sunday). When both day fields are restricted, either matching is
    enough (as in cron)."""
    _RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

    def __init__(self, text):
        if not isinstance(text, str) or len(text) > 120:
            raise ValueError("must be five fields such as `0 8-19 * * 1-5`")
        parts = text.split()
        if len(parts) != 5:
            raise ValueError("must be five fields such as `0 8-19 * * 1-5`")
        self.text = " ".join(parts)
        self.sets = [self._field(p, lo, hi) for p, (lo, hi) in zip(parts, self._RANGES)]
        self.sets[4] = {d % 7 for d in self.sets[4]}
        self.any_dom, self.any_dow = parts[2] == "*", parts[4] == "*"

    @staticmethod
    def _field(p: str, lo: int, hi: int) -> set:
        out = set()
        for item in p.split(","):
            m = re.fullmatch(r"(\*|\d{1,2}(?:-\d{1,2})?)(?:/(\d{1,2}))?", item)
            if not m:
                raise ValueError(f"cannot read {item!r}")
            step = int(m.group(2) or 1)
            if m.group(1) == "*":
                a, b = lo, hi
            elif "-" in m.group(1):
                a, b = (int(x) for x in m.group(1).split("-"))
            else:
                a = b = int(m.group(1))
                if m.group(2):
                    b = hi
            if not (lo <= a <= b <= hi) or step < 1:
                raise ValueError(f"{item!r} is out of range {lo}-{hi}")
            out.update(range(a, b + 1, step))
        return out

    def matches(self, t: datetime) -> bool:
        mi, ho, dom, mo, dow = self.sets
        if t.minute not in mi or t.hour not in ho or t.month not in mo:
            return False
        cdow = (t.weekday() + 1) % 7  # cron: 0 is Sunday
        if self.any_dom or self.any_dow:
            return t.day in dom and cdow in dow
        return t.day in dom or cdow in dow


def _due_at(when: dict, t: datetime) -> bool:
    """Whether minute `t` (local time, seconds dropped) is a slot of `when`."""
    if "days" in when and t.weekday() not in when["days"]:
        return False
    m = t.hour * 60 + t.minute
    if "every" in when:
        lo, hi = when.get("between", (0, 1439))
        return lo <= m <= hi and (m - lo) % when["every"] == 0
    if "at" in when:
        return m in when["at"]
    if "cron" in when:
        return when["cron"].matches(t)
    return False


def _minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


SEARCH_MINUTES = 8 * 1440 + 60


def next_slot(when: dict, after: datetime) -> datetime | None:
    """The first slot strictly after `after` (local, aware), within the next eight days; None when there is none."""
    t = _minute(after) + timedelta(minutes=1)
    for _ in range(SEARCH_MINUTES):
        if _due_at(when, t):
            return t
        t += timedelta(minutes=1)
    return None


def last_slot(when: dict, at: datetime, since: datetime) -> tuple[datetime | None, int]:
    """(the newest slot in (since, at], how many slots there were in that window), the search capped at eight days."""
    t, newest, n = _minute(at), None, 0
    floor = max(_minute(since), _minute(at) - timedelta(minutes=SEARCH_MINUTES))
    while t > floor:
        if _due_at(when, t):
            newest = newest or t
            n += 1
        t -= timedelta(minutes=1)
    return newest, n


def describe(d: Definition) -> str:
    """The trigger in plain words, for lists and pages."""
    if d.kind == "listener":
        if not d.on:
            return "on an orch event"
        filt = LISTEN.get(d.on["event"])
        extra = f" ({filt} {', '.join(d.on[filt])})" if filt and d.on.get(filt) else ""
        return f"on {d.on['event']}{extra}"
    w = d.when
    days = ""
    if "days" in w:
        ds = w["days"]
        days = " weekdays" if ds == [0, 1, 2, 3, 4] else " " + ", ".join(DAYS[i].capitalize() for i in ds)
    if "every" in w:
        n = w["every"]
        every = f"every {n // 60} h" if n % 60 == 0 else f"every {n} min"
        lo, hi = w.get("between", (0, 1439))
        win = "" if (lo, hi) == (0, 1439) else f" · {lo // 60:02d}:{lo % 60:02d}-{hi // 60:02d}:{hi % 60:02d}"
        return every + win + days
    if "at" in w:
        return "at " + ", ".join(f"{m // 60:02d}:{m % 60:02d}" for m in w["at"]) + (days or " daily")
    if "cron" in w:
        return f"cron {w['cron'].text}"
    return "never"


# -- machine-local state beside the ledger ----------------------------------------------------------------------------

def _root(ws) -> Path:
    from orch.core import ledger
    return ledger.base_dir() / "schedules" / ledger.checkout_id(ws)


def _write_json(path: Path, body: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    from orch.core.fsutil import read_regular_file
    raw = read_regular_file(path, 1 << 20)
    if raw is None:
        return None
    try:
        v = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return v if isinstance(v, dict) else None


@contextlib.contextmanager
def _locked(ws):
    from filelock import FileLock, Timeout
    root = _root(ws)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with FileLock(str(root / "lock"), timeout=10):
            yield
    except Timeout as e:
        raise OrchError("the schedules folder is busy; try again") from e


def cursor(ws, sid: str) -> dict:
    return _read_json(_root(ws) / "cursors" / f"{sid}.json") or {}


def set_cursor(ws, sid: str, **fields) -> None:
    _write_json(_root(ws) / "cursors" / f"{sid}.json", {**cursor(ws, sid), **fields})


def reset_cursor(ws, sid: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        (_root(ws) / "cursors" / f"{sid}.json").unlink()


def runs_today(ws, sid: str, day: str) -> int:
    try:
        return sum(1 for n in os.listdir(_root(ws) / "budget" / sid) if n.startswith(day + "."))
    except FileNotFoundError:
        return 0
    except OSError:
        return 10**9  # unreadable: the limit (fail closed)


def take_budget(ws, sid: str, day: str, cap: int) -> bool:
    """Count one run of `sid` on `day`; False once `cap` are used. Exclusive files: editing a run record cannot give
    one back."""
    d = _root(ws) / "budget" / sid
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    for n in range(1, cap + 1):
        try:
            os.close(os.open(d / f"{day}.{n}", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            return True
        except FileExistsError:
            continue
    return False


# -- runs -------------------------------------------------------------------------------------------------------------

def _runs_dir(ws) -> Path:
    return _root(ws) / "runs"


def new_run_id(now: datetime) -> str:
    return f"r-{now.strftime('%Y%m%d-%H%M')}-{secrets.token_hex(3)}"


def run(ws, rid: str) -> dict | None:
    if not isinstance(rid, str) or not RUN_RE.fullmatch(rid):
        return None
    r = _read_json(_runs_dir(ws) / f"{rid}.json")
    return r if r and r.get("id") == rid else None


def runs(ws, sid: str | None = None, limit: int | None = None) -> list[dict]:
    """Run records, newest first."""
    try:
        names = sorted((n for n in os.listdir(_runs_dir(ws)) if n.endswith(".json") and not n.startswith(".")),
                       reverse=True)
    except OSError:
        return []
    out = []
    for n in names:
        r = run(ws, n[:-5])
        if r is None or (sid is not None and r.get("schedule") != sid):
            continue
        out.append(r)
        if limit is not None and len(out) >= limit:
            break
    return out


def save_run(ws, r: dict) -> None:
    _write_json(_runs_dir(ws) / f"{r['id']}.json", r)


def prune(ws) -> None:
    """Keep the newest MAX_RUNS_KEPT run records; a run with an open finding stays."""
    old = runs(ws)[MAX_RUNS_KEPT:]
    for r in old:
        if r.get("status") == "running" or any(f.get("state") == OPEN for f in r.get("findings") or []):
            continue
        with contextlib.suppress(OSError):
            (_runs_dir(ws) / f"{r['id']}.json").unlink()


def token_sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_record(ws, d: Definition, *, trigger: str, now: datetime, slot: str | None = None, event: dict | None = None,
                 skipped: int = 0) -> tuple[dict, str]:
    """A new run record (status "starting") and its report token, the run's one secret (only its sha is kept)."""
    token = secrets.token_hex(16)
    r = {"id": new_run_id(now), "schedule": d.id, "name": d.name, "kind": d.kind, "trigger": trigger, "slot": slot,
         "event": event, "skipped": skipped, "status": "starting", "started": now.isoformat(timespec="seconds"),
         "ended": None, "reason": "", "token": token_sha(token), "session": None, "summary": "", "findings": []}
    save_run(ws, r)
    return r, token


def finish(ws, r: dict, status: str, reason: str = "", now: datetime | None = None) -> dict:
    r["status"], r["reason"] = status, reason
    r["ended"] = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    save_run(ws, r)
    return r


def open_runs(ws) -> list[dict]:
    return [r for r in runs(ws) if r.get("status") in ("starting", "running")]


# -- reports and findings ---------------------------------------------------------------------------------------------

REPORT_HELP = """\
summary: One line about what the run did.
findings:            # at most 10; leave out when nothing needs the human
  - title: What needs the human, in a few words
    text: Why, with the facts that show it.
    ticket:          # optional: a ticket the human can file with one click
      title: Fix the CSV export of umlauts
      ask: What should be done and why.
      type: bug      # feature, bug, chore, spike or investigation
      priority: high # low, normal, high or urgent
"""


def _clean(text, limit: int, what: str) -> str:
    from orch.textsafe import lines
    if not isinstance(text, str):
        raise ValidationError(f"{what} must be text")
    text = "\n".join(lines(text.replace("\r\n", "\n"))).strip()  # hidden characters become visible escapes
    if len(text) > limit:
        raise ValidationError(f"{what} is longer than {limit} characters")
    return text


def parse_report(data) -> tuple[str, list[dict]]:
    """Validate a run's report (REPORT_HELP). Raises ValidationError."""
    from orch.core.constants import PRIORITIES, TYPES
    if data is None:
        data = {}
    if not isinstance(data, dict) or set(data) - {"summary", "findings"}:
        raise ValidationError("a report is a mapping with summary and findings", hint=REPORT_HELP)
    summary = " ".join(_clean(data.get("summary", ""), 300, "summary").split())
    items = data.get("findings") or []
    if not isinstance(items, list) or len(items) > MAX_FINDINGS:
        raise ValidationError(f"findings must be a list of at most {MAX_FINDINGS}", hint=REPORT_HELP)
    out = []
    for i, f in enumerate(items, 1):
        if not isinstance(f, dict) or set(f) - {"title", "text", "ticket"}:
            raise ValidationError(f"finding {i} must have title, text and optionally ticket", hint=REPORT_HELP)
        title = " ".join(_clean(f.get("title", ""), 200, f"finding {i} title").split())
        if not title:
            raise ValidationError(f"finding {i} needs a title")
        item = {"id": f"f{i}", "title": title, "text": _clean(f.get("text", ""), 4000, f"finding {i} text"),
                "ticket": None, "state": OPEN}
        t = f.get("ticket")
        if t is not None:
            if not isinstance(t, dict) or set(t) - {"title", "ask", "type", "priority"}:
                raise ValidationError(f"finding {i} ticket has title, ask, type and priority", hint=REPORT_HELP)
            tt = " ".join(_clean(t.get("title", ""), 200, f"finding {i} ticket title").split())
            if not tt:
                raise ValidationError(f"finding {i} ticket needs a title")
            typ, prio = t.get("type", "feature"), t.get("priority", "normal")
            if typ not in TYPES or typ == "epic":
                raise ValidationError(f"finding {i} ticket type must be one of " +
                                      ", ".join(x for x in TYPES if x != "epic"))
            if prio not in PRIORITIES:
                raise ValidationError(f"finding {i} ticket priority must be one of {', '.join(PRIORITIES)}")
            item["ticket"] = {"title": tt, "ask": _clean(t.get("ask", ""), 20000, f"finding {i} ticket ask"),
                              "type": typ, "priority": prio}
        item["sha"] = finding_sha(item)
        out.append(item)
    return summary, out


def finding_sha(f: dict) -> str:
    from orch.core.canonical import canonical_json
    return hashlib.sha256(canonical_json({k: f.get(k) for k in ("id", "title", "text", "ticket")})).hexdigest()[:24]


def report(ws, token: str, data, *, quiet: bool = False) -> dict:
    """A run reports once, with its token: a summary and findings. Anyone holding the token may (it is in the run's
    prompt only); a finding is a proposal, so nothing in orch changes until the human acts on it."""
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValidationError("not a run token (32 hex characters, from the run's prompt)")
    if quiet:  # nothing for the human: a summary at most
        data = {"summary": data.get("summary", "")} if isinstance(data, dict) else {}
    summary, items = parse_report(data)
    sha = token_sha(token)
    with _locked(ws):
        for r in open_runs(ws):
            if r.get("token") == sha:
                if r.get("reported"):
                    raise ValidationError(f"run {r['id']} has reported already")
                r["reported"], r["summary"], r["findings"] = True, summary, items
                save_run(ws, r)
                return r
    raise NotFoundError("no running run holds this token (it ended, timed out or was never started)")


def open_findings(ws) -> list[tuple[dict, dict]]:
    """(run, finding) for every finding still open, newest run first."""
    return [(r, f) for r in runs(ws) for f in r.get("findings") or [] if f.get("state") == OPEN]


def _find(ws, rid: str, fid: str) -> tuple[dict, dict]:
    r = run(ws, rid)
    f = next((x for x in (r or {}).get("findings") or [] if x.get("id") == fid), None)
    if r is None or f is None:
        raise NotFoundError(f"no finding {fid} in run {rid}")
    return r, f


def file_finding(ws, actor, rid: str, fid: str, *, expected_sha: str | None = None) -> str:
    """Human only: file the finding's proposed ticket in backlog. Returns the new ticket id. The agent's text goes
    into the Ask through neutral_text, so it can never open a section."""
    from orch.core.model import neutral_text
    from orch.core.ops import Ops
    _human(actor, "filing a ticket from a schedule's finding")
    with _locked(ws):
        r, f = _find(ws, rid, fid)
        if f.get("state") != OPEN:
            raise ValidationError(f"{fid} of run {rid} is {f.get('state')} already")
        if expected_sha is not None and expected_sha != f.get("sha"):
            raise ValidationError("the finding changed since you saw it; nothing was filed")
        t = f.get("ticket") or {"title": f["title"], "ask": f.get("text", ""), "type": "feature", "priority": "normal"}
        ask = neutral_text((t.get("ask") or "").strip())
        note = f"\n\nProposed by schedule `{r['schedule']}`, run {r['id']}."
        ticket = Ops(ws, actor).new(t["title"], type=t.get("type", "feature"), priority=t.get("priority", "normal"),
                                    ask=(ask + note).strip())
        f["state"], f["ticket_id"] = FILED, ticket.id
        save_run(ws, r)
    return ticket.id


def dismiss_finding(ws, actor, rid: str, fid: str) -> None:
    """Human only: the finding needs nothing (a dismissal hides it; it is a decision about the run's proposal)."""
    _human(actor, "dismissing a schedule's finding")
    with _locked(ws):
        r, f = _find(ws, rid, fid)
        if f.get("state") != OPEN:
            raise ValidationError(f"{fid} of run {rid} is {f.get('state')} already")
        f["state"] = DISMISSED
        save_run(ws, r)


def request_run(ws, actor, sid: str) -> None:
    """Human only: ask the runner for one run now (counted against the day's budget). The dashboard's runner starts
    it within a round."""
    _human(actor, "starting a schedule run by hand")
    d = get(ws, sid)
    st = state(ws, d)
    if not st.armed:
        raise ValidationError(f"{sid} is {st.label}: {st.why}" if st.why else f"{sid} is not armed")
    set_cursor(ws, sid, requested=True)


# -- one view for the CLI and the page --------------------------------------------------------------------------------

def overview(ws, now: datetime | None = None) -> list[dict]:
    from orch.core import ledger
    now = now or datetime.now().astimezone()
    signed = ledger.entries(ws)
    out = []
    for d in definitions(ws):
        st = state(ws, d, signed)
        recent = runs(ws, d.id, limit=24)
        last = recent[0] if recent else None
        nxt = None
        if st.armed and d.kind != "listener" and d.when:
            nxt = next_slot(d.when, now)
        out.append({"id": d.id, "name": d.name or d.id, "kind": d.kind, "trigger": describe(d) if d.ok else "",
                    "skill": d.skill, "state": st.code, "label": st.label, "why": st.why, "def_sha": st.def_sha,
                    "skill_sha": st.skill_sha or "", "next": nxt.isoformat(timespec="minutes") if nxt else None,
                    "last": last, "recent": recent, "limits": dict(d.limits), "model": d.model, "mcp": list(d.mcp),
                    "today": runs_today(ws, d.id, now.strftime("%Y-%m-%d")), "path": str(d.path.relative_to(ws.root)),
                    "ticket": d.ticket or None, "on": d.on or None})
    return out


def day_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def month_name(now: datetime) -> str:
    return calendar.month_name[now.month]


def render_title(template: str, now: datetime) -> str:
    """A recurring ticket's title with {date}, {week}, {month} and {year} filled in."""
    return (template.replace("{date}", now.strftime("%Y-%m-%d")).replace("{week}", str(now.isocalendar()[1]))
            .replace("{month}", month_name(now)).replace("{year}", str(now.year)))


def problems_text(d: Definition) -> str:
    return "; ".join(d.problems)


__all__ = ["Definition", "State", "Cron", "definitions", "get", "parse", "state", "arm", "pause", "next_slot",
           "last_slot", "describe", "report", "parse_report", "file_finding", "dismiss_finding", "request_run",
           "overview", "UsageError"]
