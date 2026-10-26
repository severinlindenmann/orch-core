"""Shared pieces of the passkey route tests: fixtures and the enrollment/login helpers."""

# ruff: noqa: F811

import pytest
from remote_kit import HTTPS, NAME, Clock
from webauthn_kit import Authenticator

from orch_dashboard.auth import Auth

JSON = {"Origin": HTTPS}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def auth(clock):
    return Auth(clock=clock)


def enroll(front, app, *, code=None, name="MacBook, Chrome", authn=None, source=None):
    """A full enrollment with a software authenticator; returns (authenticator, register response)."""
    code = code or app.state.enroll.mint()
    headers = {**JSON, **({"X-Orch-Client": source} if source else {})}
    opts = front.post("/auth/enroll/options", json={"code": code}, headers=headers)
    if opts.status_code != 200:
        return None, opts
    authn = authn or Authenticator(NAME)
    created = authn.create(opts.json()["publicKey"])
    res = front.post(
        "/auth/enroll/register",
        json={"challenge_id": opts.json()["challenge_id"], "code": code, "name": name, **created},
        headers=headers,
    )
    return authn, res


def login(front, authn, *, mutate=None, source=None):
    headers = {**JSON, **({"X-Orch-Client": source} if source else {})}
    opts = front.post("/auth/passkey/options", json={}, headers=headers)
    assert opts.status_code == 200, opts.text
    got = authn.get(opts.json()["publicKey"])
    body = {"challenge_id": opts.json()["challenge_id"], **got}
    if mutate:
        mutate(body)
    return front.post("/auth/passkey/login", json=body, headers=headers)


def code_of(res):
    return res.json()["error"]["code"]
