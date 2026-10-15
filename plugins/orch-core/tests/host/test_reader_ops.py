"""H12d-2: ``reader.declare`` and ``reader.list`` (orch-v2-host §0.8.6 item 9.10, 9.11), and the ``reader`` fields
of ``member.list`` and ``workspace.show``."""

from __future__ import annotations

from orch import crypto
from orch.store import readerrecs
from tests.host import human_helpers
from tests.host.human_helpers import err
from tests.host.human_helpers import ok as _ok
from tests.host.test_reader_gate import _now_ms
from tests.identity.reader_helpers import decl, malleate

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


def ok(res):
    return _ok(res)["data"]


def _declare(served, d):
    return served.client().req("reader.declare", {"reader_decl": d}, idem=True)


def test_a_person_declares_their_own_device_and_every_read_shows_it(served):
    owner = served.lab.ws.owner
    c = served.client()
    before = ok(c.req("reader.list"))
    assert before["required_reader"] == 0
    (row,) = [r for r in before["readers"] if r["device"] == owner.device]
    dk_hash = crypto.b64u(crypto.dk_hash(owner.sig_pub, owner.kx_pub))
    assert row == {
        "device": owner.device, "person": owner.ref, "dk_hash": dk_hash, "reader": 0, "level": 0, "digest": None,
        "decl": None,
    }  # fmt: skip
    d = decl(owner, served.wid, via="declare", created_ms=_now_ms(served))
    got = ok(_declare(served, d))
    assert got["device"] == owner.device and got["reader"] == 1 and got["level"] == 1
    assert readerrecs.read_record(served.lab.state, served.wid, owner.device[2:])["decl"] == d
    (row,) = [r for r in ok(c.req("reader.list"))["readers"] if r["device"] == owner.device]
    assert (row["reader"], row["level"], row["digest"], row["decl"]) == (1, 1, got["digest"], d)
    members = ok(c.req("member.list"))
    (dev,) = [x for m in members["members"] for x in m["devices"] if x["id"] == owner.device]
    assert dev["reader"] == 1 and members["required_reader"] == 0
    assert ok(c.req("workspace.show"))["required_reader"] == 0


def test_the_same_declaration_again_is_the_same_answer_whatever_its_signature(served):
    owner = served.lab.ws.owner
    d = decl(owner, served.wid, via="declare", created_ms=_now_ms(served))
    first = ok(_declare(served, d))
    assert ok(_declare(served, d)) == first
    assert ok(_declare(served, malleate(d))) == first  # I1: compared on sha256(cj(o)), the stored bytes stay


def test_a_lower_newer_declaration_withdraws_and_an_older_one_loses(served):
    owner = served.lab.ws.owner
    now = _now_ms(served)
    ok(_declare(served, decl(owner, served.wid, via="declare", created_ms=now)))
    older = ok(_declare(served, decl(owner, served.wid, level=0, via="declare", created_ms=now - 1000)))
    assert older["reader"] == 1  # the stored, later one is still current
    newer = ok(_declare(served, decl(owner, served.wid, level=0, via="declare", created_ms=now + 1000)))
    assert newer["reader"] == 0 and newer["level"] == 0


def test_a_declaration_is_refused_outside_its_window_for_another_device_or_another_path(served):
    owner = served.lab.ws.owner
    now = _now_ms(served)
    res = _declare(served, decl(owner, served.wid, via="declare", created_ms=now - 301_000))
    assert err(res) == "reader.decl_invalid" and res["error"]["message"].startswith("time")
    res = _declare(served, decl(owner, served.wid, via="enrol", created_ms=now))
    assert err(res) == "reader.decl_invalid" and res["error"]["message"].startswith("bind")
    q = served.lab.add_member()
    assert err(_declare(served, decl(q, served.wid, via="declare", created_ms=now))) == "op.not_allowed"
    assert err(_declare(served, {"o": 1, "sig": "x"})) == "reader.decl_invalid"
    assert readerrecs.device_ids(served.lab.state, served.wid) == []


# --- register imports the direct-mode records (§0.8.6 item 9.4 "Direct mode") ---------------------------------------


def test_register_keeps_the_records_that_verify_against_the_log_and_drops_the_rest(lab):
    import os

    from orch.ops import readers
    from tests.identity.helpers import Person
    from tests.store.helpers import WS

    now = lab.ws.clock[0] * 1000
    for p in (lab.ws.owner, Person()):  # the owner's device, and a device the log does not know
        d = decl(p, WS, via="declare", created_ms=now)
        got = readers.verify_admission(
            d, workspace_id=WS, cert=p.cert(), person=p.ref, via="declare", surfaces=("log",), now_ms=now
        )
        readers.store_record(lab.state, WS, os.getuid(), readers.make_record(got, d, p.ref, "declare", "t"))
    assert len(readerrecs.device_ids(lab.state, WS)) == 2
    with lab.host() as h, h.client() as c:
        res = lab.register(c)
        assert res["ok"] is True, res
    assert readerrecs.device_ids(lab.state, WS) == [lab.ws.owner.device[2:]]


def test_a_refused_registration_leaves_the_records_untouched(lab, tmp_path):
    """#513 review (Codex P2): an older checkout of a registered workspace is refused (host.busy) and prunes nothing:
    the records are pruned only after admission, under the store lock."""
    import os
    import shutil

    from orch.ops import readers
    from tests.identity.helpers import Person
    from tests.store.helpers import WS

    now = lab.ws.clock[0] * 1000
    with lab.host() as h, h.client() as c:
        res = lab.register(c)
        assert res["ok"] is True, res
        stranger = Person()  # a record the log does not back: a prune would drop it
        d = decl(stranger, WS, via="declare", created_ms=now)
        got = readers.verify_admission(
            d, workspace_id=WS, cert=stranger.cert(), person=stranger.ref, via="declare", surfaces=("log",),
            now_ms=now,
        )  # fmt: skip
        readers.store_record(lab.state, WS, os.getuid(), readers.make_record(got, d, stranger.ref, "declare", "t"))
        copy = tmp_path / "older-checkout"
        shutil.copytree(lab.root, copy, symlinks=True)
        again = lab.register(c, root=str(copy))
        assert again["ok"] is False, again
    assert readerrecs.device_ids(lab.state, WS) == [stranger.device[2:]]
