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

**The sealed bytes are NOT specified by the contract** (per-item key sealed to ``HXK``, the ``WSK``-signed state, the
host-chain head, the summary shape). They are produced by :func:`build_sealed_item` (the one place that changes when the
phase-4 contract extension is written); until then it refuses (``sealed_format_pending``) and nothing is sent.

Fail closed: unreadable state, an unknown rule or a malformed stored value is ``malformed``; nothing is guessed."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from orch.canon.text import clean_line
from orch.crypto import suite
from orch.crypto.encoding import b64u

from . import wire
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
    "build_sealed_item",
    "describe",
    "normalize_rule",
    "select",
    "sync",
]

MAX_ITEMS = 500  # one workspace shares at most this many items; more is refused before anything is sent
TITLE_MAX = 120
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
    kind: str  # ticket | question | summary
    ticket_uid: str
    ticket_key: str
    payload: dict[str, Any]  # what the host selects to share, plain JSON (shown to the owner; the builder seals it)
    digest: str  # b64u SHA-256 of the canonical payload: a change re-sends

    def line(self) -> str:
        """One line for the owner's review."""
        if self.kind == "summary":
            return f"needs you: {self.payload['type']}: {self.payload['title']}"
        if self.kind == "question":
            return f"question on {clean_line(self.ticket_key)}: {self.payload['title']}"
        return f"ticket {clean_line(self.ticket_key)}: {self.payload['title']}"


def _line(text: str) -> str:
    out = clean_line(text)
    return out if len(out) <= TITLE_MAX else out[: TITLE_MAX - 1] + "…"


def _item(logical: str, kind: str, t: TicketFact, payload: dict[str, Any]) -> ShareItem:
    return ShareItem(logical, kind, t.uid, t.key, payload, b64u(suite.sha256(wire.cj(payload))))


def select(rules: Iterable[Mapping[str, str]], tickets: Iterable[TicketFact]) -> list[ShareItem]:
    """The items the rules select from ``tickets`` (the owner's visible tickets), sorted by logical id."""
    rs = [normalize_rule(dict(r)) for r in rules]
    kinds = {r["kind"] for r in rs}
    out: dict[str, ShareItem] = {}
    for t in tickets:
        hit = (
            any(r["kind"] == "type" and r["type"] == t.type for r in rs)
            or any(r["kind"] == "ticket" and r["ticket"] in (t.key, t.uid) for r in rs)
            or any(r["kind"] == "tag" and r["tag"] in t.labels for r in rs)
        )
        if hit:
            out[f"ticket:{t.uid}"] = _item(
                f"ticket:{t.uid}", "ticket", t,
                {"kind": "ticket", "key": t.key, "type": t.type, "title": _line(t.title), "status": t.status,
                 "labels": sorted(t.labels), "deadline": t.due},
            )  # fmt: skip
        if "questions" in kinds:
            for q in t.questions:
                if q.to_owner:
                    lg = f"question:{t.uid}:{q.qid}"
                    out[lg] = _item(
                        lg, "question", t,
                        {"kind": "question", "key": t.key, "type": t.type, "id": q.qid, "title": _line(q.text),
                         "text": q.text, "deadline": t.due},
                    )  # fmt: skip
        if "needs_you" in kinds:
            for n in t.needs:
                if not n.to_owner:
                    continue
                title = (
                    next((q.text for q in t.questions if q.qid == n.ref), t.title) if n.kind == "question" else t.title
                )
                lg = f"summary:{t.uid}:{n.kind}:{n.ref or ''}"
                out[lg] = _item(
                    lg, "summary", t, {"kind": "summary", "type": n.kind, "title": _line(title), "deadline": t.due}
                )  # fmt: skip
    return [out[k] for k in sorted(out)]


# --- the sealed bytes: the one isolated gap ------------------------------------------------------------------------


class SealedItemBuilder(Protocol):
    def __call__(self, item_id: str, item: ShareItem) -> str:
        """The b64u ``sealed`` of one item (per-item key sealed to ``HXK``, ``WSK``-signed state, chain head)."""
        ...


def build_sealed_item(item_id: str, item: ShareItem) -> str:
    """NOT SPECIFIED by the contract (V3-D23 phase-4 extension). Refuses until it is: nothing is sent, nothing is
    invented. The fields needed are listed in the C6 report; ``select`` already fixes what ``item.payload`` holds."""
    raise HubRefused("sealed_format_pending")


# --- state ----------------------------------------------------------------------------------------------------------


@dataclass
class ShareState:
    """``rules``: the owner's rules. ``sent``: logical id -> ``{"item_id", "digest"}`` (``digest`` ``None``: a put whose
    reply never came: the next sync re-sends under the same id). ``withdrawn``: logical ids the owner revoked by hand:
    never re-shared while they match, forgotten once they stop matching or a ``ticket`` rule names them again."""

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
                type(lg) is str and type(v) is dict and set(v) == {"item_id", "digest"} and wire.hex16(v["item_id"])
                and (v["digest"] is None or type(v["digest"]) is str)
            )  # fmt: skip
            if not ok:
                raise HubRefused("malformed")
            sent[lg] = {"item_id": v["item_id"], "digest": v["digest"]}
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
    build: SealedItemBuilder = build_sealed_item,
    save: Callable[[ShareState], None],
    new_id: Callable[[], str] = new_item_id,
) -> dict[str, int]:
    """Make the hub's copy equal ``wanted`` minus the owner's withdrawn items. Revokes first, then puts; ``save`` runs
    after every link call that succeeded, so a crash or an unreachable hub loses nothing and repeats nothing. Returns
    the counts ``{"put", "revoke", "kept"}``. Stops at the first refusal (the state stays as far as it got)."""
    items = {i.logical: i for i in wanted}
    if len(items) > MAX_ITEMS:
        raise HubRefused("too_many_items")  # before anything is sent
    # a withdrawal ends when the item no longer matches
    state.withdrawn = [lg for lg in state.withdrawn if lg in items]
    live = {lg: i for lg, i in items.items() if lg not in state.withdrawn}
    counts = {"put": 0, "revoke": 0, "kept": 0}
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
        sealed = build(item_id, item)  # may refuse: nothing is recorded or sent for this item
        if have is None:
            have = state.sent[lg] = {"item_id": item_id, "digest": None}
            save(state)  # the id is recorded BEFORE the put: a lost reply is retried under the same id
        link.call("share_put", {"item_id": item_id, "sealed": sealed})
        have["digest"] = item.digest
        counts["put"] += 1
        save(state)
    save(state)
    return counts
