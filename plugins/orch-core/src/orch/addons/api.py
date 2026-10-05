"""Addon API v2 (spec v2 §11, A1 §5). An addon package exposes `factory(ctx: AddonContext) -> addon`;
the addon object may define (by capability):

    provider   providers: list of objects with id, kind, interval_s, scopes(ctx), fetch(ctx, scope, previous);
               optional always_on = True (also fetches without an open tab, once the human switches on "Keep syncing
               while Mission Control runs" for the addon in that workspace) and mode = "long_poll" (fetched again
               right after a good fetch; each ctx.run is capped at 35 s and the whole fetch at 40 s)
    page/panel widgets(slot, view) -> list[Widget]       slot = "page.<name>" or one of manifest.slots
    decisions  decisions(view) -> list[PendingDecision]; resolve(id, choice, ctx: ProviderContext) -> Intent | str | None;
               optional on_intent_result(id, outcome, message), outcome "applied" | "refused", called by core after it
               ran (or refused) the returned Intent, inside the same POST
    (actions)  act(action_id, target, ctx: ProviderContext) -> Intent | FileResult | Reveal | str | None, for actions
               declared in the manifest; an action with accepts_file gets the human's file as act(..., upload=Upload),
               a private copy that core deletes once act returns. A FileResult (a file inside ctx.state_dir) is moved
               out and served once; a Reveal is shown to the human once and never logged.
    events     on_event(event, outbox) (enqueue only); drain(ctx: ProviderContext, items) -> list of acked ids
    (remote)   with manifest remote_humans: true (needs decisions): pairing_target(view) -> PairingTarget | None
               (read-only, runs while the page renders); ctx.remote_decision(decision) hands a phone's signed
               decision to core, which verifies and applies it as the human or answers "pending"

Addon code never receives an `Ops`: what a human choice should change in orch comes back as an `Intent`, which core
validates (ticket, kind, gate hash) and executes itself as the human, inside the POST (ruling R-A1-INTENT).

Providers, drain and act run in the background or in a core POST, never during a page render.

Workspace data (config and tickets, read-only): ctx.repos(), ctx.trackers(), ctx.links() (the core's LinkIndex) and
ctx.snapshots(provider=None), also on ProviderContext; views have repos(), trackers(), links() and state_dir.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field, replace as _replace
from datetime import datetime, timezone
from pathlib import Path

from orch.addons.manifest import ADDON_API  # noqa: F401  (re-exported)
from orch.addons.manifest import MAX_UPLOAD_BYTES
from orch.addons.runner import AddonRunError, RunResult, SubprocessRunner, is_rendering
from orch.hooks.install import HEADER as COMMIT_HOOK_HEADER  # noqa: F401  orch's commit-msg hook marker line

HEALTH = ("ok", "stale", "auth_required", "offline", "rate_limited", "error", "never_fetched")
HEALTH_ORDER = ("error", "auth_required", "rate_limited", "offline", "stale", "never_fetched", "ok")
PROVIDER_KINDS = ("reviews", "issues", "status", "pages")
_ID = re.compile(r"[a-z][a-z0-9-]*")
ITEM_REQUIRED = {
    "reviews": ("provider", "host", "repo", "number", "url", "title", "state", "draft", "review", "checks"),
    "issues": ("tracker", "key", "url", "title", "category"),
    "pages": ("provider", "space", "id", "title", "url", "path", "updated_at"),
    "status": ("id", "label", "role", "text"),
}
_ENUMS = {
    "reviews": {"state": ("open", "merged", "closed"), "review": ("none", "required", "approved", "changes_requested")},
    "issues": {"category": ("todo", "in_progress", "done")},
    "status": {"role": ("ok", "info", "warn", "err", "neu")},
}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ANCHOR = re.compile(r"Q[1-9][0-9]*|gate:(?:requirements|plan)|verdict")


def worst_health(values) -> str:
    values = list(values)
    if not values:
        return "never_fetched"
    return min(values, key=lambda h: HEALTH_ORDER.index(h) if h in HEALTH_ORDER else 0)


def _has_offset(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return False
    return dt.tzinfo is not None


def item_problems(kind: str, item) -> list[str]:
    if kind not in ITEM_REQUIRED:
        return [f"unknown provider kind {kind!r} (one of {', '.join(PROVIDER_KINDS)})"]
    if not isinstance(item, dict):
        return ["an item must be a dict"]
    out = [f"missing {key!r}" for key in ITEM_REQUIRED[kind] if key not in item]
    for key, allowed in _ENUMS.get(kind, {}).items():
        if key in item and item[key] not in allowed:
            out.append(f"{key} {item[key]!r} must be one of {', '.join(allowed)}")
    if "url" in item:
        from orch.addons.widgets import safe_url

        if not safe_url(item["url"]):
            out.append(f"url {item['url']!r} must be http(s) or a same-origin path")
    for key in ("updated_at", "created_at"):
        if key in item and not _has_offset(item[key]):
            out.append(f"{key} {item[key]!r} must be an ISO datetime with an offset")
    return out


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if not isinstance(dt, datetime) or dt.tzinfo is None:
        raise ValueError("datetimes must be timezone-aware (UTC)")
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError as e:
        raise ValueError(f"not an ISO datetime: {value!r}") from e
    if dt.tzinfo is None:
        raise ValueError(f"datetime without timezone: {value!r}")
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class Snapshot:
    provider: str
    scope: str
    fetched_at: datetime
    health: str = "ok"
    message: str = ""
    complete: bool = True
    retry_after: datetime | None = None
    me: str | None = None
    items: tuple = ()
    schema: int = 1

    def to_dict(self) -> dict:
        if not _ID.fullmatch(self.provider):
            raise ValueError(f"provider id {self.provider!r} must match ^[a-z][a-z0-9-]*$")
        if not isinstance(self.scope, str) or not self.scope:
            raise ValueError("scope must be a non-empty string")
        if self.health not in HEALTH:
            raise ValueError(f"health {self.health!r} must be one of {', '.join(HEALTH)}")
        d = {"schema": 1, "provider": self.provider, "scope": self.scope, "fetched_at": _iso(self.fetched_at),
             "health": self.health, "message": str(self.message), "complete": bool(self.complete),
             "retry_after": _iso(self.retry_after), "me": self.me, "items": [dict(i) for i in self.items]}
        try:
            json.dumps(d)
        except TypeError as e:
            raise ValueError(f"snapshot items must be JSON values ({e})") from e
        return d

    @classmethod
    def from_dict(cls, d) -> "Snapshot":
        if not isinstance(d, dict) or d.get("schema") != 1:
            raise ValueError("not a schema 1 snapshot")
        items = d.get("items") or []
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise ValueError("items must be a list of objects")
        s = cls(provider=str(d.get("provider", "")), scope=str(d.get("scope", "")),
                fetched_at=_parse_dt(d.get("fetched_at")) or _EPOCH, health=str(d.get("health", "error")),
                message=str(d.get("message") or ""), complete=bool(d.get("complete", True)),
                retry_after=_parse_dt(d.get("retry_after")), me=d.get("me") if isinstance(d.get("me"), str) else None,
                items=tuple(items))
        s.to_dict()  # validate
        return s

    def content_hash(self) -> str:
        d = self.to_dict()
        d.pop("fetched_at")
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode("utf-8")).hexdigest()

    def replace(self, **changes) -> "Snapshot":
        return _replace(self, **changes)


def never_fetched(provider: str, scope: str) -> Snapshot:
    return Snapshot(provider, scope, _EPOCH, health="never_fetched", message="not fetched yet")


MAX_DECISION_TITLE = 200
MAX_DECISION_BODY = 2000
MAX_DECISION_CHOICES = 6
MAX_CHOICE_LABEL = 80


@dataclass(frozen=True)
class PendingDecision:
    id: str
    title: str
    body: str = ""
    ticket: str | None = None
    stale: bool = False
    choices: tuple = (("apply", "Apply"), ("ignore", "Ignore"))
    role: str = "info"
    anchor: str | None = None  # where it is drawn on its ticket: Q<n>, gate:requirements, gate:plan or verdict
    origin: str | None = None  # where the decision came from (e.g. "phone"): kept in the event log when it is applied

    def __post_init__(self):
        if self.role not in ("info", "warn", "err"):
            raise ValueError("a pending decision is drawn in info, warn or err, never as needs-you")
        if self.anchor is not None and not (isinstance(self.anchor, str) and _ANCHOR.fullmatch(self.anchor)):
            raise ValueError("anchor must be Q<n>, gate:requirements, gate:plan or verdict")
        if self.origin is not None and not (isinstance(self.origin, str) and 0 < len(self.origin) <= 40):
            raise ValueError("origin is a short text (at most 40 characters)")
        if not self.choices or not all(isinstance(c, tuple) and len(c) == 2 for c in self.choices):
            raise ValueError("choices must be (value, label) pairs")
        if len(str(self.title)) > MAX_DECISION_TITLE or len(str(self.body)) > MAX_DECISION_BODY:
            raise ValueError(f"a pending decision's title is at most {MAX_DECISION_TITLE} characters "
                             f"and its body at most {MAX_DECISION_BODY}")
        if len(self.choices) > MAX_DECISION_CHOICES or any(len(str(label)) > MAX_CHOICE_LABEL for _, label in self.choices):
            raise ValueError(f"at most {MAX_DECISION_CHOICES} choices, each label at most {MAX_CHOICE_LABEL} characters")


INTENT_KINDS = ("answer", "approve", "request_changes", "verdict", "move", "new", "close", "reopen", "import", "none")
TICKET_INTENTS = ("close", "reopen", "import")  # only from actions declared with "tickets": true
MAX_INTENT_TEXT = 2000
MAX_NEW_TITLE = 200
MAX_ASK_TEXT = 20000  # an imported issue's text for the Ask; longer text is cut, never refused


@dataclass(frozen=True)
class Intent:
    """What a human's choice should change in orch, returned by `resolve()` or `act()`. Core executes it with the
    human actor after checking it: `ref` must be the decision's ticket (or the action's target), approve needs the
    `expected_hash` of the gate text the human saw, answer the `question_hash` of the question the human saw.
    `kind="none"` changes nothing and shows `reason`.

        answer           ref, qid, value, expected_hash (question_hash), reason (optional note)
        approve          ref, gate (requirements|plan), expected_hash (gate_hash)
        request_changes  ref, gate, reason, expected_hash (optional gate_hash; None or "" skips the stale check)
        verdict          ref, value (done|follow-up), reason (needed for follow-up)
        move             ref, value (a status)
        new              no ref, value (the title, at most 200), reason (the Ask, optional); lands in backlog;
                         from resolve() of an addon with the decisions capability only
        import           ref (the external key), value (the title), data["ask"] (optional: the issue text, at
                         most MAX_ASK_TEXT, becomes the ticket's Ask)  actions with "tickets": true only
        close, reopen    ref, reason (needed)                            actions with "tickets": true only
    """
    kind: str
    ref: str | None = None
    gate: str | None = None
    qid: str | None = None
    value: str | None = None
    reason: str = ""
    expected_hash: str | None = None
    data: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in INTENT_KINDS:
            raise ValueError(f"intent kind must be one of {', '.join(INTENT_KINDS)}")
        for name in ("ref", "gate", "qid", "value", "expected_hash"):
            v = getattr(self, name)
            if v is not None and (not isinstance(v, str) or len(v) > MAX_INTENT_TEXT):
                raise ValueError(f"intent {name} must be a string of at most {MAX_INTENT_TEXT} characters")
        if not isinstance(self.reason, str) or len(self.reason) > MAX_INTENT_TEXT:
            raise ValueError(f"intent reason must be a string of at most {MAX_INTENT_TEXT} characters")
        if not isinstance(self.data, dict):
            raise ValueError("intent data must be a dict")


TicketIntent = Intent  # the name the addon guide uses for an Intent that changes a ticket


@dataclass(frozen=True)
class Upload:
    """The human's file for an accepts_file action: a private 0600 copy (deleted after act returns), its
    sanitised display name, its size in bytes and the browser's MIME type (a hint, never proof)."""
    path: Path
    name: str
    size: int
    mime: str


@dataclass(frozen=True)
class FileResult:
    """What act() returns to hand the human a file: `path` must be a regular file inside the addon's state_dir.
    Core moves it away and serves it once, as an attachment, within 5 minutes."""
    path: Path
    name: str
    mime: str = "application/octet-stream"


@dataclass(frozen=True)
class Reveal:
    """A secret (a link with a key in it) shown to the human once: never logged, never cached, never in a URL."""
    label: str
    text: str

    def __repr__(self) -> str:  # a traceback or log line never carries the text
        return f"Reveal(label={self.label!r}, text=<hidden>)"


@dataclass(frozen=True)
class PairingTarget:
    """Where a phone pairs, from `pairing_target(view)` of an addon whose manifest says remote_humans: true. Core
    appends `.<phone id>.<key>` to `url` (keep the key in the fragment: end `url` with `#...`) and shows the result
    once, as a QR code and a "Copy pairing link" button, with a 6-digit check code."""
    url: str
    label: str

    def __post_init__(self):
        from orch.addons.widgets import safe_url
        if not (isinstance(self.url, str) and self.url.startswith("https://") and safe_url(self.url)
                and len(self.url) <= 500):
            raise ValueError("a pairing target url must be an https:// URL of at most 500 characters")
        if not isinstance(self.label, str) or not self.label.strip() or len(self.label) > 80:
            raise ValueError("a pairing target label must be 1-80 characters")


MAX_ARTIFACT_BYTES = MAX_UPLOAD_BYTES


class _Capped:
    """A read-only stream that refuses to give out more than `cap` bytes (the file may grow after the size check)."""

    def __init__(self, f, cap: int):
        self._f, self._left = f, cap

    def read(self, n: int = -1) -> bytes:
        from orch.errors import ValidationError
        chunk = self._f.read(65536 if n is None or n < 0 else n)
        self._left -= len(chunk)
        if self._left < 0:
            raise ValidationError("the file grew past the artifact size cap while it was copied")
        return chunk


def _new_ops(ws, name: str):
    """A fresh `Ops` for this call only; never stored on `AddonOps` (an `Ops.actor` held across calls
    could be swapped by whoever can reach it, which would be a privilege escalation to human powers)."""
    from orch.core.events import Actor
    from orch.core.ops import Ops

    via = f"addon:{name}"
    return Ops(ws, Actor("agent", via, via))


class AddonOps:
    """What an addon may change, always as an agent `addon:<name>` (spec v2 §11.6). Holds only the
    workspace and the addon name — never an `Ops` instance — and allows no other attribute (`__slots__`),
    so there is nothing reachable from here whose `.actor` could be mutated to gain human powers."""

    __slots__ = ("_ws", "_name")

    def __init__(self, ws, name: str):
        self._ws = ws
        self._name = name

    @property
    def actor(self):
        """Always the agent `addon:<name>` (read-only; a fresh value, not shared mutable state)."""
        from orch.core.events import Actor

        via = f"addon:{self._name}"
        return Actor("agent", via, via)

    def log(self, ref: str, text: str):
        return _new_ops(self._ws, self._name).log(ref, text)

    def set_extra(self, ref: str, key: str, value):
        return _new_ops(self._ws, self._name).set_extra(ref, key, value)

    def link_external(self, ref: str, key: str):
        return _new_ops(self._ws, self._name).link(ref, external=key)

    def add_artifact(self, ref: str, path, name: str | None = None, *, context: bool = False):
        """Copy a file the addon already has (inside its own state_dir) into the ticket's artifacts,
        as the addon's agent. Refuses symlinks, hard links, files over MAX_ARTIFACT_BYTES and anything outside the
        addon's state folder. The file is opened once (O_NOFOLLOW) and copied from that descriptor, so nothing can be
        swapped in between the check and the copy."""
        from orch.errors import ValidationError

        refused = "an addon adds artifacts only from files in its own state folder"
        src = Path(path)
        base = (self._ws.state_dir / "addons" / self._name).resolve()
        try:
            st = os.lstat(src)
        except OSError:
            raise ValidationError(refused) from None
        if not stat.S_ISREG(st.st_mode) or not src.resolve().is_relative_to(base):
            raise ValidationError(refused)
        try:
            fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError:
            raise ValidationError(refused) from None
        with os.fdopen(fd, "rb") as f:
            fst = os.fstat(f.fileno())
            if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
                raise ValidationError(refused)
            if fst.st_nlink > 1:
                raise ValidationError("an addon may not add a hard-linked file as an artifact")
            if fst.st_size > MAX_ARTIFACT_BYTES:
                raise ValidationError(f"the file is larger than {MAX_ARTIFACT_BYTES} bytes")
            return _new_ops(self._ws, self._name).artifact_add(ref, src, name, context=context,
                                                               stream=_Capped(f, MAX_ARTIFACT_BYTES))

    def import_external(self, key: str, title: str, *, ask: str = "") -> str:
        """A backlog ticket for an external key; the existing ticket's id if one already has that key. `ask` is
        free text from outside: it goes through `neutral_text`, so it can never open a fence or forge a section."""
        from orch.core import store
        from orch.core.model import neutral_text

        wanted = key.strip().upper()
        for entry in store.scan(self._ws):
            for x in (entry.meta or {}).get("external") or []:
                if isinstance(x, dict) and str(x.get("key", "")).upper() == wanted:
                    return entry.id
        return _new_ops(self._ws, self._name).new(title, ask=neutral_text(ask or ""), external=wanted).id


@dataclass(frozen=True)
class RepoRef:
    """A repo of this workspace: the harness root first, then git.repos. Config only; no git runs here."""
    name: str
    role: str  # "harness" | "sub-repo"
    path: Path
    default_branch: str | None = None


@dataclass(frozen=True)
class TrackerRef:
    prefix: str
    pattern: str
    url: str

    def _dict(self) -> dict:
        return {"prefix": self.prefix, "pattern": self.pattern, "url": self.url}

    def matches(self, key) -> bool:
        from orch.core import trackers
        return trackers.matches(self.pattern, str(key))

    def key_for(self, ident) -> str | None:
        """The external key for a tracker-native id (an issue number): `<prefix>-<id>` when the pattern accepts it."""
        candidate = f"{self.prefix}-{ident}"
        return candidate.upper() if self.matches(candidate) else None

    def url_for(self, key) -> str | None:
        from orch.core import trackers
        return trackers.url_for(self._dict(), str(key).strip().upper())


def _text(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def workspace_repos(ws) -> list[RepoRef]:
    """The harness root first (named after the git.repos entry whose path is the root, else "harness"), then each
    other git.repos entry once by resolved path."""
    root = Path(ws.root).resolve()
    git = ws.config.get("git") if isinstance(ws.config.get("git"), dict) else {}
    configured = git.get("repos") if isinstance(git.get("repos"), dict) else {}
    harness = RepoRef("harness", "harness", root)
    named = False
    subs: list[RepoRef] = []
    for name, rc in configured.items():
        rc = rc if isinstance(rc, dict) else {}
        path = (root / (_text(rc.get("path")) or str(name))).resolve()
        branch = _text(rc.get("default_branch"))
        if path == root:
            if not named:
                harness, named = RepoRef(str(name), "harness", root, branch), True
        elif not any(s.path == path for s in subs):
            subs.append(RepoRef(str(name), "sub-repo", path, branch))
    return [harness, *subs]


def workspace_trackers(ws) -> list[TrackerRef]:
    out = []
    for t in ws.config.get("external_trackers") or []:
        if isinstance(t, dict) and all(_text(t.get(k)) for k in ("prefix", "pattern", "url")):
            out.append(TrackerRef(t["prefix"], t["pattern"], t["url"]))
    return out


class AddonContext:
    def __init__(self, ws, name: str, *, manifest=None, kind: str = "custom", runner=None):
        self.ws = ws
        self.name = name
        self.manifest = manifest
        self.kind = kind
        self.runner = runner  # tests and orch.testing inject a FakeRunner here

    @property
    def root(self) -> Path:
        return self.ws.root

    @property
    def state_dir(self) -> Path:
        return self.ws.state_dir / "addons" / self.name

    @property
    def records_dir(self) -> Path:
        """`state_dir/records/`: the one part of the addon's folder that git keeps (#36); everything else there is a
        cache. Not created until the addon writes there."""
        return self.state_dir / "records"

    @property
    def settings(self) -> dict:
        from orch.addons.userfiles import workspace_addons

        saved = workspace_addons(self.ws.root).get(self.name, {}).get("config", {})
        if self.manifest is None:
            return dict(saved)
        out = self.manifest.defaults()
        out.update({k: v for k, v in saved.items() if self.manifest.field(k) is not None})
        return out

    def show(self, ref: str):
        """Read a ticket (by ID, number or external key). Read-only: no lock, no event."""
        from orch.core import store
        return store.load(self.ws, ref)[1]

    def tickets(self) -> list:
        """Every ticket's scan entry (id, path, status, meta). Read-only: these are copies, since the core and the
        other widgets of a page work on one shared scan."""
        import copy
        import dataclasses
        from orch.core import store
        return [dataclasses.replace(e, meta=copy.deepcopy(e.meta)) for e in store.scan(self.ws)]

    def document(self, ref: str) -> dict:
        """The ticket as its versioned schema document (`orch schema ticket`): sections, gates with hashes,
        questions with question_hash, task list, needs-you items with gate hash and verdict round. A fresh
        dict each call; read-only, no lock, no event."""
        from orch.core.schema import ticket_document
        return ticket_document(self.ws, self.show(ref))

    def ticket_widgets(self, ref: str, *, theme: str = "system") -> list[dict]:
        """The ticket's ```orch widget blocks (docs/widgets.md), each drawn as far as this desktop can, for a
        companion addon that shows them elsewhere: {section, index, key, layer ("type" | "widget" | "html" |
        None), name (the type, name@version or artifact path), title, source, text (the text alternative), document
        (render_document's standalone HTML without chrome: the companion draws title, chip, text and source itself;
        None for a block with an error), raw_sha256 (of the block text: what is
        drawn, for caching), problems}. Read-only, no lock, no event."""
        import hashlib as _hashlib
        from orch import widgets as W
        from orch.widgets.blocks import MAX_BLOCKS
        from orch.widgets.render import text_of
        ticket = self.show(ref)
        wctx = W.Ctx.of(self.ws, ticket, theme=theme)
        out = []
        from orch.widgets.blocks import Problem, duplicate_ids
        blocks = W.ticket_blocks(ticket)
        dups = duplicate_ids(blocks)
        for b in blocks:
            data = b.data or {}
            problems = W.validate(b, ticket, ws=self.ws) if b.layer else []
            if data.get("id") in dups:  # never drawn on the dashboard either
                problems.append(Problem("widget-schema", f"id {data['id']!r} is used twice in this ticket"))
            ok = b.layer is not None and not any(p.level == "error" for p in problems) and b.index < MAX_BLOCKS
            out.append({"section": b.section, "index": b.index, "key": b.key, "layer": b.layer,
                        "name": str(data.get(b.layer) or "") if b.layer else "",
                        "title": str(data.get("title") or ""), "source": str(data.get("source") or ""),
                        "text": text_of(b, wctx) if ok else W.render_text(b, wctx),
                        "document": W.render_document(b, wctx, chrome=False) if ok else None,
                        "raw_sha256": _hashlib.sha256(b.raw.encode("utf-8")).hexdigest(),
                        "problems": [p.to_dict() for p in problems]})
        return out

    def page_widget_text(self, text: str, page_id: str = "", folder: str = "") -> str:
        """API 2.3: `text` (a page of yours) with each valid ```orch block replaced by its text alternative, for a
        search index or a mention scan that should read what a widget says, not its JSON. `folder` is your page
        folder (workspace-relative), where `_files/<name>` blocks are looked up. Read-only."""
        from orch.widgets.pages import text_alternatives
        return text_alternatives(self.ws, text, page_id, folder)

    def ticket_widget_copy(self, ref: str, sections) -> dict:
        """API 2.3: the ```orch blocks of the ticket's `sections`, ready to put on a page of yours, with the files they
        pin: {"blocks": [(section, fenced text naming `_files/<ID>-<name>`)], "files": {name: bytes} (digest-checked),
        "skipped": [why a block was left out]}. A block whose file is missing or changed is skipped, never copied.
        Read-only: you write the page and the files."""
        from orch.widgets.pages import ticket_copy
        return ticket_copy(self.ws, ref, tuple(sections))

    def ops(self) -> AddonOps:
        return AddonOps(self.ws, self.name)

    def repos(self) -> list[RepoRef]:
        return workspace_repos(self.ws)

    def trackers(self) -> list[TrackerRef]:
        return workspace_trackers(self.ws)

    def links(self):
        """A read-only orch.core.links.LinkIndex: the local ticket of a PR, branch or external key, out-of-sync state."""
        from orch.core.links import shared_index
        return shared_index(self.ws)

    def snapshots(self, provider: str | None = None) -> list[Snapshot]:
        """This addon's cached snapshots (what pages show), e.g. to check an action's target against current data."""
        from orch.addons import cache
        return cache.read_snapshots(self.ws, self.name, provider)

    def provider_context(self, runner=None, *, max_timeout: float | None = None) -> "ProviderContext":
        return ProviderContext(self, runner=runner if runner is not None else self.runner, max_timeout=max_timeout)


class ProviderContext:
    def __init__(self, addon_ctx: AddonContext, *, runner=None, max_timeout: float | None = None):
        self.addon = addon_ctx
        self.name = addon_ctx.name
        self.max_timeout = float(max_timeout) if max_timeout is not None else None  # a long poll's ctx.run cap
        m = addon_ctx.manifest
        self._runner = runner if runner is not None else SubprocessRunner(
            env_names=tuple(m.env) if m else (), cwd=addon_ctx.root)

    @property
    def root(self) -> Path:
        return self.addon.root

    @property
    def settings(self) -> dict:
        return self.addon.settings

    def repos(self) -> list[RepoRef]:
        return self.addon.repos()

    def trackers(self) -> list[TrackerRef]:
        return self.addon.trackers()

    def links(self):
        return self.addon.links()

    def document(self, ref: str) -> dict:
        return self.addon.document(ref)

    def ticket_widgets(self, ref: str, *, theme: str = "system") -> list[dict]:
        return self.addon.ticket_widgets(ref, theme=theme)

    def page_widget_text(self, text: str, page_id: str = "", folder: str = "") -> str:
        return self.addon.page_widget_text(text, page_id, folder)

    def ticket_widget_copy(self, ref: str, sections) -> dict:
        return self.addon.ticket_widget_copy(ref, sections)

    def snapshots(self, provider: str | None = None) -> list[Snapshot]:
        return self.addon.snapshots(provider)

    def now(self) -> datetime:
        from orch.clock import now
        return now()

    def remote_decision(self, decision: dict):
        """Hand a decrypted phone decision to core (spec §6.4). Core verifies it (pairing, signature, age,
        permission, ledger, target hash) and applies it as the human, or returns a RemoteResult whose status says
        why not ("pending": not applied, because the phone is not paired, a check failed or the owner switched the
        kind off; a valid decision is never pending, R21). Only for addons whose manifest says
        remote_humans: true. Refused while a page renders: pages read the cache only and never write."""
        if is_rendering():
            raise AddonRunError("remote_decision is not allowed while a page renders; hand decisions over in a provider")
        m = self.addon.manifest
        if m is None or not m.remote_humans:
            raise AddonRunError("this addon does not handle remote human decisions (manifest remote_humans)")
        from orch.remote.verify import verify_and_apply
        return verify_and_apply(self.addon.ws, decision, addon=self.name)

    def _allowed(self) -> set[str]:
        m = self.addon.manifest
        if m is None:
            return set()
        allowed = set(m.bare_binaries())
        settings = self.settings
        for key in m.setting_binaries():
            value = settings.get(key)
            if isinstance(value, str) and Path(value).is_absolute():
                allowed.add(value)
        return allowed

    def run(self, argv, timeout: float = 20.0) -> RunResult:
        if is_rendering():
            raise AddonRunError("ctx.run is not allowed while a page renders; fetch in a provider instead")
        if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
            raise AddonRunError("ctx.run takes an argv list of strings (no shell)")
        if argv[0] not in self._allowed():
            raise AddonRunError(f"{argv[0]!r} is not in this addon's binaries allowlist")
        timeout = float(timeout)
        if self.max_timeout is not None:
            timeout = min(timeout, self.max_timeout)
        return self._runner(list(argv), timeout)
