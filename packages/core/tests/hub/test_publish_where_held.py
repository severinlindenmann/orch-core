"""#1838 step C (hub protocol 10.9.1): core verifies the hub's signed ``publish_where`` in the join answer, holds it in
the hub state and re-verifies it at rest; the seq tombstone survives a leave. The pure checker is replayed against the
shared vectors in ``test_hub_where_vectors.py``; here is the state around it."""

from __future__ import annotations

import copy
import logging

import pytest

from orch.hubclient import HubRefused, HubState, publish_held

from .fake import FakeHub, signed_publish_where
from .test_join import NOW, Host
from .vec import V

HUB_ID = V["ids"]["hub"]


@pytest.fixture
def host(hub: FakeHub) -> Host:
    return Host(hub)


def held_seq(state: HubState) -> int | None:
    return None if state.publish_where is None else state.publish_where["o"]["seq"]


def floor_seqs(state: HubState) -> dict[str, int]:
    return {k: v["seq"] for k, v in state.publish_where_floors.items()}


def test_a_valid_publish_where_of_the_join_answer_is_stored(host: Host, hub: FakeHub) -> None:
    hub.join_answer = {"publish_where": signed_publish_where(seq=5)}
    host.join()
    assert host.state.status == "bound" and held_seq(host.state) == 5
    o, changed = publish_held.read(host.state, NOW)
    assert not changed and (o["server"], o["share"]) == ("https://pub.example.org", "https://apps.example.org")
    assert floor_seqs(host.state) == {HUB_ID: 5}
    assert host.state.publish_where_floors[HUB_ID]["digest"] == publish_held.digest_of(host.state.publish_where)
    assert HubState.from_json(copy.deepcopy(host.state.to_json())).publish_where == host.state.publish_where


@pytest.mark.parametrize(
    "bad,why",
    [
        ({"key": "wsk_a"}, "bad_signature"),
        ({"hub_id": "ab" * 16}, "other_hub"),
        ({"dev": False}, "dev_mismatch"),
        ({"server": "https://10.0.0.5"}, "bad_origin"),
        ({"share": "http://apps.example.org"}, "bad_origin"),
    ],
)
def test_a_bad_publish_where_of_the_join_answer_is_dropped_and_the_join_stands(
    host: Host, hub: FakeHub, caplog: pytest.LogCaptureFixture, bad: dict, why: str
) -> None:
    hub.join_answer = {"publish_where": signed_publish_where(**bad)}
    with caplog.at_level(logging.INFO, logger="orch.hubclient.service"):
        host.join()
    assert host.state.status == "bound" and host.state.publish_where is None and host.state.publish_where_floors == {}
    assert [r.getMessage() for r in caplog.records if "publish_where" in r.getMessage()] == [
        f"publish_where of the join answer not used ({why})"
    ]


@pytest.mark.parametrize(
    "answer",
    [
        {"publish_where": signed_publish_where(), "extra": 1},
        {"publish": "https://pub.example.org"},  # the legacy string is not part of the join answer at all
        {"other": 1},
    ],
)
def test_any_other_member_of_the_join_answer_is_malformed(host: Host, hub: FakeHub, answer: dict) -> None:
    hub.join_answer = answer
    with pytest.raises(HubRefused) as e:
        host.join()
    assert e.value.code == "malformed"
    assert host.state.status != "bound" and host.state.publish_where is None


@pytest.mark.parametrize(
    "value",
    [
        "x",
        None,
        5,
        [],
        {},
        {"o": {}, "sig": "x"},
        {"o": signed_publish_where()["o"]},
        {**signed_publish_where(), "x": 1},
    ],
)
def test_a_badly_shaped_publish_where_is_dropped_with_a_log_line_and_the_join_still_binds(
    host: Host, hub: FakeHub, caplog: pytest.LogCaptureFixture, value: object
) -> None:
    hub.join_answer = {"publish_where": value}  # the hub already committed this join: failing it now would strand it
    with caplog.at_level(logging.INFO, logger="orch.hubclient.service"):
        host.join()
    assert host.state.status == "bound" and host.state.publish_where is None and host.state.publish_where_floors == {}
    assert [r.getMessage() for r in caplog.records if "publish_where" in r.getMessage()] == [
        "publish_where of the join answer not used (malformed)"
    ]


def test_a_higher_seq_replaces_and_an_equal_one_must_be_the_same_bytes(host: Host, hub: FakeHub) -> None:
    hub.join_answer = {"publish_where": signed_publish_where(seq=5)}
    host.join()
    st = host.state
    new = signed_publish_where(seq=6, share="https://other.example.org")
    assert publish_held.offer(st, new, NOW) == (None, True) and held_seq(st) == 6
    assert floor_seqs(st) == {HUB_ID: 6}
    assert publish_held.offer(st, copy.deepcopy(new), NOW) == (None, False)  # byte-identical: nothing changes
    other = signed_publish_where(seq=6, share="https://third.example.org")
    assert publish_held.offer(st, other, NOW) == ("seq_conflict", False) and st.publish_where == new
    assert publish_held.offer(st, signed_publish_where(seq=4), NOW) == ("stale_seq", False) and st.publish_where == new
    assert publish_held.offer(st, signed_publish_where(seq=9, key="wsk_a"), NOW) == ("bad_signature", False)
    assert st.publish_where == new  # a bad new object never touches the last good one


def test_the_tombstone_survives_leave_and_an_older_seq_is_refused_after_a_rejoin(host: Host, hub: FakeHub) -> None:
    hub.join_answer = {"publish_where": signed_publish_where(seq=7)}
    host.join()
    host.leave()
    assert host.state.publish_where is None and floor_seqs(host.state) == {HUB_ID: 7}
    st = HubState.from_json(copy.deepcopy(host.state.to_json()))  # persisted with the rest
    assert st.publish_where_floors == host.state.publish_where_floors
    hub.join_answer = {"publish_where": signed_publish_where(seq=3)}
    host.join(binding_seq=2)
    assert host.state.status == "bound" and host.state.publish_where is None  # refused, the join stands
    assert floor_seqs(host.state) == {HUB_ID: 7}
    host.leave()
    hub.join_answer = {"publish_where": signed_publish_where(seq=8)}
    host.join(binding_seq=3)
    assert held_seq(host.state) == 8 and floor_seqs(host.state) == {HUB_ID: 8}


def test_after_a_leave_and_rejoin_the_same_seq_is_taken_only_with_the_same_bytes(
    host: Host, hub: FakeHub, caplog: pytest.LogCaptureFixture
) -> None:
    first = signed_publish_where(seq=7)
    hub.join_answer = {"publish_where": first}
    host.join()
    host.leave()
    assert host.state.publish_where is None and host.state.publish_where_floors[HUB_ID]["seq"] == 7
    other = signed_publish_where(seq=7, share="https://other.example.org")  # the same seq, other bytes
    hub.join_answer = {"publish_where": other}
    with caplog.at_level(logging.INFO, logger="orch.hubclient.service"):
        host.join(binding_seq=2)
    assert host.state.status == "bound" and host.state.publish_where is None  # refused, the join stands
    assert "publish_where of the join answer not used (seq_conflict)" in caplog.text
    assert publish_held.offer(host.state, other, NOW) == ("seq_conflict", False)
    assert floor_seqs(host.state) == {HUB_ID: 7}
    assert publish_held.offer(host.state, copy.deepcopy(first), NOW) == (None, True)  # the very same bytes are fine
    assert host.state.publish_where == first
    # a drop at rest leaves the same tombstone behind
    publish_held.drop(host.state)
    assert publish_held.offer(host.state, other, NOW) == ("seq_conflict", False)
    assert (
        publish_held.offer(host.state, signed_publish_where(seq=8, share="https://other.example.org"), NOW)[0] is None
    )


def test_a_floor_written_as_a_bare_seq_still_loads_and_refuses_an_equal_seq(host: Host, hub: FakeHub) -> None:
    host.join()
    doc = host.state.to_json()
    doc["publish_where_floors"] = {HUB_ID: 7}  # the shape before digests
    st = HubState.from_json(doc)
    assert st.publish_where_floors == {HUB_ID: {"seq": 7, "digest": None}}
    assert publish_held.offer(st, signed_publish_where(seq=7), NOW) == ("seq_conflict", False)
    assert publish_held.offer(st, signed_publish_where(seq=6), NOW) == ("stale_seq", False)
    assert publish_held.offer(st, signed_publish_where(seq=8), NOW) == (None, True)
    assert st.publish_where_floors[HUB_ID]["seq"] == 8 and st.publish_where_floors[HUB_ID]["digest"] is not None
    assert HubState.from_json(copy.deepcopy(st.to_json())).publish_where_floors == st.publish_where_floors


@pytest.mark.parametrize(
    "floor",
    [
        {"seq": 7},
        {"seq": 7, "digest": "A" * 64},
        {"seq": 7, "digest": "ab" * 31},
        {"seq": -1, "digest": None},
        {"seq": True, "digest": None},
        {"seq": 7, "digest": None, "x": 1},
        "7",
        -1,
        publish_held.SEQ_MAX + 1,
    ],
)
def test_a_damaged_floor_is_malformed(host: Host, hub: FakeHub, floor: object) -> None:
    host.join()
    doc = host.state.to_json()
    doc["publish_where_floors"] = {HUB_ID: floor}
    with pytest.raises(HubRefused) as e:
        HubState.from_json(doc)
    assert e.value.code == "malformed"


def test_retiring_a_full_binding_clears_the_held_object_and_keeps_the_tombstone(host: Host, hub: FakeHub) -> None:
    from orch.hubclient import retire_full_state

    hub.join_answer = {"publish_where": signed_publish_where(seq=4)}
    host.join()
    st = host.state
    st.mode = "full"  # a full binding the log has moved past
    saved: list[bool] = []
    assert retire_full_state(st, None, lambda s: saved.append(s.publish_where is None), "unbind")
    assert st.publish_where is None and floor_seqs(st) == {HUB_ID: 4} and saved == [True]  # gone before the save


def test_a_revoked_or_expired_op_cert_makes_the_held_object_absent_and_deletes_it(host: Host, hub: FakeHub) -> None:
    hub.join_answer = {"publish_where": signed_publish_where(seq=2)}
    host.join()
    st = host.state
    assert publish_held.read(st, NOW)[0] is not None
    late = st.card["op_cert"]["o"]["expires_ms"]
    assert publish_held.read(copy.deepcopy(st), late) == (None, True)  # expired
    st.revoked_op_certs.append(st.card["op_cert"]["o"]["cert_id"])
    assert publish_held.read(st, NOW) == (None, True) and st.publish_where is None  # revoked
    assert publish_held.read(st, NOW) == (None, False)
    assert floor_seqs(st) == {HUB_ID: 2}  # the tombstone stays


def test_a_card_that_changed_so_the_object_no_longer_verifies_drops_it(host: Host, hub: FakeHub) -> None:
    hub.join_answer = {"publish_where": signed_publish_where(seq=2)}
    host.join()
    st = host.state
    st.publish_where = signed_publish_where(seq=2, key="wsk_a")  # signed by another key than the card's op cert
    assert publish_held.read(st, NOW) == (None, True) and st.publish_where is None


def test_an_unbound_state_holds_nothing(host: Host, hub: FakeHub) -> None:
    hub.join_answer = {"publish_where": signed_publish_where(seq=2)}
    host.join()
    host.state.status = "unbound"
    assert publish_held.read(host.state, NOW) == (None, True)
    assert publish_held.offer(host.state, signed_publish_where(seq=3), NOW)[0] == "card_unverified"


def test_the_real_link_accepts_publish_where_in_the_join_and_hub_where_replies_and_no_other_member() -> None:
    from orch.hubclient.tlslink.client import _REPLY

    pw = signed_publish_where()
    where = {"addresses": [{"kind": "private", "link": "10.0.0.5:8105"}], "name": "H"}
    assert _REPLY["join"]({}) and _REPLY["join"]({"publish_where": pw})
    for bad in ({"publish_where": pw, "x": 1}, {"x": 1}, {"publish": "https://x.example.org"}):
        assert not _REPLY["join"](bad)
    assert _REPLY["join"]({"publish_where": "x"})  # a bad value is the host's to drop (the join is committed already)
    assert _REPLY["hub_where"](where) and _REPLY["hub_where"]({**where, "publish_where": pw})
    assert not _REPLY["hub_where"]({**where, "publish_where": "x"}) and not _REPLY["hub_where"]({**where, "x": 1})


def test_the_legacy_hub_publish_member_of_an_old_hub_json_is_read_and_dropped(host: Host, hub: FakeHub) -> None:
    host.join()
    doc = {**host.state.to_json(), "hub_publish": "http://127.0.0.1:8102"}
    st = HubState.from_json(doc)
    assert "hub_publish" not in st.to_json() and not hasattr(st, "hub_publish")
