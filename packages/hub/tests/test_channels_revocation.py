"""V3-D7: active_channel, revocation, snapshots, roles of the revoker, the CLI."""

from __future__ import annotations

import base64
import json

import pytest
from channels_helpers import PW, Env, register, wall_codes
from test_channels_service import held, refused

from orch_hub.access import ADMIN_GROUP
from orch_hub.cli import main
from orch_hub.contract import hub as H
from orch_hub.contract.canon import b64u

WS = "a1" * 16


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def snapshot_revoked(env):
    s = env.snaps.snapshot(WS, "direct")
    o = H.verify_snapshot(
        s, held(env), workspace_id=WS, dev_mode=True, pinned=None, revoked_op_certs=env.keys.revoked_op_certs()
    )
    return o["revoked_channels"]


def test_revoke_makes_active_channel_none_at_once_and_lands_in_next_snapshot(env):
    u = env.user("ann")
    _, cert = register(env, u)
    cid = cert["o"]["channel_id"]
    assert env.svc.active_channel(u, cid) == cert
    assert snapshot_revoked(env) == []
    env.svc.revoke(cid, actor=u)
    assert env.svc.active_channel(u, cid) is None
    assert snapshot_revoked(env) == [cid]
    assert [c["status"] for c in env.svc.channels(u)] == ["revoked"]
    assert [e["action"] for e in env.svc.events()] == ["register", "revoke"]
    env.svc.revoke(cid, actor=u)  # idempotent, no second event
    assert [e["action"] for e in env.svc.events()] == ["register", "revoke"]


def test_revoked_key_cannot_register_again_and_frees_a_slot(env):
    u = env.user("ann")
    env.svc.max_channels = 1
    s, cert = register(env, u)
    env.svc.revoke(cert["o"]["channel_id"], actor=u)
    t = env.sign_in(u)
    begun = env.svc.begin_registration(t, s.public)
    from channels_helpers import prove

    refused("channel_revoked", env.svc.complete_registration, begun["challenge_id"], prove(s, begun))
    register(env, u)  # a new key fits in the freed slot


def test_expired_cert_is_inactive(env):
    u = env.user("ann")
    _, cert = register(env, u)
    cid = cert["o"]["channel_id"]
    env.clock.advance(31 * 24 * 3600 * 1000 - 1)
    assert env.svc.active_channel(u, cid) == cert
    env.clock.advance(2)
    assert env.svc.active_channel(u, cid) is None
    assert env.svc.channels(u)[0]["status"] == "expired"


def test_active_channel_checks_user_registration_and_op_cert(env):
    u, v = env.user("ann"), env.user("bob")
    _, cert = register(env, u)
    cid = cert["o"]["channel_id"]
    assert env.svc.active_channel(v, cid) is None  # another user's channel
    assert env.svc.active_channel(u, "00" * 16) is None
    assert env.svc.active_channel(u, "nothex") is None and env.svc.active_channel(None, cid) is None
    # a channel certified by the dev CLI path without proof of possession is not a registered channel
    from channels_helpers import new_channel

    raw = env.keys.issue_channel(u, new_channel().public, "password+totp")
    assert env.svc.active_channel(u, raw["o"]["channel_id"]) is None
    # a routine op rotation reissues the certificate; it stays active under the new op cert
    env.keys.rotate_op()
    again = env.svc.active_channel(u, cid)
    assert again is not None and again["o"]["op_cert_id"] == env.keys.op_cert()["o"]["cert_id"]
    # the user disappears: their channels die
    env.acc.remove_user(u)
    assert env.svc.active_channel(u, cid) is None


def test_active_channel_after_op_revocation_is_none(env):
    u = env.user("ann")
    _, cert = register(env, u)
    env.keys.revoke_op()
    assert env.svc.active_channel(u, cert["o"]["channel_id"]) is None


def test_hub_admin_can_revoke_another_users_channel_normal_user_cannot(env):
    ann, bob, root = env.user("ann"), env.user("bob"), env.user("root")
    _, c_ann = register(env, ann)
    _, c_bob = register(env, bob)
    a, b = c_ann["o"]["channel_id"], c_bob["o"]["channel_id"]
    refused("forbidden", env.svc.revoke, b, actor=ann)
    refused("forbidden", env.svc.revoke, "00" * 16, actor=ann)  # same answer for a channel that does not exist
    assert env.svc.active_channel(bob, b) is not None
    env.acc.add_member(ADMIN_GROUP, root)
    env.svc.revoke(b, actor=root)
    assert env.svc.active_channel(bob, b) is None and env.svc.active_channel(ann, a) is not None
    assert env.svc.channels(bob)[0]["revoked_by"] == f"u_{root}"
    assert env.svc.events()[-1] == {**env.svc.events()[-1], "action": "revoke", "actor": f"u_{root}", "user_id": bob}
    refused("unknown_user", env.svc.revoke, a, actor="cd" * 16)


def test_revoke_as_session_and_list_for_session(env):
    ann, bob = env.user("ann"), env.user("bob")
    _, c_ann = register(env, ann)
    _, c_bob = register(env, bob)
    t = env.sign_in(ann)
    assert [c["channel_id"] for c in env.svc.channels_for_session(t)] == [c_ann["o"]["channel_id"]]
    refused("forbidden", env.svc.revoke_as, t, c_bob["o"]["channel_id"])
    env.svc.revoke_as(t, c_ann["o"]["channel_id"])
    assert env.svc.active_channel(ann, c_ann["o"]["channel_id"]) is None


def test_revocation_is_permanent_and_the_key_never_registers_again(env):
    from channels_helpers import prove

    assert not hasattr(env.svc, "unrevoke") and not hasattr(env.acc, "unrevoke_channel")
    u = env.user("ann")
    s, cert = register(env, u)
    cid = cert["o"]["channel_id"]
    env.svc.revoke(cid)
    assert env.svc.active_channel(u, cid) is None
    begun = env.svc.begin_registration(env.sign_in(u), s.public)
    refused("channel_revoked", env.svc.complete_registration, begun["challenge_id"], prove(s, begun))
    assert env.svc.channels(u)[0]["status"] == "revoked" and len(env.svc.channels(u)) == 1


def test_revoking_an_unregistered_channel_is_refused(env):
    from channels_helpers import new_channel

    from orch_hub.contract.hub import channel_id

    u = env.user("ann")
    cid = channel_id(new_channel().public)
    refused("unknown_channel", env.svc.revoke, cid)  # the operator
    refused("forbidden", env.svc.revoke, cid, actor=u)  # a user cannot probe ids
    assert env.acc.revoked_channels() == [] and env.audit.store.after(0, 10) == []


def run(state, capsys, *argv, stdin=None, code=0, monkeypatch=None):
    if stdin is not None:
        import io
        import sys

        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    rc = main([argv[0], "--state", str(state), *argv[1:]])
    out = capsys.readouterr()
    assert rc == code, out.err
    return json.loads(out.out) if code == 0 and out.out.strip() else out.err


def test_cli_password_totp_list_revoke_and_pairing(tmp_path, capsys, monkeypatch):
    from orch_hub.keys import HubKeys

    st = tmp_path / "hub"
    HubKeys.init(st)
    u = run(st, capsys, "users", "add", "ann")["user_id"]
    run(st, capsys, "users", "set-password", u, stdin=PW + "\n", monkeypatch=monkeypatch)
    secret = run(st, capsys, "users", "totp", u)["secret"]
    assert PW not in json.dumps(secret)
    code = run(st, capsys, "channels", "pair-code", "--user", u)["code"]
    assert run(st, capsys, "channels", "list") == []
    from channels_helpers import new_channel, prove

    keys = HubKeys.open(st)
    from orch_hub.access import Access
    from orch_hub.channels import Channels

    acc = Access(str(st / "access.db"))
    svc = Channels(keys, acc, st / "channels.db", dev_mode=True)
    s = new_channel()
    begun = svc.begin_phone_registration(
        u,
        code,
        s.public,
        "iPhone",
        token=svc.sign_in(u, PW, wall_codes(base64.b32decode(secret + "=" * (-len(secret) % 8)))[0])["token"],
    )
    cert = svc.complete_registration(begun["challenge_id"], prove(s, begun))
    cid = cert["o"]["channel_id"]
    svc.close()
    acc.close()
    rows = run(st, capsys, "channels", "list", "--user", u)
    assert rows[0]["channel_id"] == cid and rows[0]["status"] == "active" and rows[0]["kind"] == "phone"
    run(st, capsys, "channels", "revoke", cid)
    assert run(st, capsys, "channels", "list")[0]["status"] == "revoked"
    assert run(st, capsys, "channels", "revoked") == [cid]
    assert [e["action"] for e in run(st, capsys, "channels", "events")] == ["pairing_code", "register", "revoke"]
    from orch_hub.audit import AuditStore

    store = AuditStore(str(st / "audit.db"))  # the terminal revoke is on the chain, by the operator
    (rec,) = [e for _, r, e in store.after(0, 100) if e.get("change") == "revoke"]
    store.close()
    assert rec["channel_id"] == cid and rec["actor_user_id"] is None
    assert (st / "channels.db").stat().st_mode & 0o077 == 0
    assert b64u(s.public) not in json.dumps(run(st, capsys, "channels", "revoked"))
