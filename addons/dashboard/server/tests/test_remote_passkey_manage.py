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


def link(world, link_id=None, **kw):
    body = {"link_id": link_id or getattr(world, "link_id", "A" * 22)}
    return world.front.post("/api/passkeys/enroll-link", json=body, headers=world.headers(**kw))


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
    assert refused_for_stepup(link(world, "B" * 22)), "the touch was for the link_id the server minted"
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
    assert loop.post("/api/passkeys/enroll-link", json={"link_id": "L" * 22}, headers=h).status_code == 200
    assert loop.delete(f"/api/passkeys/{b64u(other.credential_id)}", headers=h).status_code == 200


def test_the_routes_are_closed_without_a_session(front):
    assert front.get("/api/passkeys").status_code == 404
    assert front.post("/api/passkeys/enroll-link", json={"link_id": "L" * 22}, headers=JSON).status_code == 404


def test_the_routes_do_not_exist_when_remote_access_is_off(client, token):
    h = bearer(token, ORIGIN)
    assert client.get("/api/passkeys", headers=h).status_code == 404
    assert client.post("/api/passkeys/enroll-link", json={"link_id": "L" * 22}, headers=h).status_code == 404


def test_the_bodies_are_closed(world):
    world.touch("device.add")
    res = world.front.post("/api/passkeys/enroll-link", json={"x": 1}, headers=world.headers())
    assert res.status_code == 400
    assert world.front.delete("/api/passkeys/short", headers=world.headers()).status_code == 400


def login_token(world, authn):
    from passkey_kit import login

    res = login(world.front, authn)
    assert res.status_code == 200, res.text
    return res.json()["token"]


def test_removing_a_passkey_ends_the_sessions_it_opened(world):
    other = second_passkey(world)
    theirs = b64u(other.credential_id)
    finder = login_token(world, other)  # whoever holds the lost phone
    assert world.auth.valid(finder)
    world.touch("passkey.remove", credential_id=theirs)
    assert remove(world, theirs).status_code == 200
    assert not world.auth.valid(finder)
    assert world.front.get("/api/session", headers=bearer(finder)).status_code == 404
    assert world.auth.valid(world.token), "sessions of other passkeys stay"


def test_removing_the_passkey_you_are_using_ends_your_own_session_too(world):
    other = second_passkey(world)
    mine = login_token(world, world.authn)
    world.touch("passkey.remove", token=mine, credential_id=b64u(world.authn.credential_id))
    assert remove(world, b64u(world.authn.credential_id), token=mine).status_code == 200
    assert not world.auth.valid(mine)
    assert other  # the other passkey remains


def test_a_step_up_made_by_a_removed_passkey_is_dropped_from_every_session(world):
    other = second_passkey(world)
    theirs = b64u(other.credential_id)
    spare = world.auth.open_remote()["token"]
    world.touch("device.add", token=spare, authn=other)  # the phone touched, in another session
    world.touch("passkey.remove", credential_id=theirs)
    assert remove(world, theirs).status_code == 200
    assert refused_for_stepup(link(world, token=spare)), "the phone's touch no longer counts"


def test_the_sessions_record_their_credential(world):
    mine = login_token(world, world.authn)
    assert world.auth.credentials[mine] == b64u(world.authn.credential_id)
    world.auth.logout(mine)
    assert mine not in world.auth.credentials


def test_a_passkey_with_a_long_credential_id_can_be_removed(world):
    import secrets

    big = Authenticator(world.authn.rp_id)
    big.credential_id = secrets.token_bytes(1000)
    code = world.app.state.enroll.mint()
    _, res = enroll(world.front, world.app, code=code, name="Key", authn=big)
    assert res.status_code == 200, res.text
    cid = b64u(big.credential_id)
    world.touch("passkey.remove", credential_id=cid)
    assert remove(world, cid).status_code == 200


def test_the_store_never_removes_its_last_passkey_under_its_own_lock(world):
    from orch.click.passkey_store import LastPasskey

    store = world.app.state.passkeys.store
    (only,) = store.read()
    with pytest.raises(LastPasskey):
        store.remove(only.id_bytes)
    assert len(store.read()) == 1


def test_a_session_has_at_most_four_open_step_ups_and_others_are_not_hurt(world, auth):
    opened = [world.options("device.add").status_code for _ in range(5)]
    assert opened == [200, 200, 200, 200, 429]
    bystander = auth.open_remote()["token"]
    assert world.options("device.add", token=bystander).status_code == 200


def test_the_options_are_charged_to_the_source_bucket(world, auth):
    from orch_dashboard.auth import REMOTE_PER_SOURCE

    codes = set()
    for _ in range(REMOTE_PER_SOURCE + 5):
        codes.add(world.options("device.add").status_code)
        world.app.state.passkeys.ceremonies.items.clear()  # keep the per-session cap out of the way
    assert 429 in codes
