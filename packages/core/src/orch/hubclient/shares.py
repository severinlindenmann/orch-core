"""Selective-mode share rules and their evaluation (V3 C6; spec 8a, D80; hub contract 10.2,
``share_put`` / ``share_revoke``).

A selective workspace shows the hub only what its owner selects. The owner's **share rules** are a closed list:

* ``{"kind": "needs_you"}``: needs-you summaries (type, one-line title, deadline; plus a full-text
  `question` item per question need). The default and the only rule a new
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

import copy
import hashlib
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
#: the rules that select tickets: only these may carry an ``audience`` (who besides the owner may read the ticket at the
#: hub, P02) and ``description`` (the ticket's text leaves too). Both are absent in the stored form when empty / false.
TICKET_KINDS = frozenset({"type", "ticket", "tag"})
DEFAULT_RULES: tuple[dict[str, Any], ...] = ({"kind": "needs_you"},)


def normalize_audience(audience: Any) -> dict[str, list[str]] | None:
    """``{"users", "groups"}`` sorted and unique, or ``None`` for "owner only" (nothing named); else ``malformed``."""
    if type(audience) is not dict or set(audience) != {"users", "groups"}:
        raise HubRefused("malformed")
    users, groups = audience["users"], audience["groups"]
    if type(users) is not list or type(groups) is not list:
        raise HubRefused("malformed")
    if not users and not groups:
        return None
    try:
        out = {"users": sorted(set(users)), "groups": sorted(set(groups))}
    except TypeError:
        raise HubRefused("malformed") from None
    sharefmt.check_audience(out, "ticket")
    return out


def normalize_rule(rule: Any) -> dict[str, Any]:
    """The rule in its one stored form, or ``malformed``. Values are short single-line text."""
    if type(rule) is not dict or rule.get("kind") not in _RULE_KEYS:
        raise HubRefused("malformed")
    kind = rule["kind"]
    optional = {"audience", "description"} if kind in TICKET_KINDS else set()
    if not {"kind", *_RULE_KEYS[kind]} <= set(rule) <= {"kind", *_RULE_KEYS[kind], *optional}:
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
    out: dict[str, Any] = {k: rule[k] for k in ("kind", *sorted(_RULE_KEYS[kind]))}
    if "audience" in rule and (aud := normalize_audience(rule["audience"])) is not None:
        out["audience"] = aud
    if "description" in rule:
        if type(rule["description"]) is not bool:
            raise HubRefused("malformed")
        if rule["description"]:
            out["description"] = True
    return out


def describe(rule: Mapping[str, Any]) -> str:
    """What the rule shares, in words (WYSIWYS: the owner types a confirmation after reading this)."""
    k = rule["kind"]
    if k == "needs_you":
        return (
            "needs-you summaries (type, a one-line title and the deadline); a question you must answer is also shared "
            "in full (up to 2000 bytes) as its own item"
        )
    if k == "questions":
        return "every open question addressed to you (the full question)"
    what = "title, status, labels, deadline" + (" and description" if rule.get("description") else "")
    aud = rule.get("audience")
    who = (
        ""
        if not aud
        else f"; readable by {len(aud['users'])} named people and {len(aud['groups'])} teams on the hub besides you"
    )
    if k == "type":
        return f"every ticket of type {clean_line(rule['type'])!r} ({what}){who}"
    if k == "ticket":
        return f"the ticket {clean_line(rule['ticket'])!r} ({what}){who}"
    return f"every ticket labelled {clean_line(rule['tag'])!r} ({what}){who}"


# --- facts (the host builds these from its replayed state; this module never imports the model) ---------------------


@dataclass(frozen=True)
class QuestionFact:
    qid: str  # the question id within the ticket
    text: str
    to_owner: bool  # addressed to an owner of this workspace
    hash: str | None = None  # the question hash (ticket-format 5.6) now; for the item's f1


@dataclass(frozen=True)
class GateFact:
    """One gate of a ticket as the F1 values of ``f1.gates`` (hub contract 10.2b): the current generation, the gate hash
    and policy hash, and (``code``, ``verify`` only) the source list as ``(repo, ref, sha)`` triples."""

    gate: str
    gen: int
    hash: str
    policy_hash: str
    source_sha: tuple[tuple[str, str, str], ...] | None = None


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
    gates: tuple[GateFact, ...] = ()  # the gates whose hash can be built now
    roster_v: int | None = None  # the workspace member-roster version (WorkspaceView.roster_v); None: no f1
    #: ``(seq, head)`` of the ticket's log, read in the same locked snapshot as the fields above (C6r3-2): the chain
    #: claim of an item then always describes the content it carries. ``None``: not known (the builder refuses).
    chain: tuple[int, str] | None = None
    description: str = ""  # the ticket's Summary and Context text (shared only when the rule says so)


@dataclass(frozen=True)
class ShareItem:
    logical: str  # stable identity: "ticket:<uid>", "question:<uid>:<qid>", "summary:<uid>:<kind>:<ref>"
    kind: str  # ticket | question | summary (summary is the contract's item_kind ``needs_you``)
    ticket_uid: str
    ticket_key: str
    payload: dict[str, Any]  # the exact 10.2b content of this kind (shown to the owner; the builder signs and seals it)
    digest: str  # b64u SHA-256 of the canonical payload and f1: a change of either re-sends (a new item_version)
    f1: dict[str, Any] | None = field(default=None, compare=False)  # the 10.2b f1 block (None: shared without one)
    shown: str = field(default="", compare=False)  # b64u SHA-256 of what the owner is shown (payload + decision data)
    data: tuple[str, ...] = field(default=(), compare=False)  # decision data lines (repos and refs of f1), C6b-3
    chain: tuple[int, str] | None = field(default=None, compare=False)  # the snapshot's (seq, head); not in the digest
    #: who besides the owner may read this ticket at the hub (contract 10.2b ``audience``): in the digest and in the
    #: plan the owner confirms, so a change of audience is a new, shown, confirmed version
    audience: dict[str, list[str]] | None = field(default=None, compare=False)

    def line(self) -> str:
        """One line for the owner's review."""
        extra = "".join(f" [{d}]" for d in self.data)
        if self.kind == "summary":
            return f"needs you: {self.payload['type']}: {self.payload['title']}{extra}"
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


def make_f1(t: TicketFact, gates: Iterable[str] = (), questions: Iterable[str] = ()) -> dict[str, Any] | None:
    """The ``f1`` block of one item (hub contract 10.2b): ``roster_v``, and for each named gate / question the F1
    values the host has NOW (a gate whose hash cannot be built, or whose entry the contract cannot express, is left
    out: the hub then answers ``unknown_item`` for it, never a wrong value). ``None`` when the ticket carries no
    roster version."""
    if t.roster_v is None:
        return None
    want_g, want_q = set(gates), set(questions)
    out_g: dict[str, Any] = {}
    for g in t.gates:
        if g.gate not in want_g:
            continue
        entry: dict[str, Any] = {"gen": g.gen, "hash": g.hash, "policy_hash": g.policy_hash}
        if g.gate in sharefmt.F1_SOURCE_GATES:
            entry["source_sha"] = [{"repo": r, "ref": f, "sha": s} for r, f, s in (g.source_sha or ())]
        if sharefmt.f1_gate_ok(g.gate, entry):
            out_g[g.gate] = entry
    out_q = {q.qid: {"hash": q.hash} for q in t.questions if q.qid in want_q and q.hash is not None}
    f1 = {"roster_v": t.roster_v, "gates": out_g, "questions": dict(sorted(out_q.items())[: sharefmt.F1_MAX_QUESTIONS])}
    try:
        sharefmt.check_f1(f1, {"log": t.uid})
    except HubRefused:
        return None
    return f1


def decision_data(f1: Mapping[str, Any] | None) -> list[str]:
    """What f1 carries beyond ids and hashes, as preview lines: the repo URLs and branch refs of a gate's source list
    (C6b-3). They reach the hub, so the preview shows them and the plan covers them."""
    if f1 is None:
        return []
    return sorted(
        f"{g} source: {clean_line(e['repo'])} {clean_line(e['ref'])}"
        for g, v in f1["gates"].items() for e in v.get("source_sha", ())
    )  # fmt: skip


def decision_raw(f1: Mapping[str, Any] | None) -> list[list[str]]:
    """The exact ``[gate, repo, ref]`` entries of each ``source_sha`` list behind :func:`decision_data`, uncleaned: what
    the plan hashes. The commit sha stays out: it is not shown; a moved sha changes the digest, not the plan."""
    if f1 is None:
        return []
    return sorted(
        [g, e["repo"], e["ref"]]
        for g, v in f1["gates"].items() for e in v.get("source_sha", ())
    )  # fmt: skip


def _item(
    logical: str,
    kind: str,
    t: TicketFact,
    payload: dict[str, Any],
    f1: dict[str, Any] | None = None,
    audience: dict[str, list[str]] | None = None,
) -> ShareItem | None:
    """The item, or ``None`` when the contract cannot express it (a key, label or text outside its rules, a question
    over 2000 bytes): it is then not offered and nothing of it is sent. ``f1`` is part of the digest, so a change of
    any of its values (a member change raises ``roster_v``, a new generation, a changed policy or source list, a
    re-asked question) re-sends the item under a higher ``item_version``; the plan the owner is shown covers the
    payload only (``shown``): those values are not content."""
    try:
        sharefmt.check_content(ITEM_KIND[kind], payload)
    except HubRefused:
        return None
    data = decision_data(f1)
    # the plan covers the RAW repo and ref of each source_sha entry, not the cleaned preview lines: repo strings that
    # differ only in whitespace look alike once cleaned (C6bd-1, core#1040)
    raw = decision_raw(f1)
    extra: dict[str, Any] = ({"decision_data": raw} if raw else {}) | ({"audience": audience} if audience else {})
    shown = b64u(suite.sha256(wire.cj({"payload": payload, **extra} if extra else payload)))
    digest = (
        shown
        if f1 is None
        else b64u(suite.sha256(wire.cj({"payload": payload, "f1": f1} | ({"audience": audience} if audience else {}))))
    )
    return ShareItem(logical, kind, t.uid, t.key, payload, digest, f1, shown, tuple(data), t.chain, audience)


def _valid_labels(labels: Iterable[str]) -> list[str]:
    ok = sorted({x for x in labels if sharefmt.LABEL_RE.fullmatch(x)})
    return ok[: sharefmt.MAX_LABELS]


def _description(text: str) -> str:
    """A ticket's description as the contract's text (LF line ends, invisible characters made visible), cut to its
    limit; ``""`` when there is none."""
    out = _cut(_multiline(text), sharefmt.DESCRIPTION_BYTES).strip()
    return out


def _union_audience(rules: Iterable[Mapping[str, Any]]) -> dict[str, list[str]] | None:
    users: set[str] = set()
    groups: set[str] = set()
    for r in rules:
        a = r.get("audience")
        if a:
            users |= set(a["users"])
            groups |= set(a["groups"])
    if not users and not groups:
        return None
    return {
        "users": sorted(users)[: sharefmt.MAX_AUDIENCE_USERS],
        "groups": sorted(groups)[: sharefmt.MAX_AUDIENCE_GROUPS],
    }


def select(rules: Iterable[Mapping[str, Any]], tickets: Iterable[TicketFact]) -> list[ShareItem]:
    """The items the rules select from ``tickets`` (the owner's visible tickets), sorted by logical id."""
    rs = [normalize_rule(dict(r)) for r in rules]
    kinds = {r["kind"] for r in rs}
    out: dict[str, ShareItem] = {}

    def put(item: ShareItem | None) -> None:
        if item is not None:
            out[item.logical] = item

    for t in tickets:
        matching = [
            r for r in rs
            if (r["kind"] == "type" and r["type"] == t.type)
            or (r["kind"] == "ticket" and r["ticket"] in (t.key, t.uid))
            or (r["kind"] == "tag" and r["tag"] in t.labels)
        ]  # fmt: skip
        if matching:
            content: dict[str, Any] = {
                "key": t.key,
                "type": t.type,
                "title": _line(t.title),
                "status": t.status,
                "labels": _valid_labels(t.labels),
                "deadline": t.due,
            }
            # the text leaves only when EVERY rule that selects the ticket says so (fail closed: a rule from before
            # this option, or one that unticks it, keeps the text home); the readers are the rules' audiences joined
            if all(r.get("description") for r in matching) and (text := _description(t.description)):
                content["description"] = text
            put(_item(f"ticket:{t.uid}", "ticket", t, content, None, _union_audience(matching)))
            # (no f1: a decision is only built from an item that names that decision, C6b-1/2)
        if "questions" in kinds:
            for q in t.questions:
                if q.to_owner:
                    put(_item(
                        f"question:{t.uid}:{q.qid}", "question", t,
                        {"key": t.key, "type": t.type, "id": q.qid, "title": _line(q.text),
                         "text": _multiline(q.text), "deadline": t.due},
                        make_f1(t, (), {q.qid}),
                    ))  # fmt: skip
        if "needs_you" in kinds:
            for n in t.needs:
                if not n.to_owner or n.kind not in NEED_TYPES:
                    continue
                if n.kind == "question":
                    # C6-3 / C6bd-2 (V3-D31): the one-line summary never carries f1.questions (nothing answerable from a
                    # cut title); the full text goes in a `question` item, the only one that names the question.
                    q = next((x for x in t.questions if x.qid == n.ref), None)
                    if q is not None:
                        put(_item(
                            f"question:{t.uid}:{q.qid}", "question", t,
                            {"key": t.key, "type": t.type, "id": q.qid, "title": _line(q.text),
                             "text": _multiline(q.text), "deadline": t.due},
                            make_f1(t, (), {q.qid}),
                        ))  # fmt: skip
                    put(_item(
                        f"summary:{t.uid}:{n.kind}:{n.ref or ''}", "summary", t,
                        {"type": NEED_TYPES[n.kind], "title": _line(q.text if q else t.title), "deadline": t.due},
                    ))  # fmt: skip
                    continue
                put(_item(
                    f"summary:{t.uid}:{n.kind}:{n.ref or ''}", "summary", t,
                    {"type": NEED_TYPES[n.kind], "title": _line(t.title), "deadline": t.due},
                    make_f1(t, {n.ref} if n.ref else ()),
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
            content=item.payload, dev=dev, sign=sign, f1=item.f1, audience=item.audience,
        )  # fmt: skip
        try:
            return sharefmt.seal_share_item(signed, hxk_pub=hxk_pub), sharefmt.item_hash(signed)
        except HubRefused as exc:
            if exc.code != "too_large" or item.f1 is None:
                raise
        # the F1 values do not fit the 16 KiB: share the item without them (the hub then cannot build on it:
        # ``unavailable``, fail closed) rather than not at all
        signed = sharefmt.build_signed_item(
            hub_id=hub_id, workspace_id=workspace_id, item_id=item_id, item_kind=ITEM_KIND[item.kind],
            item_version=version, issued_ms=now_ms(), chain={"log": item.ticket_uid, "seq": head[0], "head": head[1]},
            content=item.payload, dev=dev, sign=sign, audience=item.audience,
        )  # fmt: skip
        return sharefmt.seal_share_item(signed, hxk_pub=hxk_pub), sharefmt.item_hash(signed)

    return build


# --- state ----------------------------------------------------------------------------------------------------------


def plan_of(items: Iterable[ShareItem]) -> str:
    """The digest of a selection (logical id and content digest of every item): what ``share.preview`` hands the owner
    and what a confirming op must present back."""
    h = hashlib.sha256()
    for i in items:
        h.update(f"{i.logical}\0{i.shown or i.digest}\n".encode())
    return b64u(h.digest())


_STATE_KEYS = frozenset({"rules", "sent", "withdrawn", "refused", "confirmed"})


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

    rules: list[dict[str, Any]] = field(default_factory=lambda: [dict(r) for r in DEFAULT_RULES])
    sent: dict[str, dict[str, Any]] = field(default_factory=dict)
    withdrawn: list[str] = field(default_factory=list)
    refused: dict[str, dict[str, str]] = field(default_factory=dict)
    #: logical ids of the selection the owner last confirmed (a rule change, ``share.sync``, ``share.revoke``): the
    #: only items an automatic pass may send or update. ``None``: a state from before this field: its ``sent`` items.
    confirmed: list[str] | None = None

    def confirm(self, selected: Iterable[ShareItem], expect: str) -> None:
        """The owner confirmed this selection: remember it for the automatic passes.

        Invariant (C6fud-1): a selection is confirmed only against the plan the owner was shown (``expect``, see
        :func:`plan_of`); no other path may widen what an automatic pass sends. Withdrawn items are not part of what
        was shown. A different selection raises :class:`HubRefused` (``plan_changed``) and changes nothing."""
        items = [i for i in selected if i.logical not in self.withdrawn]
        if not isinstance(expect, str) or plan_of(items) != expect:
            raise HubRefused("plan_changed")
        self.confirmed = sorted({i.logical for i in items})

    def auto_wanted(self, selected: Iterable[ShareItem]) -> list[ShareItem]:
        """What an automatic pass may send: the confirmed items that still match, with their CURRENT content.

        C6 follow-up rule: the owner confirmed the rules, not each content change, so a changed ticket that is already
        in the confirmed selection is re-sent (a new version) and one that stops matching is revoked (it is missing
        here, so :func:`sync` withdraws it). A ticket, question or need that starts matching AFTER the confirmation
        is not in the confirmed set and is never sent by a pass: it waits for the owner's next confirm (what was shown
        is what is sent, C6-2). The confirmed set also shrinks to what still matches, so an item that left and comes
        back needs a new confirm. An agent changing tickets therefore cannot widen what the hub sees."""
        known = set(self.sent) if self.confirmed is None else set(self.confirmed)
        out = [i for i in selected if i.logical in known]
        self.confirmed = sorted(i.logical for i in out)
        return out

    def refused_items(self) -> list[dict[str, Any]]:
        """What the owner is told about refused items: ``{"item", "code", "stale"}``, sorted by logical id.
        ``stale``: the hub holds an older version of this item that this refusal kept from being replaced."""
        return [
            {"item": r["line"], "code": r["code"], "stale": (self.sent.get(lg) or {}).get("digest") is not None}
            for lg, r in sorted(self.refused.items())
        ]

    def to_json(self) -> dict[str, Any]:
        return {
            "rules": self.rules,
            "sent": self.sent,
            "withdrawn": sorted(self.withdrawn),
            "refused": self.refused,
        } | ({} if self.confirmed is None else {"confirmed": sorted(self.confirmed)})

    @classmethod
    def from_json(cls, d: Any) -> ShareState:
        if type(d) is not dict or not {"rules", "sent", "withdrawn"} <= set(d) <= _STATE_KEYS:
            raise HubRefused("malformed")
        if type(d["rules"]) is not list or type(d["sent"]) is not dict or type(d["withdrawn"]) is not list:
            raise HubRefused("malformed")
        rules = [normalize_rule(r) for r in d["rules"]]
        sent: dict[str, dict[str, Any]] = {}
        for lg, v in d["sent"].items():
            ok = (
                type(lg) is str and type(v) is dict
                and {"item_id", "digest"} <= set(v) <= {"item_id", "digest", "version", "item_hash", *_ADMISSION_KEYS}
                and wire.hex16(v["item_id"]) and (v["digest"] is None or type(v["digest"]) is str)
                and (type(v.get("version", 0)) is int and 0 <= v.get("version", 0) < 2**32 - 1)
                and (v.get("item_hash") is None or sharefmt.HASH_RE.fullmatch(str(v["item_hash"])) is not None)
                and _admission_ok(v)
            )  # fmt: skip
            if not ok:
                raise HubRefused("malformed")
            sent[lg] = {
                "item_id": v["item_id"], "digest": v["digest"], "version": v.get("version", 0),
                "item_hash": v.get("item_hash"),
            } | {k: copy.deepcopy(v[k]) for k in _ADMISSION_KEYS if k in v}  # fmt: skip
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
        confirmed = d.get("confirmed")
        if confirmed is not None and (type(confirmed) is not list or any(type(x) is not str for x in confirmed)):
            raise HubRefused("malformed")
        return cls(rules=rules, sent=sent, withdrawn=list(d["withdrawn"]), refused=refused, confirmed=confirmed)

    def add_rule(self, rule: Mapping[str, Any]) -> bool:
        r = normalize_rule(dict(rule))
        if r in self.rules:
            return False
        self.rules.append(r)
        return True

    def remove_rule(self, rule: Mapping[str, Any]) -> bool:
        r = normalize_rule(dict(rule))
        if r not in self.rules:
            return False
        self.rules.remove(r)
        return True


#: what a ``sent`` entry keeps for the admission of a selective custodial event (15.7), and how many earlier item
#: hashes it remembers (a decision on an older version is ``event_changed``, not ``not_granted``)
_ADMISSION_KEYS = ("chain", "f1", "superseded")
MAX_SUPERSEDED = 16


def _admission_ok(v: Mapping[str, Any]) -> bool:
    chain, f1, old = v.get("chain"), v.get("f1"), v.get("superseded", [])
    return (
        (chain is None or (
            type(chain) is dict and set(chain) == {"log", "seq", "head"} and type(chain["log"]) is str
            and type(chain["seq"]) is int and chain["seq"] >= 1 and type(chain["head"]) is str
        ))
        and (f1 is None or type(f1) is dict)
        and type(old) is list and len(old) <= MAX_SUPERSEDED
        and all(type(h) is str and sharefmt.HASH_RE.fullmatch(h) is not None for h in old)
    )  # fmt: skip


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
        if have.get("item_hash") not in (
            None,
            ihash,
        ):  # the hash of the version this put replaces (15.7: event_changed)
            old = [h for h in have.get("superseded", []) if h != ihash]
            have["superseded"] = [*old, have["item_hash"]][-MAX_SUPERSEDED:]
        have["digest"] = item.digest
        have["item_hash"] = ihash  # what a custodial event about this item binds (10.2b), for the return check
        # what the person is shown, kept for the admission check of a selective custodial event (ticket-format 15.7):
        # the chain claim and the f1 values the item was signed with. Without both the item grants no decision.
        have.pop("chain", None)
        have.pop("f1", None)
        if item.chain is not None and item.f1 is not None:
            have["chain"] = {"log": item.ticket_uid, "seq": item.chain[0], "head": item.chain[1]}
            have["f1"] = copy.deepcopy(item.f1)
        counts["put"] += 1
        save(state)
    if refused:
        counts["refused"] = refused
        counts["refused_items"] = state.refused_items()  # for the owner's text output (with the stale flag)
    save(state)
    return counts
