"""R3 T3 (orch-core #1591; spec D89, design I2/I3): the credential minted inside a stream works on that stream only, and
every signer call made inside a stream says `remote: true` with the stream id, stamped in ONE place."""

# ruff: noqa: F811

import pytest
from conftest import ORIGIN, PORT
from device_kit import (  # noqa: F401
    DEVICE,
    OTHER_STREAM,
    PERSON,
    STREAM,
    Adapter,
    app,
    click_start,
    fake,
    passkey_store,
    stream_client,
    ws,
)
from fastapi.testclient import TestClient
from remote_kit import HTTPS, SOURCE, bearer
from starlette.requests import Request

from orch_dashboard import click_signer
from orch_dashboard.click_signer import STREAM_ARGS, CallerSuppliedRemote
from orch_dashboard.errors import HostError
from orch_dashboard.front import Front
from orch_dashboard.remote_identity import REMOTE_STREAM, RemoteIdentity
from orch_dashboard.stepup import refuse_step_up


def code(res):
    return res.json().get("error", {}).get("code")


# --- the credential --------------------------------------------------------------------------------------------------
def test_stream_token_refused_on_every_listener(app, auth, ws):
    client, identity, token = stream_client(app, auth, ws)
    assert client.get(f"/api/w/{ws}/today").status_code != 404  # on its own stream it works
    loop = TestClient(app, base_url=ORIGIN, headers={"X-Orch-Click-Contract": "4"})
    front = TestClient(Front(app), base_url=HTTPS, headers={"X-Orch-Client": SOURCE, "X-Orch-Click-Contract": "4"})
    unknown = "x" * 43  # what a made-up token gets is what the stream's token gets
    for path in (f"/api/w/{ws}/today", "/api/session", f"/api/w/{ws}/click/status"):
        res = loop.get(path, headers=bearer(token, ORIGIN))
        assert res.status_code == loop.get(path, headers=bearer(unknown, ORIGIN)).status_code == 401
        res = front.get(path, headers=bearer(token, HTTPS))
        assert res.status_code == front.get(path, headers=bearer(unknown, HTTPS)).status_code == 404
    assert auth.valid(token)  # and none of the above ended it


def test_stream_token_refused_on_another_stream(app, auth, ws):
    _, identity_a, token_a = stream_client(app, auth, ws, stream=STREAM)
    _, identity_b, token_b = stream_client(app, auth, ws, stream=OTHER_STREAM)
    other = identity_b
    # stream B's request, carrying stream A's credential
    crossed = TestClient(Adapter(app, other, token_a), base_url="https://orch-remote")
    assert crossed.get(f"/api/w/{ws}/today").status_code == 404
    # stream A's identity under the context value of stream B (the adapter's marker disagrees with its own scope)
    confused = TestClient(Adapter(app, identity_a, token_a, stream=OTHER_STREAM), base_url="https://orch-remote")
    assert confused.get(f"/api/w/{ws}/today").status_code == 404
    # and an identity that names stream A with the token of B
    swapped = TestClient(Adapter(app, identity_a, token_b), base_url="https://orch-remote")
    assert swapped.get(f"/api/w/{ws}/today").status_code == 404
    # a made-up token
    fake_token = TestClient(Adapter(app, identity_a, "x" * 43), base_url="https://orch-remote")
    assert fake_token.get(f"/api/w/{ws}/today").status_code == 404


def test_a_launch_session_or_remote_session_is_no_stream_credential(app, auth, ws):
    local = auth.exchange(auth.launch(PORT)["code"], "local")[0]["token"]
    remote = auth.open_remote()["token"]
    identity = RemoteIdentity(STREAM, ws, DEVICE, PERSON, "terminals")
    for token in (local, remote):
        client = TestClient(Adapter(app, identity, token), base_url="https://orch-remote")
        assert client.get(f"/api/w/{ws}/today").status_code == 404


def test_the_token_never_leaves_the_dashboard(app, auth, ws):
    client, _, token = stream_client(app, auth, ws)
    for path in ("/api/session", f"/api/w/{ws}/today", "/api/workspaces", f"/api/w/{ws}/identity"):
        res = client.get(path)
        assert token not in res.text and token not in str(dict(res.headers))
        assert "set-cookie" not in res.headers


def test_closing_a_stream_drops_its_credential(app, auth, ws):
    ended = []
    auth.on_session_end = ended.append
    client, identity, token = stream_client(app, auth, ws)
    assert auth.stream_of(token) == STREAM and auth.bound_workspace(token) == ws
    auth.drop_stream(STREAM)
    assert auth.stream_of(token) is None and not auth.valid(token) and ended == [token]
    assert client.get(f"/api/w/{ws}/today").status_code == 404
    assert STREAM not in auth.stream_tokens and token not in auth.streams
    auth.drop_stream(STREAM)  # twice is nothing
    auth.open_stream(STREAM, ws, PERSON)  # the id is free again


def test_one_credential_per_stream_and_ids_only(auth, ws):
    token = auth.open_stream(STREAM, ws, PERSON)
    with pytest.raises(ValueError):
        auth.open_stream(STREAM, ws, PERSON)
    for bad in (("", ws, PERSON), (STREAM, "", PERSON), (STREAM, ws, None)):
        with pytest.raises(ValueError):
            auth.open_stream(*bad)
    assert auth.login_kind(token) == "remote-device" and auth.listener_of(token) == "remote-device"
    assert auth.switch_workspace(token) is None  # it is not a switch session


def test_a_stream_credential_expires_like_the_stream(auth, ws):
    from orch_dashboard.auth import Auth

    now = [1_000.0]
    auth = Auth(clock=lambda: now[0])
    token = auth.open_stream(STREAM, ws, PERSON)
    now[0] += 7_201  # 2 h idle
    assert not auth.valid(token) and auth.stream_of(token) is None
    assert STREAM not in auth.stream_tokens


# --- the signer sees `remote: true` for every stream call ------------------------------------------------------------
class Spy:
    def __init__(self):
        self.calls = []
        self.proc = None

    def call(self, op, args, timeout=None):
        self.calls.append((op, dict(args)))
        if op == "status":
            return {"contract": 4, "features": [], "workspaces": []}
        return {}

    def close(self):
        pass


def signer_with_spy(tmp_path, monkeypatch):
    monkeypatch.delenv("ORCH_DASHBOARD_CLICK", raising=False)
    spy = Spy()
    return click_signer.Signer(tmp_path, start=lambda env: spy), spy


def test_signer_sees_remote_true_for_every_stream_call(tmp_path, monkeypatch):
    signer, spy = signer_with_spy(tmp_path, monkeypatch)
    ops = sorted(click_signer.TIMEOUT)
    marker = REMOTE_STREAM.set(STREAM)
    try:
        for op in ops:
            signer.call(op, {"workspace_id": "w"})
        signer.status()
    finally:
        REMOTE_STREAM.reset(marker)
    seen = [(op, args) for op, args in spy.calls if args.get("stream") is not None or op != "status"]
    assert {op for op, _ in seen} >= set(ops)
    for op, args in seen:
        assert args["remote"] is True and args["stream"] == STREAM, op
    # outside a stream nothing is stamped
    spy.calls.clear()
    signer.call("prepare", {"workspace_id": "w"})
    assert spy.calls[-1][1].get("remote") is None and "stream" not in spy.calls[-1][1]


def test_caller_supplied_remote_raises(tmp_path, monkeypatch):
    signer, spy = signer_with_spy(tmp_path, monkeypatch)
    marker = REMOTE_STREAM.set(STREAM)
    try:
        for extra in ({"remote": True}, {"remote": False}, {"stream": OTHER_STREAM}, {"stepup": {"a": 1}}):
            assert set(extra) <= STREAM_ARGS
            with pytest.raises(CallerSuppliedRemote):
                signer.call("sign", {"workspace_id": "w", **extra})
    finally:
        REMOTE_STREAM.reset(marker)
    assert not [c for c in spy.calls if c[0] == "sign"]  # nothing reached the signer
    # outside a stream the passkey session's own `remote` argument is as before
    signer.call("sign", {"workspace_id": "w", "remote": True})
    assert spy.calls[-1] == ("sign", {"workspace_id": "w", "remote": True, "contract": 4})


def test_a_stream_status_does_not_replace_the_loopback_cache(tmp_path, monkeypatch):
    signer, spy = signer_with_spy(tmp_path, monkeypatch)
    signer._last = {"w": {"workspace_id": "w"}}
    marker = REMOTE_STREAM.set(STREAM)
    try:
        signer.status()
    finally:
        REMOTE_STREAM.reset(marker)
    assert signer._last == {"w": {"workspace_id": "w"}}


def test_click_routes_stamp_through_the_one_place(app, auth, ws, fake):
    client = stream_client(app, auth, ws)[0]
    res = client.get(f"/api/w/{ws}/click/status")
    assert res.status_code == 200, res.text
    stamped = [(op, a) for op, a in fake.calls if op != "status" or "stream" in a]
    assert stamped and all(a["remote"] is True and a["stream"] == STREAM for _, a in stamped)
    fake.calls.clear()
    for step in ("view", "state", "cancel", "prepare", "sign"):
        client.post(f"/api/w/{ws}/click/{step}", json={})
    # whatever those handlers did with a body this empty, every call that reached the signer is stamped, none is a lie
    assert all(a.get("remote") is True and a.get("stream") == STREAM for op, a in fake.calls if op != "status")


# --- refuse_step_up in a stream --------------------------------------------------------------------------------------
class Body:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def request_for(token):
    scope = {"type": "http", "state": {"token": token}}
    return Request(scope)


def test_a_stream_gets_no_stepup_arguments_from_the_handlers(auth, ws):
    token = auth.open_stream(STREAM, ws, PERSON)
    plain = Body(sealed=None, reveal_key=None)
    assert refuse_step_up(auth, request_for(token), "sign", plain, ws) == {}  # the signer client stamps `remote`
    sealed = Body(sealed={"x": 1}, reveal_key=None)
    with pytest.raises(HostError) as err:
        refuse_step_up(auth, request_for(token), "sign", sealed, ws)
    assert err.value.error["code"] == "passkey.stepup_required"
    # the same helper for a passkey session still says `remote: True` (unchanged)
    remote = auth.open_remote()["token"]
    assert refuse_step_up(auth, request_for(remote), "sign", plain, ws) == {"remote": True}
