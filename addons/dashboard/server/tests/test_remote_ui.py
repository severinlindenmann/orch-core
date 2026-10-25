"""R1-8 server pieces: "Log out everywhere", the web address in the session answer, the tier B line on an agent preview."""

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
    assert execution_line("orch-agent-sev") == "This terminal runs as orch-agent-sev. It cannot read your keys. Never type secrets into it."
    assert execution_line("root") is None and execution_line(None) is None
