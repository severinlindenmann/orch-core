"""Offset-driven uploads, with one signed request per attempt and bounded retries."""

import http.client
import json

import pytest

from orch.publishclient.client import PublishClient, PublishError, PublishResponse
from orch.publishclient.flow import put_file_parts
from tests.hub.test_publish_out import _signer


def accepted(received, done=False):
    return {"received": received, "done": done, "result": "stored"}


class Client:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.calls = []

    def request(self, method, target, body, **kwargs):
        assert method == "PUT" and kwargs == {"idempotent": False}
        self.calls.append((int(target.split("/")[-2]), body))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return PublishResponse(200, json.dumps(reply).encode())


def run(client, **kwargs):
    return put_file_parts(client, "a" * 32, "b" * 32, "f", b"abcdef", part_size=2, **kwargs)


def test_three_parts_and_byte_progress():
    c = Client(accepted(2), accepted(4), accepted(6, True))
    progress = []
    assert run(c, on_progress=lambda *p: progress.append(p))["done"]
    assert c.calls == [(0, b"ab"), (2, b"cd"), (4, b"ef")]
    assert progress == [(2, 6), (4, 6), (6, 6)]


@pytest.mark.parametrize("code", ["part_out_of_order", "part_conflict"])
def test_resume_from_error_offset(code):
    c = Client(PublishError(code, 409, received=3), accepted(5), accepted(6, True))
    run(c)
    assert c.calls == [(0, b"ab"), (3, b"de"), (5, b"f")]


def test_dropped_reply_uses_server_offset():
    c = Client(PublishError("network"), accepted(4), accepted(6, True))
    sleeps = []
    run(c, sleep=sleeps.append)
    assert c.calls == [(0, b"ab"), (0, b"ab"), (4, b"ef")]
    assert sleeps == [1]


@pytest.mark.parametrize("code", ["part_busy", "rate_limited", "network"])
def test_backoff_and_reset_after_accepted_part(code):
    c = Client(*[PublishError(code)] * 4, accepted(4), PublishError(code), accepted(6, True))
    sleeps = []
    run(c, sleep=sleeps.append)
    assert sleeps == [1, 2, 4, 8, 1]


def test_five_consecutive_failures_are_fatal():
    error = PublishError("rate_limited", 429)
    c = Client(*[error] * 5)
    sleeps = []
    with pytest.raises(PublishError) as exc:
        run(c, sleep=sleeps.append)
    assert exc.value is error
    assert len(c.calls) == 5 and sleeps == [1, 2, 4, 8]


@pytest.mark.parametrize("twice", [False, True])
def test_content_mismatch_restarts_only_once(twice):
    mismatch = PublishError("content_mismatch", 422, received=0)
    c = Client(accepted(2), mismatch, accepted(2), mismatch if twice else accepted(6, True))
    if twice:
        with pytest.raises(PublishError, match="content_mismatch"):
            run(c)
    else:
        run(c)
    assert [offset for offset, _ in c.calls] == [0, 2, 0, 2]


def test_repeated_conflict_at_same_request_offset_is_fatal():
    c = Client(*[PublishError("part_conflict", 409, received=0)] * 2)
    with pytest.raises(PublishError, match="part_conflict"):
        run(c)
    assert len(c.calls) == 2


@pytest.mark.parametrize("reply", [accepted(-1), accepted(7), accepted(True), accepted(2, True), {}])
def test_invalid_reply_fails_closed(reply):
    with pytest.raises(PublishError, match="bad_reply"):
        run(Client(reply))


@pytest.mark.parametrize("code,status", [("too_large", 413), ("gone", 410), ("share_committed", 409)])
def test_lifecycle_refusals_are_final(code, status):
    with pytest.raises(PublishError, match=code):
        run(Client(PublishError(code, status)))


def test_real_client_preserves_received_and_signs_every_attempt():
    calls = []
    replies = iter([(409, {"error": "part_out_of_order", "received": 4}), (200, accepted(6, True))])

    def transport(scheme, host, method, target, headers, body, timeout):
        calls.append((target, headers, body))
        status, reply = next(replies)
        return status, json.dumps(reply).encode()

    signer = _signer()
    client = PublishClient(signer, transport=transport)
    put_file_parts(client, signer.ctx.workspace_id, "b" * 32, "f", b"abcdef", part_size=2)
    assert [c[2] for c in calls] == [b"ab", b"ef"]
    assert calls[0][1]["Orch-Nonce"] != calls[1][1]["Orch-Nonce"]


@pytest.mark.parametrize("failure", [OSError("lost"), http.client.HTTPException("lost"), 502, 503, 504])
def test_real_transport_failures_count_once_per_attempt(failure):
    calls, sleeps = [], []

    def transport(scheme, host, method, target, headers, body, timeout):
        calls.append(headers)
        if isinstance(failure, Exception):
            raise failure
        return failure, b'{"error":"unavailable"}'

    signer = _signer()
    client = PublishClient(signer, transport=transport, sleep=lambda _: pytest.fail("nested retry"))
    with pytest.raises(PublishError) as exc:
        put_file_parts(client, signer.ctx.workspace_id, "b" * 32, "f", b"abcdef", part_size=2, sleep=sleeps.append)
    assert exc.value.code == ("network" if isinstance(failure, Exception) else "unavailable")
    assert len(calls) == 5
    assert len({h["Orch-Nonce"] for h in calls}) == 5
    assert sleeps == [1, 2, 4, 8]


def test_completed_offset_still_requires_done_and_probes_with_empty_part():
    c = Client(accepted(6), accepted(6, True))
    run(c)
    assert c.calls == [(0, b"ab"), (6, b"")]


def test_a_server_that_never_advances_cannot_keep_the_upload_running():
    class Stuck:
        calls = 0

        def request(self, method, target, body, idempotent=False):
            Stuck.calls += 1

            class R:
                status = 200

                @staticmethod
                def json():
                    return {"received": 0, "done": False, "result": "unchanged"}

            return R()

    with pytest.raises(PublishError) as e:
        put_file_parts(Stuck(), "a" * 32, "b" * 32, "f.txt", b"x" * 10, part_size=4, sleep=lambda s: None)
    assert e.value.code == "bad_reply"
    assert Stuck.calls <= 3 * (10 // 4 + 1) + 21
