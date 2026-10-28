"""Selective-mode sharing, host side (V3 C6; spec 8a, D80; hub contract 10.2). The CLI is ``orch hub share ...``.

Ops (all column P, owner role; an agent is refused, and no op here is reachable from a hub message):

* ``share.rules {}``: the owner's rules and how many items each selects now.
* ``share.preview {add?, remove?}``: what the rule set would share if ``add`` / ``remove`` were applied: one line per
  item, counts, and ``plan`` (a digest of the exact item set). Nothing changes.
* ``share.rule_add {rule, expect}`` / ``share.rule_remove {rule, expect}``: change the rules, then sync. ``expect`` is
  required: the ``plan`` the owner was shown: if the selection is different now, nothing changes (WYSIWYS).
* ``share.revoke {ref}``: withdraw one item now (a ticket key or a ``logical`` id from ``share.preview``); it stays
  withdrawn while it still matches a rule.
* ``share.revoke`` and ``share.rule_remove`` only take sharing away (:data:`REDUCE_OPS`): the dashboard sends them with
  one click as the session's person, like ``workspace.cosmetic.set``; at most 10 a minute, and the security-log line
  names the person. ``orch hub share rule remove`` still asks its own confirmation at the terminal.
* ``share.sync {expect?}``: make the hub's copy equal the selection (``share_put`` / ``share_revoke``). With ``expect``
  (the ``share.preview`` plan, checked like a rule change) the current selection is confirmed; without it a sync only
  retries what was already confirmed. Also run after every rule change and revoke.

Selective workspaces only: in full mode every op is refused (``hub.refused: wrong_mode``), as the hub refuses the link
operations there. A rule change is saved before the hub is asked; if the hub cannot be reached the answer says
``synced: false`` and the next ``share.sync`` finishes the job (a removed rule is never forgotten).

The state is ``hub/shares.json`` (0600, host-owned). What goes into ``sealed`` is the hub contract's 10.2b format
(:func:`orch.hubclient.shares.make_builder`); ``share.revoke`` is idempotent."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from . import shareauto
from .errors import HostError

if TYPE_CHECKING:
    from .workspace import Call, WorkspaceService

__all__ = ["NETWORK_OPS", "SHARE_OPS", "SHARE_READS", "execute", "serve", "sync_now", "tickets_for"]

SHARE_READS = frozenset({"share.rules", "share.preview"})
SHARE_OPS: dict[str, int] = {
    "share.rules": 1, "share.preview": 1, "share.rule_add": 1, "share.rule_remove": 1, "share.revoke": 1,
    "share.sync": 1,
}  # fmt: skip
NETWORK_OPS = frozenset({"share.rule_add", "share.rule_remove", "share.revoke", "share.sync"})
#: The two ops that only take sharing away. Like ``workspace.cosmetic.set`` (cosmeticops) they are unsigned owner
#: actions the dashboard may send with one click as the session's person (column P, owner role, never a relay); they
#: share one rate limit and each writes a security-log line naming the person.
REDUCE_OPS = frozenset({"share.revoke", "share.rule_remove"})
REDUCE_RATE = 10  # per minute and workspace, as for the cosmetic settings
_MU = threading.Lock()
_RULE = {
    "type": "object",
    "properties": {"kind": {"enum": ["needs_you", "questions", "type", "ticket", "tag"]}, "type": {"type": "string"},
                   "ticket": {"type": "string"}, "tag": {"type": "string"}},
    "required": ["kind"],
    "additionalProperties": False,
}  # fmt: skip
_PLAN = {"type": "string", "pattern": "^[A-Za-z0-9_-]{43}$"}
_SCHEMAS: dict[str, dict[str, Any]] = {
    "share.rules": {"type": "object", "properties": {}, "additionalProperties": False},
    "share.preview": {"type": "object", "properties": {"add": _RULE, "remove": _RULE}, "additionalProperties": False},
    "share.rule_add": {
        "type": "object",
        "properties": {"rule": _RULE, "expect": _PLAN},
        "required": ["rule", "expect"],
        "additionalProperties": False,
    },  # fmt: skip
    "share.rule_remove": {
        "type": "object",
        "properties": {"rule": _RULE, "expect": _PLAN},
        "required": ["rule", "expect"],
        "additionalProperties": False,
    },  # fmt: skip
    "share.revoke": {
        "type": "object",
        "properties": {"ref": {"type": "string", "minLength": 1, "maxLength": 200}},
        "required": ["ref"],
        "additionalProperties": False,
    },  # fmt: skip
    "share.sync": {"type": "object", "properties": {"expect": _PLAN}, "additionalProperties": False},
}
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}


def _dir(service: WorkspaceService) -> Any:
    from . import hubops

    return hubops._dir(service)  # noqa: SLF001


def _owner(call: Call) -> None:
    if call.column.name != "P" or call.viewer is None or call.relay is not None:
        raise HostError(
            "op.not_allowed", "a person does this at their own terminal; an agent never changes what is shared"
        )
    member = call.service.store.state.workspace.members.get(call.viewer)
    if member is None or member.role != "owner":
        raise HostError("role.denied", "share rules are for workspace owners")


def _check(call: Call) -> tuple[str, dict[str, Any]]:
    op = call.frame["op"]
    args = dict(call.frame["args"])
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        raise HostError("invalid.input", f"{op}: {'.'.join(map(str, errors[0].absolute_path)) or 'args'} is not valid")
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    if (call.frame.get("opts") or {}).get("dry_run"):
        raise HostError("invalid.input", f"dry_run is not supported by {op}; use share.preview")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    if op not in SHARE_READS and service.state == "degraded":
        raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")
    _owner(call)
    service.store.refresh()
    return op, args


def _selective(service: WorkspaceService) -> Any:
    """The bound hub, in selective mode only (``wrong_mode`` otherwise: these operations do not exist in full mode)."""
    from . import peerops

    state = peerops._hub_state(service, full=False)  # noqa: SLF001
    model = dict(service.store.state.workspace.hub)
    if state.mode != "selective" or model.get("mode") != "selective":
        raise HostError("hub.refused", "the hub is refused: wrong_mode: sharing rules exist only in selective mode")
    return state


def serve(call: Call) -> dict[str, Any]:
    """The worker half: shape, who, and that a selective hub is bound. Reads and the rule bookkeeping are answered
    here (no network); the hub work runs off the worker in :func:`execute`."""
    op, args = _check(call)
    _selective(call.service)
    if op in SHARE_READS:
        return _read(call, op, args)
    if op in REDUCE_OPS:
        _rate(call.service)
    return {"plan": {"op": op, "args": args}}


def _rate(service: Any) -> None:
    """At most :data:`REDUCE_RATE` revokes and rule removals a minute (the worker runs one call at a time)."""
    now = service.clock()
    hits = [t for t in getattr(service, "_share_reduce_hits", []) if now - t < 60]
    if len(hits) >= REDUCE_RATE:
        raise HostError("host.busy", "too many sharing changes: wait a minute", retry_after_ms=60_000)
    hits.append(now)
    service._share_reduce_hits = hits  # noqa: SLF001


async def execute(call: Call, planned: dict[str, Any]) -> dict[str, Any]:
    import asyncio

    if "plan" not in planned:
        return planned
    return await asyncio.to_thread(_run, call, planned["plan"])


# --- state ------------------------------------------------------------------------------------------------------------


def _load(service: WorkspaceService) -> Any:
    from orch.hubclient.errors import HubRefused
    from orch.hubclient.shares import ShareState

    from .custody import read_json

    doc = read_json(_dir(service) / "shares.json", service.host_uid, limit=16 << 20)
    if doc is None:
        return ShareState()
    try:
        return ShareState.from_json(doc)
    except HubRefused:
        raise HostError("hub.state_mismatch", "hub/shares.json is not readable: the host refuses to guess") from None


def _save(service: WorkspaceService, st: Any) -> None:
    from orch.store.fsio import write_atomic

    from .custody import ensure_private_dir

    d = _dir(service)
    for level in (d.parent, d):
        ensure_private_dir(level, service.host_uid)
    write_atomic(d / "shares.json", json.dumps(st.to_json(), sort_keys=True).encode(), mode=0o600)
    shareauto.arm(service)  # the owner has used sharing: ticket changes now trigger a debounced pass


# --- facts and selection ----------------------------------------------------------------------------------------------


def _shareable(state: Any, viewer: str | None) -> dict[str, Any]:
    """Share candidates, failing closed: only workspace-visible tickets (a restricted ticket is never shared or
    previewed, C6-4), and with a ``viewer`` only those that person may see."""
    pool = state.tickets if viewer is None else state.visible_to(viewer)
    return {u: t for u, t in pool.items() if t.visibility == "workspace"}


def tickets_for(service: WorkspaceService, viewer: str | None = None) -> list[Any]:
    """The owner's visible tickets as :class:`~orch.hubclient.shares.TicketFact` (restricted tickets the owners cannot
    see are never offered). A question or need is "to me" when an owner of this workspace is an addressee."""
    store = service.store
    # C6r3-2: the fields and each ticket's (seq, head) come from ONE locked read, so an item's chain claim always
    # describes the content it carries (a writer cannot slip a commit in between)
    with store.locked():
        state = store.state
        seen = _shareable(state, viewer)
        return [ticket_fact(store, state, uid, t) for uid, t in sorted(seen.items())]


def ticket_fact(store: Any, state: Any, uid: str, t: Any) -> Any:
    """One ticket as a :class:`~orch.hubclient.shares.TicketFact`: its fields, gate facts (the ones whose hash can be
    built now), the workspace roster version and its log's ``(seq, head)``. The caller holds ``store.locked()`` and
    passes the ``state`` it read there. One copy of the gate fact code: the selective ``f1`` and the full-mode
    projection ``P`` (D15b) are both made from it."""
    from orch.hubclient.shares import GateFact, NeedFact, QuestionFact, TicketFact

    owners = sorted(p for p, m in state.workspace.members.items() if m.role == "owner")
    texts = {q["id"]: str(q.get("text", "")) for q in t.fields.get("questions", ()) if isinstance(q, Mapping)}
    qs = tuple(
        QuestionFact(q.id, texts.get(q.id, ""), (not q.answered) and bool(set(q.addressees) & set(owners)), q.hash)
        for q in t.questions
    )
    needs = tuple(NeedFact(n.kind, n.ref, bool(set(n.who) & set(owners))) for n in t.needs)
    due = t.fields.get("due")
    gates = tuple(
        GateFact(
            g.gate, g.gen, g.hash, g.policy_hash,
            tuple((e["repo"], e["ref"], e["sha"]) for e in g.input.get("source_sha", ()))
            if g.gate in ("code", "verify") else None,
        )
        for g in t.gates.values()
        if g.hash is not None and g.input is not None
    )  # fmt: skip
    seq = store.log_seq(uid)
    head = store.head_at(uid, seq) if seq else None
    return TicketFact(
        uid, t.key, t.type, t.title, t.status, tuple(t.fields.get("labels", ())),
        due if isinstance(due, str) else None, qs, needs, gates, state.workspace.roster_v,
        (seq, head) if seq and head else None,
    )  # fmt: skip


def _plan(items: list[Any]) -> str:
    from orch.hubclient.shares import plan_of

    return plan_of(items)


def _rows(items: list[Any], limit: int = 100) -> dict[str, Any]:
    rows = [{"logical": i.logical, "kind": i.kind, "line": i.line()} for i in items[:limit]]
    return {"count": len(items), "items": rows, "more": max(0, len(items) - limit), "plan": _plan(items)}


def _after(st: Any, facts: list[Any], *, add: Any = None, remove: Any = None) -> tuple[list[Any], list[str], list[Any]]:
    """The rules and withdrawn list after ``add`` / ``remove`` and the items they would share (withdrawn ones left
    out): what ``share.preview`` shows, what ``expect`` is checked against and what the sync sends."""
    from orch.hubclient import shares as S

    rules = [dict(r) for r in st.rules]
    if remove is not None:
        rules = [r for r in rules if r != remove]
    if add is not None and add not in rules:
        rules.append(add)
    withdrawn = list(st.withdrawn)
    if add is not None and add["kind"] == "ticket":  # naming a ticket again is a fresh, explicit choice
        withdrawn = [w for w in withdrawn if not any(w == f"ticket:{t.uid}" and add["ticket"] in (t.key, t.uid)
                                                      for t in facts)]  # fmt: skip
    return rules, withdrawn, [i for i in S.select(rules, facts) if i.logical not in withdrawn]


def _read(call: Call, op: str, args: dict[str, Any]) -> dict[str, Any]:
    from orch.hubclient import shares as S
    from orch.hubclient.errors import HubRefused

    from .workspace import ok_result

    st = _load(call.service)
    facts = tickets_for(call.service, call.viewer)
    try:
        if op == "share.rules":
            rules = [{**r, "count": len(S.select([r], facts)), "describes": S.describe(r)} for r in st.rules]
            return ok_result({"rules": rules, "shared": len(st.sent), "withdrawn": sorted(st.withdrawn),
                              "refused_items": st.refused_items()})  # fmt: skip
        rules, _w, want = _after(
            st, facts,
            add=S.normalize_rule(args["add"]) if "add" in args else None,
            remove=S.normalize_rule(args["remove"]) if "remove" in args else None,
        )  # fmt: skip
        had = set(st.sent)
        now = {i.logical for i in want}
        out = _rows(want)
        out["new"] = len(now - had)
        out["revoked"] = len(had - now)
        out["rules"] = [S.describe(r) for r in rules]
        return ok_result(out)
    except HubRefused:
        raise HostError("invalid.input", f"{op}: the rule is not valid") from None


# --- writes and the hub -----------------------------------------------------------------------------------------------


def _link(service: WorkspaceService) -> Any:
    from .spaceops import _Transient

    return _Transient(service)


def _make_builder(service: WorkspaceService) -> Any:
    """The contract 10.2b builder for the workspace's bound hub: its ``HXK`` (from the verified card), the ``WSK`` in
    custody, the dev marker of the binding and the ticket log's head. Refuses (``hub.not_joined``) without a hub."""
    from orch.crypto.encoding import unb64u
    from orch.hubclient import shares as S
    from orch.hubclient.errors import HubRefused

    from . import hubops, peerops

    state = peerops._hub_state(service, full=False)  # noqa: SLF001
    if state.mode != "selective":
        raise HubRefused("wrong_mode")  # shares exist only in selective mode (full mode: the hub holds the logs)
    if state.hxk_pub is None:
        raise HubRefused("malformed")
    _pub, sign = hubops._wsk_sign(service)  # noqa: SLF001
    return S.make_builder(
        hub_id=state.hub_id, workspace_id=service.workspace_id, hxk_pub=unb64u(state.hxk_pub, 65), sign=sign,
        dev=bool(state.binding["o"]["dev"]), now_ms=lambda: int(service.clock() * 1000),
    )  # fmt: skip


def _builder(service: WorkspaceService) -> Any:
    """:func:`_make_builder`, built on the first put: a sync that only revokes needs no key and no card."""
    held: list[Any] = []

    def build(item_id: str, item: Any, version: int) -> tuple[str, str]:
        if not held:
            held.append(_make_builder(service))
        sealed, ihash = held[0](item_id, item, version)
        return str(sealed), str(ihash)

    return build


def _sync_locked(service: WorkspaceService, st: Any, wanted: list[Any]) -> dict[str, Any]:
    """Send exactly ``wanted`` (the caller holds ``_MU`` and read ``st`` under it)."""
    from orch.hubclient import shares as S
    from orch.hubclient.errors import HubRefused

    closer = _link(service)
    try:
        try:
            counts = S.sync(st, wanted, link=closer, build=_builder(service), save=lambda s: _save(service, s))
        except HubRefused as exc:
            _save(service, st)
            return {"synced": False, "pending": exc.code}
        except OSError:
            _save(service, st)
            return {"synced": False, "pending": "hub_unreachable"}
        return {"synced": True, **counts}
    finally:
        closer.close()


def _approve(st: Any, selection: list[Any], expect: str) -> None:
    """The one place a selection is confirmed: against the plan the owner was shown (C6fud-1)."""
    from orch.hubclient.errors import HubRefused

    try:
        st.confirm(selection, expect)
    except HubRefused:
        raise HostError(
            "invalid.input", "share: what would be shared is not what you were shown; nothing changed"
        ) from None


def sync_now(service: WorkspaceService, viewer: str | None = None, expect: str | None = None) -> dict[str, Any]:
    """Evaluate and sync (the caller holds no lock). ``{"synced": True, put, revoke, kept}`` or
    ``{"synced": False, "pending": code}``: the rules stay saved and the next sync repeats only what is missing.
    ``refused`` lists items the contract or the hub refused one by one (skipped, the rest was sent).

    C6fu2-3: the owner's confirmation covers what they were shown. With ``expect`` (the ``share.preview`` plan) the
    current selection is checked against it and then confirmed; without it a sync only retries the confirmed set
    (:meth:`ShareState.auto_wanted`) and confirms nothing new."""
    from orch.hubclient import shares as S

    with _MU:
        st = _load(service)
        facts = tickets_for(service, viewer)
        if expect is None:
            wanted = st.auto_wanted(S.select(st.rules, facts))
        else:
            wanted = _after(st, facts)[2]
            _approve(st, wanted, expect)
        return _sync_locked(service, st, wanted)


def auto_sync(service: WorkspaceService) -> dict[str, Any] | None:
    """One automatic pass (:mod:`orch.host.shareauto`, no caller, no person): re-send the confirmed items whose ticket
    changed and withdraw those that stopped matching. It sends nothing the owner has not confirmed: an item that
    starts matching after the confirmation waits for the owner's next confirm (``share.rule_add`` / ``share.sync``;
    see :meth:`ShareState.auto_wanted`), so this pass can only narrow or refresh what the hub already has.

    Raises :class:`HostError` where sharing does not apply (full mode, no bound hub: nothing is sent, ever);
    returns ``None`` where it is not the time (closing, degraded, no store); otherwise the sync answer."""
    from orch.hubclient import shares as S

    if service.closing or service.store is None or service.state != "ok":
        return None
    with _MU:
        _selective(service)  # inside the lock, from the files as they are now: full mode and no hub never send
        st = _load(service)
        service.store.refresh()
        wanted = st.auto_wanted(S.select(st.rules, tickets_for(service)))
        before = {lg: v.get("digest") for lg, v in st.sent.items()}
        got = _sync_locked(service, st, wanted)
        _audit_auto(service, before, st)
        return got


def _audit_auto(service: WorkspaceService, before: dict[str, Any], st: Any) -> None:
    """C6fu2-2: one host audit entry (the channel of the manual share ops) per pass that changed the hub's copy:
    logical ids only, never content. ``unknown``: a put whose reply never came (the hub may or may not hold it)."""
    from . import securitylog

    after = {lg: v.get("digest") for lg, v in st.sent.items()}
    revoked = sorted(set(before) - set(after))
    changed = {lg for lg, d in after.items() if before.get(lg) != d}
    put = sorted(lg for lg in changed if after[lg] is not None)
    unknown = sorted(lg for lg in changed if after[lg] is None)  # a put sent without a reply: the hub may hold it
    if revoked or put or unknown:
        securitylog.record(
            service.state_dir, service.host_uid, service.clock(), "share.auto",
            workspace_id=service.workspace_id, principal="host:auto", put=put, revoked=revoked, unknown=unknown,
        )  # fmt: skip


def _find(st: Any, ref: str, facts: list[Any]) -> str:
    """The ``logical`` id a ``revoke`` ref names: a logical id itself or a ticket key (its ticket item)."""
    known = set(st.sent) | set(st.withdrawn)  # a second revoke of a withdrawn item is the same answer, not an error
    if ref in known:
        return ref
    for t in facts:
        if ref in (t.key, t.uid) and f"ticket:{t.uid}" in known:
            return f"ticket:{t.uid}"
    raise HostError("not_found", "nothing is shared under that name: orch hub share rule list")


def _run(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from orch.hubclient import shares as S
    from orch.hubclient.errors import HubRefused

    from . import hubops
    from .workspace import ok_result

    op, args, service = plan["op"], plan["args"], call.service
    try:
        if op == "share.sync":
            got = sync_now(service, call.viewer, args.get("expect"))
            hubops._audit(call, "share.sync", synced=got["synced"], confirmed="expect" in args)  # noqa: SLF001
            return ok_result(got)
        with _MU:
            st = _load(service)
            facts = tickets_for(service, call.viewer)
            if op == "share.revoke":
                lg = _find(st, args["ref"], facts)
                if lg not in st.withdrawn:
                    st.withdrawn.append(lg)
                _save(service, st)
                note = "withdrawn; what the hub already read cannot be taken back"
            else:
                rule = S.normalize_rule(args["rule"])
                after, withdrawn, shown = _after(st, facts, add=rule if op == "share.rule_add" else None,
                                                 remove=rule if op == "share.rule_remove" else None)  # fmt: skip
                changed = after != st.rules
                if _plan(shown) != args["expect"]:
                    raise HostError(
                        "invalid.input", "share: what would be shared is not what you were shown; nothing changed"
                    )
                if changed:
                    st.rules, st.withdrawn = after, withdrawn
                    _save(service, st)
                note = "changed" if changed else "unchanged"
            # C6-2: the sync sends exactly the selection that was confirmed above, under the same lock (never a
            # second selection that a ticket change in between could have altered). Only a rule change confirms
            # (against the shown plan); a revoke narrows and sends nothing the owner has not confirmed (C6fud-1).
            selected = S.select(st.rules, facts)
            if op == "share.revoke":
                selected = st.auto_wanted(selected)
            else:
                _approve(st, [i for i in selected if i.logical not in st.withdrawn], args["expect"])
            got = _sync_locked(service, st, selected)
        more = {"person": call.viewer} if op in REDUCE_OPS else {}
        hubops._audit(call, op, kind=args["rule"]["kind"] if "rule" in args else "item", **more)  # noqa: SLF001
        return ok_result({"note": note} | got)
    except HubRefused as exc:
        raise hubops._refused(exc.code) from None  # noqa: SLF001
