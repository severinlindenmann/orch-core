"""From a v1 ticket to the signed events of its v2 twin: the mapping of format doc section 14 (amendment C10).

Nothing here touches a store or a key. :func:`plan_ticket` turns one :class:`~orch.importer.v1.V1Ticket` into a
:class:`TicketPlan`: an ordered list of :class:`Step` s, each one a v2 event (without actor, envelope or ``base_rev``,
which the executor adds from the real state), plus what was left out and why. The plan is a pure function of the v1
files: the same v1 ticket always gives the same steps, so a repeated or resumed import finds its own work again.

The rules, short (the amendment has the reasons):

* no new event type: ``ticket.created``, ``artifact.added``, ``ticket.updated``, ``question.asked``,
  ``status.changed`` and ``ticket.closed``, all person events of the one who imports;
* the whole v1 ticket and its history travel as one artifact (``v1-import.json``), hashed in ``artifact.added``;
* nothing v1 decided is carried as a v2 decision: no approval, no verdict, no task state, no evidence link, no
  command that could run;
* what cannot be mapped stays in that artifact and is listed; text is cleaned, never trusted.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orch import canon
from orch.canon import TextError
from orch.store.render import refs_of

from .v1 import V1Ticket, V1Workspace, read_artifact

__all__ = [
    "FileSrc",
    "PlanRefused",
    "Step",
    "TicketPlan",
    "TicketPlanner",
    "clean_text",
    "complete_text",
    "source_id",
    "uid_for",
]

MARKER = "v1-import.json"
MARKER_SCHEMA = "orch.import.v1/1"
LABEL = "imported-v1"
MAX_STR = 4096
HANDOFF_MAX = 2048
_KEY = re.compile(r"[A-Z][A-Z0-9]{0,15}-(?:(?!0000)[0-9]{4}|[1-9][0-9]{4,})")
_ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_REF = re.compile(r"\(artifact:([A-Za-z0-9][A-Za-z0-9._-]{0,127})\)")
_LABEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_GRANT = re.compile(r"gr_[0-7][0-9A-HJKMNP-TV-Z]{25}\.[A-Za-z0-9_-]{43}")
_TYPES = {"feature", "bug", "chore", "spike", "epic", "investigation"}
_KINDS = {"screenshot", "log", "report", "link", "dataset", "build", "diagram", "other"}
_KIND_OF = {"receipt": "log", "feedback": "other"}  # receipts are made by `task done --run`, feedback by a person
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def complete_text(digest: str) -> str:
    """The text of the last event of an imported ticket: it says the import of that ticket finished."""
    return f"import.v1: complete {digest}"


class PlanRefused(Exception):
    """The whole ticket is not imported; the message says why (all or nothing per ticket)."""


@dataclass(frozen=True)
class FileSrc:
    """An evidence file of v1, read at execution (and checked against ``digest`` again then)."""

    home: Path
    key: str
    v1_name: str
    size: int
    digest: str

    def read(self) -> bytes:
        data = read_artifact(self.home, self.key, self.v1_name)
        if canon.artifact_digest(data) != self.digest:
            raise OSError(f"{self.v1_name} changed since it was planned")
        return data


@dataclass
class Step:
    kind: str  # created, marker, artifact, update, question, status, close
    event: dict[str, Any]  # the event without actor, envelope and base_rev
    body: dict[str, str] | None = None
    data: bytes | FileSrc | None = None  # an artifact's bytes
    name: str | None = None  # an artifact's v2 name

    def identity(self) -> dict[str, Any]:
        out: dict[str, Any] = {"event": self.event}
        if self.body:
            out["body"] = {k: canon.section_hash(v) for k, v in sorted(self.body.items())}
        return out


@dataclass
class TicketPlan:
    v1_key: str
    key: str
    uid: str
    title: str
    ticket_type: str
    steps: list[Step]
    warnings: list[str] = field(default_factory=list)
    left_out: list[str] = field(default_factory=list)
    marker_digest: str = ""
    v1_status: str = ""

    def digest(self) -> str:
        doc = {"key": self.key, "uid": self.uid, "steps": [s.identity() for s in self.steps]}
        return "sha256:" + hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ---------------------------------------------------------------------------------------------------- identities


def source_id(ws: V1Workspace) -> str:
    """Names the v1 workspace in the marker artifact: 16 hex of a hash over its customer and prefix (v1 has no id)."""
    h = hashlib.sha256(f"orch/v2/import-v1|{ws.customer}|{ws.prefix}".encode()).hexdigest()
    return h[:16]


def _epoch_ms(stamp: Any) -> int:
    if isinstance(stamp, str):
        for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return max(0, calendar.timegm(time.strptime(stamp, fmt)) * 1000)
            except (ValueError, OverflowError):
                continue
    return 0


def uid_for(source: str, key: str, created: Any) -> str:
    """The v2 uid of a v1 ticket: a ULID whose time part is the v1 creation time (so uids sort like the v1 history)
    and whose random part is a hash of the source and the key. The same v1 ticket always has the same uid, which is
    how a second run finds the first one's work."""
    ms = min(_epoch_ms(created), (1 << 46) - 1)
    rnd = int.from_bytes(hashlib.sha256(f"orch/v2/import-v1/uid|{source}|{key}".encode()).digest()[:10], "big")
    n = (ms << 80) | rnd
    return "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


# ---------------------------------------------------------------------------------------------------- text


def clean_text(text: Any, *, one_line: bool = False, warn: Callable[[str], None] | None = None, what: str = "") -> str:
    """v1 text as v2 text: CRLF to LF, NFC, and every character §11.3 refuses (controls, bidi controls, unassigned
    code points, lone surrogates) replaced with U+FFFD, counted in a warning. A grant-shaped secret is redacted."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if one_line:
        text = " ".join(text.split("\n")).strip()
    bad = 0
    try:
        out = canon.normalize_text(text, one_line=one_line)
    except TextError:
        seen: dict[str, str] = {}
        parts = []
        for ch in text:
            r = seen.get(ch)
            if r is None:
                try:
                    canon.check_text(ch)
                    r = ch
                except TextError:
                    r = "�"
                seen[ch] = r
            if r != ch:
                bad += 1
            parts.append(r)
        try:
            out = canon.normalize_text("".join(parts), one_line=one_line)
        except TextError as e:  # pragma: no cover - defensive
            raise PlanRefused(f"{what or 'text'} cannot be made valid text: {e}") from None
    if _GRANT.search(out):
        out = _GRANT.sub("[redacted]", out)
        bad += 1
    if bad and warn:
        warn(f"{what or 'text'}: {bad} unusable characters replaced")
    return out


def _clip(text: str, limit: int = MAX_STR) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[: limit - 3].decode("utf-8", "ignore").rstrip() + "..."


# ------------------------------------------------------------------------------------------ parts of a v1 ticket


def parse_acceptance(text: str) -> tuple[list[str], int]:
    """The criteria of v1's ``## Acceptance criteria`` (``- [ ] text``, continuation lines joined) and how many other
    non-empty lines there were."""
    items: list[list[str]] = []
    other = 0
    for line in text.split("\n"):
        m = re.match(r"^\s*[-*+] \[[ xX/!-]\]\s*(.*)$", line)
        if m:
            items.append([m.group(1).strip()])
        elif line.strip() and items and line[:1] in (" ", "\t"):
            items[-1].append(line.strip())
        elif line.strip():
            other += 1
    return [" ".join(p for p in it if p) for it in items if any(it)], other


@dataclass
class V1Task:
    num: int
    state: str
    text: str
    fields: dict[str, list[str]]


_TASK = re.compile(r"^[-*+] \[(.)\] [Tt](\d+)(?: (.*))?$")
_FIELD = re.compile(r"^(?: {2,}|\t+)[-*+] ([A-Za-z]+):(?: (.*))?$")
_STATE = {" ": "todo", "/": "doing", "x": "done", "X": "done", "-": "skipped", "!": "blocked"}


def parse_tasks(text: str) -> list[V1Task]:
    out: list[V1Task] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line.strip():
            continue
        m = _TASK.match(line.rstrip())
        if m and m.group(1) in _STATE and int(m.group(2)) >= 1:
            out.append(V1Task(int(m.group(2)), _STATE[m.group(1)], " ".join((m.group(3) or "").split()), {}))
            continue
        f = _FIELD.match(line.rstrip())
        if f and out:
            out[-1].fields.setdefault(f.group(1).lower(), []).append(" ".join((f.group(2) or "").split()))
    return out


def _label(raw: Any) -> str | None:
    s = re.sub(r"[^a-z0-9._-]+", "-", str(raw).lower()).strip("-._")
    s = s[:32].rstrip("-._")
    return s if _LABEL.fullmatch(s or "!") else None


def _artifact_name(raw: str, taken: set[str]) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).lstrip("._-") or "file"
    s = s[:120]
    base, n = s, 1
    while s in taken or not _ARTIFACT_NAME.fullmatch(s):
        n += 1
        s = f"{base[:110]}-{n}"
    return s


# ---------------------------------------------------------------------------------------------------- the planner


@dataclass
class TicketPlanner:
    """What the plan of one ticket needs to know about the target workspace and the rest of the import."""

    source: str
    home: Path
    owner: str  # the person who imports (the owner of every ticket)
    repos: frozenset[str]  # settings.repos of the target workspace
    sections_by_type: dict[str, frozenset[str]]
    known_keys: frozenset[str] = frozenset()  # tickets that exist (or are planned earlier) in the target

    def plan(self, t: V1Ticket) -> TicketPlan:
        warnings: list[str] = []
        left_out: list[str] = []

        def warn(msg: str) -> None:
            warnings.append(f"{t.key}: {msg}")

        meta = t.meta
        if not _KEY.fullmatch(t.key):
            raise PlanRefused(f"{t.key} is not a key v2 can hold (PREFIX-0000 with four or more digits)")
        title = clean_text(meta.get("title"), one_line=True, warn=warn, what="title")
        title = _clip(title, 200).strip()
        if not title:
            raise PlanRefused("no title")
        v1_type = str(meta.get("type") or "feature")
        if v1_type not in _TYPES:
            raise PlanRefused(f"unknown type {v1_type!r}")
        ttype = "spike" if v1_type == "investigation" else v1_type
        allowed = self.sections_by_type[ttype]

        # -- sections
        sec = {k: v for k, v in t.sections.items()}
        texts: dict[str, str] = {}
        in_history: list[str] = []

        def take(v1name: str, v2: str | None, text: str | None = None) -> None:
            raw = sec.get(v1name, "") if text is None else text
            if not raw.strip():
                return
            if v2 is None or v2 not in allowed:
                in_history.append(v1name)
                return
            texts[v2] = clean_text(raw, warn=warn, what=f"section {v1name}").strip("\n")

        ask = sec.get("Ask", "").strip()
        ctx = sec.get("Context", "").strip()
        if ask and ctx:
            take("Context", "context", f"Ask (v1):\n\n{ask}\n\n{ctx}")
        elif ask:
            take("Ask", "context", f"Ask (v1):\n\n{ask}")
        else:
            take("Context", "context")
        take("Summary", "summary")
        take("Requirements", "requirements")
        take("Out of scope", "out_of_scope")
        take("Plan", "plan")
        take("Verification", "verification")
        take("Findings", "findings")
        for name in ("Log",):
            if sec.get(name, "").strip():
                in_history.append(name)
        for name in sec:
            if (
                name
                not in (
                    "Ask",
                    "Summary",
                    "Context",
                    "Requirements",
                    "Acceptance criteria",
                    "Out of scope",
                    "Plan",
                    "Tasks",
                    "Current state",
                    "Verification",
                    "Log",
                    "Findings",
                )
                and sec[name].strip()
            ):
                in_history.append(name)  # a section v1 does not know (an older orch's, or a hand edit)
        if in_history:
            left_out.append("sections only in the history: " + ", ".join(dict.fromkeys(in_history)))

        # -- acceptance criteria
        crit, other = parse_acceptance(sec.get("Acceptance criteria", ""))
        acceptance = []
        for i, c in enumerate(crit, 1):
            txt = _clip(clean_text(c, warn=warn, what=f"criterion {i}")).strip()
            if txt:
                acceptance.append({"id": f"AC{i}", "text": txt})
        if len(acceptance) != len(crit):
            raise PlanRefused("an empty acceptance criterion")
        if other:
            left_out.append(f"{other} lines of the acceptance criteria are not criteria (kept in the history)")

        # -- tasks (definitions only: no state, no commands)
        v1_tasks = parse_tasks(sec.get("Tasks", ""))
        nums = [x.num for x in v1_tasks]
        if len(set(nums)) != len(nums):
            raise PlanRefused("two tasks have the same id")
        tasks: list[dict[str, Any]] = []
        states: list[str] = []
        verify_dropped = 0
        for x in v1_tasks:
            text = _clip(clean_text(x.text, one_line=True, warn=warn, what=f"task T{x.num}")).strip()
            if not text:
                raise PlanRefused(f"task T{x.num} has no text")
            proves = sorted(
                {
                    f"AC{int(r.split(':', 1)[1].split()[0])}"
                    for r in x.fields.get("ref", [])
                    if re.fullmatch(r"(?i)ac:\s*\d+(?:\s.*)?", r)
                    and 1 <= int(r.split(":", 1)[1].split()[0]) <= len(acceptance)
                },
                key=lambda a: int(a[2:]),
            )
            if x.fields.get("verify"):
                verify_dropped += 1
            tasks.append({"id": f"T{x.num}", "text": text, "verify": None, "proves": proves})
            states.append(f"T{x.num} {x.state}")
        if verify_dropped:
            left_out.append(f"{verify_dropped} task verify lines (commands are never imported; kept in the history)")

        # -- fields
        sets: dict[str, Any] = {}
        prio = {"normal": "medium"}.get(str(meta.get("priority") or ""), str(meta.get("priority") or ""))
        if prio in ("low", "high", "urgent"):
            sets["ticket.priority"] = prio
        size = meta.get("size")
        if size in ("xs", "s", "m", "l"):
            sets["ticket.size"] = size
        labels = [LABEL]
        for raw in meta.get("labels") if isinstance(meta.get("labels"), list) else []:
            lab = _label(raw)
            if lab and lab not in labels:
                labels.append(lab)
            elif not lab:
                left_out.append(f"label {str(raw)[:40]!r} is no v2 label")
        sets["ticket.labels"] = sorted(labels)
        due = meta.get("due")
        if isinstance(due, str) and _DATE.fullmatch(due):
            try:
                time.strptime(due, "%Y-%m-%d")
                sets["ticket.due"] = due
            except ValueError:
                left_out.append("an invalid due date")
        parent = meta.get("parent")
        if isinstance(parent, str) and parent:
            if parent in self.known_keys:
                sets["ticket.parent"] = parent
            else:
                left_out.append(f"parent {parent[:20]} (not imported)")
        blocked = [
            b
            for b in (meta.get("blocked_by") if isinstance(meta.get("blocked_by"), list) else [])
            if isinstance(b, str)
        ]
        keep = sorted({b for b in blocked if b in self.known_keys})
        if keep:
            sets["ticket.blocked_by"] = keep
        if len(keep) != len(blocked):
            left_out.append("blocked_by entries that are not imported tickets")
        links, link_left = self._links(meta)
        if links != {"repos": [], "branches": {}, "prs": [], "external": []}:
            sets["ticket.links"] = links
        left_out += link_left
        if acceptance:
            sets["ticket.acceptance"] = acceptance
        if tasks:
            sets["ticket.tasks"] = tasks

        # -- artifacts
        steps_art, art_map, marker_files = self._artifacts(t, warn, left_out)
        # references in the text: a renamed one follows, an unknown one is neutralised (v2 refuses a dangling ref)
        for sid, text in list(texts.items()):
            texts[sid] = _REF.sub(lambda m: self._ref(m, art_map), text)

        # -- the note and the current state
        status = str(meta.get("status") or t.status_dir)
        note = f"Imported from v1 as {t.key} (v1 status: {status}, created {meta.get('created')}). "
        note += f"The v1 ticket, its history and what was not imported: artifact {MARKER}."
        if states:
            note += "\nv1 task state: " + ", ".join(states) + "."
        cs = sec.get("Current state", "").strip()
        if cs:
            note += "\n\n" + clean_text(cs, warn=warn, what="Current state")
        texts["current_state"] = _clip(note, HANDOFF_MAX).strip("\n")

        # -- questions that are still open
        qsteps = []
        qids = set()
        if status != "done":
            for q in meta.get("questions") if isinstance(meta.get("questions"), list) else []:
                if not isinstance(q, dict) or q.get("answer") not in (None, "") or q.get("answered"):
                    continue
                built = self._question(q, qids, warn)
                if built is not None:
                    qsteps.append(built)
                else:
                    left_out.append("an open question that v2 cannot hold (kept in the history)")

        uid = uid_for(self.source, t.key, meta.get("created"))
        marker = self._marker(t, meta, art_map, marker_files, left_out)
        mdigest = canon.artifact_digest(marker)
        steps: list[Step] = [
            Step(
                "created",
                {"type": "ticket.created", "key": t.key, "ticket_type": ttype, "title": title, "owner": self.owner},
            ),
            Step(
                "marker",
                {
                    "type": "artifact.added",
                    "name": MARKER,
                    "kind": "other",
                    "sha256": mdigest,
                    "bytes": len(marker),
                    "label": "the v1 ticket and its history (read-only copy, not evidence)",
                },
                data=marker,
                name=MARKER,
            ),
            *steps_art,
        ]
        upd: dict[str, Any] = {"type": "ticket.updated"}
        if sets:
            upd["set"] = sets
        sections = {sid: {"hash": canon.section_hash(txt), "refs": _refs_of(txt)} for sid, txt in texts.items()}
        upd["sections"] = sections
        steps.append(Step("update", upd, body=dict(texts)))
        steps += qsteps
        if status == "done":
            steps.append(self._close(meta, t.key, warn))
        elif status == "backlog":
            steps.append(Step("status", {"type": "status.changed", "from": "open", "to": "backlog"}))
        steps.append(Step("complete", {"type": "log.added", "text": complete_text(mdigest)}))
        return TicketPlan(t.key, t.key, uid, title, ttype, steps, warnings, left_out, mdigest, status)

    # -- pieces
    def _ref(self, m: re.Match[str], art_map: dict[str, str | None]) -> str:
        new = art_map.get(m.group(1))
        return f"(artifact:{new})" if new else f"(missing-artifact:{m.group(1)})"

    def _links(self, meta: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        left: list[str] = []
        links: dict[str, Any] = {"repos": [], "branches": {}, "prs": [], "external": []}
        repos = [r for r in (meta.get("repos") if isinstance(meta.get("repos"), list) else []) if isinstance(r, str)]
        branches = meta.get("branches") if isinstance(meta.get("branches"), dict) else {}
        for r in sorted(set(repos)):
            if r in self.repos:
                links["repos"].append(r)
            else:
                left.append(f"repo {r[:30]} (not in settings.repos of this workspace)")
        for r, b in sorted(branches.items()):
            if r in links["repos"] and isinstance(b, str) and b:
                links["branches"][r] = b
            elif isinstance(b, str):
                left.append(f"branch of {str(r)[:30]}")
        for pr in meta.get("prs") if isinstance(meta.get("prs"), list) else []:
            url = pr.get("url") if isinstance(pr, dict) else None
            repo = pr.get("repo") if isinstance(pr, dict) else None
            if (
                isinstance(url, str)
                and url.startswith("https://")
                and repo in links["repos"]
                and re.fullmatch(r"https://[!-~]+", url)
            ):
                links["prs"].append({"repo": repo, "url": url})
            else:
                left.append("a pull request link")
        for ext in meta.get("external") if isinstance(meta.get("external"), list) else []:
            url = ext.get("url") if isinstance(ext, dict) else ext
            if isinstance(url, str) and re.fullmatch(r"https://[!-~]+", url) and url not in links["external"]:
                links["external"].append(url)
            else:
                left.append("an external link that is not https")
        return links, left

    def _question(self, q: dict[str, Any], qids: set[str], warn: Callable[[str], None]) -> Step | None:
        m = re.fullmatch(r"[Qq]([1-9][0-9]*)", str(q.get("id") or ""))
        if m is None or f"Q{m.group(1)}" in qids:
            return None
        qid = f"Q{m.group(1)}"
        text = _clip(clean_text(q.get("text"), warn=warn, what=f"question {qid}")).strip()
        if not text:
            return None
        built: dict[str, Any] = {
            "id": qid,
            "to": "ticket_owner",
            "text": text,
            "blocking": bool(q.get("blocking", True)),
        }
        why = _clip(clean_text(q.get("why"), warn=warn, what=f"question {qid} why")).strip() if q.get("why") else ""
        if why:
            built["why"] = why
        options = []
        for o in q.get("options") if isinstance(q.get("options"), list) else []:
            key = str(o.get("key")) if isinstance(o, dict) else ""
            label = clean_text(o.get("label"), one_line=True, warn=warn, what="option") if isinstance(o, dict) else ""
            if re.fullmatch(r"[a-z][a-z0-9_]*", key) and label.strip() and all(x["key"] != key for x in options):
                options.append({"key": key, "label": _clip(label.strip(), 200)})
        if options and len(options) == len(q.get("options") or []):
            built["options"] = options
            rec = q.get("recommended")
            if isinstance(rec, str) and any(x["key"] == rec for x in options):
                built["recommended"] = rec
        qids.add(qid)
        return Step(
            "question",
            {
                "type": "question.asked",
                "question": built,
                "qid": "",  # filled by the executor (needs the workspace id and the uid)
                "hash": "",
            },
        )

    def _close(self, meta: dict[str, Any], key: str, warn: Callable[[str], None]) -> Step:
        res = str(meta.get("resolution") or "completed")
        mapped = {"completed": "other", "wont-do": "wont_do", "superseded": "obsolete", "duplicate": "duplicate"}.get(
            res, "other"
        )
        text = f"Imported from v1: done ({res}). v1 approved and verified nothing for v2: reopen to work on it."
        ev: dict[str, Any] = {"type": "ticket.closed", "resolution": mapped, "text": text}
        sup = meta.get("superseded_by")
        if mapped == "duplicate" and isinstance(sup, str) and sup in self.known_keys and sup != key:
            ev["duplicate_of"] = sup
        elif isinstance(sup, str) and sup:
            ev["text"] = text + f" Replaced by {sup[:20]} in v1."
        return Step("close", ev)

    def _artifacts(
        self, t: V1Ticket, warn: Callable[[str], None], left_out: list[str]
    ) -> tuple[list[Step], dict[str, str | None], list[dict[str, Any]]]:
        steps: list[Step] = []
        art_map: dict[str, str | None] = {}
        listed: list[dict[str, Any]] = []
        taken: set[str] = {MARKER}
        raw_list = t.meta.get("artifacts") if isinstance(t.meta.get("artifacts"), list) else []
        skipped_other = 0
        for e in raw_list:
            if not isinstance(e, dict):
                skipped_other += 1
                continue
            name = e.get("name")
            if not isinstance(name, str) or not name:
                skipped_other += 1  # a web link or a static file: not a file artifact of the ticket
                continue
            try:
                data = read_artifact(self.home, t.key, name)
            except (OSError, ValueError):
                warn(f"artifact {name[:60]}: the file is missing or unsafe")
                art_map[name] = None
                listed.append({"name": name, "imported": False, "why": "missing or unsafe"})
                continue
            digest = canon.artifact_digest(data)
            want = e.get("sha256")
            if isinstance(want, str) and want and "sha256:" + want.removeprefix("sha256:") != digest:
                warn(f"artifact {name[:60]}: the bytes differ from the sha256 v1 recorded; not imported")
                art_map[name] = None
                listed.append({"name": name, "imported": False, "why": "sha256 differs from v1's record"})
                continue
            new = _artifact_name(name.replace("/", "_"), taken)
            taken.add(new)
            art_map[name] = new
            v1kind = str(e.get("kind") or "other")
            kind = _KIND_OF.get(v1kind, v1kind if v1kind in _KINDS else "other")
            bits = [f"v1 {v1kind}"]
            for k in ("ac", "task"):
                if e.get(k) not in (None, ""):
                    bits.append(f"{k} {str(e[k])[:10]}")
            if e.get("label"):
                bits.append(clean_text(e["label"], one_line=True, warn=warn, what="artifact label"))
            label = _clip(" | ".join(bits), 300)
            src = FileSrc(self.home, t.key, name, len(data), digest)
            listed.append(
                {"name": name, "as": new, "imported": True, "sha256": digest, "bytes": len(data), "kind": v1kind}
            )
            steps.append(
                Step(
                    "artifact",
                    {
                        "type": "artifact.added",
                        "name": new,
                        "kind": kind,
                        "sha256": digest,
                        "bytes": len(data),
                        "label": label,
                    },
                    data=src,
                    name=new,
                )
            )
        if skipped_other:
            left_out.append(f"{skipped_other} artifact entries that are links or static files (kept in the history)")
        return steps, art_map, listed

    def _marker(
        self,
        t: V1Ticket,
        meta: dict[str, Any],
        art_map: dict[str, str | None],
        listed: list[dict[str, Any]],
        left_out: list[str],
    ) -> bytes:
        doc = {
            "schema": MARKER_SCHEMA,
            "source": self.source,
            "v1_key": t.key,
            "v1_status": str(meta.get("status") or t.status_dir),
            "v1_file_sha256": canon.artifact_digest(t.raw),
            "v1_file": t.raw.decode("utf-8", "replace"),
            "events": t.events,
            "artifacts": listed,
            "not_imported": left_out,
            "note": "A read-only copy of what v1 held. It is data, not evidence: v1 approvals, verdicts and task "
            "states were not carried over.",
        }
        return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _refs_of(text: str) -> list[str]:
    return refs_of(text)
