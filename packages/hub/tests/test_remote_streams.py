"""The hub's stream router (R4, #1592): frame paths, limits, close rules, error mapping, logs. Fake link ports."""

from __future__ import annotations

import logging
import threading
import time

import pytest
from remote_helpers import (
    CODE,
    DEV,
    DEV2,
    HUB,
    WS_A,
    WS_B,
    FakePort,
    FakeSink,
    answer,
    close_frame,
    data_frame,
    open_stream,
)

from orch_hub.backup.hubop import FENCE
from orch_hub.contract import remote as R
from orch_hub.contract.canon import b64u
from orch_hub.remote import Limits, Refused, StreamRouter


def make(**kw):
    lim = kw.pop("limits", None)
    r = StreamRouter(hub_id=HUB, limits=lim, **kw)
    a, b = FakePort(), FakePort()
    r.attach(WS_A, a)
    r.attach(WS_B, b)
    return r, a, b


def open_async(r, ws, user="u1", dev=DEV, kind="session", sink=None):
    out: dict = {}

    def go():
        try:
            out["sid"] = r.open(ws, user, dev, kind, sink or FakeSink())
        except Refused as e:
            out["err"] = e.code

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, out


# --- open and the answer -----------------------------------------------------------------------------------------


def test_stream_open_reply_reaches_router_by_re_and_port():
    r, a, b = make()
    t, out = open_async(r, WS_A)
    seq, body = a.wait_op("stream_open")[0]
    assert set(body) == {"stream_id", "device_id", "kind"} and len(body["stream_id"]) == 32
    # the same seq answered by ANOTHER workspace's port does nothing
    r.on_reply(WS_A, b, {"re": seq, "body": {}})
    r.on_reply(WS_B, b, {"re": seq, "body": {}})
    t.join(0.2)
    assert t.is_alive()
    r.on_reply(WS_A, a, {"re": seq, "body": {}})
    t.join(5)
    assert out["sid"] == body["stream_id"]


def test_stream_unbound_workspace_is_not_allowed():
    r, _a, _ = make()
    with pytest.raises(Refused) as e:
        r.open("33" * 16, "u1", DEV, "session", FakeSink())
    assert e.value.code == "not_allowed"


@pytest.mark.parametrize(
    "body,want",
    [
        ({"error": "remote.off"}, "remote.off"),
        ({"error": "remote.device"}, "remote.device"),
        ({"error": "remote.busy"}, "remote.busy"),
        ({"error": "remote.no_code"}, "remote.no_code"),
        ({"error": "replay"}, "replay"),
        ({"error": "remote.version"}, "unavailable"),
        ({"error": "some free text from the host"}, "unavailable"),
        ({"error": "malformed"}, "unavailable"),
        ({"error": 5}, "unavailable"),
        ({"x": 1}, "unavailable"),
        ({"error": "replay", "extra": 1}, "unavailable"),
    ],
)
def test_stream_host_error_codes_verbatim_else_unavailable(body, want):
    r, a, _ = make()
    t, out = open_async(r, WS_A)
    answer(r, WS_A, a, body)
    t.join(5)
    assert out == {"err": want} and r.open_count() == 0


def test_stream_open_times_out_unavailable_and_late_answer_closes_host_side():
    r, a, _ = make(limits=Limits(open_timeout_s=0.1))
    t, out = open_async(r, WS_A)
    seq, body = a.wait_op("stream_open")[0]
    t.join(5)
    assert out == {"err": "unavailable"} and r.open_count() == 0
    r.on_reply(WS_A, a, {"re": seq, "body": {}})  # the host opened it after we gave up
    assert a.ops("stream_close") == [(2, {"stream_id": body["stream_id"], "reason": "closed"})]


def test_stream_open_during_restore_fence_is_unavailable():
    r, _, _ = make()
    FENCE.set()
    try:
        with pytest.raises(Refused) as e:
            r.open(WS_A, "u1", DEV, "session", FakeSink())
    finally:
        FENCE.clear()
    assert e.value.code == "unavailable"


def test_stream_id_is_drawn_by_the_hub_and_unique():
    r, a, _ = make()
    ids = {open_stream(r, WS_A, a, dev=f"rd_{i:032x}")[0] for i in range(4)}
    assert len(ids) == 4 and all(len(i) == 32 for i in ids)


# --- frames, host -> client ---------------------------------------------------------------------------------------


def test_stream_w2h_data_reaches_the_client_in_order():
    r, a, _ = make()
    sid, sink = open_stream(r, WS_A, a)
    for i, d in enumerate([b"one", b"two", b"three"], 1):
        r.on_frame(WS_A, a, data_frame(sid, i, d), 1024)
    assert sink.frames == [b"one", b"two", b"three"] and sink.ended == []


def test_stream_w2h_frame_for_unknown_id_dropped():
    r, a, _ = make()
    _sid, sink = open_stream(r, WS_A, a)
    r.on_frame(WS_A, a, data_frame("ff" * 16, 1, b"x"), 1024)
    r.on_frame(WS_A, a, close_frame("ff" * 16, "closed"), 1024)
    assert r.dropped == 2 and sink.frames == [] and sink.ended == [] and r.open_count() == 1


def test_stream_w2h_frame_from_other_link_port_dropped():
    r, a, b = make()
    sid, sink = open_stream(r, WS_A, a)  # ws A's stream
    r.on_frame(WS_B, b, data_frame(sid, 1, b"inject"), 1024)  # ws B's link names it
    r.on_frame(WS_B, b, close_frame(sid, "closed"), 1024)
    r.on_frame(WS_A, b, data_frame(sid, 1, b"inject"), 1024)  # even claiming ws A, with the wrong port
    assert sink.frames == [] and sink.ended == [] and r.open_count() == 1 and r.dropped == 3


def test_stream_w2h_frame_after_close_dropped():
    r, a, _ = make()
    sid, sink = open_stream(r, WS_A, a)
    r.on_frame(WS_A, a, close_frame(sid, "idle"), 1024)
    r.on_frame(WS_A, a, data_frame(sid, 1, b"late"), 1024)
    assert sink.ended == ["idle"] and sink.frames == [] and r.dropped == 1


def test_stream_w2h_bad_seq_closes_stream_malformed_link_stays():
    r, a, _ = make()
    sid, sink = open_stream(r, WS_A, a)
    r.on_frame(WS_A, a, data_frame(sid, 2, b"skip"), 1024)
    assert sink.ended == ["malformed"] and r.open_count() == 0
    assert a.ops("stream_close")[-1][1] == {"stream_id": sid, "reason": "malformed"}
    sid2, sink2 = open_stream(r, WS_A, a, dev=DEV2)  # the link is still usable
    r.on_frame(WS_A, a, data_frame(sid2, 1, b"ok"), 1024)
    assert sink2.frames == [b"ok"]


def test_stream_w2h_replayed_seq_closes_stream():
    r, a, _ = make()
    sid, sink = open_stream(r, WS_A, a)
    r.on_frame(WS_A, a, data_frame(sid, 1, b"x"), 1024)
    r.on_frame(WS_A, a, data_frame(sid, 1, b"x"), 1024)
    assert sink.frames == [b"x"] and sink.ended == ["malformed"]


def test_stream_w2h_close_reason_verbatim_and_no_h2w_echo():
    r, a, _ = make()
    sid, sink = open_stream(r, WS_A, a)
    r.on_frame(WS_A, a, close_frame(sid, "revoked"), 1024)
    assert sink.ended == ["revoked"] and a.ops("stream_close") == []


def test_stream_w2h_data_before_open_answer_closes_malformed():
    r, a, _ = make()
    sink = FakeSink()
    t, out = open_async(r, WS_A, sink=sink)
    _, body = a.wait_op("stream_open")[0]
    r.on_frame(WS_A, a, data_frame(body["stream_id"], 1, b"early"), 1024)
    t.join(5)
    assert out.get("err") == "unavailable" and sink.ended == ["malformed"]


def test_stream_slow_client_closes_busy_link_thread_never_blocks():
    r, a, _ = make()
    sid, sink = open_stream(r, WS_A, a, sink=FakeSink(accept=False))
    t0 = time.monotonic()
    r.on_frame(WS_A, a, data_frame(sid, 1, b"x"), 1024)
    assert time.monotonic() - t0 < 0.5
    assert sink.ended == ["remote.busy"] and a.ops("stream_close")[-1][1]["reason"] == "remote.busy"


# --- frames, client -> host ---------------------------------------------------------------------------------------


def test_stream_client_data_goes_out_with_consecutive_seq():
    r, a, _ = make()
    sid, _ = open_stream(r, WS_A, a)
    r.send(sid, b"hello")
    r.send(sid, b"world")
    got = a.ops("stream_data")
    assert [b["seq"] for _, b in got] == [1, 2] and got[0][1] == {"stream_id": sid, "seq": 1, "bytes": b64u(b"hello")}


def test_stream_oversize_client_frame_too_large():
    r, a, _ = make()
    sid, sink = open_stream(r, WS_A, a)
    with pytest.raises(Refused):
        r.send(sid, b"x" * (R.FRAME_MAX + 1))
    assert sink.ended == ["too_large"] and a.ops("stream_data") == []


def test_stream_max_size_client_frame_goes_through():
    r, a, _ = make()
    sid, _ = open_stream(r, WS_A, a)
    r.send(sid, b"x" * R.FRAME_MAX)
    assert len(a.ops("stream_data")) == 1


def test_stream_empty_client_frame_malformed():
    r, a, _ = make()
    sid, sink = open_stream(r, WS_A, a)
    with pytest.raises(Refused):
        r.send(sid, b"")
    assert sink.ended == ["malformed"]


def test_stream_send_after_close_refused():
    r, a, _ = make()
    sid, _ = open_stream(r, WS_A, a)
    assert r.close(sid, "closed") is True and r.close(sid, "closed") is False
    with pytest.raises(Refused):
        r.send(sid, b"x")


def test_stream_client_close_tells_host_once():
    r, a, _ = make()
    sid, sink = open_stream(r, WS_A, a)
    r.close(sid, "closed", by="client")
    r.close(sid, "closed", by="client")
    assert a.ops("stream_close") == [(2, {"stream_id": sid, "reason": "closed"})] and sink.ended == ["closed"]


def test_stream_send_on_broken_link_closes_stream_closed():
    r, a, _ = make()
    sid, sink = open_stream(r, WS_A, a)
    a.fail = True
    with pytest.raises(Refused):
        r.send(sid, b"x")
    assert sink.ended == ["closed"] and r.open_count() == 0


# --- limits (design §3) -------------------------------------------------------------------------------------------


def test_stream_limit_per_device():
    r, a, _ = make()
    for _ in range(4):
        open_stream(r, WS_A, a)
    with pytest.raises(Refused) as e:
        r.open(WS_A, "u1", DEV, "session", FakeSink())
    assert e.value.code == "remote.busy"
    open_stream(r, WS_A, a, dev=DEV2)  # another device is fine


def test_stream_limit_per_workspace():
    r, a, b = make(limits=Limits(per_user=99, opens_per_user_min=99))
    for i in range(16):
        open_stream(r, WS_A, a, dev=f"rd_{i:032x}")
    with pytest.raises(Refused) as e:
        r.open(WS_A, "u1", "rd_" + "ee" * 16, "session", FakeSink())
    assert e.value.code == "remote.busy"
    open_stream(r, WS_B, b, dev=DEV)  # another workspace is not affected


def test_stream_limit_per_hub_user():
    r, a, _ = make()
    for i in range(8):
        open_stream(r, WS_A, a, user="u1", dev=f"rd_{i:032x}")
    with pytest.raises(Refused) as e:
        r.open(WS_A, "u1", "rd_" + "ee" * 16, "session", FakeSink())
    assert e.value.code == "remote.busy"
    open_stream(r, WS_A, a, user="u2", dev="rd_" + "ee" * 16)


def test_stream_limit_hub_wide():
    r, a, _ = make(limits=Limits(hub_wide=3, per_user=99, opens_per_user_min=99))
    for i in range(3):
        open_stream(r, WS_A, a, dev=f"rd_{i:032x}")
    with pytest.raises(Refused) as e:
        r.open(WS_A, "u1", "rd_" + "ee" * 16, "session", FakeSink())
    assert e.value.code == "remote.busy"


def test_stream_limit_opens_per_device_per_minute():
    t = [1000.0]
    r, a, _ = make(now=lambda: t[0])
    for _ in range(10):  # open and close: the rate counts attempts, not live streams
        sid, _s = open_stream(r, WS_A, a, user="u")
        r.close(sid, "closed")
        t[0] += 0.1
    # the per-user rate (20) is not the limit here; the per-device rate (10) is
    with pytest.raises(Refused) as e:
        r.open(WS_A, "u", DEV, "session", FakeSink())
    assert e.value.code == "remote.busy"
    t[0] += 61
    sid, _s = open_stream(r, WS_A, a, user="u")  # the window moved on


def test_stream_limit_enroll_opens_per_workspace_per_minute():
    r, a, _ = make()
    for i in range(3):
        sid, _s = open_stream(r, WS_A, a, dev=f"rc_{i:032x}", kind="enroll")
        r.close(sid, "closed")
    with pytest.raises(Refused) as e:
        r.open(WS_A, "u1", CODE, "enroll", FakeSink())
    assert e.value.code == "remote.busy"
    open_stream(r, WS_A, a, dev=DEV)  # sessions are not limited by the enroll rate


def test_stream_enroll_rate_is_per_user_and_workspace_and_counts_only_accepted_opens():
    r, a, _ = make()
    for i in range(3):  # user A burns the per-(user, workspace) enroll rate with opens the host refuses
        n = len(a.ops("stream_open")) + 1
        out: dict = {}
        t = threading.Thread(
            target=lambda i=i, out=out: out.update(r_=open_async_result(r, "uA", f"rc_{i:032x}")), daemon=True
        )
        t.start()
        answer(r, WS_A, a, {"error": "remote.no_code"}, n)
        t.join(5)
        assert out["r_"] == "remote.no_code"
    assert open_async_result(r, "uA", CODE) == "remote.busy"  # A is capped ...
    open_stream(
        r, WS_A, a, user="uB", dev=CODE, kind="enroll"
    )  # ... B's enroll open still goes through (A's did not count)
    # the shared per-workspace rate (6) counts accepted opens only: B and C fill it, then D is busy
    for i, u in enumerate(("uB", "uB", "uC", "uC", "uC")):
        open_stream(r, WS_A, a, user=u, dev=f"rc_{i + 1:032x}", kind="enroll")
    assert open_async_result(r, "uD", CODE) == "remote.busy"


def test_stream_enroll_slots_are_reserved_atomically_and_freed_when_the_open_fails():
    n = 3
    r, a, _ = make(
        limits=Limits(enroll_opens_per_ws_min=n, enroll_opens_per_user_ws_min=99, opens_per_user_min=99, per_user=99)
    )
    barrier = threading.Barrier(n + 1)
    res: list[str] = []
    mu = threading.Lock()

    def go(i: int) -> None:
        barrier.wait()
        out = open_async_result(r, f"u{i}", f"rc_{i:032x}")
        with mu:
            res.append(out)

    ts = [threading.Thread(target=go, args=(i,), daemon=True) for i in range(n + 1)]
    for t in ts:
        t.start()
    a.wait_op("stream_open", n)  # exactly n reached the host (the extra one was refused at once)
    deadline = time.monotonic() + 3
    while not res and time.monotonic() < deadline:
        time.sleep(0.01)
    assert res == ["remote.busy"] and len(a.ops("stream_open")) == n
    for i in range(n):  # the host refuses all of them: every reservation is freed
        seq, _b = a.ops("stream_open")[i]
        r.on_reply(WS_A, a, {"re": seq, "body": {"error": "remote.no_code"}})
    for t in ts:
        t.join(5)
    assert sorted(res) == ["remote.busy"] + ["remote.no_code"] * n
    open_stream(r, WS_A, a, user="uz", dev="rc_" + "dd" * 16, kind="enroll")  # a later open passes


def open_async_result(r, user, dev):
    try:
        r.open(WS_A, user, dev, "enroll", FakeSink())
    except Refused as e:
        return e.code
    return "ok"


def test_stream_limit_opens_per_user_per_minute():
    r, a, _ = make(limits=Limits(per_device=99, per_user=99, opens_per_device_min=99))
    for i in range(20):
        sid, _s = open_stream(r, WS_A, a, dev=f"rd_{i:032x}")
        r.close(sid, "closed")
    with pytest.raises(Refused) as e:
        r.open(WS_A, "u1", DEV, "session", FakeSink())
    assert e.value.code == "remote.busy"


def test_stream_limit_refused_open_sends_nothing_to_host():
    r, a, _ = make()
    for _ in range(4):
        open_stream(r, WS_A, a)
    n = len(a.ops("stream_open"))
    with pytest.raises(Refused):
        r.open(WS_A, "u1", DEV, "session", FakeSink())
    assert len(a.ops("stream_open")) == n


def test_stream_limit_byte_budget_life_both_directions():
    r, a, _ = make(limits=Limits(life_bytes=5000, min_bytes=1 << 30, link_bytes_per_min=1 << 30))
    sid, sink = open_stream(r, WS_A, a)
    for i in range(1, 5):  # four frames of 1024 fit
        r.on_frame(WS_A, a, data_frame(sid, i, b"x"), 1024)
    r.on_frame(WS_A, a, data_frame(sid, 5, b"x"), 1024)
    assert sink.ended == ["remote.busy"]
    sid2, sink2 = open_stream(r, WS_A, a, dev=DEV2)
    with pytest.raises(Refused):
        for _ in range(10):
            r.send(sid2, b"y")
    assert sink2.ended == ["remote.busy"]


def test_stream_limit_byte_budget_per_minute_resets():
    t = [1000.0]
    r, a, _ = make(limits=Limits(min_bytes=2048, link_bytes_per_min=1 << 30), now=lambda: t[0])
    sid, sink = open_stream(r, WS_A, a)
    r.on_frame(WS_A, a, data_frame(sid, 1, b"a"), 1024)
    r.on_frame(WS_A, a, data_frame(sid, 2, b"b"), 1024)
    t[0] += 61
    r.on_frame(WS_A, a, data_frame(sid, 3, b"c"), 1024)
    assert sink.frames == [b"a", b"b", b"c"] and sink.ended == []
    r.on_frame(WS_A, a, data_frame(sid, 4, b"d"), 2048)
    r.on_frame(WS_A, a, data_frame(sid, 5, b"e"), 1024)
    assert sink.ended == ["remote.busy"]


def test_stream_limit_byte_cost_of_a_big_frame_is_its_signed_size():
    r, a, _ = make(limits=Limits(min_bytes=100_000, link_bytes_per_min=1 << 30))
    sid, sink = open_stream(r, WS_A, a)
    r.on_frame(WS_A, a, data_frame(sid, 1, b"x"), 90_000)
    r.on_frame(WS_A, a, data_frame(sid, 2, b"x"), 20_000)
    assert sink.ended == ["remote.busy"]


def test_stream_out_cost_never_undercounts_a_real_frame():
    r, a, _ = make()
    sid, _ = open_stream(r, WS_A, a)
    s = r._streams[sid]
    for n in (1, 100, 1000, R.FRAME_MAX):
        real = {
            "o": {
                "v": 2, "suite": 2, "kind": "hub.link", "hv": 0, "hub_id": HUB, "workspace_id": WS_A, "dir": "h2w",
                "seq": 12345, "ts_ms": 1_800_000_000_000, "op": "stream_data", "re": None,
                "body": {"stream_id": sid, "seq": 7, "bytes": b64u(b"z" * n)},
            },
            "sig": b64u(b"s" * 64),
        }  # fmt: skip
        assert r._out_cost(s, n) >= R.frame_cost(real)


def test_stream_limit_link_budget_closes_the_crossing_stream_only():
    r, a, _ = make(limits=Limits(link_bytes_per_min=3000))
    s1, k1 = open_stream(r, WS_A, a)
    s2, k2 = open_stream(r, WS_A, a, dev=DEV2)
    r.on_frame(WS_A, a, data_frame(s1, 1, b"a"), 1024)
    r.on_frame(WS_A, a, data_frame(s2, 1, b"a"), 1024)
    r.on_frame(WS_A, a, data_frame(s1, 2, b"a"), 1024)  # crosses the link's minute budget
    assert k1.ended == ["remote.busy"] and k2.ended == [] and r.open_count() == 1


# --- reconnect, expiry, revocation --------------------------------------------------------------------------------


def test_stream_reconnect_closes_streams_never_resumes():
    r, a, _ = make()
    sid, sink = open_stream(r, WS_A, a)
    r.detach(WS_A, a)
    assert sink.ended == ["closed"] and r.open_count() == 0
    assert a.ops("stream_close") == []  # the old link is gone: no h2w frame
    new = FakePort()
    r.attach(WS_A, new)
    r.on_frame(WS_A, new, data_frame(sid, 1, b"zombie"), 1024)  # the old id on the NEW port
    with pytest.raises(Refused):
        r.send(sid, b"x")
    assert sink.frames == [] and new.ops("stream_close") == []


def test_stream_attach_of_a_second_port_closes_the_first_ports_streams():
    r, a, _ = make()
    sid, sink = open_stream(r, WS_A, a)
    r.attach(WS_A, FakePort())
    assert sink.ended == ["closed"]
    r.on_frame(WS_A, a, data_frame(sid, 1, b"x"), 1024)
    assert r.dropped == 1


def test_stream_detach_during_open_fails_the_open():
    r, a, _ = make()
    t, out = open_async(r, WS_A)
    a.wait_op("stream_open")
    r.detach(WS_A, a)
    t.join(5)
    assert out == {"err": "unavailable"}


def test_stream_detach_of_an_old_port_leaves_the_new_one():
    r, a, _ = make()
    new = FakePort()
    r.attach(WS_A, new)
    r.detach(WS_A, a)  # the old link's thread ends late
    sid, _ = open_stream(r, WS_A, new)
    assert r.alive(sid)


def test_stream_open_on_a_dead_link_unavailable():
    r, a, _ = make()
    a.fail = True
    with pytest.raises(Refused) as e:
        r.open(WS_A, "u1", DEV, "session", FakeSink())
    assert e.value.code == "unavailable" and r.open_count() == 0


def test_stream_sweep_expires_old_streams():
    t = [0.0]
    r, a, _ = make(now=lambda: t[0])
    _sid, sink = open_stream(r, WS_A, a)
    t[0] = 12 * 3600
    r.sweep()
    assert sink.ended == []
    t[0] = 12 * 3600 + 301
    r.sweep()
    assert sink.ended == ["expired"] and a.ops("stream_close")[-1][1]["reason"] == "expired"


def test_stream_sweep_closes_streams_whose_user_left_the_rail():
    live = {"u1": True, "u2": True}
    r, a, _ = make(allowed=lambda u, ws: live[u])
    _s1, k1 = open_stream(r, WS_A, a, user="u1")
    _s2, k2 = open_stream(r, WS_A, a, user="u2", dev=DEV2)
    live["u1"] = False
    r.sweep()
    assert k1.ended == ["revoked"] and k2.ended == []
    assert r.revoke_user("u2") == 1 and k2.ended == [
        "revoked"
    ]  # the production wiring is tested in test_remote_endpoint


def test_stream_sweep_runs_each_streams_own_recheck():
    ok = {"a": True, "b": True}
    r, a, _ = make()
    ka, kb = FakeSink(), FakeSink()
    for sink, key, dev in ((ka, "a", DEV), (kb, "b", DEV2)):
        n = len(a.ops("stream_open")) + 1
        t = threading.Thread(
            target=lambda sink=sink, key=key, dev=dev: r.open(WS_A, "u1", dev, "session", sink, lambda: ok[key]),
            daemon=True,
        )
        t.start()
        answer(r, WS_A, a, None, n)
        t.join(5)
    ok["a"] = False
    r.sweep()
    assert ka.ended == ["revoked"] and kb.ended == []


def test_stream_sweep_fails_closed_when_the_check_raises():
    def boom(u, ws):
        raise RuntimeError("db gone")

    r, a, _ = make(allowed=boom)
    _, sink = open_stream(r, WS_A, a)
    r.sweep()
    assert sink.ended == ["revoked"]


def test_stream_close_all_ends_everything():
    r, a, _ = make()
    _, sink = open_stream(r, WS_A, a)
    r.close_all()
    assert sink.ended == ["closed"] and r.open_count() == 0


# --- logs ---------------------------------------------------------------------------------------------------------


def test_stream_logs_hold_no_frame_content(caplog, capsys, tmp_path):
    caplog.set_level(logging.DEBUG)
    secret = b"SECRET-PLAINTEXT-MARKER-0123456789"
    r, a, _ = make()
    sid, _sink = open_stream(r, WS_A, a, dev=DEV)
    r.send(sid, secret)
    r.on_frame(WS_A, a, data_frame(sid, 1, secret), 1024)
    r.on_frame(WS_A, a, data_frame(sid, 9, secret), 1024)  # bad seq: a malformed close is logged
    r.close(sid, "closed")
    with pytest.raises(Refused):
        r.send(sid, b"x")
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    err = capsys.readouterr()
    for hay in (text, err.out, err.err):
        assert secret.decode() not in hay and b64u(secret) not in hay and DEV not in hay
    assert "stream_open" in text and "stream_close" in text and sid in text
    assert not list(tmp_path.iterdir())  # the router writes nothing to disk
