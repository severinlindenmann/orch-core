"""Shared pieces of the R3 stream tests: a stand-in for the in-process stream adapter (T4 builds the real one), a fake
signer channel that records what the signer would see, and the explicit D90 list."""

# ruff: noqa: F811

import pytest
from fastapi.testclient import TestClient
from remote_kit import app, passkey_store  # noqa: F401

from orch_dashboard.click_signer import CONTRACTS
from orch_dashboard.policy import REMOTE_DEVICE, STREAM_HOST
from orch_dashboard.remote_identity import REMOTE_STREAM, RemoteIdentity

HOST = f"https://{STREAM_HOST}"
STREAM = "5e" * 16
OTHER_STREAM = "6f" * 16
DEVICE = "rd_" + "a1" * 16
PERSON = "p_" + "0f" * 16


class Adapter:
    """What the real adapter does for ONE request, and nothing more: it builds the scope (listener, identity, the
    stream's own credential) and sets the context value for the time of the ASGI call. The host's frame gives it the
    identity; here the test does."""

    def __init__(self, app, identity: RemoteIdentity, token: str, stream: str | None = None):
        self.app, self.identity, self.token = app, identity, token
        self.stream = identity.stream_id if stream is None else stream

    async def __call__(self, scope, receive, send):
        scope["orch_listener"] = REMOTE_DEVICE
        scope["state"] = {**scope.get("state", {}), "remote": self.identity, "token": self.token}
        marker = REMOTE_STREAM.set(self.stream)
        try:
            await self.app(scope, receive, send)
        finally:
            REMOTE_STREAM.reset(marker)


def stream_client(app, auth, ws, level="work", stream=STREAM, headers=None):
    identity = RemoteIdentity(stream, ws, DEVICE, PERSON, level)
    token = auth.open_stream(stream, ws, PERSON)
    client = TestClient(
        Adapter(app, identity, token), base_url=HOST, headers={"Content-Type": "application/json", **(headers or {})}
    )
    return client, identity, token


class Fake:
    """A scripted signer channel: records every call (op, args)."""

    def __init__(self, ws):
        self.ws, self.calls, self.proc = ws, [], None
        self.answers = {}

    def call(self, op, args, timeout=None):
        self.calls.append((op, dict(args)))
        if op in self.answers:
            got = self.answers[op]
            if isinstance(got, Exception):
                raise got
            return got
        if op == "status":
            row = {
                "workspace_id": self.ws,
                "prefix": "DEMO",
                "custody": "convenience",
                "enabled": True,
                "available": True,
            }
            return {"core": "2.0.0.dev0", "contract": CONTRACTS[0], "workspaces": [row], "features": []}
        return {}

    def close(self):
        pass


@pytest.fixture
def fake(stub):
    return Fake(stub.ws)


@pytest.fixture
def click_start(fake, monkeypatch):
    monkeypatch.delenv("ORCH_DASHBOARD_CLICK", raising=False)  # the fake signer channel is on
    return lambda env: fake


@pytest.fixture
def ws(stub):
    return stub.ws


@pytest.fixture
def device(app, auth, ws):
    return stream_client(app, auth, ws)[0]


def levels_client(app, auth, ws, level):
    return stream_client(app, auth, ws, level=level, stream=f"{level[:2]}" * 16)[0]


#: The D90 list, written out: every one is refused at EVERY level. (method, concrete path). Never read from the table.
D90_REFUSED = [
    ("POST", "/api/w/{ws}/hub/connect"),
    ("POST", "/api/w/{ws}/hub/link"),
    ("POST", "/api/w/{ws}/hub/refresh"),
    ("POST", "/api/w/{ws}/hub/switch"),
    ("POST", "/api/w/{ws}/hubsign"),
    ("POST", "/api/w/{ws}/hub/approve/prepare"),
    ("GET", "/api/w/{ws}/hub/approve/hr_x"),
    ("GET", "/api/passkeys"),
    ("POST", "/api/passkeys/enroll-link"),
    ("DELETE", "/api/passkeys/abc"),
    ("POST", "/api/stepup/options"),
    ("POST", "/api/stepup/verify"),
    ("POST", "/api/w/{ws}/stepup/options"),
    ("GET", "/api/trusted"),
    ("POST", "/api/trusted/enrol"),
    ("DELETE", "/api/trusted/k1"),
    ("POST", "/auth/session"),
    ("POST", "/auth/logout"),
    ("POST", "/auth/switch"),
    ("POST", "/auth/switch/challenge"),
    ("POST", "/auth/passkey/options"),
    ("POST", "/auth/passkey/login"),
    ("POST", "/auth/enroll/options"),
    ("POST", "/auth/enroll/register"),
    ("POST", "/auth/setup"),
    ("GET", "/api/setup/anything"),
    ("POST", "/api/setup/anything"),
    ("GET", "/api/w/{ws}/remote"),
    ("POST", "/api/w/{ws}/remote/code"),
    ("POST", "/api/w/{ws}/remote/devices/rd_x/remove"),
    ("POST", "/api/w/{ws}/remote/sessions/s1/end"),
    ("POST", "/api/w/{ws}/click/custody"),
    ("POST", "/api/w/{ws}/click/custody/seal"),
    ("POST", "/api/w/{ws}/click/runner"),
    ("POST", "/api/w/{ws}/click/runner/review"),
    ("POST", "/api/w/{ws}/click/rotate"),
    ("POST", "/api/w/{ws}/click/rotate/seal"),
    ("GET", "/api/w/{ws}/click/github"),
    ("POST", "/api/w/{ws}/click/github"),
    ("POST", "/api/w/{ws}/click/github/seal"),
    ("POST", "/api/w/{ws}/click/github/remove"),
    ("GET", "/api/w/{ws}/market"),
    ("GET", "/api/w/{ws}/market/installed"),
    ("POST", "/api/w/{ws}/market/stage"),
    ("DELETE", "/api/w/{ws}/market/stage/s1"),
    ("POST", "/api/w/{ws}/market/uninstall"),
    ("POST", "/api/w/{ws}/market/add"),
    ("POST", "/api/w/{ws}/market/refresh"),
    ("GET", "/api/w/{ws}/claude-trust"),
    ("POST", "/api/w/{ws}/claude-trust"),
    ("POST", "/api/w/{ws}/hub/share/rule-add"),
    ("POST", "/api/w/{ws}/hub/share/sync"),
    ("POST", "/api/w/{ws}/shares/register"),
    ("POST", "/api/w/{ws}/records/auto-on"),
    ("POST", "/api/w/{ws}/org/accept"),
    ("POST", "/api/w/{ws}/org/tool"),
    ("POST", "/api/w/{ws}/phone/request"),
    ("GET", "/widget-frame/x"),
    ("GET", "/api/testrun/r1"),
    ("PUT", "/api/testrun/r1/steps/s1"),
    ("GET", "/"),
    ("GET", "/assets/main-hash.js"),
]
