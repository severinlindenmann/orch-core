"""#1198 C16: the GitHub token typed in the dashboard, sealed to the click signer, stored in the person's custody.

The same store path as ``orch addon login github`` (``credential.person_store``). A bare signer (no host): the
add-on list is a seam. Adversarial cases: a wrong purpose, request, workspace or review, a replay, a corrupted
envelope, a value that is no token, an add-on that is off, a clear token, and secrets in logs and state."""

from __future__ import annotations

import json
import os

import pytest

from orch.addons import credential
from orch.click import sealed
from orch.click.protocol import FEATURES, OPS, ClickError
from orch.click.signer import GITHUB_ADDON, ClickSigner
from tests.host.test_click import Mono

WID = "ab" * 16
TOKEN = "ghp_" + "Ab3" * 12
OTHER = "github_pat_" + "Zy9" * 14


@pytest.fixture
def world(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (state / "hosts" / WID).mkdir(parents=True, mode=0o700)
    entry = {"workspace_id": WID, "prefix": "w", "root": str(tmp_path), "host_uid": os.getuid(), "peers": {}}
    mono = Mono()
    signer = ClickSigner(state, workspaces=lambda: [entry], mono=mono, wall=lambda: 1_700_000_000.0)
    rows = {"github": {"name": "github", "state": "active", "credential": "missing"}}
    signer._call = lambda e, op, args, **kw: {"addons": list(rows.values())}  # noqa: SLF001
    monkeypatch.setattr("orch.market.ADDONS", "addons")
    w = type("W", (), {})()
    w.state, w.signer, w.mono, w.rows, w.entry = state, signer, mono, rows, entry
    yield w
    signer.close()


def begin(w):
    return w.signer.handle("github_token", {"workspace_id": WID, "begin": True})


def seal(offer, text=TOKEN, *, purpose="github.token", rid=None, wid=None, digest=None):
    ctx = sealed.Context(purpose, wid or WID, rid or offer["request_id"], digest or offer["view_digest"])
    return sealed.seal(offer["seal"]["pk"], text, ctx)


def go(w, offer, env, **over):
    args = {"workspace_id": WID, "request_id": offer["request_id"], "view_digest": offer["view_digest"], "sealed": env}
    return w.signer.handle("github_token", {**args, **over})


def stored(w):
    got = credential.load(w.state, WID, os.getuid(), GITHUB_ADDON)
    return None if got is None else got.reveal()


def test_the_signer_advertises_the_feature():
    assert "github_token" in OPS and "github_token" in FEATURES


def test_begin_answers_a_one_use_key_for_the_one_purpose(world):
    offer = begin(world)
    assert set(offer) == {"request_id", "view_digest", "seal", "expires_in"}
    assert offer["seal"]["purposes"] == ["github.token"] and offer["view_digest"].startswith("sha256:")
    assert begin(world)["request_id"] != offer["request_id"] and len(world.signer._token_offers) == 1  # noqa: SLF001


def test_the_sealed_token_is_stored_through_the_one_store_path(world):
    offer = begin(world)
    res = go(world, offer, {"github.token": seal(offer)})
    assert res == {"workspace_id": WID, "credential": "stored"}
    assert stored(world) == TOKEN
    audit = (world.state / "host" / "audit-security.jsonl").read_text().splitlines()
    assert json.loads(audit[-1]) == {"at": 1_700_000_000, "event": "addon.login", "addon": "github"}
    for p in world.state.rglob("*"):
        if p.is_file() and p.name != "credential":
            assert TOKEN.encode() not in p.read_bytes(), p
    assert not world.signer._token_offers  # noqa: SLF001 - used up


def test_a_new_token_replaces_the_old_one_and_remove_deletes_it(world):
    offer = begin(world)
    go(world, offer, {"github.token": seal(offer)})
    offer = begin(world)
    go(world, offer, {"github.token": seal(offer, OTHER)})
    assert stored(world) == OTHER
    assert world.signer.handle("github_token", {"workspace_id": WID, "remove": True}) == {
        "workspace_id": WID,
        "credential": "missing",
    }
    assert stored(world) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"purpose": "custody.passphrase"},
        {"rid": "hr_" + "1" * 26},
        {"wid": "cd" * 16},
        {"digest": "sha256:" + "3" * 64},
    ],
)
def test_an_envelope_for_another_purpose_request_workspace_or_review_stores_nothing(world, bad):
    offer = begin(world)
    with pytest.raises(ClickError):
        go(world, offer, {"github.token": seal(offer, **bad)})
    assert stored(world) is None


def test_a_replay_stores_nothing_and_a_corrupted_envelope_leaves_the_offer(world):
    offer = begin(world)
    env = {"github.token": seal(offer)}
    broken = {"github.token": {**env["github.token"], "ct": "A" + env["github.token"]["ct"][1:]}}
    with pytest.raises(ClickError):
        go(world, offer, broken)
    go(world, offer, env)  # the corrupted try did not burn the offer
    assert stored(world) == TOKEN
    with pytest.raises(ClickError) as caught:
        go(world, offer, env)
    assert "window closed" in str(caught.value)


@pytest.mark.parametrize("text", ["not a token", "ghp_short", "ghp_" + "é" * 30, ("ghp_" + "a" * 2000), "a" * 40])
def test_a_value_that_is_no_github_token_is_refused(world, text):
    offer = begin(world)
    with pytest.raises(ClickError) as caught:
        go(world, offer, {"github.token": seal(offer, text)})
    assert caught.value.code == "invalid.input" and text not in str(caught.value)
    assert stored(world) is None


@pytest.mark.parametrize(
    "row",
    [
        None,
        {"name": "github", "state": "off", "credential": "missing"},
        {"name": "github", "state": "active", "credential": None},
    ],
)
def test_an_addon_that_is_off_or_asks_for_no_key_takes_no_token(world, row):
    world.rows.clear()
    if row:
        world.rows["github"] = row
    with pytest.raises(ClickError):
        begin(world)
    world.rows["github"] = {"name": "github", "state": "active", "credential": "missing"}
    offer = begin(world)
    world.rows["github"] = row or {"name": "x", "state": "active", "credential": "missing"}
    with pytest.raises(ClickError):
        go(world, offer, {"github.token": seal(offer)})
    assert stored(world) is None


def test_the_wrong_shapes_of_a_call_are_refused(world):
    offer = begin(world)
    env = {"github.token": seal(offer)}
    for extra in ({"token": TOKEN}, {"passphrase": "x"}, {"begin": True}, {"remove": True}):
        with pytest.raises(ClickError):
            go(world, offer, env, **extra)
    for sealed_arg in ({}, {"github.token": {}, "x": 1}, {"hub.backup_key": env["github.token"]}):
        with pytest.raises(ClickError):
            go(world, offer, sealed_arg)
    with pytest.raises(ClickError):
        world.signer.handle("github_token", {"workspace_id": WID, "begin": False})
    assert stored(world) is None


def test_an_expired_offer_is_refused(world):
    offer = begin(world)
    world.mono.t += 301  # noqa: SLF001
    with pytest.raises(ClickError):
        go(world, offer, {"github.token": seal(offer)})
    assert stored(world) is None


def test_a_host_of_another_user_is_terminal_only(world):
    world.entry["host_uid"] = os.getuid() + 1
    with pytest.raises(ClickError) as caught:
        begin(world)
    assert caught.value.code == "custody.terminal_needed"
    assert "orch addon login github" in str(caught.value) and "convenience" not in str(caught.value)


def test_the_cli_and_the_dashboard_share_one_store_path(world, monkeypatch):
    from orch.cli import addon_credential

    seen = []
    real = credential.person_store
    monkeypatch.setattr(credential, "person_store", lambda *a: (seen.append(a[3]), real(*a))[1])
    offer = begin(world)
    go(world, offer, {"github.token": seal(offer)})
    assert seen == ["github"]
    assert "person_store" in open(addon_credential.__file__).read()
