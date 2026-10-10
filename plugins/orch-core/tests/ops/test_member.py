"""``orch member add|remove|role``: roster changes signed by owners (and, for members and viewers, maintainers)."""

from __future__ import annotations

import json

from orch import crypto
from tests.identity.helpers import Person


def invite(ws, tmp, name="newbie"):
    """What an invitee hands the owner: their public key and their first device certificate (a file)."""
    p = Person()
    cert = tmp / f"{name}.cert.json"
    cert.write_text(json.dumps(p.cert(created=ws.clock[0] * 1000)))
    return p, crypto.b64u(p.pk_pub), str(cert)


def roster(ws):
    return [e for e in ws.read_events("workspace") if e["type"] in ("member.added", "member.removed", "role.changed")]


def members(ws):
    s = ws.other()
    try:
        return {m.person: m.role for m in s.state.workspace.members.values()}
    finally:
        s.close()


def test_the_owner_adds_a_member_from_the_invitees_key_and_certificate(hws, me, tmp_path):
    p, pk, cert = invite(hws, tmp_path)
    r = me("member", "add", "--name", "Nina", "--pk", pk, "--cert", cert, "--role", "maintainer")
    assert r.code == 0, r.err
    assert r.first.startswith(f"ok member.added {p.ref} maintainer seq=")
    e = roster(hws)[-1]
    assert (e["person"], e["name"], e["role"], e["pk_pub"]) == (p.ref, "Nina", "maintainer", pk)
    assert (
        e["actor"]["id"] == hws.owner.ref and e["auth"] == "passphrase" and e["roster_v"] == 1
    )  # the member list as it was when it signed
    assert members(hws)[p.ref] == "maintainer"
    assert f"their person id is {p.ref}" in r.out and "out of band" in r.out


def test_the_certificate_can_come_on_stdin_and_a_second_add_is_refused(hws, me, tmp_path):
    p, pk, cert = invite(hws, tmp_path)
    text = open(cert).read()
    assert me("member", "add", "--name", "Nina", "--pk", pk, "--cert", "-", stdin=text).code == 0
    assert members(hws)[p.ref] == "member"  # the default role
    again = me("member", "add", "--name", "Nina", "--pk", pk, "--cert", cert, "--json")
    assert again.code == 5 and "member.exists" in again.doc["error"]["message"]


def test_a_bad_key_a_bad_certificate_or_another_persons_certificate_is_refused(hws, me, tmp_path):
    p, pk, cert = invite(hws, tmp_path)
    q, qk, qcert = invite(hws, tmp_path, "other")
    n = len(roster(hws))
    assert me("member", "add", "--name", "N", "--pk", "AAAA", "--cert", cert, "--json").err_code == "invalid.input"
    (tmp_path / "junk.json").write_text("{not json")
    assert (
        me("member", "add", "--name", "N", "--pk", pk, "--cert", str(tmp_path / "junk.json"), "--json").err_code
        == "parse.json"
    )
    r = me("member", "add", "--name", "N", "--pk", pk, "--cert", qcert, "--json")  # q's certificate for p's key
    assert r.code == 5 and r.err_code == "invalid.input" and len(roster(hws)) == n


def test_roles_the_last_owner_and_removal(hws, me, tmp_path):
    p, pk, cert = invite(hws, tmp_path)
    assert me("member", "add", "--name", "N", "--pk", pk, "--cert", cert).code == 0
    r = me("member", "role", p.ref, "maintainer")
    assert r.code == 0 and r.first.startswith(f"ok role.changed {p.ref} maintainer seq=")
    assert members(hws)[p.ref] == "maintainer"
    # the last owner can neither be demoted nor removed
    r = me("member", "role", hws.owner.ref, "member", "--json")
    assert r.code == 5 and "members.last_owner" in r.doc["error"]["message"]
    r = me("member", "remove", hws.owner.ref, "--json")
    assert r.code == 5 and "members.last_owner" in r.doc["error"]["message"]
    assert me("member", "role", p.ref, "owner").code == 0  # a second owner...
    assert me("member", "role", hws.owner.ref, "member").code == 0  # ...and now the first may step down
    assert members(hws)[hws.owner.ref] == "member"
    assert me("member", "remove", "p_" + "0" * 32, "--json").err_code == "not_found"


def test_remove_ends_the_members_standing(hws, me, tmp_path):
    q = hws.add_member("quinn", "member")
    assert me("member", "remove", q.ref).code == 0
    assert q.ref not in members(hws)
    assert roster(hws)[-1]["type"] == "member.removed"
    hws.act_as(q)
    hws.provider.requests.clear()
    r = me("grant", "--json")  # their device no longer belongs to a member
    assert r.code == 3 and r.err_code == "role.denied" and hws.provider.requests == []


def test_a_maintainer_adds_members_and_viewers_only(hws, me, tmp_path):
    m = hws.add_member("mia", "maintainer")
    hws.act_as(m)
    p, pk, cert = invite(hws, tmp_path)
    assert me("member", "add", "--name", "V", "--pk", pk, "--cert", cert, "--role", "viewer").code == 0
    q, qk, qcert = invite(hws, tmp_path, "q")
    r = me("member", "add", "--name", "Q", "--pk", qk, "--cert", qcert, "--role", "owner", "--json")
    assert r.code == 3 and r.err_code == "role.denied"
    r = me("member", "role", p.ref, "member", "--json")  # role changes are for owners
    assert r.code == 3 and r.err_code == "role.denied"
    assert members(hws)[p.ref] == "viewer"


def test_member_management_needs_the_operate_scope_of_the_device(hws, me, tmp_path):
    m = hws.add_member("dana", "maintainer", scopes=("look", "decide"))  # may decide, may not operate
    hws.act_as(m)
    p, pk, cert = invite(hws, tmp_path)
    n = len(roster(hws))
    r = me("member", "add", "--name", "N", "--pk", pk, "--cert", cert, "--json")
    assert r.code == 5 and "device.scope" in r.doc["error"]["message"], r.out
    assert len(roster(hws)) == n
