"""The remote flag of the dashboard command: preflight, the workspace key, the host loop through a fake transport child
(tests/fake_sharing.py speaks the bridge-host pipe protocol), the heartbeat, the start-up listing and the guard rule
for the sharing CLI's bridge commands. No network, no TIX, no dashboard server: ASGI in memory and a fake child."""
import asyncio
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("fastapi")

import test_bridge_host_vectors as V  # noqa: E402
from orch.dashboard import bridge_loop  # noqa: E402
from orch.dashboard.app import create_app, dashboard_routes  # noqa: E402
from orch.dashboard.bridge_loop import HostLoop, route_hook  # noqa: E402
from orch.remote import presence, remote_start  # noqa: E402
from orch.remote.bridge_host import envelope as E, files, keys, signatures  # noqa: E402
from orch.remote.bridge_host.host_check import Host  # noqa: E402
from orch.remote.bridge_host.registry import Device, Registry  # noqa: E402

FAKE = str(Path(__file__).with_name("fake_sharing.py"))
WS_HEX = V.VEC["keys"]["workspace"]
WS = bytes.fromhex(WS_HEX)
HOST_KEY = signatures.private_key(bytes.fromhex(V.VEC["keys"]["host"]["d"]))
KEY_A = signatures.private_key(bytes.fromhex(V.VEC["keys"]["device_a"]["d"]))
KEY_B = signatures.private_key(bytes.fromhex(V.VEC["keys"]["device_b"]["d"]))
TOKEN = "tok-remote-1f2e3d"
FORM = {"content-type": "application/x-www-form-urlencoded"}


def now_ms():
    return time.time_ns() // 1_000_000


def did(k):
    return keys.device_id(WS, signatures.public_bytes(k)).hex()


# -- the device side, built from the library's primitives and the shared vectors' fake keys -------------------------

class Device_:
    def __init__(self, key):
        self.key, self.seq = key, 0

    def envelope(self, meta, data=b"", *, stream_flag=False, stream=E.ZERO_ID):
        self.seq += 1
        h = E.Header(E.TO_HOST, E.F_STREAM if stream_flag else 0, WS, bytes.fromhex(did(self.key)), os.urandom(16),
                     stream, self.seq, now_ms(), os.urandom(16))
        hb = h.encode()
        body = keys.seal(V.K_WS, hb, E.frame(meta, data))
        return hb + body + signatures.sign(self.key, signatures.signed_bytes(hb, body))


def http(method, path, headers=None):
    return {"op": "http", "method": method, "path": path, "headers": headers or {}}


def open_chunk(raw: bytes):
    """What a device accepts: the pinned host key's signature, then the tag (spec §7)."""
    hb, body, sig = E.split(raw)
    assert signatures.verify(signatures.public_bytes(HOST_KEY), sig, signatures.signed_bytes(hb, body))
    h = E.Header.decode(hb)
    assert h.direction == E.TO_DEVICE
    meta, data = E.unframe(keys.open_sealed(V.K_WS, hb, body))
    return h, meta, data


# -- the fake child's directory ------------------------------------------------------------------------------------

class Fake:
    def __init__(self, path: Path):
        self.dir = path
        (path / "queue").mkdir(parents=True, exist_ok=True)
        self.n = 0

    def _put(self, obj):
        self.n += 1
        (self.dir / "queue" / f"{self.n:05d}.json").write_text(json.dumps(obj), encoding="utf-8")

    def request(self, env: bytes) -> str:
        rid = E.Header.decode(env).rid.hex()
        self._put({"rid": rid, "body": E.b64u(env)})
        return rid

    def inject(self, op, **kw):
        self._put({"op": op, **kw})

    def log(self):
        p = self.dir / "log.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def ops(self, op):
        return [x for x in self.log() if x.get("op") == op]

    def chunks(self, rid):
        """The opened response chunks for `rid`, in the order they were posted."""
        out = []
        for x in self.ops("respond"):
            if x["rid"] == rid:
                h, meta, data = open_chunk(E.unb64u(x["body"]))
                assert (h.seq, bool(h.flags & E.F_LAST)) == (x["idx"], x["last"])  # the mailbox fields match
                out.append((h, meta, data))
        return out


async def until(pred, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = Fake(tmp_path / "fake")
    monkeypatch.setenv("FAKE_SHARING_DIR", str(f.dir))
    return f


def make_host(per_device=None, scope_a="operate", scope_b="look", ws=None):
    from orch.dashboard.launch import config_dir
    host = Host(workspace=WS, k_ws=V.K_WS, host_key=HOST_KEY, root=files.bridge_dir(config_dir(), WS_HEX),
                clock=now_ms, route=route_hook(dashboard_routes(), ws), phone_key=lambda pid: None, rp_id=V.RP_ID,
                origin=V.ORIGIN, per_device=per_device)
    if not host.registry.devices():
        host.registry.add(Device(did(KEY_A), signatures.public_bytes(KEY_A), scope_a, "Laptop", 0), 0)
        host.registry.add(Device(did(KEY_B), signatures.public_bytes(KEY_B), scope_b, "Phone", 0), 0)
    return host


def make_loop(ws, host, said=None, app=None, **kw):
    app = app or create_app(ws, TOKEN)
    args = dict(say=(said if said is not None else []).append, poll_wait=1, heartbeat_s=0.05, keepalive_s=0.3,
                frame_s=0.05, beat=lambda ws: {"sessions": 0, "in_progress": 0, "needs_you": 0, "factory": "none"})
    args.update(kw)
    loop = HostLoop(app, host, [sys.executable, FAKE, "bridge-host", "--workspace", WS_HEX], ws, **args)
    _LOOPS.append(loop)
    return loop


_LOOPS: list = []  # every loop a test built, stopped by arun whatever happened
_RUNNER: list = [None]  # arun's own task, which the leak checks leave out


@pytest.fixture(autouse=True)
def _forget_loops():
    yield
    _LOOPS.clear()
TEST_TIMEOUT = 30.0  # seconds per test body: a regression fails fast instead of stalling CI


def arun(main, timeout: float = TEST_TIMEOUT):
    """asyncio.run for these tests: the body gets `timeout` seconds, and afterwards (passed, failed or timed out)
    every loop it built is stopped and its fake child killed, so a failing test never hangs the suite."""
    async def guarded():
        _RUNNER[0] = asyncio.current_task()
        try:
            return await asyncio.wait_for(main, timeout)
        finally:
            loops, _LOOPS[:] = list(_LOOPS), []
            for loop in loops:
                try:
                    await asyncio.wait_for(loop.stop(), 10)
                except BaseException:  # noqa: BLE001 - clean-up goes on whatever the stop did
                    pass
                proc = loop.child.proc
                if proc is not None and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            for t in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
                t.cancel()
    return asyncio.run(guarded())


def no_backoff(loop, delays):
    async def fake_sleep(s):
        delays.append(s)
        await asyncio.sleep(0)
    loop._sleep = fake_sleep  # noqa: SLF001 - the loop's backoff sleep


async def started(loop):
    task = asyncio.ensure_future(loop.run())
    await until(lambda: loop.link.status()["state"] in ("online", "error", "stopped"))
    return task


async def finish(loop, task):
    await loop.stop()
    await asyncio.wait_for(task, 10)
    left = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and t is not _RUNNER[0]]
    assert not left, left  # no task leaks


# -- requests through the loop -------------------------------------------------------------------------------------

def test_a_page_at_look_comes_back_sealed_and_signed(ws, fake, put):
    put("backlog")
    host, b = make_host(), Device_(KEY_B)

    async def main():
        loop = make_loop(ws, host)
        task = await started(loop)
        assert loop.link.status()["state"] == "online" and loop.link.status()["host_online"] is True
        rid = fake.request(b.envelope(http("GET", "/", {"accept": "text/html"})))
        await until(lambda: any(x[0].flags & E.F_LAST for x in fake.chunks(rid)))
        chunks = fake.chunks(rid)
        assert chunks[0][1]["status"] == 200 and b"<html" in b"".join(c[2] for c in chunks)
        assert chunks[0][1]["page"] is True  # the device draws only a page the host tags (found by the end-to-end run)
        assert not any(c[0].flags & E.F_REFUSAL for c in chunks)
        assert chunks[0][1]["headers"]["content-type"].startswith("text/html")  # a mapping, not a list of pairs
        assert all("set-cookie" not in k.lower() for k in chunks[0][1]["headers"])
        await finish(loop, task)
    arun(main())


def test_a_device_below_the_routes_scope_gets_a_sealed_refusal_and_nothing_runs(ws, fake, put):
    tid = put("backlog")
    host, b = make_host(), Device_(KEY_B)  # b holds Look; a comment needs Operate

    async def main():
        loop = make_loop(ws, host)
        task = await started(loop)
        rid = fake.request(b.envelope(http("POST", f"/t/{tid}/comment", FORM), b"text=hello-from-phone"))
        await until(lambda: fake.chunks(rid))
        (h, meta, _), = fake.chunks(rid)
        assert h.flags & E.F_REFUSAL and meta == {"refusal": "forbidden_scope"}
        await finish(loop, task)
    arun(main())
    from orch.core import store
    assert "hello-from-phone" not in store.resolve(ws, tid).path.read_text()


def test_a_replay_is_answered_from_the_record_and_never_runs_again(ws, fake, put):
    from orch.core import store
    tid = put("backlog")
    host, a = make_host(), Device_(KEY_A)

    async def main():
        loop = make_loop(ws, host)
        task = await started(loop)
        env = a.envelope(http("POST", f"/t/{tid}/comment", FORM), b"text=only-once")
        rid = fake.request(env)
        await until(lambda: fake.chunks(rid))
        first = fake.chunks(rid)
        fake.request(env)  # the same bytes again (a retry)
        await until(lambda: len(fake.chunks(rid)) == 2 * len(first))
        again = fake.chunks(rid)[len(first):]
        assert [m for _, m, _ in again] == [m for _, m, _ in first] and first[0][1]["status"] == 303
        await finish(loop, task)
    arun(main())
    text = store.resolve(ws, tid).path.read_text()
    assert text.count("only-once") == 1


def test_a_revocation_between_the_decision_and_the_run_runs_nothing(ws, fake, put):
    """still_authorized() is asked immediately before the app runs: a device revoked after authorize() (elsewhere)
    gets the stored refusal and the change never happens."""
    from orch.core import store
    tid = put("backlog")
    host, a = make_host(), Device_(KEY_A)
    real = host.authorize

    def authorize_then_revoke(acc):
        out = real(acc)
        Registry(host.root, WS).revoke(did(KEY_A), now_ms())
        return out
    host.authorize = authorize_then_revoke

    async def main():
        loop = make_loop(ws, host)
        task = await started(loop)
        rid = fake.request(a.envelope(http("POST", f"/t/{tid}/comment", FORM), b"text=never-written"))
        await until(lambda: fake.chunks(rid))
        (h, meta, _), = fake.chunks(rid)
        assert h.flags & E.F_REFUSAL and meta == {"refusal": "revoked"}
        await finish(loop, task)
    arun(main())
    assert "never-written" not in store.resolve(ws, tid).path.read_text()


def test_when_finish_says_no_the_stored_refusal_is_sent_instead_of_the_result(ws, fake):
    host, b = make_host(), Device_(KEY_B)
    real = host.finish

    def revoke_then_finish(rids, head, body=b""):
        host.revoke(did(KEY_B))  # revoked while the run was going: its record is refused
        return real(rids, head, body)
    host.finish = revoke_then_finish

    async def main():
        loop = make_loop(ws, host)
        task = await started(loop)
        rid = fake.request(b.envelope(http("GET", "/palette.json")))
        await until(lambda: fake.chunks(rid))
        (h, meta, data), = fake.chunks(rid)
        assert h.flags & E.F_REFUSAL and meta == {"refusal": "revoked"} and data == b""
        await finish(loop, task)
    arun(main())


def test_a_large_response_is_split_into_chunks_of_at_most_256_kib(ws):
    host = make_host()
    loop = make_loop(ws, host)
    a = Device_(KEY_A)
    e = a.envelope(http("GET", "/"))
    header = E.Header.decode(e)
    body = os.urandom(700 * 1024)
    chunks = loop._chunks(header, {"status": 200, "headers": [["content-type", "x/y"]]}, body, stream=False)  # noqa: SLF001
    assert len(chunks) == 3 and all(len(c) <= E.MAX_CHUNK for c in chunks)
    opened = [open_chunk(c) for c in chunks]
    assert [h.seq for h, _, _ in opened] == [0, 1, 2] and [bool(h.flags & E.F_LAST) for h, _, _ in opened] == \
        [False, False, True]
    assert b"".join(d for _, _, d in opened) == body and opened[0][1]["status"] == 200 and opened[1][1] == {}


def _stream(dev, path="/events"):
    return dev.envelope(http("GET", path, {"accept": "text/event-stream"}), stream_flag=True)


def test_revocation_mid_stream_ends_it_with_the_stored_refusal(ws, fake):
    host, a = make_host(), Device_(KEY_A)

    async def main():
        loop = make_loop(ws, host, keepalive_s=0.2)
        task = await started(loop)
        rid = fake.request(_stream(a))
        await until(lambda: len(fake.chunks(rid)) >= 2)  # the head and the first frame
        assert all(h.flags & E.F_STREAM for h, _, _ in fake.chunks(rid))
        # revoked elsewhere (another process's registry): the next frame check sees it
        Registry(host.root, WS).revoke(did(KEY_A), now_ms())
        await until(lambda: fake.chunks(rid)[-1][0].flags & E.F_LAST)
        h, meta, _ = fake.chunks(rid)[-1]
        assert h.flags & E.F_REFUSAL and h.flags & E.F_STREAM and meta == {"refusal": "revoked"}
        await until(lambda: rid not in host.streams and rid not in loop._streams)  # noqa: SLF001 - closed on both
        await finish(loop, task)
    arun(main())


def test_revoke_through_the_host_closes_the_stream_at_once(ws, fake):
    host, a = make_host(), Device_(KEY_A)

    async def main():
        loop = make_loop(ws, host, keepalive_s=60)  # no keepalive would come in time: the close does it
        task = await started(loop)
        rid = fake.request(_stream(a))
        await until(lambda: len(fake.chunks(rid)) >= 2)
        loop.close_streams(host.revoke(did(KEY_A)), "revoked")
        await until(lambda: fake.chunks(rid)[-1][0].flags & E.F_LAST, timeout=3)
        assert fake.chunks(rid)[-1][1] == {"refusal": "revoked"}
        await finish(loop, task)
    arun(main())


def test_the_kill_switch_closes_streams_says_goodbye_and_keeps_nothing_running(ws, fake):
    host, a = make_host(), Device_(KEY_A)

    async def main():
        loop = make_loop(ws, host, keepalive_s=60)
        task = await started(loop)
        rid = fake.request(_stream(a))
        await until(lambda: len(fake.chunks(rid)) >= 2)
        loop.link.disconnect()
        await until(lambda: loop.link.status()["state"] == "stopped")
        assert fake.chunks(rid)[-1][1] == {"refusal": "stopped"}
        ops = [x.get("op") for x in fake.log() if "op" in x]
        assert ops.index("goodbye") < ops.index("release")
        assert fake.log()[-1] == {"eof": True}
        assert loop.link.status()["host_online"] is False
        # after it, nothing runs: a request that still arrives is refused `stopped`
        e = a.envelope(http("GET", "/"))
        v = host.authorize(host.check(e, E.Header.decode(e).rid.hex()))
        assert (v.result, v.code) == ("refuse", "stopped")
        await asyncio.wait_for(task, 10)
        await finish(loop, task)
    arun(main())


def test_the_request_store_quota_answers_busy(ws, fake):
    host, b = make_host(per_device=2), Device_(KEY_B)

    async def main():
        loop = make_loop(ws, host)
        task = await started(loop)
        rids = [fake.request(b.envelope(http("GET", "/palette.json"))) for _ in range(3)]
        await until(lambda: all(fake.chunks(r) for r in rids))
        assert [fake.chunks(r)[-1][1].get("refusal") for r in rids].count("busy") == 1
        await finish(loop, task)
    arun(main())


def test_more_streams_than_the_device_cap_are_refused_busy(ws, fake, monkeypatch):
    monkeypatch.setattr(bridge_loop, "MAX_STREAMS", 1)
    host, a = make_host(), Device_(KEY_A)

    async def main():
        loop = make_loop(ws, host, keepalive_s=60)
        task = await started(loop)
        one = fake.request(_stream(a))
        await until(lambda: len(fake.chunks(one)) >= 2)
        two = fake.request(_stream(a))
        await until(lambda: fake.chunks(two))
        (h, meta, _), = fake.chunks(two)
        assert meta == {"refusal": "busy"} and h.flags & E.F_STREAM and h.flags & E.F_REFUSAL
        await finish(loop, task)
    arun(main())


def test_a_device_cancel_ends_its_stream(ws, fake):
    host, a = make_host(), Device_(KEY_A)

    async def main():
        loop = make_loop(ws, host, keepalive_s=60)
        task = await started(loop)
        rid = fake.request(_stream(a))
        await until(lambda: len(fake.chunks(rid)) >= 2)
        c = fake.request(a.envelope({"op": "cancel"}, stream=bytes.fromhex(rid)))
        await until(lambda: fake.chunks(c) and fake.chunks(rid)[-1][0].flags & E.F_LAST)
        assert fake.chunks(c)[0][1] == {"cancelled": rid}
        h, meta, _ = fake.chunks(rid)[-1]
        assert meta == {} and not h.flags & E.F_REFUSAL
        await finish(loop, task)
    arun(main())


@pytest.mark.parametrize("code,word", [("host_taken", "--take-over"), ("lease_lost", "took this workspace over"),
                                       ("unauthorized", "Approve it again"), ("pending", "not approved"),
                                       ("not_owner", "owner")])
def test_a_fatal_mailbox_answer_stops_the_link_with_its_message(ws, fake, code, word):
    host, said = make_host(), []
    fake.inject("poll", code=code)

    async def main():
        loop = make_loop(ws, host, said)
        task = await started(loop)
        await asyncio.wait_for(task, 10)
        st = loop.link.status()
        assert (st["state"], st["last_error"], st["host_online"]) == ("error", code, False)
        assert any(word in s for s in said), said
        assert not fake.ops("goodbye")  # another host is live, or TIX refused us: no goodbye in its name
        await finish(loop, task)
    arun(main())


def test_retryable_errors_back_off_from_one_second_to_a_ten_second_cap(ws, fake):
    host, delays = make_host(), []
    for _ in range(6):
        fake.inject("poll", code="rate_limited")

    async def main():
        loop = make_loop(ws, host)
        no_backoff(loop, delays)
        task = asyncio.ensure_future(loop.run())
        await until(lambda: loop.link.status()["state"] == "online")
        assert delays == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]
        assert loop.link.status()["last_error"] is None
        await finish(loop, task)
    arun(main())


def test_a_response_chunk_refused_for_rate_is_retried(ws, fake):
    host, b, delays = make_host(), Device_(KEY_B), []
    fake.inject("respond", code="server")
    fake.inject("respond", code="rate_limited")

    async def main():
        loop = make_loop(ws, host)
        no_backoff(loop, delays)
        task = await started(loop)
        rid = fake.request(b.envelope(http("GET", "/palette.json")))
        await until(lambda: len(fake.ops("respond")) == 3)  # two refused, then taken
        assert delays == [1.0, 2.0] and {x["rid"] for x in fake.ops("respond")} == {rid}
        await finish(loop, task)
    arun(main())


def test_the_transport_child_dying_stops_the_link(ws, fake):
    host, said = make_host(), []
    fake.inject("poll", exit=1)

    async def main():
        loop = make_loop(ws, host, said)
        task = await started(loop)
        await asyncio.wait_for(task, 10)
        assert loop.link.status()["last_error"] == "transport_exited"
        await finish(loop, task)
    arun(main())


def test_stream_frames_are_coalesced_to_the_latest_at_most_every_frame_interval(ws, fake):
    """A gated app whose stream produces 30 frames at once: only the latest goes out, one per interval."""
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse
    from orch.dashboard.remote_gate import RemoteGate

    app = FastAPI()
    app.state.token = TOKEN

    @app.get("/events")
    async def events():
        async def gen():
            for i in range(30):
                yield f"data: {i}\n\n"
            await asyncio.sleep(30)
        return StreamingResponse(gen(), media_type="text/event-stream")
    app.add_middleware(RemoteGate, routes=app.routes, ws=ws)
    host, a = make_host(), Device_(KEY_A)

    async def main():
        loop = make_loop(ws, host, app=app, frame_s=0.3, keepalive_s=60)
        task = await started(loop)
        rid = fake.request(_stream(a))
        await until(lambda: any(d == b"data: 29\n\n" for _, _, d in fake.chunks(rid)), timeout=5)
        frames = [d for _, m, d in fake.chunks(rid)[1:] if d]
        assert len(frames) < 5 and frames[-1] == b"data: 29\n\n"
        await finish(loop, task)
    arun(main())


def test_an_idle_stream_gets_keepalives(ws, fake):
    host, a = make_host(), Device_(KEY_A)

    async def main():
        loop = make_loop(ws, host, keepalive_s=0.1)
        task = await started(loop)
        rid = fake.request(_stream(a))
        await until(lambda: sum(1 for _, m, _ in fake.chunks(rid) if m == {"keepalive": True}) >= 2)
        await finish(loop, task)
        assert fake.chunks(rid)[-1][1] == {"refusal": "stopped"}  # a shutdown ends it `stopped`
    arun(main())


# -- F: the factory-epic rule, end to end ----------------------------------------------------------------------------

def test_an_operate_device_cannot_change_things_under_a_running_factory_epic(configure, agent, human, fake):
    """A decision asks for a fresh assertion (nothing ran); an edit needs Type, so an Operate device is refused."""
    from conftest import human_ops
    from orch.core import store
    from orch.core.ops import Ops
    fws = configure(factory={"enabled": True})
    a_ops, h = Ops(fws, agent), human_ops(fws, human)
    epic = a_ops.new("Revamp", type="epic")
    a_ops.set_section(epic.id, "Requirements", "r")
    a_ops.set_section(epic.id, "Acceptance criteria", "- [ ] a")
    h.approve(epic.id, "requirements", delegate={"factory": True})
    child = a_ops.new("child", epic=epic.id)
    before = store.resolve(fws, child.id).path.read_text()
    host, dev = make_host(scope_a="operate", ws=fws), Device_(KEY_A)

    async def main():
        loop = make_loop(fws, host, app=create_app(fws, TOKEN))
        task = await started(loop)
        cases = (("approve", b"gate=requirements&seen=x", "forbidden_scope"), ("move", b"to=backlog", "forbidden_scope"),
                 ("edit", b"text=changed", "forbidden_scope"))
        rids = [fake.request(dev.envelope(http("POST", f"/t/{child.id}/{action}", FORM), body))
                for action, body, _ in cases]
        await until(lambda: all(fake.chunks(r) and fake.chunks(r)[-1][0].flags & E.F_LAST for r in rids))
        # the device has no authenticator: it is asked, cannot answer, and nothing runs
        assert [fake.chunks(r)[0][1].get("refusal") for r in rids] == [c[2] for c in cases]
        await finish(loop, task)
    arun(main())
    assert store.resolve(fws, child.id).path.read_text() == before


# -- the library rule the loop leans on: end_run --------------------------------------------------------------------

def _run(host, dev):
    e = dev.envelope(http("GET", "/"))
    r = host.authorize(host.check(e, E.Header.decode(e).rid.hex()))
    assert r.result == "run"
    return e, r


def test_end_run_stores_its_refusal_first_and_a_retry_gets_it(ws):
    host, a = make_host(), Device_(KEY_A)
    e, r = _run(host, a)
    v = host.end_run(r, "busy")
    assert (v.result, v.code, v.header) == ("refuse", "busy", r.header)
    again = host.check(e, r.rid)  # the same bytes again: the stored refusal, never a run
    assert (again.result, again.code) == ("refuse", "busy")


def test_end_run_keeps_a_stored_refusal(ws):
    host, a = make_host(), Device_(KEY_A)
    _, r = _run(host, a)
    host.revoke(did(KEY_A))  # marks the running record refused
    assert host.end_run(r, "busy").code == "revoked"


def test_end_run_defaults(ws):
    host, a = make_host(), Device_(KEY_A)
    _, r1 = _run(host, a)
    Registry(host.root, WS).set_scope(did(KEY_A), "look", now_ms())  # changed elsewhere: nothing stored for r1
    assert host.end_run(r1).code == "scope_changed"
    _, r2 = _run(host, a)
    Registry(host.root, WS).revoke(did(KEY_A), now_ms())
    assert host.end_run(r2).code == "revoked"
    host2, b = make_host(), Device_(KEY_B)
    _, r3 = _run(host2, b)
    host2.stop()
    assert host2.end_run(r3).code == "stopped"
    host2.finish(r3.answer_rids, {"status": 200}, b"")  # an outcome already finished is never replaced
    assert host2.store.get(r3.rid, now_ms()).outcome == {"refusal": "stopped"}


# -- the heartbeat, derived from the real data ----------------------------------------------------------------------

def test_the_heartbeat_counts_come_from_the_workspace(ws, put, monkeypatch):
    from orch.core import query
    from orch.dashboard import terminals
    put("in-progress")
    put("in-progress")
    put("backlog")
    put("testing")  # waits for your verdict: a needs-you item, so a hard-coded 0 cannot pass
    put("testing")
    monkeypatch.setattr(terminals, "sessions", lambda w: ["one", "two", "three"])
    beat = presence.heartbeat(ws)
    needs = query.counts(query.waiting(ws))["blocking"]
    assert needs >= 2
    assert beat == {"sessions": 3, "in_progress": 2, "needs_you": needs, "factory": "none"}
    assert set(beat) <= {"sessions", "in_progress", "needs_you", "factory", "children_done", "children_total",
                         "budget_pct"}
    assert len(json.dumps(beat)) <= 512 and all(type(v) is int for k, v in beat.items() if k != "factory")


def test_the_heartbeat_reports_a_running_factory_epic_and_its_children(configure, agent, human):
    from conftest import human_ops
    from orch.core.ops import Ops
    fws = configure(factory={"enabled": True})
    a, h = Ops(fws, agent), human_ops(fws, human)
    epic = a.new("Revamp", type="epic")
    a.set_section(epic.id, "Requirements", "r")
    a.set_section(epic.id, "Acceptance criteria", "- [ ] a")
    h.approve(epic.id, "requirements", delegate={"factory": True, "max_children": 4})
    a.new("one", epic=epic.id)
    a.new("two", epic=epic.id)
    beat = presence.heartbeat(fws)
    assert beat["factory"] == "running"
    assert (beat["children_done"], beat["children_total"]) == (0, 2)
    assert 0 <= beat["budget_pct"] <= 999
    assert presence.heartbeat(configure(factory={"enabled": False}))["factory"] == "none"


def test_the_loop_sends_heartbeats_with_those_fields(ws, fake):
    host = make_host()

    async def main():
        loop = make_loop(ws, host, beat=presence.heartbeat)
        task = await started(loop)
        await until(lambda: len(fake.ops("heartbeat")) >= 2)
        hb = fake.ops("heartbeat")[0]
        assert {k: v for k, v in hb.items() if k not in ("id", "op")} == presence.heartbeat(ws)
        await finish(loop, task)
    arun(main())


# -- the lifespan: started beside the runner, goodbye on shutdown, nothing left behind -------------------------------

def test_the_app_runs_the_loop_in_its_lifespan_and_says_goodbye_on_shutdown(ws, fake):
    host = make_host()
    box = {}

    def factory(app):
        box["loop"] = make_loop(ws, host, app=app)
        return box["loop"]
    app = create_app(ws, TOKEN, remote=factory)
    assert app.state.bridge_host is host and app.state.bridge_link is box["loop"].link

    async def main():
        async with app.router.lifespan_context(app):
            await until(lambda: app.state.bridge_link.status()["state"] == "online")
        assert app.state.bridge_link.status()["state"] == "stopped"
        ops = [x.get("op") for x in fake.log() if "op" in x]
        assert ops[-2:] == ["goodbye", "release"] and fake.log()[-1] == {"eof": True}
        left = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and t is not _RUNNER[0]]
        assert not left, left
    arun(main())


def test_without_the_remote_flag_the_app_has_no_bridge(ws):
    app = create_app(ws, TOKEN)
    assert app.state.bridge_host is None and app.state.bridge_loop is None
    assert app.state.bridge_link.status() == {"state": "off", "since": None, "last_error": None, "host_online": False}


def test_a_local_dashboard_does_not_import_the_bridge(tmp_path):
    root = tmp_path / "ws"
    (root / "orchestrator").mkdir(parents=True)
    (root / "orchestrator" / "config.json").write_text(json.dumps(
        {"schema": 1, "customer": "acme", "id": {"prefix": "L", "pad": 4}}), encoding="utf-8")
    code = (
        "import sys, uvicorn, orch.actor\n"
        "from orch import cli\n"
        "orch.actor.require_human_terminal = lambda *a, **k: None\n"
        "uvicorn.Server.run = lambda self, sockets=None: [s.close() for s in sockets]\n"
        "assert cli.run(['serve', '--no-open', '--no-update']) == 0\n"
        # the Remote tab reads the records' file helpers (no crypto, no state) for its read-only view; nothing else
        "ok = {'orch.remote.bridge_host', 'orch.remote.bridge_host.files'}\n"
        "bad = [m for m in sys.modules if m.startswith(('orch.remote.bridge_host', 'orch.dashboard.bridge_loop',"
        " 'orch.remote.transport', 'orch.remote.remote_start', 'orch.remote.presence')) and m not in ok]\n"
        "assert not bad, bad\n")
    env = {**os.environ, "ORCH_STATE_DIR": str(tmp_path / "state"), "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
           "CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}
    for var in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "ORCH_HOME", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(var, None)
    r = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr


# -- preflight, the key and the command line -----------------------------------------------------------------------

def _wrapper(tmp_path) -> str:
    """An executable sharing CLI that runs the fake."""
    p = tmp_path / "bin" / "sharing"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{FAKE}' \"$@\"\n", encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


@pytest.fixture
def ready(ws, fake, tmp_path, monkeypatch):
    """Everything the preflight wants, with the fake sharing CLI: returns a function that breaks one piece."""
    from types import SimpleNamespace
    from orch.addons import discovery, userfiles
    sharing = _wrapper(tmp_path)
    state = {"enabled": True, "trusted": True, "sharing": sharing}
    found = SimpleNamespace(name="orch-tix", error=None, manifest=SimpleNamespace(
        remote_humans=True, setting_binaries=lambda: ("sharing_path",)))
    unrelated = SimpleNamespace(name="wiki", error=None, manifest=SimpleNamespace(
        remote_humans=False, setting_binaries=lambda: ()))
    monkeypatch.setattr(userfiles, "workspace_addons", lambda root: {
        "orch-tix": {"enabled": state["enabled"], "config": {"sharing_path": state["sharing"]}},
        "wiki": {"enabled": True, "config": {}}})
    monkeypatch.setattr(discovery, "discover", lambda: [unrelated, found])
    monkeypatch.setattr(userfiles, "trust_state", lambda f: "trusted" if state["trusted"] else "untrusted")
    (fake.dir / "space.json").write_text(json.dumps({"space_id": WS_HEX, "owner": True,
                                                    "server": "https://tix.example"}))
    (fake.dir / "key").write_text(V.K_WS.hex())
    return state


def _missing(ws):
    with pytest.raises(remote_start.RemoteNotReady) as e:
        remote_start.preflight(ws)
    return e.value


def test_preflight_passes_when_everything_is_there(ws, ready):
    assert remote_start.preflight(ws) == {"tool": ready["sharing"], "space": WS_HEX,
                                          "server": "https://tix.example", "warnings": []}


@pytest.mark.parametrize("brk,expect", [
    ("enabled", "the relay addon enabled in this workspace: enable orch-tix"), ("trusted", "the orch-tix addon trusted"),
    ("sharing", "its sharing_path setting"), ("pending", "approved on the relay"), ("revoked", "approved on the relay"),
    ("no_space", "a space on the relay"), ("not_owner", "the owner"), ("network", "a reachable relay server"),
    ("cryptography", "cryptography"), ("config", "config directory"),
])
def test_preflight_names_each_missing_piece_with_its_fix(ws, ready, fake, monkeypatch, tmp_path, brk, expect):
    from orch.remote import bridge_host
    if brk in ("enabled", "trusted"):
        ready[brk] = False
    elif brk == "sharing":
        ready["sharing"] = str(tmp_path / "nowhere" / "sharing")
    elif brk in ("pending", "revoked", "no_space", "network"):
        (fake.dir / "space.json").write_text(json.dumps({"error": {"revoked": "unauthenticated"}.get(brk, brk),
                                                        "exit": 3}))
    elif brk == "not_owner":
        (fake.dir / "space.json").write_text(json.dumps({"space_id": WS_HEX, "owner": False,
                                                        "server": "https://tix.example"}))
    elif brk == "cryptography":
        monkeypatch.setattr(bridge_host, "available", lambda: False)
    elif brk == "config":
        monkeypatch.setenv("ORCH_STATE_DIR", str(ws.root / ".orch-state"))  # inside the workspace
    err = _missing(ws)
    assert err.exit_code == 8 and expect in err.hint, err.hint
    assert err.hint.count("\n") + 1 == int(err.message.split(": ")[1].split()[0])  # the count matches the list


@pytest.mark.parametrize("server,ok", [
    ("http://localhost:8123", True), ("http://127.0.0.1:8123", True), ("http://[::1]:8123", True),
    ("http://tix.example", False), ("http://localhost.evil.example", False), ("ftp://localhost", False),
    ("https://tix.example", True), ("http://localhost:99999", False),
    ("http://127.0.0.1.evil.com", False), ("http://localhost.evil.com:8123", False), ("http://evil.com#@localhost", False),
    ("http://localhost@evil.com", False), ("http://127.1", False), ("http://[::2]:8123", False),
    ("http://0.0.0.0:8123", False), ("HTTP://LOCALHOST:8123", True)])
def test_preflight_takes_http_only_for_this_machine(ws, ready, fake, server, ok):
    (fake.dir / "space.json").write_text(json.dumps({"space_id": WS_HEX, "owner": True, "server": server}))
    if ok:
        assert remote_start.preflight(ws)["server"] == server
    else:
        assert "the relay server address" in _missing(ws).hint


def test_preflight_lists_everything_missing_at_once(ws, ready, monkeypatch, tmp_path):
    from orch.remote import bridge_host
    ready["trusted"] = False
    ready["sharing"] = str(tmp_path / "nowhere")
    monkeypatch.setattr(bridge_host, "available", lambda: False)
    err = _missing(ws)
    assert "4 thing(s)" in err.message and err.hint.count("\n- ") == 3
    for what in ("cryptography", "trusted", "sharing_path", "not checked"):
        assert what in err.hint


def test_preflight_wants_exactly_one_relay_addon(ws, ready, monkeypatch):
    from types import SimpleNamespace
    from orch.addons import discovery, userfiles
    two = [SimpleNamespace(name=n, error=None, manifest=SimpleNamespace(remote_humans=True,
                                                                         setting_binaries=lambda: ("p",)))
           for n in ("relay-a", "relay-b")]
    monkeypatch.setattr(discovery, "discover", lambda: two)
    monkeypatch.setattr(userfiles, "workspace_addons", lambda root: {n.name: {"enabled": True} for n in two})
    assert "disable all but one of relay-a, relay-b" in _missing(ws).hint
    monkeypatch.setattr(discovery, "discover", lambda: [])
    assert "install the relay addon" in _missing(ws).hint


def test_the_key_is_read_from_a_pipe_and_never_written_or_logged(ws, ready, fake, caplog, tmp_path):
    import logging
    caplog.set_level(logging.DEBUG)
    lines = []
    remote = remote_start.prepare(ws, out=lines.append)
    assert remote.host.k_ws == V.K_WS
    assert remote.child_argv == [ready["sharing"], "bridge-host", "--workspace", WS_HEX]
    assert fake.log()[-1] == {"argv": ["bridge-key", "--workspace", WS_HEX]}
    from orch.dashboard.launch import config_dir
    secret_hex, secret = V.K_WS.hex().encode(), V.K_WS
    for base in (config_dir(), ws.root):
        for p in Path(base).rglob("*"):
            if p.is_file() and fake.dir not in p.parents:
                data = p.read_bytes()
                assert secret_hex not in data and secret not in data and secret_hex.upper() not in data, p
    assert V.K_WS.hex() not in caplog.text and all(V.K_WS.hex() not in x for x in lines)
    assert "--take-over" in remote_start.prepare(ws, take_over=True, out=lines.append).child_argv


@pytest.mark.parametrize("code,word", [(3, "not approved"), (6, "own the space"), (2, "does not exist")])
def test_a_refused_key_names_why_and_never_echoes_the_output(ws, ready, fake, code, word):
    (fake.dir / "key-exit").write_text(str(code))
    with pytest.raises(remote_start.RemoteNotReady) as e:
        remote_start.prepare(ws, out=lambda s: None)
    assert word in e.value.hint


def test_the_start_up_listing_shows_devices_and_the_changes_since_the_last_start(ws, ready):
    first = []
    remote = remote_start.prepare(ws, out=first.append)
    remote.host.registry.add(Device(did(KEY_A), signatures.public_bytes(KEY_A), "operate", "Laptop", now_ms()),
                             now_ms())
    second = []
    remote_start.prepare(ws, out=second.append)
    text = "\n".join(second)
    assert "1 paired device(s)" in text and "Laptop  scope operate" in text
    assert "1 registry change(s) since the last start" in text and "added" in text
    third = []
    remote_start.prepare(ws, out=third.append)
    assert "no registry changes since the last start" in "\n".join(third)
    audit = Path(remote.host.root) / "audit.jsonl"  # an earlier entry rewritten: shown, not hidden
    audit.write_text(audit.read_text().replace("operate", "type"))
    fourth = []
    remote_start.prepare(ws, out=fourth.append)
    assert "changed since the last start" in "\n".join(fourth)


@pytest.fixture
def served(monkeypatch, ws_root):
    import uvicorn
    import orch.actor
    from orch import cli
    monkeypatch.setattr(orch.actor, "require_human_terminal", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_workspace", None)
    box = {}

    def fake_run(self, sockets=None):
        box["app"] = self.config.app
        for s in sockets:
            s.close()
    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    return lambda *args: cli.run(["serve", "--no-open", "--no-update", *args]), box


def test_remote_and_lan_together_are_refused(served, capsys):
    go, box = served
    assert go("--remote", "--lan") == 2 and "app" not in box
    assert "cannot be combined" in capsys.readouterr().err
    assert go("--take-over") == 2


def test_a_failed_preflight_refuses_before_anything_binds(served, ws, monkeypatch, capsys):
    go, box = served
    assert go("--remote") == 8 and "app" not in box
    err = capsys.readouterr().err
    assert "the remote bridge cannot start" in err and "the relay addon enabled" in err


def test_remote_serve_builds_the_app_with_the_bridge(served, ws, ready, capsys):
    go, box = served
    assert go("--remote") == 0
    app = box["app"]
    assert app.state.bridge_host is not None and app.state.bridge_link.status()["state"] == "off"
    out = capsys.readouterr().out
    assert "paired device(s)" in out and V.K_WS.hex() not in out


def test_remote_serve_refuses_a_bind_off_this_machine(served, configure):
    go, box = served
    configure(dashboard={"host": "0.0.0.0"})
    assert go("--remote") == 2 and "app" not in box


# -- E: the guard keeps agents away from the sharing CLI's bridge commands ------------------------------------------

@pytest.mark.parametrize("cmd", [
    "sharing bridge-key --workspace " + WS_HEX, "/opt/tix/sharing bridge-host --workspace " + WS_HEX,
    "~/.claude/skills/sharing/sharing.py bridge-key --workspace x | cat", "K=$(sharing bridge-key --workspace x)",
    "python3 .claude/skills/sharing/sharing.py bridge-host --workspace x", "sharing 'bridge-key' --workspace x",
    "sharing bri\"dge-k\"ey --workspace x", "sharing $'bridge\\x2dkey' --workspace x", "S=bridge-key; sharing $S",
    "bash -c 'sharing bridge-host'", "echo x | sharing BRIDGE-HOST", "sharing bridge-key\n",
    "sharing bridge-key --allow-terminal --workspace x",
    # an expansion or a glob that the shell turns into the name (re-checked after a review)
    "sharing bri${x}dge-key", "sharing bri$()dge-key", "sharing bridge-${A:-key}", "sharing bri`true`dge-host",
    "sharing bridge-*", "sharing bridge-?ey", "sharing bridge-[k]ey", "sharing b*-key", "sharing *-host",
    "uv run sharing bridge-key", "python3 .claude/skills/sharing/sharing.py bridge-key", "sharing bridge\\\n-key",
    "echo `sharing bridge-key`", "x " * 50000 + "; sharing bridge-key",
])
def test_the_guard_refuses_the_bridge_commands(ws, cmd):
    from orch.hooks.guard import evaluate
    d = evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(ws.root)})
    assert not d.allow and "bridge" in d.reason, cmd


@pytest.mark.parametrize("cmd", [
    "uv run pytest -q tests/test_bridge_host.py", "rg -n bridge_host src",
    "sharing whoami --json", "sharing space show --json", "cat docs/bridge-protocol.md", "ls src/orch/remote",
    "git log --oneline", "cat tests/fake_bridge_hosts.txt",
])
def test_the_guard_still_allows_neighbours_of_the_bridge_commands(ws, cmd):
    from orch.hooks.guard import evaluate
    d = evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(ws.root)})
    assert d.allow, (cmd, d.reason)


def test_the_bridge_commands_are_never_grantable(ws):
    from orch.core import permits
    assert permits.never_grantable(ws, "sharing bridge-key --workspace " + WS_HEX)
    assert permits.never_grantable(ws, "sharing bridge-host --workspace " + WS_HEX)


# -- review follow-ups: the windows the dispatcher's own checks do not cover -----------------------------------------

def _deny_from(host, n):
    """host.still_authorized answers yes for its first n-1 calls and no from call n: the device is "revoked" right
    after the dispatcher's own checks, inside the loop's window before sealing."""
    real, calls = host.still_authorized, []

    def fake(run):
        calls.append(1)
        return len(calls) < n and real(run)
    host.still_authorized = fake
    return calls


def test_a_stream_head_is_checked_again_before_it_is_sealed(ws, fake):
    host, a = make_host(), Device_(KEY_A)
    _deny_from(host, 3)  # 1: before running, 2: before the Start event (dispatcher), 3: the loop's own head check

    async def main():
        loop = make_loop(ws, host, keepalive_s=60)
        task = await started(loop)
        rid = fake.request(_stream(a))
        await until(lambda: fake.chunks(rid))
        (h, meta, _), = fake.chunks(rid)  # only the refusal: no head went out
        assert h.flags & E.F_REFUSAL and h.flags & E.F_STREAM and "refusal" in meta
        await finish(loop, task)
    arun(main())


def test_a_stream_frame_the_app_produced_before_a_revocation_is_not_sent(ws, fake):
    host, a = make_host(), Device_(KEY_A)
    _deny_from(host, 5)  # 3: head (loop), 4: the first body event (dispatcher), 5: the loop's check before its frame

    async def main():
        loop = make_loop(ws, host, keepalive_s=60)
        task = await started(loop)
        rid = fake.request(_stream(a))
        await until(lambda: fake.chunks(rid) and fake.chunks(rid)[-1][0].flags & E.F_LAST)
        chunks = fake.chunks(rid)
        assert len(chunks) == 2 and "status" in chunks[0][1]  # the head, then the refusal: the frame never left
        assert chunks[1][0].flags & E.F_REFUSAL and chunks[1][2] == b""
        await finish(loop, task)
    arun(main())


def _big_app(ws, size):
    from fastapi import FastAPI
    from fastapi.responses import Response
    from orch.dashboard.remote_gate import RemoteGate
    app = FastAPI()
    app.state.token = TOKEN

    @app.get("/palette.json")
    async def big():
        return Response(b"x" * size, media_type="application/octet-stream")
    app.add_middleware(RemoteGate, routes=app.routes, ws=ws)
    return app


def test_a_page_too_large_to_store_is_checked_once_more_before_it_is_sent(ws, fake):
    from orch.dashboard.bridge_dispatch import Limits
    host, b = make_host(), Device_(KEY_B)
    _deny_from(host, 4)  # 1: run, 2: Start, 3: the one body event, 4: the loop's check for an unstored body

    async def main():
        loop = make_loop(ws, host, app=_big_app(ws, 100 * 1024), limits=Limits(max_chunk=1 << 20))
        task = await started(loop)
        rid = fake.request(b.envelope(http("GET", "/palette.json")))
        await until(lambda: fake.chunks(rid))
        (h, meta, data), = fake.chunks(rid)
        assert h.flags & E.F_REFUSAL and "refusal" in meta and data == b""
        await finish(loop, task)
    arun(main())


def test_a_page_too_large_to_store_is_sent_while_still_authorised(ws, fake):
    from orch.dashboard.bridge_dispatch import Limits
    host, b = make_host(), Device_(KEY_B)

    async def main():
        loop = make_loop(ws, host, app=_big_app(ws, 100 * 1024), limits=Limits(max_chunk=1 << 20))
        task = await started(loop)
        rid = fake.request(b.envelope(http("GET", "/palette.json")))
        await until(lambda: fake.chunks(rid) and fake.chunks(rid)[-1][0].flags & E.F_LAST)
        assert len(b"".join(d for _, _, d in fake.chunks(rid))) == 100 * 1024
        await finish(loop, task)
    arun(main())


def test_an_envelope_delivered_under_another_mailbox_id_is_dropped(ws, fake):
    host, b = make_host(), Device_(KEY_B)

    async def main():
        loop = make_loop(ws, host)
        task = await started(loop)
        env = b.envelope(http("GET", "/palette.json"))
        other = os.urandom(16).hex()
        fake._put({"rid": other, "body": E.b64u(env)})  # noqa: SLF001 - the child claims another id for these bytes
        ok = fake.request(b.envelope(http("GET", "/palette.json")))  # a proper one after it: the loop is alive
        await until(lambda: fake.chunks(ok))
        await asyncio.sleep(0.2)
        assert {x["rid"] for x in fake.ops("respond")} == {ok}  # nothing answered for either id of the first
        await finish(loop, task)
    arun(main())


def test_the_origin_scope_comes_from_the_decision_not_from_the_request(ws, fake, put):
    """A Decide device that writes "scope": "type" (and "fresh") into its meta gets the gate's answer for Decide."""
    from orch.dashboard.remote_gate import FRESH, NO_WAY
    tid = put("backlog")
    host, a = make_host(scope_a="decide"), Device_(KEY_A)

    async def main():
        loop = make_loop(ws, host)
        task = await started(loop)
        meta = {**http("POST", f"/t/{tid}/approve", FORM), "scope": "type", "fresh": True, "label": "x"}
        rid = fake.request(a.envelope(meta, b"gate=requirements&seen=x&factory=1"))  # arming: Type and fresh
        await until(lambda: fake.chunks(rid) and fake.chunks(rid)[-1][0].flags & E.F_LAST)
        body = b"".join(d for _, _, d in fake.chunks(rid))
        assert fake.chunks(rid)[0][1]["status"] == 403 and NO_WAY.encode() in body and FRESH.encode() not in body
        await finish(loop, task)
    arun(main())


def test_end_run_never_replaces_a_finished_outcome(ws):
    host, a = make_host(), Device_(KEY_A)
    e, r = _run(host, a)
    assert host.finish(r.answer_rids, {"status": 200, "headers": []}, b"done-once") is True
    host.end_run(r, "busy")
    assert host.store.get(r.rid, now_ms()).outcome == {"status": 200, "headers": []}
    again = host.check(e, r.rid)
    assert (again.result, again.body) == ("replay", b"done-once")


def test_a_failing_key_tool_never_shows_its_output(ws, ready, fake):
    (fake.dir / "key-exit").write_text("1")  # it prints the key, then fails
    with pytest.raises(remote_start.RemoteNotReady) as e:
        remote_start.prepare(ws, out=lambda s: None)
    text = e.value.message + (e.value.hint or "")
    assert V.K_WS.hex() not in text and V.K_WS.hex()[:16] not in text and "code 1" in text


def test_a_key_that_is_not_64_hex_characters_is_refused(ws, ready, fake):
    (fake.dir / "key").write_text("zz" * 32)
    with pytest.raises(remote_start.RemoteNotReady):
        remote_start.prepare(ws, out=lambda s: None)
    (fake.dir / "key").write_text(V.K_WS.hex()[:62])
    with pytest.raises(remote_start.RemoteNotReady):
        remote_start.prepare(ws, out=lambda s: None)


def test_a_relative_tool_path_is_refused(ws, ready):
    rel = Path(_wrapper(ws.root)).relative_to(ws.root)  # an executable at bin/sharing below the cwd
    ready["sharing"] = str(rel)
    assert "its sharing_path setting" in _missing(ws).hint


def test_a_tool_inside_the_workspace_is_warned_about(ws, ready):
    lines = []
    ready["sharing"] = _wrapper(ws.root)
    remote_start.prepare(ws, out=lines.append)
    assert any("inside this workspace" in s for s in lines)
    ready["sharing"] = _wrapper(ws.root.parent / "elsewhere")
    lines.clear()
    remote_start.prepare(ws, out=lines.append)
    assert not any("inside this workspace" in s for s in lines)


@pytest.mark.parametrize("broken", ["sessions", "in_progress", "needs_you", "factory"])
def test_a_heartbeat_field_that_cannot_be_read_fails_the_beat(configure, broken, monkeypatch):
    from orch.core import ledger, query, store
    from orch.dashboard import terminals
    fws = configure(factory={"enabled": True})

    def boom(*a, **k):
        raise OSError("unreadable")
    target = {"sessions": (terminals, "sessions"), "in_progress": (store, "scan"), "needs_you": (query, "waiting"),
              "factory": (ledger, "entries")}[broken]
    monkeypatch.setattr(*target, boom)
    with pytest.raises(OSError):
        presence.heartbeat(fws)


def test_an_epic_that_cannot_be_read_fails_the_beat_instead_of_reading_none(configure, agent, human, monkeypatch):
    from conftest import human_ops
    from orch.core import store
    from orch.core.ops import Ops
    fws = configure(factory={"enabled": True})
    a, h = Ops(fws, agent), human_ops(fws, human)
    epic = a.new("Revamp", type="epic")
    a.set_section(epic.id, "Requirements", "r")
    a.set_section(epic.id, "Acceptance criteria", "- [ ] a")
    h.approve(epic.id, "requirements", delegate={"factory": True})

    real, epic_path = store.read_ticket, store.resolve(fws, epic.id).path

    def boom(path, *a, **k):  # only the epic itself cannot be read
        if Path(path) == Path(epic_path):
            raise OSError("unreadable")
        return real(path, *a, **k)
    monkeypatch.setattr(store, "read_ticket", boom)
    with pytest.raises(OSError):
        presence.factory(fws)


def test_a_malformed_mailbox_id_is_ignored_before_anything_is_checked(ws):
    host, a = make_host(), Device_(KEY_A)
    loop = make_loop(ws, host)
    seen = []
    host.check = lambda env, rid: seen.append(rid)
    env = a.envelope(http("GET", "/"))
    rid = E.Header.decode(env).rid.hex()
    for bad in (rid.upper(), rid + "\n", rid[:-2], 7, None):
        asyncio.run(loop._handle({"rid": bad, "body": E.b64u(env)}))  # noqa: SLF001
    asyncio.run(loop._handle({"rid": rid, "body": 5}))  # noqa: SLF001
    assert seen == []


@pytest.mark.parametrize("action,code", [("revoke", "revoked"), ("scope", "scope_changed")])
def test_the_remote_tab_closes_the_devices_streams(ws, action, code, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    host, closed = make_host(), []
    stream = "ab" * 16
    host.streams[stream] = did(KEY_B)

    class Loop:
        def close_streams(self, rids, c):
            closed.append((list(rids), c))
    app = create_app(ws, TOKEN)
    app.state.bridge_host, app.state.bridge_loop = host, Loop()
    client = TestClient(app)
    client.get(f"/?token={TOKEN}")
    path = f"/workspace/remote/devices/{did(KEY_B)}/{action}"
    client.post(path, data={"scope": "decide"} if action == "scope" else {}, headers={"origin": "http://testserver"},
                follow_redirects=False)
    assert closed == [([stream], code)]


def test_close_streams_from_another_thread_ends_the_stream(ws, fake):
    import threading
    host, a = make_host(), Device_(KEY_A)

    async def main():
        loop = make_loop(ws, host, keepalive_s=60)
        task = await started(loop)
        rid = fake.request(_stream(a))
        await until(lambda: len(fake.chunks(rid)) >= 2)
        t = threading.Thread(target=loop.close_streams, args=(host.revoke(did(KEY_A)), "revoked"))
        t.start()
        t.join()
        await until(lambda: fake.chunks(rid)[-1][0].flags & E.F_LAST, timeout=3)
        assert fake.chunks(rid)[-1][1] == {"refusal": "revoked"}
        await finish(loop, task)
    arun(main())


def test_an_unknown_error_code_from_the_child_reads_as_protocol(fake):
    from orch.remote.transport import Child, TransportError
    fake.inject("poll", code="something_new")

    async def main():
        child = Child([sys.executable, FAKE, "bridge-host", "--workspace", WS_HEX], fake.dir)
        await child.start()
        with pytest.raises(TransportError) as e:
            await child.call("poll", wait=0)
        assert e.value.code == "protocol"
        fake.inject("poll", code="rate_limited")
        with pytest.raises(TransportError) as e:
            await child.call("poll", wait=0)
        assert e.value.code == "rate_limited"
        await child.close()
    arun(main())


def test_a_failed_beat_sends_nothing_and_the_next_one_goes_out(ws, fake):
    host, beats = make_host(), []

    def beat(w):
        beats.append(1)
        if len(beats) == 1:
            raise OSError("unreadable")
        return {"sessions": 0, "in_progress": 0, "needs_you": 1, "factory": "none"}

    async def main():
        loop = make_loop(ws, host, beat=beat)
        task = await started(loop)
        await until(lambda: fake.ops("heartbeat"))
        assert len(beats) >= 2 and fake.ops("heartbeat")[0]["needs_you"] == 1
        await finish(loop, task)
    arun(main())


def test_a_crash_in_the_loop_shows_as_an_error_and_leaves_nothing_running(ws, fake):
    host, said = make_host(), []

    async def main():
        loop = make_loop(ws, host, said)

        async def broken():
            raise RuntimeError("bug")
        loop._polling = broken  # noqa: SLF001
        task = asyncio.ensure_future(loop.run())
        await asyncio.wait_for(task, 10)
        st = loop.link.status()
        assert (st["state"], st["last_error"], st["host_online"]) == ("error", "loop_crashed", False)
        assert any("failed" in s for s in said) and fake.log()[-1] == {"eof": True}
        await finish(loop, task)
    arun(main())


def test_close_streams_from_a_worker_thread_runs_on_the_loops_thread(ws, fake):
    """What the Remote tab does (its routes run in worker threads): the stream table is only ever touched on the
    loop's own thread, and the stream still ends at once."""
    import threading
    host, a = make_host(), Device_(KEY_A)

    async def main():
        loop = make_loop(ws, host, keepalive_s=60)
        threads, real = [], loop._close_streams  # noqa: SLF001

        def spy(rids, code):
            threads.append(threading.get_ident())
            return real(rids, code)
        loop._close_streams = spy  # noqa: SLF001
        task = await started(loop)
        rid = fake.request(_stream(a))
        await until(lambda: len(fake.chunks(rid)) >= 2)
        ended = host.revoke(did(KEY_A))
        await asyncio.to_thread(loop.close_streams, ended, "revoked")  # the loop keeps running meanwhile
        await until(lambda: fake.chunks(rid)[-1][0].flags & E.F_LAST, timeout=3)
        assert fake.chunks(rid)[-1][1] == {"refusal": "revoked"}
        assert threads and set(threads) == {threading.get_ident()}  # this coroutine runs on the loop's thread
        await finish(loop, task)
    arun(main())


@pytest.mark.parametrize("method,arg", [("close_stream", "ab" * 16), ("end_lease", "cd" * 16)])
def test_stream_and_lease_changes_wait_for_the_host_lock(ws, method, arg):
    """close_stream and end_lease change the tables revoke and set_scope iterate under the host lock: they take it too
    (a Remote tab revoke on another thread holds it while it walks the streams)."""
    import threading
    host = make_host()
    host.streams[arg], host.leases[arg] = "x", 1
    held, release, done = threading.Event(), threading.Event(), threading.Event()

    def holder():
        with host._lock:  # noqa: SLF001 - what Host.revoke holds while it walks the tables
            held.set()
            release.wait(5)
    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    worker = threading.Thread(target=lambda: (getattr(host, method)(arg), done.set()))
    worker.start()
    try:
        assert not done.wait(0.3)  # still waiting for the lock
        assert arg in (host.streams if method == "close_stream" else host.leases)
    finally:
        release.set()
        t.join()
        worker.join(5)
    assert done.is_set() and arg not in (host.streams if method == "close_stream" else host.leases)


def test_revoke_from_a_worker_thread_never_races_the_loops_stream_changes(ws):
    """The Remote tab revokes and rescopes on a worker thread while the loop opens and closes streams: the host lock
    covers both, so neither side ever sees a table changing under it."""
    import sys as _sys
    import threading
    host, a = make_host(scope_a="look"), Device_(KEY_A)
    errors, stop = [], threading.Event()

    def hammer():
        i = 0
        while not stop.is_set():
            try:
                k = signatures.private_key((1000 + i).to_bytes(32, "big"))
                d = host.registry.add(Device(did(k), signatures.public_bytes(k), "look", "x", 0), now_ms())
                host.set_scope(d.id, "decide")
                host.revoke(d.id)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            i += 1
    old = _sys.getswitchinterval()
    _sys.setswitchinterval(1e-6)
    t = threading.Thread(target=hammer)
    t.start()
    try:
        open_rids = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            e = a.envelope(http("GET", "/events"), stream_flag=True)
            v = host.check(e, E.Header.decode(e).rid.hex())
            if v.result == "accept":
                open_rids.append(v.rid)
            if len(open_rids) > 50:
                for r in open_rids[:25]:
                    host.close_stream(r)
                    host.end_lease(did(KEY_A))
                del open_rids[:25]
    except Exception as e:  # noqa: BLE001
        errors.append(e)
    finally:
        _sys.setswitchinterval(old)
        stop.set()
        t.join()
    assert not errors, errors[:3]
