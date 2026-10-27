"""The webhook doorbell receiver: always the same 204, body never used, optional HMAC (GitHub style)."""

import asyncio
import hashlib
import hmac
import os
import time
from types import SimpleNamespace as NS

import pytest

from orch.host import hooks
from orch.model import followup_events as fe


class Runner:
    def __init__(self):
        self.rings = []
        self.known = set()

    async def ring(self, hook_hash):
        self.rings.append(hook_hash)
        return hook_hash in self.known


@pytest.fixture
def rx(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    runner = Runner()
    host = NS(state_dir=state, host_uid=os.getuid(), services={"w": NS(followups=runner, workspace_id="w1")})
    receiver = hooks.HookReceiver(host, "127.0.0.1:0")
    loop = asyncio.new_event_loop()
    loop.run_until_complete(receiver.start())
    yield NS(loop=loop, receiver=receiver, runner=runner, host=host, state=state)
    loop.run_until_complete(receiver.close())
    loop.close()


def post(rx, path, body=b"", headers=None, method="POST"):
    async def go():
        r, w = await asyncio.open_connection("127.0.0.1", rx.receiver.port)
        head = f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n"
        head += "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items()) + "\r\n"
        w.write(head.encode() + body)
        await w.drain()
        data = await asyncio.wait_for(r.read(), 5)
        w.close()
        return data

    return rx.loop.run_until_complete(go())


def test_same_answer_for_known_unknown_and_junk(rx):
    hid = fe.new_hook_id()
    rx.runner.known.add(fe.hook_hash(hid, "w1"))
    answers = {
        post(rx, f"/h/{hid}", b'{"secret":"x"}'),
        post(rx, f"/h/{fe.new_hook_id()}"),
        post(rx, "/h/nonsense"),
        post(rx, f"/h/{hid}", method="GET"),
    }
    assert answers == {hooks.ANSWER}
    assert rx.runner.rings.count(fe.hook_hash(hid, "w1")) == 1  # the GET and the junk never rang


def test_a_second_ring_is_asked_and_dropped_by_the_runner(rx):
    hid = fe.new_hook_id()
    post(rx, f"/h/{hid}")
    post(rx, f"/h/{hid}")
    assert rx.runner.rings == [fe.hook_hash(hid, "w1")] * 2  # the runner decides (only the first is pending)


def test_the_body_never_reaches_the_runner(rx):
    hid = fe.new_hook_id()
    post(rx, f"/h/{hid}", b"A" * 100_000)
    assert rx.runner.rings == [fe.hook_hash(hid, "w1")]


def test_hmac_secret_is_required_when_set_and_kept_private(rx):
    hid = fe.new_hook_id()
    hooks.set_secret(rx.state, os.getuid(), hid, "s3cret")
    assert (hooks.secrets_path(rx.state).stat().st_mode & 0o777) == 0o600
    body = b'{"action":"completed"}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    bad = "sha256=" + hmac.new(b"other", body, hashlib.sha256).hexdigest()
    for headers in ({}, {"X-Hub-Signature-256": bad}):
        assert post(rx, f"/h/{hid}", body, headers) == hooks.ANSWER
    assert rx.runner.rings == []
    assert post(rx, f"/h/{hid}", body, {"X-Hub-Signature-256": good}) == hooks.ANSWER
    assert rx.runner.rings == [fe.hook_hash(hid, "w1")]
    assert "s3cret" not in " ".join(rx.runner.rings)


def test_b1_a_ring_never_starts_a_runner(rx):
    started = []

    async def start(service):
        started.append(service)

    rx.host._start_followups = start
    rx.host.services["cold"] = NS(followups=None, workspace_id="w2")
    assert post(rx, f"/h/{fe.new_hook_id()}") == hooks.ANSWER
    assert started == []


def test_b3_every_workspace_gets_the_ring_under_its_own_hash(rx):
    other = Runner()
    rx.host.services["w2"] = NS(followups=other, workspace_id="w2")
    hid = fe.new_hook_id()
    post(rx, f"/h/{hid}")
    assert rx.runner.rings == [fe.hook_hash(hid, "w1")]
    assert other.rings == [fe.hook_hash(hid, "w2")]  # not the same hash, and the first one did not stop the loop


def test_b2_excess_connections_get_the_same_answer_at_once(rx, monkeypatch):
    monkeypatch.setattr(rx.receiver, "_slots", asyncio.Semaphore(2))

    async def go():
        stuck = [await asyncio.open_connection("127.0.0.1", rx.receiver.port) for _ in range(2)]
        for _, w in stuck:
            w.write(b"POST /h/")  # an unfinished head holds a slot
            await w.drain()
        await asyncio.sleep(0.2)
        r, w = await asyncio.open_connection("127.0.0.1", rx.receiver.port)
        data = await asyncio.wait_for(r.read(), 2)
        for _, sw in stuck:
            sw.close()
        return data

    assert rx.loop.run_until_complete(go()) == hooks.ANSWER


def test_b2_a_wedged_runner_cannot_hold_a_request(rx, monkeypatch):
    monkeypatch.setattr(hooks, "RING_TIMEOUT", 0.3)

    async def hang(hook_hash):
        await asyncio.sleep(60)

    rx.runner.ring = hang
    t0 = time.monotonic()
    assert post(rx, f"/h/{fe.new_hook_id()}") == hooks.ANSWER
    assert time.monotonic() - t0 < 3


def test_unreadable_secret_file_fails_closed(rx):
    hid = fe.new_hook_id()
    hooks.set_secret(rx.state, os.getuid(), hid, "s3cret")
    hooks.secrets_path(rx.state).chmod(0o644)  # group/other readable: custody refuses it
    assert post(rx, f"/h/{hid}") == hooks.ANSWER
    assert rx.runner.rings == [], "an unreadable secret file must not disable the signature check"


def test_non_ascii_signature_header_is_just_a_bad_signature(rx):
    hid = fe.new_hook_id()
    hooks.set_secret(rx.state, os.getuid(), hid, "s3cret")
    assert post(rx, f"/h/{hid}", b"x", {"X-Hub-Signature-256": "sha256=\u00e9"}) == hooks.ANSWER
    assert rx.runner.rings == []


def test_b4_the_default_hook_url_is_what_was_bound():
    import shutil
    import tempfile
    from pathlib import Path

    from orch.host.service import Host

    tmp_path = Path(tempfile.mkdtemp(prefix=".tb4-", dir=Path.home()))  # short path: unix socket limit

    async def go():
        host = Host(tmp_path / "s", tmp_path / "r", hook_listen=":0")
        await host.start()
        try:
            return host.hook_url, host.hooks.port
        finally:
            await host.close()

    (tmp_path / "s").mkdir(mode=0o700)
    (tmp_path / "r").mkdir(mode=0o711)
    (tmp_path / "r").chmod(0o711)
    try:
        url, port = asyncio.run(go())
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)
    assert url == f"http://127.0.0.1:{port}" and port != 0


def test_the_op_journal_never_keeps_a_raw_hook_id():
    from orch.host.outcomes import _mask_hooks

    hid = fe.new_hook_id()
    masked = _mask_hooks({"on": f"webhook:{hid}", "in": "3d", "n": 3})
    assert hid not in str(masked) and masked["on"] == "webhook:hk_…" and masked["n"] == 3
