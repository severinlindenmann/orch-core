"""HttpPublishSync against a scripted loopback server: request shape and status/answer mapping."""

from __future__ import annotations

import json

import pytest
from publish_helpers import FakeUpstream

from orch_hub.publish import HttpPublishSync, PublishSyncError

WS = "11" * 16
OBJ = {"o": {"kind": "hub.binding"}, "sig": "x"}


@pytest.fixture
def up():
    u = FakeUpstream()
    u.body = b'{"ok":true}'
    yield u
    u.close()


def test_requests_are_puts_of_the_canonical_object(up):
    s = HttpPublishSync(up.url)
    s.put_hub_card(OBJ)
    s.put_op_revoked(OBJ)
    s.put_binding(WS, OBJ)
    assert [(r["method"], r["target"]) for r in up.requests] == [
        ("PUT", "/v2/hub/card"),
        ("PUT", "/v2/hub/op-revoked"),
        ("PUT", f"/v2/hub/bindings/{WS}"),
    ]
    assert all(json.loads(r["body"]) == OBJ for r in up.requests)


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (403, b'{"error":"not_bound"}', "not_bound"),
        (409, b'{"error":"unbound"}', "unbound"),
        (401, b'{"error":"bad_signature"}', "bad_signature"),
        (400, b'{"error":"made_up_code"}', "unavailable"),
        (400, b'{"error":"malformed","detail":"x"}', "unavailable"),
        (413, b'{"error":"too_large"}', "unavailable"),
        (500, b'{"error":"internal"}', "unavailable"),
        (503, b'{"error":"unavailable"}', "unavailable"),
        (404, b"<html>", "unavailable"),
        (200, b'{"ok":false}', "unavailable"),
        (200, b"nope", "unavailable"),
        (302, b"", "unavailable"),
    ],
)
def test_answers_map_to_codes(up, status, body, code):
    up.status, up.body = status, body
    if status == 302:
        up.headers = {"Location": "http://127.0.0.1:1/"}
    with pytest.raises(PublishSyncError) as x:
        HttpPublishSync(up.url).put_hub_card(OBJ)
    assert x.value.code == code
    assert len(up.requests) == 1  # no redirect followed, no retry


def test_connection_refused_and_timeout_are_unavailable(up):
    with pytest.raises(PublishSyncError) as x:
        HttpPublishSync("http://127.0.0.1:1", timeout_s=1).put_hub_card(OBJ)
    assert x.value.code == "unavailable"
    up.delay = 2.5
    with pytest.raises(PublishSyncError) as x:
        HttpPublishSync(up.url, timeout_s=1).put_hub_card(OBJ)
    assert x.value.code == "unavailable"


def test_workspace_id_is_validated_before_it_reaches_the_path(up):
    for bad in ("../x", "11" * 15, "AB" * 16, "11" * 16 + "/x", 5):
        with pytest.raises(PublishSyncError) as x:
            HttpPublishSync(up.url).put_binding(bad, OBJ)
        assert x.value.code == "malformed"
    assert up.requests == []


def test_card_is_put_to_the_workspace_route_and_ids_are_checked(up):
    s = HttpPublishSync(up.url)
    s.put_card(WS, OBJ, "pkb64")
    (r,) = up.requests
    assert (r["method"], r["target"]) == ("PUT", f"/v2/hub/cards/{WS}")
    assert json.loads(r["body"]) == {"card": OBJ, "pk_pub": "pkb64"}
    with pytest.raises(PublishSyncError):
        s.put_card("../x", OBJ, "pkb64")
    up.status, up.body = 401, b'{"error":"unknown_workspace"}'
    with pytest.raises(PublishSyncError) as x:
        s.put_card(WS, OBJ, "pkb64")
    assert x.value.code == "unknown_workspace"
