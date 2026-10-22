"""Selective-mode share rules and their evaluation (V3 C6; spec 8a, D80; hub contract 10.2,
``share_put`` / ``share_revoke``).

A selective workspace shows the hub only what its owner selects. The owner's **share rules** are a closed list:

* ``{"kind": "needs_you"}``: needs-you summaries (type, one-line title, deadline). The default and the only rule a new
  selective join has: nothing else leaves the host.
* ``{"kind": "type", "type": T}``: every ticket of type ``T``.
* ``{"kind": "ticket", "ticket": KEY}``: one ticket.
* ``{"kind": "tag", "tag": L}``: every ticket labelled ``L`` (a ticket's label is its tag).
* ``{"kind": "questions"}``: the open questions addressed to the owner ("questions to me").

:func:`select` turns rules plus the owner's view of the tickets into the items to share (pure, no I/O). :func:`sync`
makes the hub's copy equal that set: ``share_put`` for a new or changed item, ``share_revoke`` for an item that no
longer
matches (rule removed, label removed, ticket closed, owner withdrew it). It resends only what changed: a put that did
not get a reply keeps its item id, so a retry replaces the item and never duplicates it.

**The sealed bytes** are the hub contract's 10.2b format (:mod:`orch.hubclient.sharefmt`): a ``WSK``-signed
``share.item`` with exact content per kind, its key sealed to ``HXK``. ``select`` already produces that exact content
(an item the contract cannot express is not offered, never sent). :func:`make_builder` wires the format to the
workspace's keys. Each put carries a per-item ``item_version`` that only grows (kept in the share state, bumped for
every put attempt, so a retry after a lost reply replaces and never collides); a revoked item that is shared again gets
a fresh id.

Fail closed: unreadable state, an unknown rule or a malformed stored value is ``malformed``; nothing is guessed."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from orch.canon.text import clean, clean_line
from orch.crypto import suite
from orch.crypto.encoding import b64u

from . import sharefmt, wire
from .errors import HubRefused

__all__ = [
    "DEFAULT_RULES",
    "MAX_ITEMS",
    "NeedFact",
    "QuestionFact",
    "ShareItem",
    "ShareLink",
    "ShareState",
    "TicketFact",
    "describe",
    "make_builder",
    "normalize_rule",
    "select",
    "sync",
]

MAX_ITEMS = sharefmt.MAX_ITEMS  # one workspace shares at most this many items; more is refused before anything is sent
TITLE_MAX = 120
TITLE_BYTES = 200
TEXT_BYTES = 2000
# a need of the model -> the contract's closed needs_you type; the others (no_eligible, acknowledge, addon) have none
NEED_TYPES = {"question": "question", "approve": "gate"}
_RULE_KEYS = {"needs_you": set(), "questions": set(), "type": {"type"}, "ticket": {"ticket"}, "tag": {"tag"}}
DEFAULT_RULES: tuple[dict[str, str], ...] = ({"kind": "needs_you"},)


def normalize_rule(rule: Any) -> dict[str, str]:
    """The rule in its one stored form, or ``malformed``. Values are short single-line text."""
    if type(rule) is not dict or rule.get("kind") not in _RULE_KEYS:
        raise HubRefused("malformed")
    kind = rule["kind"]
    if set(rule) != {"kind", *_RULE_KEYS[kind]}:
        raise HubRefused("malformed")
    for k in _RULE_KEYS[kind]:
        v = rule[k]
        if (
            type(v) is not str
            or not 0 < len(v) <= 100
            or v != v.strip()
            or any(ord(c) < 0x20 or ord(c) == 0x7F for c in v)
        ):
            raise HubRefused("malformed")
    return {k: rule[k] for k in ("kind", *sorted(_RULE_KEYS[kind]))}


def describe(rule: Mapping[str, str]) -> str:
    """What the rule shares, in words (WYSIWYS: the owner types a confirmation after reading this)."""
    k = rule["kind"]
    if k == "needs_you":
        return "needs-you summaries (type, a one-line title and the deadline)"
    if k == "questions":
        return "every open question addressed to you (the full question)"
    if k == "type":
        return f"every ticket of type {clean_line(rule['type'])!r} (title, status, labels, deadline)"
    if k == "ticket":
        return f"the ticket {clean_line(rule['ticket'])!r} (title, status, labels, deadline)"
    return f"every ticket labelled {clean_line(rule['tag'])!r} (title, status, labels, deadline)"


# --- facts (the host builds these from its replayed state; this module never imports the model) ---------------------


@dataclass(frozen=True)
class QuestionFact:
    qid: str  # the question id within the ticket
    text: str
    to_owner: bool  # addressed to an owner of this workspace


@dataclass(frozen=True)
class NeedFact:
    kind: str  # question | approve | no_eligible | acknowledge | addon
    ref: str | None
    to_owner: bool


@dataclass(frozen=True)
class TicketFact:
    uid: str
    key: str
    type: str
    title: str
    status: str
    labels: tuple[str, ...]
    due: str | None
    questions: tuple[QuestionFact, ...] = ()
    needs: tuple[NeedFact, ...] = ()


@dataclass(frozen=True)
class ShareItem:
    logical: str  # stable identity: "ticket:<uid>", "question:<uid>:<qid>", "summary:<uid>:<kind>:<ref>"
    kind: str  # ticket | question | summary (summary is the contract's item_kind ``needs_you``)
    ticket_uid: str
    ticket_key: str
    payload: dict[str, Any]  # the exact 10.2b content of this kind (shown to the owner; the builder signs and seals it)
    digest: str  # b64u SHA-256 of the canonical payload: a change re-sends

    def line(self) -> str:
        """One line for the owner's review."""
        if self.kind == "summary":
            return f"needs you: {self.payload['type']}: {self.payload['title']}"
        if self.kind == "question":
            return f"question on {clean_line(self.ticket_key)}: {self.payload['title']}"
        return f"ticket {clean_line(self.ticket_key)}: {self.payload['title']}"


def _cut(text: str, n: int) -> str:
    """``text`` cut at a character boundary to at most ``n`` UTF-8 bytes."""
    return text.encode()[:n].decode("utf-8", "ignore")


def _line(text: str) -> str:
    out = clean_line(text)
    if len(out) > TITLE_MAX:
        out = out[: TITLE_MAX - 1] + "…"
    return _cut(out, TITLE_BYTES).strip()


def _multiline(text: str) -> str:
    """A question's full text: LF line ends, invisible characters made visible (``clean`` keeps LF only)."""
    return clean(text.replace("\r\n", "\n").replace("\r", "\n")).strip()


# a refusal about this one item (its content), not about the hub, the mode or the link: the sync goes on without it
ITEM_REFUSALS = frozenset({"malformed", "too_large"})
ITEM_KIND = {"summary": "needs_you", "ticket": "ticket", "question": "question"}


def _item(logical: str, kind: str, t: TicketFact, payload: dict[str, Any]) -> ShareItem | None:
    """The item, or ``None`` when the contract cannot express it (a key, label or text outside its rules, a question
    over 2000 bytes): it is then not offered and nothing of it is sent."""
    try:
        sharefmt.check_content(ITEM_KIND[kind], payload)
    except HubRefused:
        return None
    return ShareItem(logical, kind, t.uid, t.key, payload, b64u(suite.sha256(wire.cj(payload))))


def _valid_labels(labels: Iterable[str]) -> list[str]:
    ok = sorted({x for x in labels if sharefmt.LABEL_RE.fullmatch(x)})
    return ok[: sharefmt.MAX_LABELS]


def select(rules: Iterable[Mapping[str, str]], tickets: Iterable[TicketFact]) -> list[ShareItem]:
    """The items the rules select from ``tickets`` (the owner's visible tickets), sorted by logical id."""
    rs = [normalize_rule(dict(r)) for r in rules]
    kinds = {r["kind"] for r in rs}
    out: dict[str, ShareItem] = {}

    def put(item: ShareItem | None) -> None:
        if item is not None:
            out[item.logical] = item

    for t in tickets:
        hit = (
            any(r["kind"] == "type" and r["type"] == t.type for r in rs)
            or any(r["kind"] == "ticket" and r["ticket"] in (t.key, t.uid) for r in rs)
            or any(r["kind"] == "tag" and r["tag"] in t.labels for r in rs)
        )
        if hit:
            put(_item(
                f"ticket:{t.uid}", "ticket", t,
                {"key": t.key, "type": t.type, "title": _line(t.title), "status": t.status,
                 "labels": _valid_labels(t.labels), "deadline": t.due},
            ))  # fmt: skip
        if "questions" in kinds:
            for q in t.questions:
                if q.to_owner:
                    put(_item(
                        f"question:{t.uid}:{q.qid}", "question", t,
                        {"key": t.key, "type": t.type, "id": q.qid, "title": _line(q.text),
                         "text": _multiline(q.text), "deadline": t.due},
                    ))  # fmt: skip
        if "needs_you" in kinds:
            for n in t.needs:
                if not n.to_owner or n.kind not in NEED_TYPES:
                    continue
                title = (
                    next((q.text for q in t.questions if q.qid == n.ref), t.title) if n.kind == "question" else t.title
                )
                put(_item(
                    f"summary:{t.uid}:{n.kind}:{n.ref or ''}", "summary", t,
                    {"type": NEED_TYPES[n.kind], "title": _line(title), "deadline": t.due},
                ))  # fmt: skip
    return [out[k] for k in sorted(out)]


# --- the sealed bytes (hub contract 10.2b) -------------------------------------------------------------------------


class SealedItemBuilder(Protocol):
    def __call__(self, item_id: str, item: ShareItem, version: int) -> str:
        """The b64u ``sealed`` of one put: the ``WSK``-signed ``share.item`` at ``version``, sealed under a fresh item
        key that is itself sealed to ``HXK``. Refuses (``malformed``, ``too_large``) rather than send anything the hub
        would refuse."""
        ...


def make_builder(
    *,
    hub_id: str,
    workspace_id: str,
    hxk_pub: bytes,
    sign: Callable[[bytes], bytes],
    dev: bool,
    now_ms: Callable[[], int],
    chain_of: Callable[[str], tuple[int, str] | None],
) -> SealedItemBuilder:
    """The 10.2b builder for one hub binding. ``chain_of(ticket_uid)`` is ``(seq, head)`` of that ticket's log now
    (``None``: the log is unreadable or empty, which refuses: an item without its chain head is never sent)."""

    def build(item_id: str, item: ShareItem, version: int) -> str:
        head = chain_of(item.ticket_uid)
        if head is None:
            raise HubRefused("malformed")
        signed = sharefmt.build_signed_item(
            hub_id=hub_id, workspace_id=workspace_id, item_id=item_id, item_kind=ITEM_KIND[item.kind],
            item_version=version, issued_ms=now_ms(), chain={"log": item.ticket_uid, "seq": head[0], "head": head[1]},
            content=item.payload, dev=dev, sign=sign,
        )  # fmt: skip
        return sharefmt.seal_share_item(signed, hxk_pub=hxk_pub)

    return build


# --- state ----------------------------------------------------------------------------------------------------------


@dataclass
class ShareState:
    """``rules``: the owner's rules. ``sent``: logical id -> ``{"item_id", "digest", "version"}`` (``digest``
    ``None``: a put whose reply never came: the next sync re-sends under the same id and a higher ``version``;
    the version only grows, it is the hub's order of an item's puts, 10.2b). ``withdrawn``: logical ids the owner
    revoked by hand: never re-shared while they match, forgotten once they stop matching or a ``ticket`` rule names
    them again."""

    rules: list[dict[str, str]] = field(default_factory=lambda: [dict(r) for r in DEFAULT_RULES])
    sent: dict[str, dict[str, Any]] = field(default_factory=dict)
    withdrawn: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"rules": self.rules, "sent": self.sent, "withdrawn": sorted(self.withdrawn)}

    @classmethod
    def from_json(cls, d: Any) -> ShareState:
        if type(d) is not dict or set(d) != {"rules", "sent", "withdrawn"}:
            raise HubRefused("malformed")
        if type(d["rules"]) is not list or type(d["sent"]) is not dict or type(d["withdrawn"]) is not list:
            raise HubRefused("malformed")
        rules = [normalize_rule(r) for r in d["rules"]]
        sent: dict[str, dict[str, Any]] = {}
        for lg, v in d["sent"].items():
            ok = (
                type(lg) is str and type(v) is dict
                and {"item_id", "digest"} <= set(v) <= {"item_id", "digest", "version"}
                and wire.hex16(v["item_id"]) and (v["digest"] is None or type(v["digest"]) is str)
                and (type(v.get("version", 0)) is int and 0 <= v.get("version", 0) < 2**32 - 1)
            )  # fmt: skip
            if not ok:
                raise HubRefused("malformed")
            sent[lg] = {"item_id": v["item_id"], "digest": v["digest"], "version": v.get("version", 0)}
        if any(type(x) is not str for x in d["withdrawn"]):
            raise HubRefused("malformed")
        return cls(rules=rules, sent=sent, withdrawn=list(d["withdrawn"]))

    def add_rule(self, rule: Mapping[str, str]) -> bool:
        r = normalize_rule(dict(rule))
        if r in self.rules:
            return False
        self.rules.append(r)
        return True

    def remove_rule(self, rule: Mapping[str, str]) -> bool:
        r = normalize_rule(dict(rule))
        if r not in self.rules:
            return False
        self.rules.remove(r)
        return True


class ShareLink(Protocol):
    def call(self, op: str, body: dict[str, Any]) -> dict[str, Any]: ...


def new_item_id() -> str:
    return os.urandom(16).hex()


def sync(
    state: ShareState,
    wanted: Iterable[ShareItem],
    *,
    link: ShareLink,
    build: SealedItemBuilder,
    save: Callable[[ShareState], None],
    new_id: Callable[[], str] = new_item_id,
) -> dict[str, Any]:
    """Make the hub's copy equal ``wanted`` minus the owner's withdrawn items. Revokes first, then puts; ``save`` runs
    after every link call that succeeded, so a crash or an unreachable hub loses nothing and repeats nothing. Returns
    the counts ``{"put", "revoke", "kept"}``, plus ``"refused"`` (logical id -> code) when items were skipped. One item
    the contract or the hub refuses as such (``malformed``, ``too_large``) is skipped and reported, and the rest goes
    on; any other refusal (hub not reachable, wrong mode, ...) stops the sync (the state stays as far as it got)."""
    items = {i.logical: i for i in wanted}
    if len(items) > MAX_ITEMS:
        raise HubRefused("too_many_items")  # before anything is sent
    # a withdrawal ends when the item no longer matches
    state.withdrawn = [lg for lg in state.withdrawn if lg in items]
    live = {lg: i for lg, i in items.items() if lg not in state.withdrawn}
    counts: dict[str, Any] = {"put": 0, "revoke": 0, "kept": 0}
    refused: dict[str, str] = {}
    for lg in sorted(set(state.sent) - set(live)):
        link.call("share_revoke", {"item_id": state.sent[lg]["item_id"]})
        del state.sent[lg]
        counts["revoke"] += 1
        save(state)
    for lg, item in sorted(live.items()):
        have = state.sent.get(lg)
        if have is not None and have["digest"] == item.digest:
            counts["kept"] += 1
            continue
        item_id = new_id() if have is None else have["item_id"]
        version = (0 if have is None else have.get("version", 0)) + 1  # every put attempt takes a higher version
        try:
            sealed = build(item_id, item, version)  # may refuse: nothing is recorded or sent for this item
        except HubRefused as exc:
            if exc.code not in ITEM_REFUSALS:
                raise
            refused[lg] = exc.code
            continue
        if have is None:
            have = state.sent[lg] = {"item_id": item_id, "digest": None, "version": version}
        have["version"] = version
        save(
            state
        )  # id and version are recorded BEFORE the put: a lost reply is retried under the same id, higher version
        try:
            link.call("share_put", {"item_id": item_id, "sealed": sealed})
        except HubRefused as exc:
            if exc.code not in ITEM_REFUSALS:
                raise
            refused[lg] = exc.code
            continue
        have["digest"] = item.digest
        counts["put"] += 1
        save(state)
    if refused:
        counts["refused"] = refused
    save(state)
    return counts
