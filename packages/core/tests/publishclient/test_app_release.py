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
    assert "App name: \u201cSales board\u201d" in lines and f"Address: {SHARE}/a/sales-board" in lines
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
    assert (
        "- Who can open it: was anyone with the link who knows a password (set by an agent); "
        "now anyone with the link, no sign-in" in text
    )
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


def live(version=2, **kw):
    return {"version": version, "meta": make_meta(**kw)}


def test_every_op_has_its_own_action_words():
    got = {
        "commit": words(op="commit", version=3, previous=live(2)),
        "rollback": words(op="rollback", version=1, previous=live(2)),
        "start": words(op="start", version=2, previous=live(2)),
    }
    actions = {op: next(x for x in t.split("\n") if x.startswith("Action:")) for op, t in got.items()}
    assert len(set(actions.values())) == 3


@pytest.mark.parametrize("op, version, previous", [
    ("rollback", 1, None),  # a rollback needs the live version to go back from
    ("rollback", 2, live(2)),  # ... a version below it
    ("rollback", 3, live(2)),
    ("start", 2, None),  # a start is the live version itself
    ("start", 1, live(2)),
    ("start", 3, live(2)),
    ("commit", 2, live(2)),  # a commit is above the live version
    ("commit", 1, live(2)),
])  # fmt: skip
def test_the_version_must_fit_the_action_against_the_live_version(op, version, previous):
    with pytest.raises(ValueError):
        words(op=op, version=version, previous=previous)


def test_a_rollback_and_a_start_never_read_as_a_first_version():
    for op, version in (("rollback", 1), ("start", 2)):
        text = words(op=op, version=version, previous=live(2))
        assert "this is the first version" not in text and "Compared with the live version 2:" in text


def test_any_change_to_what_goes_live_changes_the_signed_text():
    base = words()
    meta = make_meta()
    variants = [
        make_meta(name="Sales board 2"), make_meta(reach=["https://api.example.com"]),
        make_meta(access="company", audience="company"),
        make_meta(files=[file("app.js", b"changed"), file("index.html", b"<p>")]), make_meta(label="other"),
    ]  # fmt: skip
    assert len({base, *(words(meta=m) for m in variants)}) == len(variants) + 1
    assert words(version=3) != base and words(app_id="cd" * 16) != base and words(op="start", previous=live(2)) != base
    assert words(meta=copy.deepcopy(meta)) == base  # deterministic


def test_a_change_the_review_does_not_list_is_still_in_the_hash():
    """Images past the listed ones are only counted; a same-size change to one changes only the ``App hash`` line."""
    images = [file(f"i{i:03d}.png", f"{i:03d}".encode()) for i in range(40)]
    many = sorted([*images, file("index.html")], key=lambda f: f["path"])
    a = words(meta=make_meta(files=many))
    changed = copy.deepcopy(many)
    k = next(i for i, f in enumerate(changed) if f["path"] == "i035.png")
    changed[k] = file("i035.png", b"xyz")  # the same size as the original, other bytes
    assert changed[k]["size"] == many[k]["size"] and changed[k]["sha256"] != many[k]["sha256"]
    b = words(meta=make_meta(files=changed))
    assert "and 15 more files that are not code" in a
    diff = [(x, y) for x, y in zip(a.split("\n"), b.split("\n"), strict=True) if x != y]
    assert len(diff) == 1 and diff[0][0].startswith("App hash: ")


def test_code_files_are_always_all_listed_first_and_files_are_counted_by_type():
    images = [file(f"i{i:03d}.png", str(i).encode()) for i in range(40)]
    code = [file("index.html"), file("sw.js", b"s"), file("engine.wasm", b"w"), file("a.css", b"c")]
    text = words(meta=make_meta(files=sorted([*images, *code], key=lambda f: f["path"])))
    lines = text.split("\n")
    rows = [x for x in lines if x.startswith("- ") and "sha256" in x]
    assert [r.split("  ")[0] for r in rows[:4]] == ["- index.html", "- sw.js", "- engine.wasm", "- a.css"]
    assert any(x.endswith(": 4 code files, 40 images") for x in lines if x.startswith("Files ("))
    assert "and 15 more files that are not code" in text


def test_the_name_is_quoted_on_its_own_line_and_the_address_cannot_be_spoofed():
    lines = words(meta=make_meta(name="Sales board")).split("\n")
    assert "App name: \u201cSales board\u201d" in lines and "Kind: app" in lines
    assert f"Address: {SHARE}/a/sales-board" in lines
    spoof = make_meta(name="Bank login at https://bank.example.org/a/x")
    text = words(meta=spoof)
    assert "App name: \u201cBank login at https://bank.example.org/a/x\u201d" in text.split("\n")
    assert sum(x.startswith("Address: ") for x in text.split("\n")) == 1
    with pytest.raises(ValueError):
        words(meta=make_meta(name="He said \u201cgo\u201d"))  # a quote could close the quoted name early


def test_a_name_that_mixes_scripts_is_flagged():
    mixed = make_meta(name="p\u0430ypal")  # Cyrillic a in a Latin word
    assert "(mixed letters: check the name carefully)" in words(meta=mixed)
    assert "mixed letters" not in words(meta=make_meta(name="Paypal board"))
    assert "mixed letters" not in words(meta=make_meta(name="\u0414\u043e\u0441\u043a\u0430"))  # one script


def test_the_password_wording_says_who_set_it_and_a_changed_password_is_shown():
    rec_a = pub.make_password_record("one", bytes(16))
    rec_b = pub.make_password_record("two", bytes(16))
    m1 = make_meta(access="password", pw=rec_a)
    m2 = make_meta(access="password", pw=rec_b)
    assert "knows a password (set by an agent)" in words(meta=m1, origin="agent")
    assert "knows a password (set by you)" in words(meta=m1, origin="person")
    assert "you set" not in words(meta=m1, origin="agent")
    text = words(meta=m2, previous={"version": 1, "meta": m1})
    assert "- Password: changed" in text
    same = words(meta=m1, previous={"version": 1, "meta": m1})
    assert "- Password: changed" not in same


@pytest.mark.parametrize("bad", [
    {"staged_id": "ps_" + "a" * 32}, {"staged_id": "pa_x"}, {"origin": "robot"}, {"op": "stop"}, {"app_id": "zz"},
    {"version": 0}, {"version": True}, {"ticket_key": "TST-1\nmore"}, {"server": SERVER + "\nx"}, {"share": "a b"},
    {"meta": make_meta(name="a") | {"name": "line\nbreak"}}, {"meta": make_meta() | {"reach": ["http://x.example.com"]}},
    {"meta": make_meta() | {"files": []}}, {"previous": {"version": 5, "meta": make_meta()}},
    {"op": "rollback", "previous": None},
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
