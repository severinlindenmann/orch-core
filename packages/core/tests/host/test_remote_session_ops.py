"""R3 T1b (#1591): the host ops around a remote session: ``remote.attach`` (the dashboard's adapter), ``remote.stream``
(what the signer is told), ``remote.presence_used`` (the counter compare-and-set and its log line),
``remote.session_end`` and the richer ``remote.sessions``."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from orch.canon.jcs import dumps
from orch.crypto.encoding import b64u, unb64u
from orch.host import remote
from orch.host.remote import adapter, counters, wrk
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, ok
from tests.host.human_helpers import err
from tests.host.remote_world import Client, World, frame, head
from tests.host.test_remote_ops import call

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

NEW_OPS = ("remote.attach", "remote.stream", "remote.presence_used", "remote.session_end")


def args_for(op: str) -> dict[str, Any]:
    sid = "a" * 32
    return {
        "remote.attach": {},
        "remote.stream": {"stream_id": sid},
        "remote.session_end": {"stream_id": sid},
        "remote.presence_used": {"stream_id": sid, "device_id": "rd_" + "0" * 32, "count": 0, "n": 1},
    }[op]


@pytest.mark.parametrize("op", NEW_OPS)
def test_agent_column_g_refused_every_new_op(served, op):
    assert op in remote.REMOTE_OPS
    res = served.client().req(op, args_for(op), grant=served.grant, session=SESSION, idem=True)
    assert res["ok"] is False and res["error"]["code"] == "op.not_allowed", res


@pytest.mark.parametrize("op", NEW_OPS)
def test_new_ops_refuse_unknown_arguments(human, served, op):
    assert err(call(served, op, {**args_for(op), "x": 1})) == "invalid.input"


def test_the_cap_is_listed(served):
    from orch.host.dispatch import RegistryBackend

    assert "remote/1" in RegistryBackend().caps


def live(human, served, **kw) -> tuple[World, Client]:
    w = World(human, served, **kw)
    adapter.release(w.service)  # these tests attach through the op itself
    first = ok(call(served, "remote.attach"))["data"]
    w.ad = adapter.current(w.service)
    assert first["attach_id"] == w.ad.attach_id and first["frames"] == []
    c = w.client()
    c.connect()
    return w, c


def test_attach_hands_out_the_open_and_the_frames_and_takes_the_answers(human, served):
    w, c = live(human, served)
    c.say(head(1))
    c.say(frame("END", 1))
    got = ok(call(served, "remote.attach", {"attach_id": w.ad.attach_id}))["data"]["frames"]
    assert [m["t"] for m in got] == ["open", "frame", "frame"]
    assert got[0]["person"] == w.person and got[1]["identity"]["level"] == "work"
    answer = frame("HEAD", 1, dumps({"headers": [], "status": 200}))
    body = [answer, frame("BODY", 1, b"{}"), frame("END", 1)]
    ok(
        call(
            served,
            "remote.attach",
            {"attach_id": w.ad.attach_id, "send": [{"stream_id": c.sid, "bytes": b64u(b)} for b in body]},
        )
    )
    w.pump()
    assert c.hear() == body  # sealed by the host in order, sent by the pump
    assert w.link.data(c.sid)  # and they went out as stream_data frames


def test_attach_second_adapter_busy_and_a_stale_id_must_attach_again(human, served):
    w, _ = live(human, served)
    assert err(call(served, "remote.attach")) == "remote.busy"  # a second claim while the lease is live
    assert err(call(served, "remote.attach", {"attach_id": "at_" + "0" * 16})) == "invalid.input"
    w.keep_lease = False
    w.advance(16)  # the first dashboard went away: its lease ran out and the next one may attach
    again = ok(call(served, "remote.attach"))["data"]
    assert again["attach_id"] != w.ad.attach_id
    assert err(call(served, "remote.attach", {"attach_id": w.ad.attach_id})) == "invalid.input"


def test_an_adapter_cannot_touch_a_stream_it_did_not_open(human, served):
    w, c = live(human, served)
    other = "ab" * 16
    res = ok(
        call(served, "remote.attach", {"attach_id": w.ad.attach_id, "send": [{"stream_id": other, "bytes": "AAAA"}]})
    )
    assert res["data"]["frames"] and w.sess(other) is None  # unknown stream: dropped, nothing raised
    bad = {"attach_id": w.ad.attach_id, "send": [{"stream_id": c.sid, "bytes": "!!"}]}
    assert err(call(served, "remote.attach", bad)) == "invalid.input"  # not base64url


def test_adapter_close_ends_the_stream_with_its_reason(human, served):
    w, c = live(human, served)
    ok(
        call(
            served,
            "remote.attach",
            {"attach_id": w.ad.attach_id, "close": [{"stream_id": c.sid, "reason": "malformed"}]},
        )
    )
    w.pump()
    assert w.link.closes(c.sid) == ["malformed"]


def test_a_bad_frame_from_the_adapter_closes_malformed(human, served):
    w, c = live(human, served)
    bad = b64u(frame("BODY", 5, b"x"))  # the host never opened exchange 5, nor did the device
    ok(call(served, "remote.attach", {"attach_id": w.ad.attach_id, "send": [{"stream_id": c.sid, "bytes": bad}]}))
    w.pump()
    assert w.link.closes(c.sid) == ["malformed"]


def test_attach_wait_parks_until_a_frame_arrives(human, served):
    w, c = live(human, served)
    w.ad.take(10)

    class Call:
        service = w.service
        frame = {"args": {"wait_ms": 5000}}

    async def run():
        task = asyncio.ensure_future(remote.attach_wait(Call, {"data": {"attach_id": w.ad.attach_id, "frames": []}}))
        await asyncio.sleep(0.05)
        assert not task.done()
        await asyncio.get_running_loop().run_in_executor(None, c.say, head(1))
        return await asyncio.wait_for(task, 3)

    out = asyncio.run(run())
    assert [m["t"] for m in out["data"]["frames"]] == ["frame"]
    quiet = asyncio.run(
        remote.attach_wait(
            type("C", (), {"service": w.service, "frame": {"args": {"wait_ms": 50}}}),
            {"data": {"attach_id": w.ad.attach_id, "frames": []}},
        )
    )
    assert quiet["data"]["frames"] == []


def test_stream_facts_for_the_signer_and_closed_is_no_stream(human, served):
    w, c = live(human, served, kind="fido2")
    w.dev.count = 0
    c2 = w.client()
    c2.open_stream()
    c2.handshake()
    c2.say(
        dumps(
            {
                "t": "open",
                "api": [1, 3],
                "presence": w.dev.assertion(
                    __import__("tests.host.remote_port", fromlist=["x"]).open_bytes(w.ws, w.did, c2.hh, c2.challenge),
                    count=3,
                ),
            }
        )
    )
    d = ok(call(served, "remote.stream", {"stream_id": c2.sid}))["data"]
    assert d["workspace_id"] == w.ws and d["device_id"] == w.did and d["person"] == w.person and d["level"] == "work"
    assert (
        unb64u(d["hh"], 32) == c2.hh and d["presence_kind"] == "fido2" and unb64u(d["presence_pub"], 65) == w.dev.ppub
    )
    assert d["counter"] == 3
    assert err(call(served, "remote.stream", {"stream_id": "cd" * 16})) == "remote.no_stream"  # unknown
    ok(call(served, "remote.session_end", {"stream_id": c2.sid}))
    assert err(call(served, "remote.stream", {"stream_id": c2.sid})) == "remote.no_stream"  # closed


def test_stream_of_another_person_is_no_stream(human, served):
    w, c = live(human, served)
    w.sess(c.sid).person = "p_" + "9" * 32  # the stream belongs to another owner than the caller
    assert err(call(served, "remote.stream", {"stream_id": c.sid})) == "remote.no_stream"


def test_presence_used_is_kind_aware_and_shares_the_counter(human, served):
    w, c = live(human, served, kind="fido2")
    from tests.host import remote_port as RP

    d = w.client()
    d.open_stream()
    d.handshake()
    d.say(
        dumps(
            {
                "t": "open",
                "api": [1, 3],
                "presence": w.dev.assertion(RP.open_bytes(w.ws, w.did, d.hh, d.challenge), count=5),
            }
        )
    )
    arg = {"stream_id": d.sid, "device_id": w.did, "count": 6, "n": 2}
    assert ok(call(served, "remote.presence_used", arg))["data"] == {}
    assert counters.held(w.service, w.did) == 6
    assert err(call(served, "remote.presence_used", arg)) == "remote.presence_replay"  # not greater
    assert err(call(served, "remote.presence_used", {**arg, "count": 5})) == "remote.presence_replay"
    assert err(call(served, "remote.presence_used", {**arg, "device_id": "rd_" + "1" * 32})) == "remote.no_stream"
    assert err(call(served, "remote.presence_used", {**arg, "stream_id": "ef" * 16})) == "remote.no_stream"
    lines = w.log("remote.presence")
    assert [(e["stream_id"], e["device_id"], e["n"]) for e in lines] == [(d.sid, w.did, 2)]
    assert "sig" not in str(lines[0].get("n")) and set(lines[0]) >= {"stream_id", "device_id", "n"}
    # the open check reads the same counter: a new open at 6 is a replay
    e = w.client()
    e.open_stream()
    e.handshake()
    e.say(
        dumps(
            {
                "t": "open",
                "api": [1, 3],
                "presence": w.dev.assertion(RP.open_bytes(w.ws, w.did, e.hh, e.challenge), count=6),
            }
        )
    )
    assert w.link.closes(e.sid) == ["presence"]


def test_presence_used_for_a_platform_key_has_no_counter(human, served):
    w, c = live(human, served)
    arg = {"stream_id": c.sid, "device_id": w.did, "count": 0, "n": 1}
    for _ in range(2):  # no counter to compare: every batch is accepted and logged
        assert ok(call(served, "remote.presence_used", arg))["data"] == {}
    assert counters.held(w.service, w.did) == 0 and len(w.log("remote.presence")) == 2


def test_session_end_closes_and_logs(human, served):
    w, c = live(human, served)
    w.ad.take(10)
    assert ok(call(served, "remote.session_end", {"stream_id": c.sid}))["data"] == {"ended": True}
    assert w.ad.take(10) == [{"t": "closed", "stream_id": c.sid, "reason": "revoked"}]
    w.pump()
    assert w.link.closes(c.sid) == ["revoked"]
    (line,) = w.log("remote.session_closed")
    assert (line["stream_id"], line["device_id"], line["reason"]) == (c.sid, w.did, "revoked")
    assert err(call(served, "remote.session_end", {"stream_id": c.sid})) == "remote.no_stream"  # once
    assert len(w.log("remote.session_closed")) == 1


def test_remote_sessions_shows_who_and_since_when(human, served):
    w, c = live(human, served)
    (row,) = ok(call(served, "remote.sessions"))["data"]["sessions"]
    assert row["stream_id"] == c.sid and row["device_id"] == w.did and row["person"] == w.person
    assert row["level"] == "work" and row["name"].startswith("Laptop") and row["since_ms"] > 0


def test_wrk_never_leaves_host_in_ops_and_attach_frames(human, served):
    w, c = live(human, served)
    scalar = wrk.load(w.service)
    c.say(head(1))
    blob = repr(ok(call(served, "remote.attach", {"attach_id": w.ad.attach_id}))).encode()
    for op, a in (("remote.status", {}), ("remote.sessions", {}), ("remote.stream", {"stream_id": c.sid})):
        blob += repr(ok(call(served, op, a))).encode()
    for needle in (scalar, scalar.hex().encode(), b64u(scalar).encode()):
        assert needle not in blob
