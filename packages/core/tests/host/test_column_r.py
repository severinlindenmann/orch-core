"""H12a: column R (the relay peer, orch-v2-host §11A.4), the ``append_signed`` choke point, the reader reservation
(``_relay_paired``, §11A.6), the relay-linked workspace cap and ``orch host relay stop`` (§11A.4, §11A.7).

Every check here fails closed: an unknown or unresolvable relay peer resolves to nothing, every op outside column R's
closed list is refused, a relay peer never becomes a person principal, and only device-signed events of the
requesting device pass ``append_signed`` on the relay path.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import os
from pathlib import Path
from typing import Any

import pytest

from orch.host import columns
from orch.host.auth import Verified
from orch.host.columns import Column, refusal, resolve, resolve_relay
from orch.host.errors import HostError
from orch.host.peer import Peer
from orch.host.relayid import RelayIdentity

UID = os.getuid()
WS = "a" * 32
DEV = "d_" + "1" * 32
PERSON = "p_" + "2" * 32
DIGEST = "sha256:" + "3" * 64
LIST = "sha256:" + "4" * 64
SRC = Path(__file__).resolve().parents[2] / "src" / "orch" / "host"


def ident(**kw: Any) -> RelayIdentity:
    base = {
        "workspace_id": WS,
        "device_id": DEV,
        "person_id": PERSON,
        "cert_digest": DIGEST,
        "epoch": 3,
        "list_hash": LIST,
    }
    return RelayIdentity(**{**base, **kw})


# --- RelayIdentity and the relay Peer ---------------------------------------------------------------------------------


def test_relay_identity_is_frozen_and_its_principal_is_r_device():
    i = ident()
    assert i.principal == f"r:{DEV}"
    with pytest.raises(dataclasses.FrozenInstanceError):
        i.device_id = "d_" + "9" * 32  # type: ignore[misc]
    assert i.rd is None  # H12d hook: the reader declaration digest, unused in v1.7


@pytest.mark.parametrize(
    "bad",
    [
        {"workspace_id": "x"},
        {"device_id": "p_" + "1" * 32},
        {"device_id": "d_" + "1" * 31},
        {"device_id": "d_" + "1" * 32 + "\n"},
        {"person_id": "uid:0"},
        {"person_id": ""},
        {"cert_digest": "sha1:00"},
        {"list_hash": None},
        {"epoch": -1},
        {"epoch": True},
        {"epoch": "3"},
        {"rd": "nope"},
    ],
)
def test_a_malformed_relay_identity_is_refused(bad):
    with pytest.raises(ValueError):
        ident(**bad)


def test_the_relay_peer_has_uid_minus_one_and_the_devices_person():
    p = ident().peer()
    assert p == Peer(-1, "relay", PERSON)
    with pytest.raises(ValueError):
        Peer(0, "relay", PERSON)  # never a kernel UID
    with pytest.raises(ValueError):
        Peer(-1, "relay", None)  # never without its person
    with pytest.raises(ValueError):
        Peer(-1, "person", PERSON)  # -1 is the relay's alone


# --- resolve: fail closed ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("peer", ["relay", "runner", "addon", "", None])
@pytest.mark.parametrize("grant", ["absent", "verified"])
def test_resolve_refuses_the_relay_and_every_peer_class_it_does_not_list(peer, grant):
    with pytest.raises(ValueError):
        resolve(peer, grant, True)  # type: ignore[arg-type]


def test_resolve_relay_ok_is_column_r_with_the_person_viewer():
    col = resolve_relay(True)
    assert (col.name, col.viewer) == ("R", "person")


def test_resolve_relay_not_ok_refuses_every_op():
    col = resolve_relay(False)
    assert col.name == "N"
    kinds = [
        "read", "person_read", "pg_read", "person_write", "pg_write", "agent", "unattended", "human", "human_api",
        "job", "control", "device_signed",
    ]  # fmt: skip
    for kind in kinds:
        assert refusal(col, kind, "ticket.get") == "op.not_allowed", kind
        assert refusal(col, kind) == "op.not_allowed", kind


@pytest.mark.parametrize("ok", [True, False, 1, None, "yes"])
def test_resolve_relay_takes_only_a_real_bool(ok):
    if ok is True or ok is False:
        resolve_relay(ok)
    else:
        with pytest.raises(ValueError):
            resolve_relay(ok)  # type: ignore[arg-type]


def test_matrix_r_is_read_and_device_signed_and_no_other_column_has_device_signed():
    assert columns._MATRIX["R"] == {"read", "device_signed"}
    for name, kinds in columns._MATRIX.items():
        if name != "R":
            assert "device_signed" not in kinds, name


def test_column_r_serves_only_its_closed_op_list():
    r = resolve_relay(True)
    for op in sorted(columns.RELAY_OPS - columns.DEVICE_SIGNED_OPS):
        assert refusal(r, "read", op) is None, op
    assert refusal(r, "device_signed", "relay.submit") is None
    assert refusal(r, "device_signed", "reader.declare") is None  # H12d-3
    assert refusal(r, "person_write", "reader.declare") == "op.not_allowed"  # never as the socket's kind
    # the kind alone never serves R: an op must be named and listed
    assert refusal(r, "read") == "op.not_allowed"
    assert refusal(r, "read", None) == "op.not_allowed"
    refused = [
        "doctor", "instructions.sync", "instructions.hook", "wait", "sub.open", "sub.close", "blob.put", "blob.drop",
        "host.status", "session.list", "grant.list", "records.snapshot", "run.list", "run.sub", "run.stop",
        "term.open", "term.attach", "worktree.list", "worktree.create", "human.prepare", "human.get", "human.submit",
        "human.pending", "human.cancel", "grant.issue", "ask", "log", "artifact.add", "new", "move", "decision",
        "job.next", "host.list", "agent.start",
    ]  # fmt: skip
    for op in refused:
        assert op not in columns.RELAY_OPS, op
        for kind in ("read", "device_signed", "unattended", "agent", "human_api"):
            assert refusal(r, kind, op) == "op.not_allowed", (op, kind)
    # a listed op under a kind outside R's matrix is refused too (a write never passes as a read)
    assert refusal(r, "unattended", "show") == "op.not_allowed"
    assert refusal(r, "device_signed", "show") == "op.not_allowed"
    assert refusal(r, "read", "relay.submit") == "op.not_allowed"
    assert refusal(r, "human", "show") == "op.not_allowed"


def test_relay_ops_are_the_spec_table_and_track_the_registry_reads():
    """§11A.4: registry read ops except doctor and instructions.*, the listed host reads, whoami, outcome.get and
    relay.submit. ``wait`` is left out (it parks on a socket session's bus; a judgement call). A new registry read op
    fails this test until it is deliberately listed (or refused) for R."""
    import orch.ops as ops

    reads = {o.name for o in ops.all() if o.who == "read"}
    reads -= {"doctor", "instructions.sync", "instructions.hook", "wait"}
    # widgets.check reads a file path and echoes content: over the relay it would be a remote file-read probe; both
    # widgets ops are refused to R on purpose (agents' local helpers)
    reads -= {"widgets.check", "widgets.types"}
    assert not {"widgets.check", "widgets.types"} & columns.RELAY_OPS
    reads -= {"addon.preview"}  # refused for R on purpose: turning an add-on on is a sequence, which no phone signs
    host = {
        "ticket.query", "ticket.get", "needs", "activity.list", "diff", "artifact.get", "workspace.show",
        "member.list", "ticket.text", "artifact.query", "commit.list", "workspace.settings", "quick.list",
        "reader.list",  # H12d-2 (§0.8.6 item 9.11): P and R
        "phone.requests", "gate.input",  # B10 Part B: the viewer's own phone requests; the gate-hash input
    }  # fmt: skip
    own = {"whoami", "outcome.get", "relay.submit", "reader.declare"}  # reader.declare: H12d-3, the device's own
    assert columns.RELAY_OPS == frozenset(reads | host | own)
    assert columns.DEVICE_SIGNED_OPS == frozenset({"relay.submit", "reader.declare"})


def test_no_socket_op_has_kind_device_signed_and_relay_submit_is_not_served_on_the_socket():
    from orch.host.dispatch import op_kind, served_ops

    served = served_ops()
    assert "relay.submit" not in served
    for op in served:
        kind = op_kind(op)
        assert kind is None or kind[0] != "device_signed", op


# --- Call.authorize, principal(), principal_of() --------------------------------------------------------------------


class _NoConvenienceStore:
    """H12d-3: what ``check_rd`` asks of the store (no convenience history: rd is not required)."""

    state = object()
    state_dir = None

    def merged_events(self) -> list:
        return []

    @contextlib.contextmanager
    def locked(self):
        yield

    def revision(self) -> tuple:
        return ((), ())


class WsService:
    def __init__(self, workspace_id: str = WS) -> None:
        self.workspace_id = workspace_id
        self.state_dir = Path("/nonexistent-h12d3-state")
        self.host_uid = UID
        self.store = _NoConvenienceStore()


def relay_call(
    identity: RelayIdentity | None,
    op: str,
    kind: str | None,
    cols: Any,
    peer: Peer | None = None,
    service: Any = None,
) -> Any:
    from orch.host.workspace import Call

    peer = peer if peer is not None else (identity.peer() if identity is not None else Peer(UID, "person", PERSON))
    service = service if service is not None else WsService()
    call = Call(service, None, peer, {"op": op, "args": {}}, Column("N", "no-person"), None)  # type: ignore[arg-type]
    call.relay = identity
    call.columns = cols
    call.kind = kind  # type: ignore[assignment]
    return call


def r_cols() -> tuple[Column, str | None, Verified]:
    return resolve_relay(True), PERSON, Verified("absent")


def test_authorize_with_relay_keeps_principal_r_device_and_never_a_uid_principal():
    call = relay_call(ident(), "ticket.get", "read", r_cols)
    call.authorize()
    assert call.column.name == "R" and call.viewer == PERSON
    assert call.principal == f"r:{DEV}"
    assert call.verified == Verified("absent")


def test_authorize_with_relay_refuses_an_op_outside_r():
    for op, kind in [("sub.open", "read"), ("ask", "unattended"), ("human.prepare", "human_api"), ("new", "agent")]:
        call = relay_call(ident(), op, kind, r_cols)
        with pytest.raises(HostError) as exc:
            call.authorize()
        assert exc.value.code == "op.not_allowed", op


def test_authorize_with_relay_refuses_when_the_identity_no_longer_holds():
    call = relay_call(ident(), "ticket.get", "read", lambda: (resolve_relay(False), None, Verified("absent")))
    with pytest.raises(HostError) as exc:
        call.authorize()
    assert exc.value.code == "op.not_allowed"


def test_authorize_with_relay_fails_closed_on_every_inconsistency():
    # no recompute function: a relay call is never served from the column it was built with
    with pytest.raises(HostError):
        relay_call(ident(), "ticket.get", "read", None).authorize()
    # a socket peer carrying a relay identity, and a relay peer without one
    with pytest.raises(HostError):
        relay_call(ident(), "ticket.get", "read", r_cols, peer=Peer(UID, "person", PERSON)).authorize()
    with pytest.raises(HostError):
        relay_call(None, "ticket.get", "read", r_cols, peer=Peer(-1, "relay", PERSON)).authorize()
    # the recompute answering a socket column (P, G, …) or another viewer than the device's person
    for bad in [
        lambda: (Column("P", "person"), PERSON, Verified("absent")),
        lambda: (Column("G", "grant"), PERSON, Verified("absent")),
        lambda: (resolve_relay(True), "p_" + "9" * 32, Verified("absent")),
        lambda: (resolve_relay(True), None, Verified("absent")),
    ]:
        with pytest.raises(HostError) as exc:
            relay_call(ident(), "ticket.get", "read", bad).authorize()
        assert exc.value.code == "op.not_allowed"
    # an identity whose peer person differs from the identity's person
    with pytest.raises(HostError):
        relay_call(ident(), "ticket.get", "read", r_cols, peer=Peer(-1, "relay", "p_" + "9" * 32)).authorize()


def test_a_socket_call_never_reaches_column_r():
    from orch.host.workspace import Call

    call = Call(None, None, Peer(UID, "person", PERSON), {"op": "ticket.get"}, Column("N", "no-person"), None)  # type: ignore[arg-type]
    call.columns = r_cols
    call.kind = "read"
    with pytest.raises(HostError) as exc:
        call.authorize()  # a socket call whose recompute answers R is refused, never served as R
    assert exc.value.code == "op.not_allowed"


def test_principal_refuses_a_relay_peer_and_principal_uid_of_r_is_none():
    from orch.host.workspace import principal, principal_uid

    with pytest.raises(ValueError):
        principal(-1, Verified("absent"))
    assert principal(UID, Verified("absent")) == f"uid:{UID}"
    assert principal_uid(f"r:{DEV}") is None
    assert principal_uid("uid:-1") is None


def test_principal_of_refuses_column_r_and_any_unknown_column():
    from orch.host.outcomes import principal_of

    for column in ("R", "X", ""):
        with pytest.raises(ValueError):
            principal_of(UID, column)
    with pytest.raises(ValueError):
        principal_of(-1, "P")
    assert principal_of(UID, "P") == f"uid:{UID}"


# --- review round 1 (Opus M1, L1, L2) ------------------------------------------------------------------------------


def test_authorize_refuses_an_identity_of_another_workspace():
    """Opus M1 PoC: an identity for workspace B on workspace A's service is never served as R in A."""
    call = relay_call(ident(), "ticket.get", "read", r_cols, service=WsService("b" * 32))
    with pytest.raises(HostError) as exc:
        call.authorize()
    assert exc.value.code == "op.not_allowed"
    with pytest.raises(HostError):
        relay_call(ident(), "ticket.get", "read", r_cols, service=object()).authorize()  # no workspace id


def test_a_prebuilt_column_r_without_identity_or_recompute_is_refused():
    """Opus L1 PoC: column R without a relay identity is refused before the no-recompute early return."""
    from orch.host.workspace import Call

    call = Call(WsService(), None, Peer(UID, "person", PERSON), {"op": "ticket.get"}, resolve_relay(True), PERSON)  # type: ignore[arg-type]
    with pytest.raises(HostError) as exc:
        call.authorize()
    assert exc.value.code == "op.not_allowed"


def test_the_relay_op_kind_is_derived_never_taken_from_the_caller(monkeypatch):
    """Opus L2: the kind comes from ``op_kind`` / ``DEVICE_SIGNED_OPS``; a caller's kind that differs is refused."""
    import orch.host.dispatch as dispatch

    call = relay_call(ident(), "ticket.get", None, r_cols)  # no kind given: derived
    call.authorize()
    assert call.kind == "read"
    with pytest.raises(HostError):
        relay_call(ident(), "relay.submit", "read", r_cols).authorize()  # relay.submit is device_signed
    call = relay_call(ident(), "relay.submit", None, r_cols)
    call.authorize()
    assert call.kind == "device_signed"
    with pytest.raises(HostError):
        relay_call(ident(), "show", "device_signed", r_cols).authorize()
    real = dispatch.op_kind
    monkeypatch.setattr(dispatch, "op_kind", lambda op: ("unattended", "write") if op == "show" else real(op))
    with pytest.raises(HostError):  # a listed op whose real kind is a write: refused although the caller says read
        relay_call(ident(), "show", "read", r_cols).authorize()
    with pytest.raises(HostError):
        relay_call(ident(), "show", None, r_cols).authorize()
    with pytest.raises(HostError):  # an op nobody serves has no kind: refused
        relay_call(ident(), "artifact.query", None, r_cols).authorize()


# --- outcomes: relay.submit, NO_PARTIAL and the relay_fields hook ---------------------------------------------------


def test_relay_submit_is_a_log_write_that_needs_an_idem_and_takes_no_dry_run():
    from orch.host.outcomes import NO_PARTIAL, classify, dry_run_supported, request_idem

    assert "relay.submit" in NO_PARTIAL and "human.submit" in NO_PARTIAL
    assert classify("relay.submit") == "log"
    assert dry_run_supported("relay.submit") is False
    with pytest.raises(HostError) as exc:
        request_idem("relay.submit", {})
    assert exc.value.code == "idem.required"
    with pytest.raises(HostError):
        request_idem("relay.submit", {"dry_run": True})


class FakeLog:
    def __init__(self) -> None:
        self.events: dict[tuple[str, str], dict[str, Any]] = {}

    @contextlib.contextmanager
    def locked(self):
        yield

    def find(self, log: str, event_id: str) -> dict[str, Any] | None:
        return self.events.get((log, event_id))

    def rebuild(self, record: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        from orch.host.workspace import ok_result

        return ok_result({}, seq=events[-1]["seq"])


@pytest.fixture
def outcomes(tmp_path):
    from orch.host.outcomes import OutcomeStore

    (tmp_path / "state").mkdir(mode=0o700)
    log = FakeLog()
    store = OutcomeStore(tmp_path / "state", WS, UID, clock=lambda: 1_800_000_000.0, reconciler=log)
    store.open()
    return store, log


IDEM = "a_01J9ZP0000000000000000000A"


def _pending_relay_submit(store: Any) -> Any:
    from orch.host.outcomes import Planned

    intent = store.begin(
        f"r:{DEV}", IDEM, "relay.submit", {"n": 2}, planned=[Planned("L", "e1", "x"), Planned("L", "e2", "x")]
    )
    store.release(intent)
    return intent


def test_a_landed_relay_submit_is_never_partial_and_without_the_hook_stays_pending(outcomes):
    store, log = outcomes
    human_calls: list[Any] = []
    store.roll_forward = lambda record: human_calls.append(record) or "keep"
    _pending_relay_submit(store)
    log.events[("L", "e1")] = {"id": "e1", "seq": 4, "type": "x"}
    assert store.reconcile_all() == [("relay.submit", "pending")]
    assert human_calls == []  # the human roll-forward never handles a relay record


def test_a_landed_relay_submit_rolls_forward_through_relay_fields(outcomes):
    store, log = outcomes
    seen: list[str] = []
    store.roll_forward = lambda record: pytest.fail("human roll-forward called for relay.submit")

    def relay_fields(record: dict[str, Any]) -> dict[str, Any] | str:
        seen.append(record["op"])
        return {"step": "failed", "error": HostError("invalid.input", "x (landed: seq 4)").error(), "exit": 5}

    store.relay_fields = relay_fields
    _pending_relay_submit(store)
    log.events[("L", "e1")] = {"id": "e1", "seq": 4, "type": "x"}
    assert store.reconcile_all() == [("relay.submit", "failed")]
    assert seen == ["relay.submit"]
    assert store.lookup(f"r:{DEV}", IDEM)["state"] == "failed"


def test_the_human_hook_still_rolls_human_submit_forward(outcomes):
    from orch.host.outcomes import Planned

    store, log = outcomes
    store.relay_fields = lambda record: pytest.fail("relay hook called for human.submit")
    store.roll_forward = lambda record: "keep"
    intent = store.begin(
        f"uid:{UID}", IDEM, "human.submit", {}, planned=[Planned("L", "e1", "x"), Planned("L", "e2", "x")]
    )
    store.release(intent)
    log.events[("L", "e1")] = {"id": "e1", "seq": 4, "type": "x"}
    assert store.reconcile_all() == [("human.submit", "pending")]


# --- append_signed: the single choke point ----------------------------------------------------------------------------


class FakeStore:
    def __init__(self, refuse_at: int | None = None) -> None:
        self.appended: list[dict[str, Any]] = []
        self.append_check: Any = None
        self.refuse_at = refuse_at
        self.lock_depth = 0

    @contextlib.contextmanager
    def locked(self):
        self.lock_depth += 1
        try:
            yield
        finally:
            self.lock_depth -= 1

    def refresh(self) -> None:
        """H12d-2: ``append_signed`` reads the readers' state before a convenience or reader event."""

    @property
    def state(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            workspace=SimpleNamespace(devices={}, members={}, settings={}, hub={}), device_evidence=lambda d: None
        )

    def logged_event(self, log: str, event_id: str) -> dict[str, Any] | None:
        for e in self.appended:
            if e["id"] == event_id:
                return e
        return None

    def append(self, event: dict[str, Any], *, log: str, body: Any = None, artifacts: Any = None) -> Any:
        from orch.store import StoreError

        assert self.lock_depth > 0
        if self.append_check is not None:
            self.append_check(log, event)
        if self.refuse_at is not None and len(self.appended) == self.refuse_at:
            raise StoreError("validation.signature", "bad signature")
        done = {**event, "seq": len(self.appended) + 10}
        self.appended.append(done)
        return type("Appended", (), {"event": done})()


class FakeService:
    def __init__(self, store: Any) -> None:
        self.store = store


def signed(n: int, *, device: str = DEV, person: str = PERSON, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"e{n}",
        "type": "question.answered",
        "actor": {"kind": "person", "id": person, "device": device},
        "auth": "secure-enclave",
        "sig": "s" * 86,
        **extra,
    }


def test_append_signed_appends_in_order_and_reports_landed_seqs():
    from orch.host.human import append_signed

    store = FakeStore()
    events = [signed(1), signed(2)]
    landed, error = append_signed(FakeService(store), "L", events, [{}, {}], signed_by=None)
    assert error is None and landed == [10, 11]
    landed, error = append_signed(FakeService(store), "L", events, [{}, {}], signed_by=None)  # already logged
    assert error is None and landed == [10, 11] and len(store.appended) == 2


def test_append_signed_stops_at_the_first_refusal_and_names_what_landed():
    from orch.host.human import append_signed

    store = FakeStore(refuse_at=1)
    landed, error = append_signed(
        FakeService(store), "L", [signed(1), signed(2), signed(3)], [{}, {}, {}], signed_by=None
    )
    assert landed == [10] and error is not None and error.code == "validation.signature"
    assert store.append_check is None  # restored


@pytest.mark.parametrize(
    "bad",
    [
        {"actor": {"kind": "agent", "id": "x", "session": "s", "unattended": True}},
        {"actor": {"kind": "person", "id": PERSON, "device": "d_" + "9" * 32}},  # another device
        {"actor": {"kind": "person", "id": "p_" + "9" * 32, "device": DEV}},  # another person
        {"actor": {"kind": "host"}},
        {"auth": "convenience"},
        {"sig": None},
        {"sig": ""},
    ],
)
def test_append_signed_on_the_relay_path_takes_only_events_signed_by_the_requesting_device(bad):
    from orch.host.human import append_signed

    store = FakeStore()
    events = [signed(1), {**signed(2), **bad}]
    landed, error = append_signed(FakeService(store), "L", events, [{}, {}], signed_by=(PERSON, DEV))
    assert landed == [] and error is not None and error.code == "op.not_allowed"
    assert store.appended == []  # checked before anything lands


def test_append_signed_refuses_an_event_missing_its_signature_key_on_the_relay_path():
    from orch.host.human import append_signed

    store = FakeStore()
    ev = signed(1)
    del ev["sig"]
    landed, error = append_signed(FakeService(store), "L", [ev], [{}], signed_by=(PERSON, DEV))
    assert landed == [] and error is not None and store.appended == []


def test_signed_by_is_keyword_required_and_append_relay_always_checks_the_device():
    """Opus L3: the relay check is not opt-in."""
    from orch.host.human import append_relay, append_signed

    store = FakeStore()
    with pytest.raises(TypeError):
        append_signed(FakeService(store), "L", [signed(1)], [{}])  # type: ignore[call-arg]
    landed, error = append_relay(
        FakeService(store), "L", [signed(1, device="d_" + "9" * 32)], [{}], person_id=PERSON, device_id=DEV
    )
    assert landed == [] and error is not None and error.code == "op.not_allowed" and store.appended == []
    landed, error = append_relay(FakeService(store), "L", [signed(1)], [{}], person_id=PERSON, device_id=DEV)
    assert error is None and landed == [10]


def test_relay_code_never_calls_append_signed_directly():
    """Opus L3: ``orch/relay`` appends only through ``append_relay`` (vacuous until R2/R5 land)."""
    relay = SRC.parent / "relay"
    for path in sorted(relay.rglob("*.py")) if relay.is_dir() else []:
        tree = ast.parse(path.read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "append_signed" not in names, path


@pytest.mark.parametrize(
    "change",
    [
        {"sig": "t" * 86},
        {"actor": {"kind": "person", "id": PERSON, "device": "d_" + "9" * 32}},
        {"actor": {"kind": "person", "id": "p_" + "9" * 32, "device": DEV}},
    ],
)
def test_an_already_logged_id_with_another_signer_is_a_refusal_on_the_relay_path(change):
    """Opus L4: on the relay path the "already logged" shortcut compares ``sig`` and ``actor``."""
    from orch.host.human import append_relay

    store = FakeStore()
    store.appended.append({**signed(1), **change, "seq": 7})  # someone else's event under the same id
    landed, error = append_relay(
        FakeService(store), "L", [signed(1), signed(2)], [{}, {}], person_id=PERSON, device_id=DEV
    )
    assert error is not None and error.code == "op.not_allowed"
    assert landed == [] and len(store.appended) == 1  # never committed, nothing appended after it


def test_an_already_logged_identical_event_counts_as_landed_on_the_relay_path():
    from orch.host.human import append_relay

    store = FakeStore()
    store.appended.append({**signed(1), "seq": 7})
    landed, error = append_relay(FakeService(store), "L", [signed(1)], [{}], person_id=PERSON, device_id=DEV)
    assert error is None and landed == [7]


def test_append_signed_is_the_only_append_of_signed_events_in_human_and_finish_uses_it():
    """The choke point: in ``host/human.py`` only ``append_signed`` calls ``store.append``, and ``finish`` calls it."""
    tree = ast.parse((SRC / "human.py").read_text())
    appends: list[str] = []
    callers: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "append" and ast.unparse(node.func.value) == "store":
                    appends.append(fn.name)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "append_signed":
                callers.append(fn.name)
    assert appends == ["append_signed"]
    assert "finish" in callers


# --- the reader reservation: _relay_paired (§11A.6) -------------------------------------------------------------------


def _relay_dir(state: Path, ws: str = WS) -> Path:
    d = state / "hosts" / ws / "relay"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def test_relay_paired_reads_link_json_unlinked_files_and_membership(tmp_path):
    from orch.host.convenience import _relay_paired

    state = tmp_path / "state"
    state.mkdir()
    assert _relay_paired(state, WS) is False
    d = _relay_dir(state)
    assert _relay_paired(state, WS) is False  # an empty relay directory is no link
    (d / "link.json").write_text("{}")
    assert _relay_paired(state, WS) is True
    (d / "link.json").rename(d / "unlinked-1800000000000.json")
    assert _relay_paired(state, WS) is True  # the reservation is permanent
    (d / "unlinked-1800000000000.json").unlink()
    assert _relay_paired(state, WS) is False
    (state / "hosts" / WS / "membership").mkdir()
    assert _relay_paired(state, WS) is True


def test_relay_paired_counts_a_dangling_link_and_an_unreadable_directory(tmp_path):
    from orch.host.convenience import _relay_paired

    state = tmp_path / "state"
    d = _relay_dir(state)
    (d / "link.json").symlink_to(tmp_path / "nowhere")
    assert _relay_paired(state, WS) is True
    (d / "link.json").unlink()
    os.chmod(d, 0)
    try:
        assert _relay_paired(state, WS) is True  # cannot tell: fail closed
    finally:
        os.chmod(d, 0o700)


# --- the relay-linked workspace cap and the link reservation -------------------------------------------------------


class ConvStore:
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = events or []
        self.lock_depth = 0

    @contextlib.contextmanager
    def locked(self):
        self.lock_depth += 1
        try:
            yield
        finally:
            self.lock_depth -= 1

    def merged_events(self) -> list[tuple[str, dict[str, Any]]]:
        return [("workspace", e) for e in self.events]


class LinkService:
    def __init__(self, state: Path, ws: str, store: Any) -> None:
        self.state_dir, self.workspace_id, self.host_uid, self.store = state, ws, UID, store


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    return state


def _link(state: Path, ws: str, link_state: str = "linked") -> None:
    from orch.host.custody import write_private

    write_private(_relay_dir(state, ws) / "link.json", {"v": 1, "state": link_state})


def test_host_limit_is_in_the_catalog():
    from orch.host.errors import HOST_CODES

    assert HOST_CODES["host.limit"] == (3, False)


# --- orch host relay stop: the persistent stop marker ----------------------------------------------------------------


def _short_run() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp(prefix="rr", dir="/tmp"))


# --- the relay worker lifecycle (Codex P2 #1, #3; Opus I3) -----------------------------------------------------------


class SlowWorker:
    """R5's worker contract: ``stop()`` and ``join(timeout) -> bool``."""

    def __init__(self, release: Any, alive_after: bool = False) -> None:
        import threading

        self.release, self.alive_after = release, alive_after
        self.stopped = threading.Event()
        self.joined: list[float] = []

    def stop(self) -> None:
        self.release.wait(10)
        self.stopped.set()

    def join(self, timeout: float) -> bool:
        self.joined.append(timeout)
        return not self.alive_after


def _service(tmp_path: Path) -> Any:
    from orch.host.registry import Entry
    from orch.host.workspace import WorkspaceService

    state = _state(tmp_path)
    entry = Entry(workspace_id=WS, prefix="DEMO", root=str(tmp_path), host_uid=UID, group="staff", peers={})
    return WorkspaceService(entry, state, UID)


# --- #441 / #454: the convenience_seen mark lives in append_signed -------------------------------------------------


class MarkService(FakeService):
    def __init__(self, store: Any, state: Path) -> None:
        super().__init__(store)
        self.state_dir, self.workspace_id, self.host_uid, self.clock = state, WS, UID, lambda: 1_800_000_000.0


def test_append_signed_writes_the_convenience_mark_before_the_first_convenience_event(tmp_path):
    from orch.host.human import append_signed
    from orch.store.convmark import convenience_seen

    state = _state(tmp_path)
    store = FakeStore()
    landed, error = append_signed(MarkService(store, state), "L", [signed(1)], [{}], signed_by=None)
    assert error is None and not convenience_seen(state, WS)  # no convenience event: no mark
    landed, error = append_signed(MarkService(store, state), "L", [signed(2, auth="convenience")], [{}], signed_by=None)
    assert error is None and landed == [11] and convenience_seen(state, WS)


def test_append_signed_appends_nothing_when_the_mark_cannot_be_written(tmp_path):
    from orch.host.human import append_signed

    state = _state(tmp_path)
    (state / "hosts").mkdir(mode=0o700)
    (state / "hosts" / WS).write_text("not a directory")  # the mark's directory cannot exist
    store = FakeStore()
    landed, error = append_signed(MarkService(store, state), "L", [signed(1, auth="convenience")], [{}], signed_by=None)
    # H12d-2: the readers check runs first and fails closed on the same unreadable directory (a relay may exist)
    assert landed == [] and error is not None and store.appended == []
    assert error.code in ("custody.unsafe_mode", "sign.convenience_unavailable")


# --- review round 2 ---------------------------------------------------------------------------------------------------


def _unlink_cli_env(tmp_path, run):
    from orch.host import relaylink
    from orch.host.custody import write_private
    from orch.host.registry import registry_path

    state = _state(tmp_path)
    reg = registry_path(state)
    reg.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(reg, {"v": 1, "workspaces": [{"workspace_id": WS, "prefix": "DEMO", "root": "/x"}]})
    _link(state, WS)
    env = {"HOME": str(tmp_path), "ORCH_STATE_DIR": str(state), "ORCH_HOST_RUN_DIR": str(run)}
    return state, env, relaylink.link_path(state, WS)
