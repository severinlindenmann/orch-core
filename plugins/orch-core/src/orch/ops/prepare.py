"""Running a person's operation without signing it: the capture that ``human.prepare`` uses (H5a, §0.8.1).

The host never prompts and holds no device key (D-P2-15). To prepare a person event it runs the operation's own
handler with :attr:`orch.ops.base.Context.prepare` set to a :class:`Capture`:

* the handler resolves, checks and builds exactly as in direct mode, as the mapped person;
* each event it would sign (:class:`orch.ops.human.Human`) or append (:class:`orch.ops.runtime.Projection`) is judged
  with the model as the store will judge it (without the signature, which does not exist yet) and then **recorded**
  here, never appended;
* the handler's own result is kept, so ``human.submit`` can answer with it once the signed events landed.

A :class:`Capture` names the person and, once a signer is bound, the device and ``auth``. Before binding (a hand-off
request) the events carry ``actor.device: null`` and ``auth: null``; the model still judges them under one of the
person's valid devices (:attr:`Capture.probe_device`), so a request is refused at prepare when the log would refuse it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["Capture", "Captured", "accepting", "judge"]


@dataclass
class Captured:
    """One event a handler would have signed or appended: its log, its payload (``type`` and the payload keys, without
    envelope), and the section texts and artifact bytes that go with it. A decision's D58 source check is not carried:
    ``human.submit`` re-reads git itself for every signed ``source_sha`` (:func:`orch.host.human.recheck_sources`)."""

    log: str
    event: dict[str, Any]
    body: dict[str, str | None] | None = None
    artifacts: dict[str, bytes] | None = None


@dataclass
class Capture:
    person: str
    #: the bound device, or ``None`` for a hand-off request that is not bound yet
    device: str | None
    #: the bound signer's ``auth``, or ``None`` before binding
    auth: str | None
    #: a valid device of ``person`` the model judges the unsigned events under
    probe_device: str
    events: list[Captured] = field(default_factory=list)
    #: ``before`` of a ``payload`` material: prior values of the paths a set, section.set or settings.set changes
    before: dict[str, Any] = field(default_factory=dict)
    #: ``checks`` of the prepared request (N3): one ``{repo, clean, attested_by, at}`` per working copy the clean-tree
    #: check (``tree.check``) found clean; not signed, not in ``material``
    checks: list[dict[str, Any]] = field(default_factory=list)
    #: a ``dry_run`` prepare: nothing is stored and no ``tree.check`` job runs (section 0.8.1)
    dry_run: bool = False
    #: H11d (D-P2-32): the working copy each linked repo was checked in, ``{repo, ticket, gen, registered, pin}``;
    #: binding and ``precommit`` require the same registration (generation and pin), never a fallback
    copies: list[dict[str, Any]] = field(default_factory=list)
    #: H12d-2 (§0.8.6 item 9.4): the readers the request admits, verified at prepare (``member.add --reader-decl``):
    #: ``{device, person, decl, source, reader, level, digest}``; not signed, not in ``material`` (an argument)
    readers: list[dict[str, Any]] = field(default_factory=list)
    #: H12c-e: ``relay.pair_device``'s facts from R6 (:class:`orch.identity.pairdevice.PairDeviceFacts`), set by the
    #: host before the handler runs; the material of the request is built from them (kind ``relay.pair_device``)
    pair: Any = None
    #: P5: ``publish.share``'s facts from the host (:class:`orch.publishclient.approval.ApprovalFacts`), built from the
    #: staged copy before the handler runs; the words that are signed come from them, never from the request
    publish: Any = None
    #: V3 C2a: ``hub.join``'s facts from the host (:class:`orch.host.hubops.HubFacts`: the host's own clock and whether
    #: this workspace is a dev workspace), never from the request: the hub card is checked against them
    hub: Any = None
    #: batch 11 (finding 9): the checked ``sequence`` meta of ``human.prepare`` (one step of a signer's sequence)
    sequence: dict[str, Any] | None = None
    #: V3 G3: ``addon.grant {staged}``: the stage the grant is for, ``{stage, name}`` (kept on the request record, not
    #: signed), and the marketplace block of the review (``material["market"]``: per host, not signed)
    staged: dict[str, Any] | None = None
    market: dict[str, Any] | None = None
    #: V3 G3: the host's marketplace records of this workspace (``orch.host.marketstate.MarketFiles``), set by the host
    #: before the handler runs: a handler has no state directory of its own under the host
    market_files: Any = None

    statement: str | None = None

    @property
    def actor(self) -> dict[str, Any]:
        """The actor the model judges the unsigned events with."""
        return {"kind": "person", "id": self.person, "device": self.probe_device}

    @property
    def probe_auth(self) -> str:
        return self.auth or "passphrase"

    def add(self, captured: Captured) -> None:
        if len(self.events) >= 16:
            from orch.ops.errors import OrchError

            raise OrchError("invalid.input", "a person request holds at most 16 events")
        self.events.append(captured)


def accepting(state: Any) -> Any:
    """``state`` judging every signature as valid: the unsigned events of a prepare (the signatures do not exist yet;
    ``Store.append`` and ``human.submit`` verify them for real)."""
    import dataclasses

    from orch.ops.human import _AcceptAll

    return dataclasses.replace(state, _ctx=dataclasses.replace(state._ctx, verifier=_AcceptAll(), admit=True))


def judge(store: Any, log: str, payload: dict[str, Any], capture: Capture) -> None:
    """Judge one unsigned person event (``payload``: its type and payload keys) as ``Store.append`` will, signature
    aside; raises the store's refusal."""
    from orch.identity import new_ulid
    from orch.model import Refusal, preview
    from orch.store import StoreError

    store.refresh()
    stamp = store.peek_stamp(log)
    state = accepting(store.state)
    probe = {
        "v": 2,
        "id": new_ulid(),
        "actor": capture.actor,
        "auth": capture.probe_auth,
        "hash_v": 1,
        "roster_v": state.workspace.roster_v,
        "based_on": store.log_head(log),
        **payload,
        "sig": "x",
        **stamp,
    }
    r = preview(state, probe, log=log)
    if isinstance(r, Refusal):
        raise StoreError.from_refusal(r)
