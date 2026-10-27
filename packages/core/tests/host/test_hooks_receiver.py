"""The webhook doorbell receiver: always the same 204, body never used, optional HMAC (GitHub style)."""

import asyncio
import hashlib
import hmac
import os
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
    host = NS(state_dir=state, host_uid=os.getuid(), services={"w": NS(followups=runner)})
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
    rx.runner.known.add(fe.hook_hash(hid))
    answers = {
        post(rx, f"/h/{hid}", b'{"secret":"x"}'),
        post(rx, f"/h/{fe.new_hook_id()}"),
        post(rx, "/h/nonsense"),
        post(rx, f"/h/{hid}", method="GET"),
    }
    assert answers == {hooks.ANSWER}
    assert rx.runner.rings.count(fe.hook_hash(hid)) == 1  # the GET and the junk never rang


def test_a_second_ring_is_asked_and_dropped_by_the_runner(rx):
    hid = fe.new_hook_id()
    post(rx, f"/h/{hid}")
    post(rx, f"/h/{hid}")
    assert rx.runner.rings == [fe.hook_hash(hid)] * 2  # the runner decides (only the first is pending)


def test_the_body_never_reaches_the_runner(rx):
    hid = fe.new_hook_id()
    post(rx, f"/h/{hid}", b"A" * 100_000)
    assert rx.runner.rings == [fe.hook_hash(hid)]


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
    assert rx.runner.rings == [fe.hook_hash(hid)]
    assert "s3cret" not in " ".join(rx.runner.rings)
