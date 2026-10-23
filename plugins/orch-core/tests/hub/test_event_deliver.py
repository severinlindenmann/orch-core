"""C3b tasks 5 and 6 (ticket-format 15.5, 15.7): admission planning and the reply table, with a fake store.

The full path through a served workspace is tests/host/test_hub_deliver.py."""

from __future__ import annotations

import contextlib
import copy
from types import SimpleNamespace
from typing import Any

import pytest

from orch.hubclient import HubRefused
from orch.hubclient import custodial as C
from orch.model import Refusal
from orch.model.codes import Code
from orch.model.types import Member, WsCore

from .test_custodial import SEL, _state
from .vec import V

ALICE = V["ids"]["user_alice"]
HUB = V["ids"]["hub"]


@pytest.mark.parametrize(
    ("code", "detail", "reply"),
    [
        (Code.HUB_CERT_INVALID, "revoked", "revoked"),
        (Code.HUB_DENIED, "channel_revoked", "channel_revoked"),
        (Code.HUB_UNKNOWN_SNAPSHOT, "unknown_snapshot", "unknown_snapshot"),
        (Code.HUB_FOREIGN_USER, "foreign_user", "foreign_user"),
        (Code.HUB_INVALID, "wrong_mode", "wrong_mode"),
        (Code.HUB_INVALID, "something the contract has no word for", "malformed"),
        (Code.CHAIN_BROKEN, "x", "malformed"),
        (Code.MEMBERS_STALE, "", "event_changed"),
        (Code.GATE_STALE, "", "event_changed"),
        (Code.QUESTION_BAD_HASH, "", "event_changed"),
        (Code.EVENT_BAD_BASE, "", "event_changed"),
        (Code.QUESTION_ANSWERED, "", "event_changed"),
        (Code.EVENT_DUPLICATE_ID, "", "replay"),
        (Code.FREEZE_ACTIVE, "", "unavailable"),
        (Code.ROLE_DENIED, "", "forbidden_role"),
        (Code.GATE_NOT_ELIGIBLE, "", "forbidden_role"),
    ],
)
def test_the_reply_table_of_15_5(code, detail, reply):
    assert C.reply_code(Refusal(code, detail)) == reply


def test_only_unavailable_and_unknown_snapshot_are_retryable():
    assert C.RETRYABLE == {"unavailable", "unknown_snapshot"}
    assert "event_changed" not in C.RETRYABLE and "replay" not in C.RETRYABLE  # final: a retry cannot change them


def snap(version: int, roles: dict[str, str], revoked: list[str] = ()) -> dict[str, Any]:
    return {"o": {"version": version, "roles": roles, "revoked_channels": list(revoked)}}


def ledger(**kw: Any) -> dict[str, Any]:
    return {"card": None, "revoked_op_certs": [], "snapshot": None, **kw}


def hub_state(**kw: Any) -> Any:
    return SimpleNamespace(**{"card": {"c": 1}, "revoked_op_certs": [], "revoked_op_objs": {}, "snapshot": None, **kw})


def test_pins_follow_the_order_of_the_ledger_card_revocations_snapshot():
    st = hub_state(
        revoked_op_certs=["b" * 32], revoked_op_objs={"b" * 32: {"o": "rev"}}, snapshot=snap(1, {"u": "read"})
    )
    pins = C.pins_needed(st, ledger(), None, 0)
    assert pins == [{"c": 1}, {"o": "rev"}, st.snapshot]
    assert C.pins_needed(st, None, None, 0) == []  # a workspace without a ledger pins nothing (D79)
    done = ledger(card={"c": 1}, revoked_op_certs=["b" * 32], snapshot=st.snapshot)
    assert C.pins_needed(st, done, None, 0) == []  # nothing new


def test_a_revocation_held_only_as_an_id_cannot_be_pinned_and_fails_closed():
    with pytest.raises(HubRefused) as e:
        C.pins_needed(hub_state(revoked_op_certs=["b" * 32]), ledger(card={"c": 1}), None, 0)
    assert e.value.code == "unavailable"


def test_a_snapshot_pins_at_most_once_per_ten_minutes_unless_it_narrows():
    held = snap(1, {"u": "admin", "v": "edit"})
    led = ledger(card={"c": 1}, snapshot=held)
    ten = C.SNAPSHOT_PIN_INTERVAL_MS
    wider = hub_state(snapshot=snap(2, {"u": "admin", "v": "edit", "w": "read"}))  # adds a user
    assert C.pins_needed(wider, led, 1000, 1000 + ten - 1) == []  # too soon
    assert C.pins_needed(wider, led, 1000, 1000 + ten) == [wider.snapshot]
    for narrow in (
        snap(2, {"u": "admin"}),  # drops a user
        snap(2, {"u": "admin", "v": "read"}),  # lowers a role
        snap(2, {"u": "admin", "v": "edit"}, ["c" * 32]),  # revokes a channel
    ):
        st = hub_state(snapshot=narrow)
        assert C.pins_needed(st, led, 1000, 1001) == [narrow]  # narrowing always pins, even at once
    mixed = hub_state(snapshot=snap(2, {"u": "admin", "v": "read", "w": "admin"}))  # demotes AND adds (l2-c3bB P1-2)
    assert C.pins_needed(mixed, led, 1000, 1001) == [mixed.snapshot]
    raised = hub_state(snapshot=snap(2, {"u": "admin", "v": "admin"}))
    assert C.pins_needed(raised, led, 1000, 1001) == []  # a raise is not narrowing


# --- admission with a fake store ----------------------------------------------------------------------------------


class FakeLanding:
    def __init__(self, ws: WsCore, *, logged: dict | None = None, live: dict | None = None) -> None:
        self.ws, self._logged = ws, logged or {}
        self.live = live if live is not None else {}
        self.landed: list[tuple[list, dict, str]] = []

    @contextlib.contextmanager
    def locked(self):
        yield

    def core(self):
        return SimpleNamespace(ws=self.ws, tickets={})

    def logged(self, log, event_id):
        return self._logged.get((log, event_id))

    def refresh_snapshot(self):
        return False

    def last_snapshot_pin_ms(self, hub_id):
        return None

    def shared_items(self, log):
        return self.live, frozenset()

    def log_heads(self, log):
        return SEL["log_heads"][log]

    def land(self, pins, event, log):
        self.landed.append((pins, event, log))


def selective_ws() -> WsCore:
    person = "4cfef66f1dc7bee794e9d949f2a216c3"
    ws = WsCore(created=True, workspace_id=SEL["workspace_id"])
    ws.members = {"p_" + person: Member("p_" + person, "mara", "member", "k")}
    ws.hub = {
        "status": "joined", "hub_id": HUB, "mode": "selective", "alias_book": {HUB: {person: ALICE}},
        "user_hub": {ALICE: HUB},
    }  # fmt: skip
    return ws


def sel_state(snapshot: Any) -> Any:
    st = _state(mode="selective")
    st.card = SEL["card"]
    st.snapshot = V["objects"]["snapshot_a_v3"] if snapshot == "vec" else snapshot
    st.binding = {"o": {"workspace_id": SEL["workspace_id"]}}
    return st


def accept(land: FakeLanding, event: dict[str, Any], mode: str = "selective") -> str | None:
    st = sel_state("vec")
    st.mode = mode
    try:
        C.accept_event_deliver(st, {"log": SEL["log"], "event": event}, land=land, now_ms=SEL["now_ms"], dev_mode=True)
    except HubRefused as e:
        return e.code
    return None


def test_selective_delivery_is_no_longer_wrong_mode_and_lands_with_the_item_check_passed():
    land = FakeLanding(selective_ws(), live=SEL["live_items"])
    assert accept(land, SEL["event"]) is None
    assert land.landed and land.landed[0][1] == SEL["event"] and land.landed[0][2] == SEL["log"]


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"live": {}}, "not_granted"),  # not a live item
        ({"event": {"gate_gen": 99}}, "event_changed"),  # the event differs from what the item showed
        ({"event": {"roster_v": 99}}, "event_changed"),
        ({"event": {"policy_hash": "sha256:" + "9" * 64}}, "event_changed"),
        ({"event": {"hash": "sha256:" + "9" * 64}}, "event_changed"),
        ({"event": {"based_on": "sha256:" + "9" * 64}}, "event_changed"),
        ({"heads": ["sha256:" + "0" * 64] * 2}, "view_fork"),  # chain.head is not our log's head at chain.seq
    ],
)
def test_the_item_check_values_the_person_saw(over, code):
    # the values are part of the signed payload, so a changed one must be re-signed: build it through the hub keys
    from .test_custodial import C as _C  # noqa: F401

    event = dict(SEL["event"])
    land = FakeLanding(selective_ws(), live=over.get("live", SEL["live_items"]))
    if "heads" in over:
        land.log_heads = lambda log: over["heads"]  # type: ignore[method-assign]
    if "event" in over:
        event = remint(copy.deepcopy(event), over["event"])
    assert accept(land, event) == code
    assert land.landed == []  # nothing is appended on a refusal


def remint(event: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    from orch.crypto import suite as S
    from orch.crypto.encoding import b64u

    from .vec import private_key

    event.update(changes)
    event.pop("sig", None)
    event.pop("channel_proof", None)
    ws, log = SEL["workspace_id"], SEL["log"]
    event["channel_proof"] = b64u(
        S.sign(private_key("channel_alice_phone"), C.SIG_CHANNEL_PROOF + C.event_digest(ws, log, event))
    )
    event["sig"] = b64u(S.sign(private_key("uk_alice"), C.custodial_label(event["type"]) + C._context(ws, log, event)))  # noqa: SLF001
    return event


def test_a_changed_item_hash_that_is_a_superseded_version_is_event_changed():
    land = FakeLanding(selective_ws(), live={})
    land.shared_items = lambda log: ({}, frozenset({SEL["event"]["item_hash"]}))  # type: ignore[method-assign]
    assert accept(land, SEL["event"]) == "event_changed"


def test_full_mode_refuses_an_item_hash_and_dedup_runs_before_everything():
    land = FakeLanding(selective_ws())
    assert accept(land, SEL["event"], mode="full") == "malformed"
    e = SEL["event"]
    same = FakeLanding(
        selective_ws(), logged={(SEL["log"], e["id"]): {**e, "seq": 3, "at": "x", "prev": None, "host_sig": "h"}}
    )
    other = FakeLanding(selective_ws(), logged={(SEL["log"], e["id"]): {**e, "text": "y", "seq": 3}})
    broken = copy.deepcopy(e)
    broken["sig"] = "AAAA"  # would fail verification: a dedup hit is acknowledged before verification
    assert accept(same, broken) == "replay"  # other signed bytes under a logged id, before any verification
    assert accept(same, e) is None and same.landed == []
    assert accept(other, e) == "replay"
