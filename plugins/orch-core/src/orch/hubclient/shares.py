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
    #: ``(seq, head)`` of the ticket's log, read in the same locked snapshot as the fields above (C6r3-2): the chain
    #: claim of an item then always describes the content it carries. ``None``: not known (the builder refuses).
    chain: tuple[int, str] | None = None


@dataclass(frozen=True)
class ShareItem:
    logical: str  # stable identity: "ticket:<uid>", "question:<uid>:<qid>", "summary:<uid>:<kind>:<ref>"
    kind: str  # ticket | question | summary (summary is the contract's item_kind ``needs_you``)
    ticket_uid: str
    ticket_key: str
    payload: dict[str, Any]  # the exact 10.2b content of this kind (shown to the owner; the builder signs and seals it)
    digest: str  # b64u SHA-256 of the canonical payload: a change re-sends
    chain: tuple[int, str] | None = field(default=None, compare=False)  # the snapshot's (seq, head); not in the digest

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
    return ShareItem(logical, kind, t.uid, t.key, payload, b64u(suite.sha256(wire.cj(payload))), t.chain)


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
    def __call__(self, item_id: str, item: ShareItem, version: int) -> tuple[str, str]:
        """``(sealed, item_hash)`` of one put. The b64u ``sealed`` is the ``WSK``-signed ``share.item`` at
        ``version``, sealed under a fresh item
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
    chain_of: Callable[[str], tuple[int, str] | None] | None = None,
) -> SealedItemBuilder:
    """The 10.2b builder for one hub binding. The chain claim is the item's own ``chain``, read with its content in
    one snapshot (C6r3-2). ``chain_of(ticket_uid)`` is only the fallback for an item without one (``(seq, head)`` of
    that ticket's log now); with neither, or when it answers ``None`` (unreadable or empty log), the builder refuses:
    an item without its chain head is never sent."""

    def build(item_id: str, item: ShareItem, version: int) -> tuple[str, str]:
        head = item.chain if item.chain is not None else (chain_of(item.ticket_uid) if chain_of else None)
        if head is None:
            raise HubRefused("malformed")
        signed = sharefmt.build_signed_item(
            hub_id=hub_id, workspace_id=workspace_id, item_id=item_id, item_kind=ITEM_KIND[item.kind],
            item_version=version, issued_ms=now_ms(), chain={"log": item.ticket_uid, "seq": head[0], "head": head[1]},
            content=item.payload, dev=dev, sign=sign,
        )  # fmt: skip
        return sharefmt.seal_share_item(signed, hxk_pub=hxk_pub), sharefmt.item_hash(signed)

    return build


# --- state ----------------------------------------------------------------------------------------------------------


@dataclass
class ShareState:
    """``rules``: the owner's rules. ``sent``: logical id -> ``{"item_id", "digest", "version", "item_hash"}``
    (``item_hash``: the hash of the live put, for the 10.2b return check; ``digest`` ``None``: a put whose reply
    never came: the next sync re-sends under the same id and a higher ``version``;
    the version only grows, it is the hub's order of an item's puts, 10.2b). ``withdrawn``: logical ids the owner
    revoked by hand: never re-shared while they match, forgotten once they stop matching or a ``ticket`` rule names
    them again. ``refused`` (C6r3-1): logical id -> ``{"code", "line"}`` of the items the contract or the hub refused on
    the last sync that tried them (``line`` is the owner-facing one-liner); it is shown to the owner until the item is
    sent, or stops matching. An item that is in ``sent`` with a ``digest`` AND refused is **stale**: the hub still
    serves its older version."""

    rules: list[dict[str, str]] = field(default_factory=lambda: [dict(r) for r in DEFAULT_RULES])
    sent: dict[str, dict[str, Any]] = field(default_factory=dict)
    withdrawn: list[str] = field(default_factory=list)
    refused: dict[str, dict[str, str]] = field(default_factory=dict)

    def refused_items(self) -> list[dict[str, Any]]:
        """What the owner is told about refused items: ``{"item", "code", "stale"}``, sorted by logical id.
        ``stale``: the hub holds an older version of this item that this refusal kept from being replaced."""
        return [
            {"item": r["line"], "code": r["code"], "stale": (self.sent.get(lg) or {}).get("digest") is not None}
            for lg, r in sorted(self.refused.items())
        ]

    def to_json(self) -> dict[str, Any]:
        return {"rules": self.rules, "sent": self.sent, "withdrawn": sorted(self.withdrawn), "refused": self.refused}

    @classmethod
    def from_json(cls, d: Any) -> ShareState:
        if type(d) is not dict or not {"rules", "sent", "withdrawn"} <= set(d) <= {
            "rules",
            "sent",
            "withdrawn",
            "refused",
        }:
            raise HubRefused("malformed")
        if type(d["rules"]) is not list or type(d["sent"]) is not dict or type(d["withdrawn"]) is not list:
            raise HubRefused("malformed")
        rules = [normalize_rule(r) for r in d["rules"]]
        sent: dict[str, dict[str, Any]] = {}
        for lg, v in d["sent"].items():
            ok = (
                type(lg) is str and type(v) is dict
                and {"item_id", "digest"} <= set(v) <= {"item_id", "digest", "version", "item_hash"}
                and wire.hex16(v["item_id"]) and (v["digest"] is None or type(v["digest"]) is str)
                and (type(v.get("version", 0)) is int and 0 <= v.get("version", 0) < 2**32 - 1)
                and (v.get("item_hash") is None or sharefmt.HASH_RE.fullmatch(str(v["item_hash"])) is not None)
            )  # fmt: skip
            if not ok:
                raise HubRefused("malformed")
            sent[lg] = {
                "item_id": v["item_id"], "digest": v["digest"], "version": v.get("version", 0),
                "item_hash": v.get("item_hash"),
            }  # fmt: skip
        if any(type(x) is not str for x in d["withdrawn"]):
            raise HubRefused("malformed")
        refused: dict[str, dict[str, str]] = {}
        raw = d.get("refused", {})
        if type(raw) is not dict:
            raise HubRefused("malformed")
        for lg, r in raw.items():
            if (
                type(lg) is not str or type(r) is not dict or set(r) != {"code", "line"}
                or type(r["code"]) is not str or not 0 < len(r["code"]) <= 64
                or type(r["line"]) is not str or len(r["line"]) > 1000
            ):  # fmt: skip
                raise HubRefused("malformed")
            refused[lg] = {"code": r["code"], "line": r["line"]}
        return cls(rules=rules, sent=sent, withdrawn=list(d["withdrawn"]), refused=refused)

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
    the counts ``{"put", "revoke", "kept"}``, plus ``"refused"`` (logical id -> code) and ``"refused_items"``
    (:meth:`ShareState.refused_items`) when items were skipped. One item the contract or the hub refuses as such
    (``malformed``, ``too_large``) is skipped and reported, and the rest goes on; any other refusal (hub not reachable,
    wrong mode, ...) stops the sync (the state stays as far as it got)."""
    items = {i.logical: i for i in wanted}
    if len(items) > MAX_ITEMS:
        raise HubRefused("too_many_items")  # before anything is sent
    # a withdrawal ends when the item no longer matches
    state.withdrawn = [lg for lg in state.withdrawn if lg in items]
    live = {lg: i for lg, i in items.items() if lg not in state.withdrawn}
    state.refused = {lg: r for lg, r in state.refused.items() if lg in live}  # an item that left is not refused
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
            state.refused.pop(lg, None)  # the hub holds this very version
            counts["kept"] += 1
            continue
        item_id = new_id() if have is None else have["item_id"]
        version = (0 if have is None else have.get("version", 0)) + 1  # every put attempt takes a higher version
        try:
            sealed, ihash = build(item_id, item, version)  # may refuse: nothing is recorded or sent for this item
        except HubRefused as exc:
            if exc.code not in ITEM_REFUSALS:
                raise
            refused[lg] = exc.code
            state.refused[lg] = {"code": exc.code, "line": item.line()}
            continue
        if have is None:
            have = state.sent[lg] = {"item_id": item_id, "digest": None, "version": version, "item_hash": None}
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
            state.refused[lg] = {"code": exc.code, "line": item.line()}
            continue
        state.refused.pop(lg, None)
        have["digest"] = item.digest
        have["item_hash"] = ihash  # what a custodial event about this item binds (10.2b), for the return check
        counts["put"] += 1
        save(state)
    if refused:
        counts["refused"] = refused
        counts["refused_items"] = state.refused_items()  # for the owner's text output (with the stale flag)
    save(state)
    return counts
