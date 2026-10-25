"""R1-8 server pieces: "Log out everywhere", the web address in the session answer, the tier B line."""

from remote_kit import *  # noqa: F403 - the fixtures (app, front, loop, remote, local, bearer)
from remote_kit import HTTPS, bearer

from orch_dashboard.terminals import execution_line


def test_logout_all_ends_every_remote_session_and_keeps_local(app, front, loop, auth, remote, local):
    other = auth.open_remote()["token"]
    got = front.post("/api/session/logout-all", json={}, headers=bearer(remote, HTTPS))
    assert got.status_code == 200 and got.json() == {"ok": True}
    assert not auth.valid(remote) and not auth.valid(other)
    assert auth.valid(local)


def test_logout_all_needs_a_session(front):
    assert front.post("/api/session/logout-all", json={}).status_code in (401, 404)


def test_session_answer_names_the_web_address_when_remote_is_on(loop, local):
    body = loop.get("/api/session", headers=bearer(local)).json()
    assert body["remote_address"].startswith("https://")
    assert "remote_problem" not in body


def test_tier_b_line_names_only_a_valid_agent_user():
    line = "This terminal runs as orch-agent-sev. It cannot read your keys. Never type secrets into it."
    assert execution_line("orch-agent-sev") == line
    assert execution_line("root") is None and execution_line(None) is None


def test_logout_all_burns_unused_add_a_device_links(app, front, auth, remote):
    code = app.state.enroll.mint()
    assert (
        front.post("/auth/enroll/options", json={"code": code}, headers={"Origin": HTTPS}).status_code == 200
    )  # the link works before
    fresh = app.state.enroll.mint()
    assert front.post("/api/session/logout-all", json={}, headers=bearer(remote, HTTPS)).status_code == 200
    for dead in (code, fresh):
        assert (
            front.post("/auth/enroll/options", json={"code": dead}, headers={"Origin": HTTPS}).status_code == 401
        )  # refused afterwards


def test_a_link_minted_during_the_logout_is_burned_too(app, front, auth, remote, monkeypatch):
    minted = []
    real = auth.logout_everywhere

    def late(kind="remote"):
        gone = real(kind)
        minted.append(app.state.enroll.mint())  # a session that was still live made a link just before the end
        return gone

    monkeypatch.setattr(auth, "logout_everywhere", late)
    assert front.post("/api/session/logout-all", json={}, headers=bearer(remote, HTTPS)).status_code == 200
    got = front.post("/auth/enroll/options", json={"code": minted[0]}, headers={"Origin": HTTPS})
    assert got.status_code == 401


def test_an_ended_session_cannot_make_an_enrollment_link(front, auth, remote):
    assert front.post("/api/session/logout-all", json={}, headers=bearer(remote, HTTPS)).status_code == 200
    body = {"link_id": "L" * 22}
    got = front.post("/api/passkeys/enroll-link", json=body, headers=bearer(remote, HTTPS))
    assert got.status_code in (401, 404)
    assert not auth.valid(remote)
