"""D11: the forwarder adds only the transport signature, and refuses anything that is not the exact request shape."""

from __future__ import annotations

import pytest
from publish_helpers import (
    HOST,
    NOW,
    FakeUpstream,
    RecordingSync,
    binding,
    host_request,
    make_hub,
    new_wsk,
    publish_state,
    unbound,
)

from orch_hub.contract import hub as H
from orch_hub.contract.canon import b64u
from orch_hub.contract.hub import Refused
from orch_hub.publish import HubPublish, PublishConfig, PublishSyncError

WS = "11" * 16


@pytest.fixture
def up():
    u = FakeUpstream()
    yield u
    u.close()


@pytest.fixture
def env(tmp_path, up):
    hub = make_hub(tmp_path)
    wsk = new_wsk()
    sync = RecordingSync()
    fw = HubPublish(hub, sync, PublishConfig(up.url, HOST, timeout_s=2.0))
    b = binding(wsk, hub, WS)
    fw.binding(WS, b)
    return type("E", (), {"hub": hub, "wsk": wsk, "sync": sync, "fw": fw, "b": b, "up": up})()


def req(e, **kw):
    return host_request(e.wsk, e.hub.hub_id, WS, **kw)


def test_forward_adds_exactly_two_headers_and_verifies_as_orch_publish(env):
    r = req(env, method="PUT", target=f"/v1/ws/{WS}/files/a", body=b"hello")
    out = env.fw.forward(WS, r)
    assert out == {"status": 200, "body_hex": b'{"ok":true}'.hex()}
    (seen,) = env.up.requests
    assert (seen["method"], seen["target"], seen["body"]) == ("PUT", f"/v1/ws/{WS}/files/a", b"hello")
    got = {k.lower(): v for k, v in seen["headers"].items() if k.lower().startswith("orch-")}
    assert set(got) == {k.lower() for k in r["headers"]} | {"orch-hub", "orch-hub-signature"}
    for k, v in r["headers"].items():
        assert got[k.lower()] == v  # the host's headers byte for byte
    assert got["orch-hub"] == env.hub.hub_id
    st = publish_state(env.hub, WS, env.wsk, env.b)
    res = H.check_hub_publish(
        {"method": "PUT", "host": HOST, "target": seen["target"], "headers": got, "body": seen["body"]},
        st,
        now_ms=NOW,
        dev_mode=True,
    )
    assert res["workspace"] == WS and res["hub"] == env.hub.hub_id


def test_card_and_binding_are_pushed_before_the_first_forward_and_only_once(env):
    assert [c[0] for c in env.sync.calls] == ["card", "binding"]
    env.fw.forward(WS, req(env))
    env.fw.forward(WS, req(env))
    assert [c[0] for c in env.sync.calls] == ["card", "binding"]


def test_approval_is_forwarded_and_covered_by_the_hub_path_bytes(env):
    approval = {"o": {"kind": "hub.publish_approval"}, "sig": "x"}
    r = req(env, approval=approval)
    env.fw.forward(WS, r)
    (seen,) = env.up.requests
    assert seen["headers"]["Orch-Owner-Approval"] == r["headers"]["Orch-Owner-Approval"]


def test_unbound_object_stops_forwarding_locally(env):
    env.fw.binding(WS, unbound(env.wsk, env.hub, WS))
    with pytest.raises(Refused) as x:
        env.fw.forward(WS, req(env))
    assert x.value.code == "not_bound" and env.up.requests == []
    assert env.sync.calls[-1][0] == "binding" and env.sync.calls[-1][2]["o"]["kind"] == "hub.unbound"


def test_no_binding_known_is_not_bound(tmp_path, up):
    hub = make_hub(tmp_path)
    fw = HubPublish(hub, RecordingSync(), PublishConfig(up.url, HOST))
    with pytest.raises(Refused) as x:
        fw.forward(WS, host_request(new_wsk(), hub.hub_id, WS))
    assert x.value.code == "not_bound"


@pytest.mark.parametrize(
    "mut",
    [
        lambda r: r.update(extra=1),
        lambda r: r.pop("body_hex"),
        lambda r: r.update(method="get"),
        lambda r: r.update(method="CONNECT"),
        lambda r: r.update(host="Example.com"),
        lambda r: r.update(host="evil.example:80"),  # not the configured api_host
        lambda r: r.update(target="v1/x"),
        lambda r: r.update(target="/v1/x y"),
        lambda r: r.update(target="/v1/x\r\nHost: a"),
        lambda r: r.update(target="/v1/é"),
        lambda r: r.update(body_hex="AB"),
        lambda r: r.update(body_hex="abc"),
        lambda r: r.update(body_hex=1),
        lambda r: r.update(headers=[]),
        lambda r: r["headers"].update({"Orch-Hub": "11" * 16}),
        lambda r: r["headers"].update({"orch-hub-signature": "AAAA"}),
        lambda r: r["headers"].update({"X-Forwarded-For": "1.2.3.4"}),
        lambda r: r["headers"].update({"orch-ts": "1", "ORCH-TS": "2"}),
        lambda r: r["headers"].update({"Orch-Nonce": "AB" * 16}),
        lambda r: r["headers"].update({"Orch-Workspace": "11" * 15}),
        lambda r: r["headers"].update({"Orch-Signature": "short"}),
        lambda r: r["headers"].update({"Orch-Ts": "01"}),
        lambda r: r["headers"].update({"Orch-Ts": "1\r\nX: y"}),
        lambda r: r["headers"].update({"Orch-Owner-Approval": "!!"}),
        lambda r: r["headers"].update({"Orch-Owner-Approval": b64u(b"[1]")}),
        lambda r: r["headers"].pop("Orch-Nonce"),
        lambda r: r["headers"].update({"Orch-Ts": 5}),
    ],
)
def test_not_the_exact_shape_is_malformed(env, mut):
    r = req(env)
    mut(r)
    with pytest.raises(Refused) as x:
        env.fw.forward(WS, r)
    assert x.value.code == "malformed" and env.up.requests == []


def test_other_workspace_is_refused(env):
    other = "22" * 16
    r = host_request(env.wsk, env.hub.hub_id, other)
    with pytest.raises(Refused) as x:
        env.fw.forward(WS, r)
    assert x.value.code == "other_workspace" and env.up.requests == []


def test_oversize_body_refused(tmp_path, up):
    hub = make_hub(tmp_path)
    fw = HubPublish(hub, RecordingSync(), PublishConfig(up.url, HOST, max_request_body=4))
    fw.binding(WS, binding(new_wsk(), hub, WS))
    with pytest.raises(Refused) as x:
        fw.forward(WS, host_request(new_wsk(), hub.hub_id, WS, method="PUT", body=b"12345"))
    assert x.value.code == "malformed"


def test_redirects_are_returned_not_followed(env):
    env.up.status, env.up.headers, env.up.body = 302, {"Location": "http://127.0.0.1:1/x"}, b""
    out = env.fw.forward(WS, req(env))
    assert out == {"status": 302, "body_hex": ""} and len(env.up.requests) == 1


def test_upstream_error_statuses_map_through(env):
    env.up.status, env.up.body = 403, b'{"error":"approval_required"}'
    assert env.fw.forward(WS, req(env)) == {"status": 403, "body_hex": env.up.body.hex()}


def test_response_over_limit_is_unavailable(tmp_path, up):
    hub = make_hub(tmp_path)
    fw = HubPublish(hub, RecordingSync(), PublishConfig(up.url, HOST, max_response_body=3))
    fw.binding(WS, binding(new_wsk(), hub, WS))
    with pytest.raises(Refused) as x:
        fw.forward(WS, host_request(new_wsk(), hub.hub_id, WS))
    assert x.value.code == "unavailable"


def test_timeout_is_unavailable(env):
    env.up.delay = 3.0
    with pytest.raises(Refused) as x:
        env.fw.forward(WS, req(env))
    assert x.value.code == "unavailable"


def test_connection_refused_is_unavailable(tmp_path):
    hub = make_hub(tmp_path)
    fw = HubPublish(hub, RecordingSync(), PublishConfig("http://127.0.0.1:1", HOST, timeout_s=1))
    fw.binding(WS, binding(new_wsk(), hub, WS))
    with pytest.raises(Refused) as x:
        fw.forward(WS, host_request(new_wsk(), hub.hub_id, WS))
    assert x.value.code == "unavailable"


def test_sync_failure_refuses_and_nothing_is_sent(env):
    env.fw._synced.clear()
    env.sync.fail = PublishSyncError("unavailable")
    with pytest.raises(Refused) as x:
        env.fw.forward(WS, req(env))
    assert x.value.code == "unavailable" and env.up.requests == []


def test_sync_refusal_code_passes_through(env):
    env.fw._synced.clear()
    env.sync.fail = PublishSyncError("stale_binding")
    with pytest.raises(Refused) as x:
        env.fw.forward(WS, req(env))
    assert x.value.code == "stale_binding"


def test_publish_saying_not_bound_forces_a_resync(env):
    env.up.status, env.up.body = 403, b'{"error":"not_bound"}'
    env.fw.forward(WS, req(env))
    n = len(env.sync.calls)
    env.fw.forward(WS, req(env))
    assert [c[0] for c in env.sync.calls[n:]] == ["card", "binding"]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://127.0.0.1",
        "http://example.com",
        "http://u:p@127.0.0.1",
        "http://127.0.0.1/x",
        "http://127.0.0.1?a=1",
        "x",
    ],
)
def test_base_url_is_validated(tmp_path, url):
    with pytest.raises(ValueError):
        HubPublish(make_hub(tmp_path), RecordingSync(), PublishConfig(url, HOST))


def test_hub_rotation_pushes_the_new_card(env):
    env.hub.rotate_op()
    env.fw.forward(WS, req(env))
    cards = [c for c in env.sync.calls if c[0] == "card"]
    assert len(cards) == 2 and cards[1][1] == env.hub.card()


def test_binding_for_another_workspace_is_rejected_and_not_stored(env):
    other = "22" * 16
    with pytest.raises(Refused) as x:
        env.fw.binding(other, binding(env.wsk, env.hub, WS))  # an object of WS filed under another key
    assert x.value.code == "other_workspace"
    with pytest.raises(Refused) as x:
        env.fw.binding(WS, binding(env.wsk, env.hub, other))
    assert x.value.code == "other_workspace"
    assert other not in env.fw._bindings and env.fw._bindings[WS] == env.b


def test_late_lower_or_equal_binding_cannot_undo_an_unbound(env):
    env.fw.binding(WS, unbound(env.wsk, env.hub, WS, seq=2))
    for seq in (1, 2):
        with pytest.raises(Refused) as x:
            env.fw.binding(WS, binding(env.wsk, env.hub, WS, seq=seq))
        assert x.value.code == "unbound"
    with pytest.raises(Refused) as x:
        env.fw.forward(WS, req(env))
    assert x.value.code == "not_bound" and env.up.requests == []
    env.fw.binding(WS, binding(env.wsk, env.hub, WS, seq=3))  # a later binding is a new one
    assert env.fw.forward(WS, req(env))["status"] == 200


def test_lower_seq_binding_is_stale(env):
    env.fw.binding(WS, binding(env.wsk, env.hub, WS, seq=5))
    with pytest.raises(Refused) as x:
        env.fw.binding(WS, binding(env.wsk, env.hub, WS, seq=4))
    assert x.value.code == "stale_binding"


def test_unknown_sync_code_is_unavailable(env):
    env.fw._synced.clear()
    env.sync.fail = PublishSyncError("secret internal detail")
    with pytest.raises(Refused) as x:
        env.fw.forward(WS, req(env))
    assert x.value.code == "unavailable"


def test_keys_are_read_once_per_forward(tmp_path, up):
    hub = make_hub(tmp_path)
    calls = []

    def keys():
        calls.append(1)
        return hub.op_signer, hub.card(), hub.hub_id

    fw = HubPublish(keys, RecordingSync(), PublishConfig(up.url, HOST))
    wsk = new_wsk()
    fw.binding(WS, binding(wsk, hub, WS))
    calls.clear()
    fw.forward(WS, host_request(wsk, hub.hub_id, WS))
    assert len(calls) == 1


def test_overall_deadline_applies_to_a_slow_drip(tmp_path):
    import socket
    import threading
    import time

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def drip():
        c, _ = srv.accept()
        c.recv(65536)
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
        for _ in range(100):
            try:
                c.sendall(b"x")
            except OSError:
                break
            time.sleep(0.2)
        c.close()

    threading.Thread(target=drip, daemon=True).start()
    hub = make_hub(tmp_path)
    cfg = PublishConfig(f"http://127.0.0.1:{srv.getsockname()[1]}", HOST, timeout_s=5, deadline_s=1.0)
    fw = HubPublish(hub, RecordingSync(), cfg)
    wsk = new_wsk()
    fw.binding(WS, binding(wsk, hub, WS))
    t = time.monotonic()
    with pytest.raises(Refused) as x:
        fw.forward(WS, host_request(wsk, hub.hub_id, WS))
    assert x.value.code == "unavailable" and time.monotonic() - t < 3
    srv.close()


def test_card_goes_to_orch_publish_and_a_refusal_comes_back(env):
    card = {"o": {"kind": "card"}}
    env.fw.card(WS, card, "pk")
    assert env.sync.calls[-1] == ("ws_card", WS, card, "pk")
    for code in (
        "owner_not_allowed", "unknown_workspace", "delegation_changed", "workspace_blocked", "delegation_refused",
        "other_person", "delegation_mismatch",
    ):  # fmt: skip
        env.sync.fail = PublishSyncError(code)
        with pytest.raises(Refused) as x:
            env.fw.card(WS, card, "pk")
        assert x.value.code == code
    env.sync.fail = PublishSyncError("made_up")
    with pytest.raises(Refused) as x:
        env.fw.card(WS, card, "pk")
    assert x.value.code == "unavailable"


def test_a_full_mode_workspace_is_forwarded_like_a_selective_one(tmp_path, up):
    """Run 14 P01: publishing from a workspace bound in FULL mode goes through the same forwarder, adding exactly the
    same two headers; the listing and the revoke of such links are the Published page's (test_published_*)."""
    hub = make_hub(tmp_path)
    wsk = new_wsk()
    fw = HubPublish(hub, RecordingSync(), PublishConfig(up.url, HOST, timeout_s=2.0))
    fw.binding(WS, binding(wsk, hub, WS, mode="full"))
    r = host_request(wsk, hub.hub_id, WS, method="PUT", target=f"/v1/ws/{WS}/files/a", body=b"hello")
    assert fw.forward(WS, r) == {"status": 200, "body_hex": b'{"ok":true}'.hex()}
    (seen,) = up.requests
    got = {k.lower() for k in seen["headers"] if k.lower().startswith("orch-")}
    assert got == {k.lower() for k in r["headers"]} | {"orch-hub", "orch-hub-signature"}
