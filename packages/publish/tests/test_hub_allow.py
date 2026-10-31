"""P02 (#1290): a TRUSTED hub admits one of its own workspaces with a signed ``hub.allow``; nobody else gets in.
Covers: admit + first request, wrong owner, unpinned hub, replay, stale window, no held card, bad signature, wrong
workspace, leaving the hub, a re-join at a higher seq, the pin removed, a selective-only stranger."""

import json
import os

import pytest

from pubsrv import hub_path as H
from pubsrv import relay_proto as proto
from tests.hub_helpers import Hub, binding, signed, unbound
from tests.test_publish_api_auth import NOW, PERSON, WS_A, WS_B, Env, err

OTHER_PERSON = "b" * 32


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)  # NOTHING allowed by the operator: no owner row, no prebind


def trust(e, hub=None):
    e.sql("INSERT OR IGNORE INTO trusted_hubs VALUES (?,?)", (hub or e.hub).id, NOW)


def allow_obj(hub, ws=WS_A, person=PERSON, seq=1, ts=NOW, nonce=None, key=None, **over):
    o = {
        "v": 2, "suite": 2, "hv": 0, "kind": "hub.allow", "hub_id": hub.id, "workspace_id": ws,
        "owner_person_id": person, "binding_seq": seq, "ts_ms": ts,
        "nonce": nonce or proto.b64u(os.urandom(32)),
    }  # fmt: skip
    o.update(over)
    return signed(key or hub.op, H.LABELS["sig_hub_allow"], o)


def put_card(e, hub=None):
    e.sql("SELECT 1")
    from pubsrv import db

    conn = db.connect(e.path)
    try:
        H.accept_hub_card(conn, (hub or e.hub).card, NOW, True)
    finally:
        conn.close()


def put_allow(e, obj, ws=WS_A):
    return e.c.put(f"/v2/hub/allow/{ws}", content=json.dumps(obj).encode())


def bind(e, seq=1, hub=None):
    from pubsrv import db

    conn = db.connect(e.path)
    try:
        H.accept_binding(conn, WS_A, binding(e.wsk, hub or e.hub, seq=seq), NOW, True)
    finally:
        conn.close()


def admitted(e, **kw):
    """The whole happy path: pin, card, allow, register by card_put, binding."""
    trust(e)
    put_card(e)
    assert put_allow(e, allow_obj(e.hub, **kw)).status_code == 200
    r = e.register()
    assert r.status_code == 200, r.text
    bind(e, seq=kw.get("seq", 1))


def test_allowed_workspace_registers_and_the_first_request_goes_through(env):
    admitted(env)
    assert env.status().status_code == 200
    assert (
        env.sql("SELECT COUNT(*) n FROM owners")[0]["n"] == 0
    )  # no owner row was added: no wider authority


def test_without_the_allow_the_workspace_is_refused_as_before(env):
    trust(env)
    put_card(env)
    r = env.register()
    assert (r.status_code, err(r)) == (401, "unknown_workspace")


def test_unpinned_hub_is_refused(env):
    put_card(env)  # its card is held (anyone can send one) but the operator never pinned it
    r = put_allow(env, allow_obj(env.hub))
    assert (r.status_code, err(r)) == (401, "unknown_hub")
    assert env.register().status_code == 401


def test_pinned_hub_without_a_held_card_is_refused(env):
    trust(env)
    r = put_allow(env, allow_obj(env.hub))
    assert (r.status_code, err(r)) == (401, "unknown_hub")


def test_another_hubs_signature_is_refused(env):
    trust(env)
    put_card(env)
    r = put_allow(env, allow_obj(env.hub, key=Hub().op))
    assert (r.status_code, err(r)) == (401, "bad_hub_signature")


def test_replayed_nonce_and_stale_window_are_refused(env):
    trust(env)
    put_card(env)
    obj = allow_obj(env.hub)
    assert put_allow(env, obj).status_code == 200
    r = put_allow(env, obj)
    assert (r.status_code, err(r)) == (401, "replay")
    r = put_allow(env, allow_obj(env.hub, ts=NOW - 10**6))
    assert (r.status_code, err(r)) == (401, "stale_timestamp")


def test_path_and_object_must_name_the_same_workspace(env):
    trust(env)
    put_card(env)
    r = put_allow(env, allow_obj(env.hub, ws=WS_A), ws=WS_B)
    assert (r.status_code, err(r)) == (403, "other_workspace")


def test_wrong_owner_cannot_register(env):
    trust(env)
    put_card(env)
    assert put_allow(env, allow_obj(env.hub, person=OTHER_PERSON)).status_code == 200
    r = env.register()  # the card's owner is PERSON, the hub allowed OTHER_PERSON
    assert (r.status_code, err(r)) == (401, "unknown_workspace")


def test_allow_for_a_registered_workspace_of_another_owner_is_refused(env):
    env.allow()
    assert env.register().status_code == 200
    trust(env)
    put_card(env)
    r = put_allow(env, allow_obj(env.hub, person=OTHER_PERSON))
    assert (r.status_code, err(r)) == (403, "other_person")


def test_unknown_extra_field_and_junk_are_malformed(env):
    trust(env)
    put_card(env)
    assert err(put_allow(env, allow_obj(env.hub, extra=1))) == "malformed"
    assert err(put_allow(env, {"o": 1, "sig": "x"})) == "malformed"


def test_leaving_the_hub_ends_the_allowance(env):
    admitted(env)
    assert env.status().status_code == 200
    from pubsrv import db

    conn = db.connect(env.path)
    try:
        H.accept_unbound(conn, WS_A, unbound(env.wsk, env.hub, seq=1), NOW, True)
    finally:
        conn.close()
    assert env.sql("SELECT COUNT(*) n FROM hub_allows")[0]["n"] == 0
    r = env.status()
    assert r.status_code in (401, 403)
    # and an allow for the same (old) seq cannot bring it back
    r = put_allow(env, allow_obj(env.hub, seq=1))
    assert (r.status_code, err(r)) == (409, "unbound")


def test_a_rejoin_at_a_higher_seq_needs_a_new_allow(env):
    admitted(env)
    bind(env, seq=2)  # a new binding epoch: the allowance of seq 1 is dead
    r = env.status()
    assert (r.status_code, err(r)) == (403, "owner_not_allowed")
    assert put_allow(env, allow_obj(env.hub, seq=2)).status_code == 200
    assert env.status().status_code == 200
    r = put_allow(env, allow_obj(env.hub, seq=1))  # an old epoch never comes back
    assert (r.status_code, err(r)) == (409, "stale_binding")


def test_removing_the_pin_bites_on_the_next_request(env):
    admitted(env)
    assert env.status().status_code == 200
    env.sql("DELETE FROM trusted_hubs")
    assert err(env.status()) == "owner_not_allowed"


def test_binding_to_another_hub_does_not_carry_the_allowance(env):
    admitted(env)
    other = Hub()
    trust(env, other)
    put_card(env, other)
    from pubsrv import db

    conn = db.connect(env.path)
    try:
        H.accept_unbound(conn, WS_A, unbound(env.wsk, env.hub, seq=1), NOW, True)
        H.accept_binding(conn, WS_A, binding(env.wsk, other, seq=2), NOW, True)
    finally:
        conn.close()
    # the other hub sends no allow: the old hub's row is gone, and the other hub cannot use it
    assert err(env.status(headers=None)) in (
        "owner_not_allowed",
        "other_hub",
        "unknown_hub",
        "bad_hub_signature",
    )


def test_a_second_trusted_hub_cannot_take_over_the_row_of_a_bound_workspace(env):
    admitted(env)
    other = Hub()
    trust(env, other)
    put_card(env, other)
    r = put_allow(env, allow_obj(other))
    assert (r.status_code, err(r)) == (403, "other_hub")


def test_cli_trust_untrust_list(tmp_path, capsys):
    from pubsrv import cli

    d = str(tmp_path / "d")
    assert cli.main(["admin", "--data", d, "hub", "trust", "a" * 32]) == 0
    assert cli.main(["admin", "--data", d, "hub", "list"]) == 0
    assert "a" * 32 in capsys.readouterr().out
    assert cli.main(["admin", "--data", d, "hub", "untrust", "a" * 32]) == 0
    assert "untrusted" in capsys.readouterr().out


# --- the class: an allowance is dead by construction unless every dependency holds ------------------------------------


def revoke_op_cert(e, hub):
    from pubsrv import db

    cert = hub.card["op_cert"]["o"]["cert_id"]
    conn = db.connect(e.path)
    try:
        conn.execute("INSERT INTO hub_op_revoked VALUES (?,?,?)", (hub.id, cert, NOW))
    finally:
        conn.close()


def test_a_revoked_op_cert_ends_its_allowances(env):
    admitted(env)
    assert env.status().status_code == 200
    revoke_op_cert(env, env.hub)  # the row stays, the check refuses
    assert err(env.status()) in ("owner_not_allowed", "revoked", "unknown_hub", "bad_hub_signature")
    assert env.sql("SELECT COUNT(*) n FROM hub_allows")[0]["n"] == 1


def test_accepting_an_op_revocation_drops_the_rows_it_signed(env):
    from pubsrv import db

    admitted(env)
    cert = env.hub.card["op_cert"]["o"]["cert_id"]
    conn = db.connect(env.path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        from pubsrv import cleanup

        cleanup.drop_cert(conn, env.hub.id, cert)
        conn.execute("COMMIT")
    finally:
        conn.close()
    assert env.sql("SELECT COUNT(*) n FROM hub_allows")[0]["n"] == 0


def test_rebinding_to_a_new_hub_works_and_the_old_row_is_dead(env):
    admitted(env)
    other = Hub()
    trust(env, other)
    put_card(env, other)
    from pubsrv import db

    conn = db.connect(env.path)
    try:
        H.accept_unbound(conn, WS_A, unbound(env.wsk, env.hub, seq=1), NOW, True)
        H.accept_binding(conn, WS_A, binding(env.wsk, other, seq=2), NOW, True)
    finally:
        conn.close()
    # a stale row of the old hub (re-inserted by hand: dead by construction) does not block the new hub
    env.sql(
        "INSERT OR REPLACE INTO hub_allows VALUES (?,?,?,?,?,?)",
        WS_A,
        env.hub.id,
        PERSON,
        1,
        "c" * 32,
        NOW,
    )
    assert put_allow(env, allow_obj(other, seq=2)).status_code == 200
    row = env.sql("SELECT hub_id, binding_seq FROM hub_allows")[0]
    assert (row["hub_id"], row["binding_seq"]) == (other.id, 2)


def test_operator_unbind_leaves_no_allowance_and_a_hub_cannot_readmit(env):
    from pubsrv import cli

    admitted(env)
    assert cli.main(["admin", "--data", env.data, "ws", "unbind", WS_A]) == 0
    assert env.sql("SELECT COUNT(*) n FROM hub_allows")[0]["n"] == 0
    r = put_allow(env, allow_obj(env.hub))
    assert (r.status_code, err(r)) == (400, "workspace_blocked")
    assert env.register().status_code == 401
    # only the operator clears it
    assert cli.main(["admin", "--data", env.data, "ws", "allow", WS_A, PERSON]) == 0
    assert put_allow(env, allow_obj(env.hub)).status_code == 200


def test_guard_every_delete_on_the_admission_tables_goes_through_cleanup():
    """The invariant is kept in ONE place: no other module may DELETE from these tables."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "pubsrv"
    pat = re.compile(
        r"DELETE\s+FROM\s+(workspaces|hub_bindings|trusted_hubs|hub_allows|hub_denied)\b", re.I
    )
    offenders = [
        f.name for f in root.glob("*.py") if f.name != "cleanup.py" and pat.search(f.read_text())
    ]
    assert offenders == []
