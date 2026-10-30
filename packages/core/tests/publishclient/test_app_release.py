"""#1457 (HUB-PU-07): the words a person signs to let an app go live, and the signer's rules for ``app.release``.

An app runs code in other people's browsers, so only a person's one-click signature over exactly what goes live
releases it: an agent can stage a release, never sign it. The words are built by ``approval.app_release_text`` from the
staged release alone."""

from __future__ import annotations

import copy
import hashlib

import pytest

from orch.click import protocol, remote_kinds
from orch.hubclient import published as pub
from orch.publishclient import approval

SERVER = "https://publish.example.org"
SHARE = "https://apps.example.org"
APP = "ab" * 16


def file(path: str, body: bytes = b"x") -> dict:
    return {"path": path, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}


def make_meta(**kw) -> dict:
    meta = {
        "slug": "sales-board", "name": "Sales board", "kind": "app", "access": "public", "audience": None, "pw": None,
        "reach": [], "files": [file("app.js", b"1"), file("index.html", b"<p>")], "label": "v1", "ticket": None,
    }  # fmt: skip
    meta.update(kw)
    assert pub.meta_problem(meta) is None
    return meta


def words(**kw) -> str:
    base = dict(
        staged_id="pa_" + "c" * 32, ticket_key="TST-0001", origin="agent", op="commit", app_id=APP, version=2,
        meta=make_meta(), previous=None, server=SERVER, share=SHARE,
    )  # fmt: skip
    return approval.app_release_text(**{**base, **kw})


def test_first_release_words_name_what_goes_live_and_bind_it_by_hash():
    meta = make_meta(reach=["https://api.example.com"])
    text = words(meta=meta, version=1)
    lines = text.split("\n")
    assert lines[0] == approval.APP_MARK
    assert "Ticket: TST-0001" in lines and "Prepared by: an agent" in lines
    assert "Action: make this version live" in lines
    assert f"App: Sales board (app), address {SHARE}/a/sales-board" in lines
    assert "Who can open it: anyone with the link, no sign-in" in lines
    assert "The app may contact: https://api.example.com" in lines
    assert f"The files are sent to: {SERVER}" in lines
    assert "Compared with the live version: this is the first version" in lines
    assert sum(line.startswith("- app.js") or line.startswith("- index.html") for line in lines) == 2
    assert lines[-4:] == [
        f"Staged release: pa_{'c' * 32}", f"App id: {APP}", f"App hash: {pub.meta_hash(meta)}",
        f"Reach hash: {pub.reach_hash(meta['reach'])}",
    ]  # fmt: skip


def test_no_reach_is_said_plainly():
    assert "The app may contact: nothing outside the app itself" in words().split("\n")


def test_an_update_lists_what_widens_against_the_live_version():
    live = make_meta(access="password", pw=pub.make_password_record("pw", bytes(16)))
    new = make_meta(
        reach=["https://api.example.com"], files=[file("app.js", b"2"), file("extra.js"), file("index.html", b"<p>")]
    )
    text = words(meta=new, previous={"version": 1, "meta": live})
    assert "Compared with the live version 1:" in text
    assert "- Who can open it: was anyone with the link who knows the password you set; now anyone with the link, no sign-in" in text
    assert "- The app may now also contact: https://api.example.com" in text
    assert "- Files: 1 added, 1 changed, 0 removed" in text


def test_an_identical_update_says_nothing_changed():
    meta = make_meta()
    assert "- no change to who can open it, what it may contact or the files" in words(
        meta=meta, previous={"version": 1, "meta": meta}
    )


@pytest.mark.parametrize("access, audience, said", [
    ("company", "company", "everyone signed in to your company hub"),
    ("group", "group:sales", "the hub group sales"),
    ("group", "ws:" + "12" * 16, "the people of one workspace (12121212)"),
])  # fmt: skip
def test_every_access_has_plain_words(access, audience, said):
    assert f"Who can open it: {said}" in words(meta=make_meta(access=access, audience=audience)).split("\n")


def test_every_op_has_its_own_action_words():
    got = {op: next(x for x in words(op=op, previous=None, version=2).split("\n") if x.startswith("Action:")) for op in approval.APP_OPS}
    assert len(set(got.values())) == 3


def test_any_change_to_what_goes_live_changes_the_signed_text():
    base = words()
    meta = make_meta()
    variants = [
        make_meta(name="Sales board 2"), make_meta(reach=["https://api.example.com"]), make_meta(access="company", audience="company"),
        make_meta(files=[file("app.js", b"changed"), file("index.html", b"<p>")]), make_meta(label="other"),
    ]  # fmt: skip
    assert len({base, *(words(meta=m) for m in variants)}) == len(variants) + 1
    assert words(version=3) != base and words(app_id="cd" * 16) != base and words(op="start") != base
    assert words(meta=copy.deepcopy(meta)) == base  # deterministic


def test_a_hidden_change_in_a_listed_file_is_still_in_the_hash():
    """Only the first files are listed, but ``App hash`` covers every one of them."""
    many = [file(f"f{i:03d}.js", str(i).encode()) for i in range(40)] + [file("index.html")]
    many.sort(key=lambda f: f["path"])
    a = words(meta=make_meta(files=many))
    changed = copy.deepcopy(many)
    changed[30] = file(changed[30]["path"], b"evil")
    b = words(meta=make_meta(files=changed))
    assert "and 16 more files" in a and a != b


@pytest.mark.parametrize("bad", [
    {"staged_id": "ps_" + "a" * 32}, {"staged_id": "pa_x"}, {"origin": "robot"}, {"op": "stop"}, {"app_id": "zz"},
    {"version": 0}, {"version": True}, {"ticket_key": "TST-1\nmore"}, {"server": SERVER + "\nx"}, {"share": "a b"},
    {"meta": make_meta(name="a") | {"name": "line\nbreak"}}, {"meta": make_meta() | {"reach": ["http://x.example.com"]}},
    {"meta": make_meta() | {"files": []}}, {"previous": {"version": 5, "meta": make_meta()}},
    {"previous": {"version": 1, "meta": {"slug": "x"}}},
])  # fmt: skip
def test_the_words_refuse_anything_that_could_fake_a_line_or_a_value(bad):
    with pytest.raises(ValueError):
        words(**bad)


def test_app_release_is_a_one_click_op_with_risk_words_and_an_agent_cannot_sign_it():
    assert protocol.op_class("app.release") == "click"
    assert protocol.headline("app.release") == "Let an app go live"
    assert "app.release" not in protocol.ONE_PRESS_OPS
    assert protocol.risk_lines("app.release", [{"type": "log.added", "text": "x"}]) == [protocol.APP_RELEASE_RISK]
    # a remote device stream never signs it (the code goes live for other people)
    assert remote_kinds.classify("app.release", {"staged_id": "pa_" + "0" * 32}) == remote_kinds.REFUSED
    assert remote_kinds.TABLE["app.release"] == remote_kinds.REFUSED
