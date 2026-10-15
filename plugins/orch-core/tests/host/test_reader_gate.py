"""H12d-2 at the host (orch-v2-host §0.8.6 item 9.4, 9.9, 9.11): ``member.add`` with a reader declaration, the review
line, invariant I at the append choke point (``append_signed``) and the relaxed readers condition."""

from __future__ import annotations

import base64
import json

import pytest

from orch import crypto
from orch.host import convenience, signer
from orch.ops import readers
from orch.store import readerrecs
from tests.host import human_helpers
from tests.host.human_helpers import err, ok
from tests.host.lifecycle_helpers import UID
from tests.host.test_click import Click, _click_sign, refused
from tests.identity.helpers import Person
from tests.identity.reader_helpers import decl

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


@pytest.fixture
def click(served):
    c = Click(served)
    yield c
    c.close()


def _now_ms(served) -> int:
    return int(served.service.clock() * 1000)


def _b64(doc) -> dict:
    return {"b64": base64.b64encode(json.dumps(doc).encode()).decode()}


def _member(served, level: int | None = 1, **kw):
    """``member.add`` args and files for a new person, with a reader declaration of ``level`` (None: none)."""
    q = Person()
    args = {"name": "q", "pk": crypto.b64u(q.pk_pub), "cert": "cert.json"}
    files = {"cert": _b64(q.cert())}
    if level is not None:
        args["reader_decl"] = "reader.json"
        d = decl(q, served.wid, level=level, via="enrol", created_ms=_now_ms(served), **kw)
        files["reader_decl"] = _b64(d)
    return q, args, files


def declare(served, p: Person, level: int = 1) -> None:
    """A verified record for ``p``'s device (what ``reader.declare`` writes)."""
    d = decl(p, served.wid, level=level, via="declare", created_ms=_now_ms(served))
    got = readers.verify_admission(
        d, workspace_id=served.wid, cert=p.cert(), person=p.ref, via="declare", surfaces=("log",), now_ms=None
    )
    readers.store_record(served.lab.state, served.wid, UID, readers.make_record(got, d, p.ref, "declare", "t"))


def _submit_member(human, args, files):
    prepared = human.prepare("member.add", args, files=files)
    signed = human.sign(prepared)
    return prepared, human.submit_res(prepared["request"]["id"], signed.events)


# --- member.add with a declaration -----------------------------------------------------------------------------------


def test_a_level_1_reader_is_admitted_while_the_flag_is_on_and_its_record_is_written(click, served, human):
    q, args, files = _member(served, 1)
    prepared = human.prepare("member.add", args, files=files)
    (block,) = prepared["material"]["review"]
    assert block["derived"]["reader"] == 1
    signer.check(prepared, workspace_id=served.wid, person=human.person.ref)  # the signer reproduces the review
    text = signer.render(prepared, lambda w: ("acme", "ACME"))
    assert "Reader: understands convenience signing (level 1)" in text
    signed = human.sign(prepared)
    ok(human.submit_res(prepared["request"]["id"], signed.events))
    rec = readerrecs.read_record(served.lab.state, served.wid, q.device[2:])
    assert rec["source"] == "member.add" and rec["reader"] == 1 and rec["person_id"] == q.ref[2:]


def test_without_a_declaration_the_review_says_old_and_the_signer_refuses_a_missing_level(served, human):
    _, args, files = _member(served, None)
    prepared = human.prepare("member.add", args, files=files)
    assert prepared["material"]["review"][0]["derived"]["reader"] == 0
    assert "Reader: old (level 0)" in signer.render(prepared, lambda w: ("acme", "ACME"))
    stale = json.loads(json.dumps(prepared))
    del stale["material"]["review"][0]["derived"]["reader"]  # a host before H12d-2: the signer cannot reproduce it
    with pytest.raises(signer.SignerRefused):
        signer.check(stale, workspace_id=served.wid, person=human.person.ref)


@pytest.mark.parametrize(("level", "reason"), [(None, "no_declaration"), (0, "reader_too_old")])
def test_an_old_reader_is_refused_with_convenience_history(click, served, human, level, reason):
    _, key = human.ticket()
    _click_sign(click, key)
    _, args, files = _member(served, level)
    res = human.prepare_res("member.add", args, files=files)
    assert err(res) == "sign.convenience_readers"
    assert res["error"]["detail"]["reason"] == reason


def test_a_level_1_reader_is_admitted_with_convenience_history_and_the_mark_only(click, served, human):
    _, key = human.ticket()
    _click_sign(click, key)
    convenience.set_enabled(served.lab.state, served.wid, False, UID)
    _, args, files = _member(served, 1)
    _, res = _submit_member(human, args, files)
    ok(res)


def test_a_bad_declaration_is_reader_decl_invalid(served, human):
    q, args, files = _member(served, 1)
    other = Person()
    files["reader_decl"] = _b64(decl(other, served.wid, level=1, via="enrol", created_ms=_now_ms(served)))
    res = human.prepare_res("member.add", args, files=files)
    assert err(res) == "reader.decl_invalid" and res["error"]["message"].startswith("signature")
    files["reader_decl"] = {"b64": base64.b64encode(b" " * 5000).decode()}  # I2: capped before it is parsed
    assert err(human.prepare_res("member.add", args, files=files)) == "invalid.input"


# --- invariant I at the append (authoritative) -----------------------------------------------------------------------


def test_the_append_refuses_an_old_reader_even_when_the_early_checks_were_skipped(click, served, human, monkeypatch):
    convenience.set_enabled(served.lab.state, served.wid, False, UID)
    _, args, files = _member(served, 0)
    prepared = human.prepare("member.add", args, files=files)  # no convenience yet: level 0 is fine
    signed = human.sign(prepared)
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    _, key = human.ticket()
    _click_sign(click, key)  # now a convenience event is in the history
    monkeypatch.setattr(convenience, "refuse_reader_op", lambda *a, **k: None)  # the early checks are skipped
    res = human.submit_res(prepared["request"]["id"], signed.events)
    assert err(res) == "sign.convenience_readers" and res["error"]["detail"]["reason"] == "reader_too_old"


def test_a_convenience_append_after_an_old_reader_landed_is_refused_naming_it(click, served, human, monkeypatch):
    convenience.set_enabled(served.lab.state, served.wid, False, UID)
    q, args, files = _member(served, None)
    _, res = _submit_member(human, args, files)
    ok(res)
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    _, key = human.ticket()
    e = refused(lambda: _click_sign(click, key), "sign.convenience_unavailable", "other_member")
    assert q.device in str(e)
    monkeypatch.setattr(convenience, "check", lambda *a, **k: None)  # the early condition is skipped
    e = refused(lambda: _click_sign(click, key, "again"), "sign.convenience_unavailable", "other_member")


# --- the relaxed readers condition (9.9) ----------------------------------------------------------------------------


def test_other_member_and_phone_device_count_only_readers_below_level_1(click, served, human):
    from tests.host.test_click import add_device

    _, key = human.ticket()
    q = served.lab.add_member()
    refused(lambda: click.prepare("log", {"ref": key, "text": "x"}), "sign.convenience_unavailable", "other_member")

    def problem():
        (row,) = click.signer.status({})["workspaces"]
        return row.get("problem")

    assert problem() == "readers_old"  # the click signer's status names it (9.11)
    declare(served, q, 1)
    p = click.prepare("log", {"ref": key, "text": "x"})  # every other reader is level 1 now
    assert p["request_id"] and problem() != "readers_old"
    add_device(served.lab)  # a second device of mine, no declaration
    refused(lambda: click.prepare("log", {"ref": key, "text": "y"}), "sign.convenience_unavailable", "phone_device")
    declare(served, q, 0)  # a newer, lower declaration lowers the level
    refused(lambda: click.prepare("log", {"ref": key, "text": "z"}), "sign.convenience_unavailable", "other_member")
