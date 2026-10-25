"""#1198 R1-6: the passkey store's own routes (list, "Add a device", remove) and the step-up they sit behind."""

# ruff: noqa: F811

import os

import pytest
from conftest import ORIGIN
from passkey_kit import JSON, auth, clock, enroll  # noqa: F401
from remote_kit import HTTPS, app, bearer, front, local, loop, passkey_store, remote  # noqa: F401
from test_dashboard_click import click_start, script  # noqa: F401
from test_stepup import _World, b64u, refused_for_stepup
from webauthn_kit import Authenticator

from orch_dashboard.auth import STEPUP_FRESH
from orch_dashboard.passkey_routes import ENROLL_TTL


@pytest.fixture
def world(front, app, remote, stub, script, auth):
    authn, res = enroll(front, app)
    assert res.status_code == 200, res.text
    return _World(front, app, remote, stub, script, auth, authn)


def second_passkey(world):
    """A second device, enrolled by the first one's link (the real flow)."""
    code = world.app.state.enroll.mint()
    other, res = enroll(world.front, world.app, code=code, name="Phone", authn=Authenticator(world.authn.rp_id))
    assert res.status_code == 200, res.text
    return other


def link(world, **kw):
    return world.front.post("/api/passkeys/enroll-link", json={}, headers=world.headers(**kw))


def remove(world, credential_id, **kw):
    return world.front.delete(f"/api/passkeys/{credential_id}", headers=world.headers(**kw))


def test_the_list_shows_names_and_last_use_and_no_key_material(world):
    second_passkey(world)
    res = world.front.get("/api/passkeys", headers=world.headers())
    assert res.status_code == 200
    rows = res.json()["passkeys"]
    assert [r["name"] for r in rows] == ["MacBook, Chrome", "Phone"]
    assert all(set(r) == {"id", "name", "created", "last_used"} for r in rows)


def test_add_a_device_needs_a_step_up_and_gives_a_link_that_enrolls_once(world):
    assert refused_for_stepup(link(world))
    world.touch("device.add")
    res = link(world)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["expires_in"] == ENROLL_TTL and body["link"] == f"{HTTPS}/#enroll={body['code']}"
    other, done = enroll(
        world.front, world.app, code=body["code"], name="Laptop", authn=Authenticator(world.authn.rp_id)
    )
    assert done.status_code == 200
    _, again = enroll(world.front, world.app, code=body["code"], name="Again")
    assert again.status_code == 401  # single use
    assert refused_for_stepup(link(world)), "the step-up was used once"


def test_the_link_is_not_in_the_logs(world, caplog):
    world.touch("device.add")
    with caplog.at_level("DEBUG"):
        code = link(world).json()["code"]
    assert code not in caplog.text


def test_removing_a_passkey_needs_a_step_up_for_exactly_that_passkey(world):
    other = second_passkey(world)
    mine, theirs = b64u(world.authn.credential_id), b64u(other.credential_id)
    assert refused_for_stepup(remove(world, theirs))
    world.touch("passkey.remove", credential_id=mine)  # a touch for removing the OTHER one
    assert refused_for_stepup(remove(world, theirs))
    world.touch("passkey.remove", credential_id=theirs)
    assert remove(world, theirs).status_code == 200
    names = [r["name"] for r in world.front.get("/api/passkeys", headers=world.headers()).json()["passkeys"]]
    assert names == ["MacBook, Chrome"]


def test_the_last_passkey_is_never_removed(world):
    mine = b64u(world.authn.credential_id)
    world.touch("passkey.remove", credential_id=mine)
    res = remove(world, mine)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid.input"
    assert len(world.front.get("/api/passkeys", headers=world.headers()).json()["passkeys"]) == 1


def test_an_unknown_passkey_is_not_found_after_the_touch(world):
    second_passkey(world)
    ghost = b64u(os.urandom(32))
    world.touch("passkey.remove", credential_id=ghost)
    assert remove(world, ghost).status_code == 404


def test_a_stale_or_replayed_step_up_removes_nothing(world, clock):
    other = second_passkey(world)
    theirs = b64u(other.credential_id)
    world.touch("passkey.remove", credential_id=theirs)
    clock.now += STEPUP_FRESH
    assert refused_for_stepup(remove(world, theirs))
    world.touch("passkey.remove", credential_id=theirs)
    assert remove(world, theirs).status_code == 200
    assert refused_for_stepup(remove(world, theirs)), "a replay"


def test_a_step_up_of_another_session_does_not_count(world, auth):
    other_session = auth.open_remote()["token"]
    world.touch("device.add", token=other_session)
    assert refused_for_stepup(link(world))
    assert link(world, token=other_session).status_code == 200


def test_a_removed_passkey_cannot_make_a_step_up_any_more(world):
    other = second_passkey(world)
    theirs = b64u(other.credential_id)
    world.touch("passkey.remove", credential_id=theirs)
    assert remove(world, theirs).status_code == 200
    opts = world.options("device.add").json()
    assert [c["id"] for c in opts["publicKey"]["allowCredentials"]] == [b64u(world.authn.credential_id)]
    assert world.verify(opts, authn=other).status_code == 403


def test_a_store_that_cannot_be_trusted_answers_no_to_every_route(world):
    os.chmod(world.app.state.passkeys.store.path, 0o644)
    assert world.front.get("/api/passkeys", headers=world.headers()).status_code == 403
    assert world.options("device.add").status_code == 403


def test_a_local_session_needs_no_step_up_for_any_of_it(world, loop, local):
    other = second_passkey(world)
    h = bearer(local, ORIGIN)
    assert loop.get("/api/passkeys", headers=h).status_code == 200
    assert loop.post("/api/passkeys/enroll-link", json={}, headers=h).status_code == 200
    assert loop.delete(f"/api/passkeys/{b64u(other.credential_id)}", headers=h).status_code == 200


def test_the_routes_are_closed_without_a_session(front):
    assert front.get("/api/passkeys").status_code == 404
    assert front.post("/api/passkeys/enroll-link", json={}, headers=JSON).status_code == 404


def test_the_routes_do_not_exist_when_remote_access_is_off(client, token):
    h = bearer(token, ORIGIN)
    assert client.get("/api/passkeys", headers=h).status_code == 404
    assert client.post("/api/passkeys/enroll-link", json={}, headers=h).status_code == 404


def test_the_bodies_are_closed(world):
    world.touch("device.add")
    res = world.front.post("/api/passkeys/enroll-link", json={"x": 1}, headers=world.headers())
    assert res.status_code == 400
    assert world.front.delete("/api/passkeys/short", headers=world.headers()).status_code == 400
