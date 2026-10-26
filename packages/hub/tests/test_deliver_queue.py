"""The persisted event_deliver queue (protocol.md §8.7): the ack table, backoff, durability, ordering."""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from orch_hub.contract import phase4 as P4
from orch_hub.contract.canon import cj
from orch_hub.custodial.deliver import DeliveryQueue, build_delivery

VEC = json.loads((Path(__file__).resolve().parents[3] / "contracts" / "hub" / "vectors_hub.json").read_text())
WS = "ab" * 16
LOG = "01J9ZP0000000000000000TKT1"
SEC = 1000


def ev(n: int = 1, typ: str = "gate.approved") -> dict:
    return {
        "id": f"01J9ZP00000000000000000E{n:02d}",
        "type": typ,
        "actor": {"kind": "person", "id": "u_" + "cd" * 16, "via": "ef" * 16},
        "channel": "ef" * 16,
        "hash": "sha256:" + "11" * 32,
        "sig": "AAAA",
    }


class Port:
    """A live link as the queue sees it: records the requests, replies are fed back by the test."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict, int]] = []
        self.fail = False
        self.seq = 0

    def send_request(self, op, body):
        if self.fail:
            raise OSError("link gone")
        self.seq += 1
        self.sent.append((op, body, self.seq))
        return self.seq


class Rig:
    def __init__(self, path=":memory:"):
        self.t = [1_000_000]
        self.outcomes: list[dict] = []
        self.q = DeliveryQueue(path, now_ms=lambda: self.t[0], notify=self.outcomes.append)
        self.port = Port()
        self.link = None

    def attach(self):
        self.port = Port()
        self.link = self.q.attach(WS, self.port)

    def reply(self, body, *, seq=None):
        seq = seq or self.port.seq
        self.q.on_reply(WS, self.link, {"op": "event_deliver", "re": seq, "body": body})

    def advance(self, ms):
        self.t[0] += ms


@pytest.fixture
def rig():
    r = Rig()
    yield r
    r.q.close()


def test_queue_is_the_engines_deliver_callable_and_sends_the_exact_stored_bytes(rig):
    rig.q(WS, LOG, ev())
    rig.attach()
    rig.q.pump()
    op, body, _ = rig.port.sent[0]
    assert op == "event_deliver" and body == {"log": LOG, "event": ev()}
    assert cj(body["event"]) == cj(ev())  # the same canonical bytes the engine produced


def test_done_on_empty_reply_and_next_event_follows_in_order(rig):
    rig.q(WS, LOG, ev(1))
    rig.q(WS, LOG, ev(2))
    rig.attach()
    rig.q.pump()
    rig.q.pump()  # one in flight per link: the second waits behind the first
    assert [b["event"]["id"] for _, b, _ in rig.port.sent] == [ev(1)["id"]]
    rig.reply({})
    rig.q.pump()
    assert [b["event"]["id"] for _, b, _ in rig.port.sent] == [ev(1)["id"], ev(2)["id"]]
    assert rig.outcomes[0]["outcome"] == "done" and rig.outcomes[0]["event_id"] == ev(1)["id"]
    rig.reply({})
    assert [o["state"] for o in rig.q.outcomes()] == ["done", "done"] and rig.q.pending() == []


@pytest.mark.parametrize("code", ["unavailable", "unknown_snapshot"])
def test_retryable_codes_resend_the_same_bytes_with_backoff_up_to_five_minutes(rig, code):
    rig.q(WS, LOG, ev())
    rig.attach()
    rig.q.pump()
    waits = []
    for _ in range(12):
        rig.reply({"error": code})
        before = len(rig.port.sent)
        rig.q.pump()
        assert len(rig.port.sent) == before  # still waiting: nothing goes out early
        wait = rig.q.pending()[0]["next_ms"] - rig.t[0]
        waits.append(wait)
        rig.advance(wait - 1)
        rig.q.pump()
        assert len(rig.port.sent) == before
        rig.advance(1)
        rig.q.pump()
        assert len(rig.port.sent) == before + 1
        assert rig.port.sent[-1][1] == rig.port.sent[0][1]  # the SAME event bytes
    assert waits[:4] == [1 * SEC, 2 * SEC, 4 * SEC, 8 * SEC] and max(waits) == 300 * SEC and waits[-1] == 300 * SEC
    assert rig.q.pending()[0]["last_code"] == code


def test_no_reply_is_a_resend_after_the_timeout(rig):
    rig.q(WS, LOG, ev())
    rig.attach()
    rig.q.pump()
    rig.advance(29 * SEC)
    rig.q.pump()
    assert len(rig.port.sent) == 1
    rig.advance(1 * SEC)  # 30 s: no reply
    rig.q.pump()
    assert len(rig.port.sent) == 1 and rig.q.pending()[0]["last_code"] == "unavailable"
    rig.advance(1 * SEC)
    rig.q.pump()
    assert len(rig.port.sent) == 2 and rig.port.sent[1][1] == rig.port.sent[0][1]


def test_a_late_reply_to_a_timed_out_send_is_ignored(rig):
    rig.q(WS, LOG, ev())
    rig.attach()
    rig.q.pump()
    rig.advance(31 * SEC)
    rig.q.pump()
    rig.reply({}, seq=1)  # arrives after the timeout settled attempt 1
    assert len(rig.q.pending()) == 1  # not done: only the reply to the in-flight send counts


def test_a_link_drop_keeps_the_event_and_the_wait_survives_the_new_link(rig):
    rig.q(WS, LOG, ev())
    rig.attach()
    rig.port.fail = True
    rig.q.pump()  # the write fails: no reply
    assert len(rig.q.pending()) == 1 and rig.q.pending()[0]["attempts"] == 1
    rig.attach()  # a new link at once
    rig.q.pump()
    assert rig.port.sent == []  # still under the 1 s wait
    rig.advance(1 * SEC)
    rig.q.pump()
    assert len(rig.port.sent) == 1


@pytest.mark.parametrize(
    "code",
    ["malformed", "not_bound", "wrong_mode", "replay", "event_changed", "forbidden_role", "not_granted", "foreign_user",
     "channel_revoked", "not_custodial", "factor_too_weak", "view_fork", "internal", "bad_signature", "some_new_code"],
)  # fmt: skip
def test_every_other_code_is_final_and_the_channel_is_told(rig, code):
    rig.q(WS, LOG, ev())
    rig.attach()
    rig.q.pump()
    rig.reply({"error": code})
    assert rig.q.pending() == []
    assert rig.outcomes[-1]["outcome"] == "dropped" and rig.outcomes[-1]["code"] == code
    assert rig.q.outcomes()[0]["end_code"] == code
    rig.advance(10 * 60 * SEC)
    rig.q.pump()
    assert len(rig.port.sent) == 1  # never sent again


@pytest.mark.parametrize("reply", [[], "x", {"error": 1}, {"error": "a", "x": 1}, {"ok": True}, None])
def test_a_garbled_reply_is_not_done(rig, reply):
    rig.q(WS, LOG, ev())
    rig.attach()
    rig.q.pump()
    rig.reply(reply)
    assert len(rig.q.pending()) == 1


def test_gives_up_24h_after_the_first_send_with_the_last_code(rig):
    rig.q(WS, LOG, ev())
    rig.attach()
    rig.q.pump()
    rig.reply({"error": "unknown_snapshot"})
    rig.advance(24 * 3600 * SEC)
    rig.q.pump()  # 24 h after the first send: dropped, not sent again
    assert len(rig.port.sent) == 1 and rig.q.pending() == []
    assert rig.outcomes[-1]["outcome"] == "dropped" and rig.outcomes[-1]["code"] == "unknown_snapshot"


def test_never_reached_the_host_for_24h_is_dropped_unavailable(rig):
    rig.q(WS, LOG, ev())  # no link, ever
    rig.advance(24 * 3600 * SEC)
    rig.attach()
    rig.q.pump()
    assert (
        rig.port.sent == [] and rig.outcomes[-1]["outcome"] == "dropped" and rig.outcomes[-1]["code"] == "unavailable"
    )


def test_nothing_is_sent_without_a_link_and_events_of_other_workspaces_do_not_mix(rig):
    rig.q(WS, LOG, ev(1))
    rig.q("12" * 16, LOG, ev(2))
    rig.q.pump()
    rig.attach()
    rig.q.pump()
    assert [b["event"]["id"] for _, b, _ in rig.port.sent] == [ev(1)["id"]]


def test_a_duplicate_id_is_one_entry(rig):
    rig.q(WS, LOG, ev())
    rig.q(WS, LOG, ev())
    assert len(rig.q.pending()) == 1


def test_a_reply_on_another_op_or_without_inflight_is_ignored(rig):
    rig.q(WS, LOG, ev())
    rig.attach()
    rig.q.on_reply(WS, rig.link, {"op": "route_in", "re": 1, "body": {}})
    rig.q.on_reply(WS, rig.link, {"op": "event_deliver", "re": 1, "body": {}})
    assert len(rig.q.pending()) == 1


def test_survives_a_restart_and_resends_the_same_bytes(tmp_path):
    d = tmp_path / "state"
    d.mkdir(mode=0o700)
    r1 = Rig(str(d / "q.db"))
    r1.q(WS, LOG, ev())
    r1.attach()
    r1.q.pump()
    r1.reply({"error": "unavailable"})
    first = r1.port.sent[0][1]
    r1.q.close()
    r2 = Rig(str(d / "q.db"))
    r2.t[0] = r1.t[0] + 2 * SEC
    r2.attach()
    r2.q.pump()
    assert r2.port.sent[0][1] == first and r2.q.pending()[0]["attempts"] == 2
    r2.q.close()


def test_the_file_is_private_and_a_world_readable_one_is_refused(tmp_path):
    d = tmp_path / "state"
    d.mkdir(mode=0o700)
    q = build_delivery(str(d))
    q.close()
    assert stat.S_IMODE(os.stat(d / "event-deliver.db").st_mode) == 0o600
    os.chmod(d / "event-deliver.db", 0o644)
    with pytest.raises(Exception, match="database"):
        build_delivery(str(d))


def test_unreadable_stored_bytes_are_dropped_not_sent(rig):
    rig.q(WS, LOG, ev())
    rig.q._db.execute("UPDATE deliver SET event=?", (b"{not json",))
    rig.attach()
    rig.q.pump()
    assert rig.port.sent == [] and rig.outcomes[-1]["code"] == "internal"


def test_ended_rows_keep_no_event_bytes_and_logs_carry_none(rig, caplog):
    caplog.set_level(logging.DEBUG)
    rig.q(WS, LOG, ev())
    rig.attach()
    rig.q.pump()
    rig.reply({})
    assert rig.q._db.execute("SELECT event FROM deliver").fetchone()[0] == b""
    assert "AAAA" not in caplog.text and "sha256" not in caplog.text and ev()["id"] in caplog.text


def test_a_failing_notify_hook_does_not_stop_the_queue(rig):
    rig.q._notify = lambda o: 1 / 0
    rig.q(WS, LOG, ev(1))
    rig.q(WS, LOG, ev(2))
    rig.attach()
    rig.q.pump()
    rig.reply({})
    rig.q.pump()
    assert len(rig.port.sent) == 2


# --- the deliver_* vectors, through the queue's own settle ---------------------------------------------------------

DELIVER = [c for c in VEC["cases"] if c["name"].startswith("deliver_")]


@pytest.mark.parametrize("case", DELIVER, ids=lambda c: c["name"])
def test_deliver_vectors_replay_against_the_queue(case, rig):
    a, exp = case["args"], case["expect"]
    rig.q(WS, LOG, ev())
    rig.t[0] = a["now_ms"]
    rig.q._db.execute("UPDATE deliver SET attempts=?, first_sent_ms=?", (a["attempt"], a["first_sent_ms"]))
    want = exp.get("ok", exp)
    rig.q._settle(1, a["reply"])
    row = (rig.q.pending() or rig.q.outcomes())[0]
    if want.get("done"):
        assert row["state"] == "done"
    elif "drop" in want:
        assert row["state"] == "dropped" and row["end_code"] == want["drop"]
    else:
        assert row["state"] == "pending" and row["next_ms"] - rig.t[0] == want["resend_in_ms"]
    assert P4.deliver_next(a["reply"], attempt=a["attempt"], first_sent_ms=a["first_sent_ms"], now_ms=rig.t[0]) == want


def test_a_link_that_drops_with_an_event_in_flight_makes_the_next_link_wait(rig):
    rig.q(WS, LOG, ev())
    rig.attach()
    rig.q.pump()  # sent, no reply
    rig.q.detach(WS, rig.link)
    rig.attach()
    rig.q.pump()
    assert rig.port.sent == []  # attempt 1 got no reply: 1 s wait first
    rig.advance(1 * SEC)
    rig.q.pump()
    assert len(rig.port.sent) == 1


class Sender:
    """The write seam as ``Router.send_event`` must be: returns the h2w seq of the write."""

    def __init__(self):
        self.sent, self.seq = [], 0

    def __call__(self, ws, log, e):
        self.seq += 1
        self.sent.append((ws, log, e["id"]))
        return self.seq


def test_sender_seam_matches_the_reply_on_seq():
    snd, t = Sender(), [1_000_000]
    q = DeliveryQueue(now_ms=lambda: t[0], sender=snd)
    q(WS, LOG, ev())
    link = q.attach(WS, Port())
    q.pump()
    assert snd.sent == [(WS, LOG, ev()["id"])]
    q.on_reply(WS, link, {"op": "event_deliver", "re": 77, "body": {}})  # not the in-flight seq: dropped
    assert q.pending()
    q.on_reply(WS, link, {"op": "event_deliver", "re": 1, "body": {}})
    assert q.outcomes()[0]["state"] == "done"
    q.close()


def test_sender_that_raises_is_no_reply_and_keeps_the_link():
    t = [1_000_000]

    def boom(ws, log, e):
        raise OSError("x")

    q = DeliveryQueue(now_ms=lambda: t[0], sender=boom)
    q(WS, LOG, ev())
    link = q.attach(WS, Port())
    q.pump()
    assert not link.dead and q.pending()[0]["last_code"] == "unavailable"
    q.close()


def test_sender_that_returns_no_seq_is_a_failed_send_never_an_unmatchable_one():
    q = DeliveryQueue(now_ms=lambda: 1_000_000, sender=lambda ws, log, e: None)
    q(WS, LOG, ev())
    link = q.attach(WS, Port())
    q.pump()
    assert link.inflight is None and q.pending()[0]["last_code"] == "unavailable"
    q.close()


def test_l2_d9b_1_a_late_reply_to_an_earlier_attempt_never_ends_the_next_event():
    """A's attempt 1 times out, A is resent (attempt 2); the host's late reply to attempt 1 arrives while attempt 2 is in
    flight and again while B is in flight: neither is taken for another send's reply."""
    snd, t = Sender(), [1_000_000]
    q = DeliveryQueue(now_ms=lambda: t[0], reply_timeout_ms=30_000, sender=snd)
    a, b = ev(1), ev(2)
    q(WS, LOG, a)
    q(WS, LOG, b)
    link = q.attach(WS, Port())
    q.pump()  # A attempt 1 (seq 1)
    t[0] += 31_000
    q.pump()  # timeout -> backoff 1 s
    t[0] += 1_000
    q.pump()  # A attempt 2 (seq 2)
    q.on_reply(WS, link, {"op": "event_deliver", "re": 1, "body": {}})  # late reply to attempt 1: dropped
    assert [p["event_id"] for p in q.pending()] == [a["id"], b["id"]]
    q.on_reply(WS, link, {"op": "event_deliver", "re": 2, "body": {}})  # A done
    q.pump()  # B (seq 3)
    q.on_reply(WS, link, {"op": "event_deliver", "re": 1, "body": {}})  # a stale ack again
    st = {o["event_id"]: o["state"] for o in q.outcomes()}
    assert st == {a["id"]: "done"} and [p["event_id"] for p in q.pending()] == [b["id"]]
    q.close()


def test_a_duplicate_event_id_is_logged_not_silently_ignored(rig, caplog):
    rig.q(WS, LOG, ev())
    with caplog.at_level(logging.WARNING):
        rig.q(WS, LOG, ev())
    assert "already queued" in caplog.text


def test_router_send_event_returns_the_link_seq_the_queue_matches_on():
    from orch_hub.link import Registry
    from orch_hub.router import Router

    r = Router(Registry())
    port = Port()
    r.attach(WS, port)
    q = DeliveryQueue(now_ms=lambda: 1_000_000, sender=r.send_event)
    q(WS, LOG, ev())
    link = q.attach(WS, port)
    q.pump()
    assert port.sent[0][2] == 1 and link.inflight[1] == 1
    q.on_reply(WS, link, {"op": "event_deliver", "re": 1, "body": {}})
    assert q.outcomes()[0]["state"] == "done"
    q.close()
