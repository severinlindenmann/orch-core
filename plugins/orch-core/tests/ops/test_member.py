"""``orch member add|remove|role``: roster changes signed by owners (and, for members and viewers, maintainers)."""

from __future__ import annotations

import json
import os

import pytest

from orch import crypto
from tests.identity.helpers import Person
from tests.store.helpers import WS


def secret_free(text: str) -> bool:
    """Whether the runtime's secret check (``Runtime.check_secret``) would let this text through."""
    from orch.ops.runtime import GRANT_SHAPE
    from orch.tokenshape import GITHUB_TOKEN_SHAPE

    return not (GRANT_SHAPE.search(text) or GITHUB_TOKEN_SHAPE.search(text))


def invite(ws, tmp, name="newbie"):
    """What an invitee hands the owner: their public key and their first device certificate (a file).

    Random key material is base64url, so about one certificate in 13,000 holds a ``ghp_...``-shaped run and the
    runtime refuses the file as ``grant.secret_in_args`` (#1041). The helper draws again until the file is clean, so
    a test never depends on the dice."""
    while True:
        p = Person()
        text = json.dumps(p.cert(created=ws.clock[0] * 1000))
        if secret_free(text):
            break
    cert = tmp / f"{name}.cert.json"
    cert.write_text(text)
    return p, crypto.b64u(p.pk_pub), str(cert)


def reader_decl(ws, p, tmp, *, level=1, name="reader") -> str:
    """The invitee's reader declaration file (``orch keys reader declare --enrol``, §0.8.6 item 9.4).
    The signature depends on ``created_ms``; it moves by a ms until the file passes the secret check (#1041)."""
    from tests.identity.reader_helpers import decl

    for bump in range(100):
        text = json.dumps(decl(p, WS, level=level, via="enrol", created_ms=ws.clock[0] * 1000 + bump))
        if secret_free(text):
            break
    else:  # pragma: no cover - 100 independent draws all unlucky: about 1e-400
        raise AssertionError("no clean reader declaration")
    path = tmp / f"{name}.{level}.decl.json"
    path.write_text(text)
    return str(path)


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


# --- the durable convenience_seen mark (#441) -------------------------------------------------------------------------


def test_member_add_is_refused_while_the_convenience_seen_mark_is_set(hws, me, tmp_path):
    """A restore can drop every convenience event from the merged history; the mark still refuses a new reader."""
    from orch.ops.readers import ensure_convenience_mark

    ensure_convenience_mark(hws.host_state, WS, os.getuid())
    _p, pk, cert = invite(hws, tmp_path)
    n = len(roster(hws))
    r = me("member", "add", "--name", "Nina", "--pk", pk, "--cert", cert, "--json")
    assert r.err_code == "sign.convenience_readers"
    assert len(roster(hws)) == n


def test_the_mark_fails_closed_and_is_never_cleared(tmp_path):
    from orch.ops.readers import convenience_seen, ensure_convenience_mark, seen_path

    wid = "a" * 32
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    assert not convenience_seen(state, wid)
    ensure_convenience_mark(state, wid, os.getuid())
    assert convenience_seen(state, wid) and json.loads(seen_path(state, wid).read_text())["seen"] is True
    ensure_convenience_mark(state, wid, os.getuid())  # idempotent
    # anything at the path counts as seen, a corrupt file or a link included
    seen_path(state, wid).write_text("{not json")
    assert convenience_seen(state, wid)
    seen_path(state, wid).unlink()
    seen_path(state, wid).symlink_to(tmp_path / "nowhere")
    assert convenience_seen(state, wid)
    # a directory that cannot be looked into: the doubt is "seen"
    other = "b" * 32
    (state / "hosts" / other).mkdir(mode=0o700)
    os.chmod(state / "hosts" / other, 0)
    try:
        assert convenience_seen(state, other)
    finally:
        os.chmod(state / "hosts" / other, 0o700)


def _as_convenience(monkeypatch):
    """The person's key signs with ``auth: "convenience"`` (as convenience custody does in direct mode)."""
    from orch.ops.human import Human

    real = Human.backend

    class AsConvenience:
        auth = "convenience"

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(Human, "backend", property(lambda self: AsConvenience(real.fget(self))))


def test_the_invite_helpers_never_hand_the_runtime_a_secret_shaped_file(hws, tmp_path, monkeypatch):
    """#1041: a ``ghp_``-shaped run in random key material made a member test fail now and then."""
    import tests.ops.test_member as me_mod

    dirty = Person()
    real = me_mod.Person
    queue = [dirty]
    real_cert = dirty.cert
    dirty.cert = lambda **kw: {**real_cert(**kw), "label": "ghp_" + "A" * 30}  # the first draw is unlucky
    monkeypatch.setattr(me_mod, "Person", lambda: queue.pop() if queue else real())
    p, _pk, cert = invite(hws, tmp_path)
    assert p is not dirty and secret_free(open(cert).read())
    assert not secret_free("x ghp_" + "A" * 30) and not secret_free("gr_0" + "A" * 25 + "." + "B" * 43)


def test_a_direct_convenience_append_sets_the_mark_first(hws, me, tmp_path, monkeypatch):
    from orch.ops.readers import seen_path

    _as_convenience(monkeypatch)
    p, pk, cert = invite(hws, tmp_path)
    assert not seen_path(hws.host_state, WS).exists()
    # H12d-2: a convenience-signed member.added makes the new member a reader of convenience history: level 1 needed
    assert me("member", "add", "--name", "Nina", "--pk", pk, "--cert", cert, "--json").err_code == (
        "sign.convenience_readers"
    )
    assert not seen_path(hws.host_state, WS).exists()  # refused before the mark: nothing was appended
    r = me(
        "member", "add", "--name", "Nina", "--pk", pk, "--cert", cert, "--reader-decl", reader_decl(hws, p, tmp_path)
    )
    assert r.code == 0, r.err
    assert roster(hws)[-1]["auth"] == "convenience"
    assert seen_path(hws.host_state, WS).exists()
    # a later reader is refused although the flag lives in no host here
    _q, qk, qcert = invite(hws, tmp_path, "other")
    assert me("member", "add", "--name", "O", "--pk", qk, "--cert", qcert, "--json").err_code == (
        "sign.convenience_readers"
    )


def test_a_direct_convenience_append_without_its_mark_writes_nothing(hws, me, tmp_path, monkeypatch):
    from orch.ops import readers

    def boom(*_a, **_k):
        raise OSError("no space left")

    _as_convenience(monkeypatch)
    monkeypatch.setattr(readers, "ensure_convenience_mark", boom)
    p, pk, cert = invite(hws, tmp_path)
    n = len(roster(hws))
    decl = reader_decl(hws, p, tmp_path)
    r = me("member", "add", "--name", "Nina", "--pk", pk, "--cert", cert, "--reader-decl", decl, "--json")
    assert r.err_code == "internal" and "convenience_seen" in r.doc["error"]["message"], r.doc
    assert len(roster(hws)) == n


def test_persisting_the_mark_never_trusts_the_conservative_reader(tmp_path, monkeypatch):
    """Codex #454 P2: a lookup error makes ``convenience_seen`` say "seen" (for refusals), but persisting the mark must
    raise instead of skipping the write, so the append is refused."""
    import pytest

    from orch.ops import readers

    wid = "c" * 32
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    real_lstat = os.lstat

    def flaky(path, *a, **k):
        if str(path).endswith(readers.SEEN_FILE):
            raise PermissionError(13, "denied")
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(readers.os, "lstat", flaky)
    assert readers.convenience_seen(state, wid)  # conservative for refusals
    with pytest.raises(OSError):
        readers.ensure_convenience_mark(state, wid, os.getuid())
    monkeypatch.undo()
    readers.seen_path(state, wid).parent.mkdir(parents=True, mode=0o700)
    readers.seen_path(state, wid).symlink_to(tmp_path / "elsewhere")
    with pytest.raises(Exception) as info:  # something else at the path: not the persisted mark
        readers.ensure_convenience_mark(state, wid, os.getuid())
    assert getattr(info.value, "code", "") == "custody.unsafe_mode"


# --- H12d-2: the reader declaration in direct mode (§0.8.6 item 9.4) -------------------------------------------------


def test_direct_member_add_with_a_declaration_writes_its_record(hws, me, tmp_path):
    from orch.store.readerrecs import read_record

    p, pk, cert = invite(hws, tmp_path)
    r = me(
        "member", "add", "--name", "Nina", "--pk", pk, "--cert", cert, "--reader-decl", reader_decl(hws, p, tmp_path)
    )
    assert r.code == 0, r.err
    rec = read_record(hws.host_state, WS, p.device[2:])
    assert rec["source"] == "member.add" and rec["reader"] == 1 and rec["decl"]["o"]["bind"] == {"via": "enrol"}


def test_direct_member_add_with_the_mark_needs_a_level_1_declaration(hws, me, tmp_path):
    from orch.ops.readers import ensure_convenience_mark

    ensure_convenience_mark(hws.host_state, WS, os.getuid())
    p, pk, cert = invite(hws, tmp_path)
    base = ("member", "add", "--name", "Nina", "--pk", pk, "--cert", cert, "--json")
    assert me(*base).err_code == "sign.convenience_readers"
    old = me(*base, "--reader-decl", reader_decl(hws, p, tmp_path, level=0))
    assert old.err_code == "sign.convenience_readers" and "reader_too_old" in old.doc["error"]["message"]
    assert me(*base, "--reader-decl", reader_decl(hws, p, tmp_path)).code == 0


def test_direct_member_add_refuses_a_declaration_that_does_not_verify(hws, me, tmp_path):
    p, pk, cert = invite(hws, tmp_path)
    q, _, _ = invite(hws, tmp_path, "other")
    base = ("member", "add", "--name", "Nina", "--pk", pk, "--cert", cert, "--json")
    r = me(*base, "--reader-decl", reader_decl(hws, q, tmp_path))  # another device's declaration
    assert r.err_code == "reader.decl_invalid" and r.doc["error"]["message"].startswith("signature")
    big = tmp_path / "big.json"
    big.write_text(" " * 5000)  # I2: capped before it is parsed
    assert me(*base, "--reader-decl", str(big)).err_code == "invalid.input"
    bad = tmp_path / "bad.json"
    bad.write_text('{"o": 1.5}')
    assert me(*base, "--reader-decl", str(bad)).err_code == "reader.decl_invalid"
    assert roster(hws) == []  # nothing was appended


def test_a_direct_convenience_append_is_refused_while_another_member_is_below_level_1(hws, me, tmp_path, monkeypatch):
    p, pk, cert = invite(hws, tmp_path)
    assert me("member", "add", "--name", "Nina", "--pk", pk, "--cert", cert).code == 0  # no declaration: level 0
    _as_convenience(monkeypatch)
    q, qk, qcert = invite(hws, tmp_path, "other")
    r = me("member", "add", "--name", "O", "--pk", qk, "--cert", qcert, "--reader-decl", reader_decl(hws, q, tmp_path),
           "--json")  # fmt: skip
    assert r.err_code == "sign.convenience_unavailable" and p.device in r.doc["error"]["message"], r.doc
    assert "other_member" in r.doc["error"]["message"]


def test_a_member_added_without_a_declaration_drops_a_stale_record_of_its_device(hws, me, tmp_path):
    """A record left from before a device was removed must not count again once it is re-added."""
    from orch.ops import readers
    from orch.store.readerrecs import read_record
    from tests.identity.reader_helpers import decl

    p, pk, cert = invite(hws, tmp_path)
    d = decl(p, WS, via="declare", created_ms=hws.clock[0] * 1000)
    got = readers.verify_admission(
        d, workspace_id=WS, cert=p.cert(), person=p.ref, via="declare", surfaces=("log",), now_ms=None
    )
    readers.store_record(hws.host_state, WS, os.getuid(), readers.make_record(got, d, p.ref, "declare", "t"))
    assert read_record(hws.host_state, WS, p.device[2:]) is not None
    assert me("member", "add", "--name", "Nina", "--pk", pk, "--cert", cert).code == 0
    assert read_record(hws.host_state, WS, p.device[2:]) is None


@pytest.mark.parametrize(("stale", "admitted"), [(0, 1), (1, 0)])
def test_a_direct_re_added_device_counts_at_the_admitted_level_never_a_stale_record(hws, me, tmp_path, stale, admitted):
    """#513 review (Opus M): a stored record with a later created_ms must not win over the admitted declaration."""
    from orch.ops import readers
    from orch.store.readerrecs import read_record, write_record
    from tests.identity.reader_helpers import decl

    p, pk, cert = invite(hws, tmp_path)
    d = decl(p, WS, level=stale, via="declare", created_ms=hws.clock[0] * 1000 + 100_000)
    got = readers.verify_admission(
        d, workspace_id=WS, cert=p.cert(), person=p.ref, via="declare", surfaces=("log",), now_ms=None
    )
    write_record(hws.host_state, WS, os.getuid(), readers.make_record(got, d, p.ref, "declare", "before"))
    path = reader_decl(hws, p, tmp_path, level=admitted)
    r = me("member", "add", "--name", "Nina", "--pk", pk, "--cert", cert, "--reader-decl", path)
    assert r.code == 0, r.err
    rec = read_record(hws.host_state, WS, p.device[2:])
    assert rec["reader"] == admitted and rec["source"] == "member.add"


def test_member_add_does_not_take_certificate_key_material_for_a_github_token(hws, me, tmp_path):
    """#1044: a token-shaped run inside a key field gets past the secret check (the certificate check then refuses the
    altered file); the same run in any other field of the file is a secret."""
    import random

    from tests.host.test_key_material_exempt import forced

    p, pk, cert = invite(hws, tmp_path)
    doc = json.load(open(cert))
    doc["o"]["dk_kx_pub"] = forced(65, random.Random(1044))
    planted = tmp_path / "planted.json"
    planted.write_text(json.dumps(doc))
    n = len(roster(hws))
    r = me("member", "add", "--name", "N", "--pk", pk, "--cert", str(planted), "--json")
    assert r.err_code != "grant.secret_in_args" and len(roster(hws)) == n
    doc["o"]["dk_kx_pub"] = json.load(open(cert))["o"]["dk_kx_pub"]
    doc["o"]["label"] = "ghp_" + "A" * 30
    planted.write_text(json.dumps(doc))
    assert me("member", "add", "--name", "N", "--pk", pk, "--cert", str(planted), "--json").err_code == (
        "grant.secret_in_args"
    )
    assert me("member", "add", "--name", "ghp_" + "A" * 30, "--pk", pk, "--cert", cert, "--json").err_code == (
        "grant.secret_in_args"
    )
