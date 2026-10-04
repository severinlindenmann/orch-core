"""#30: a signed head record beside the ledger (entry count and the newest MAC) notices a removed or replaced line
anywhere in the ledger, so no chained decision (status entries, settings) counts afterwards; and the checkout id of
a signed setting also binds the workspace's path inside its repository."""
import json
import subprocess
from types import SimpleNamespace

from orch.core import ledger, store
from orch.core.check import run_checks
from orch.core.ops import Ops


def _lines():
    return ledger.ledger_path().read_text(encoding="utf-8").splitlines(keepends=True)


def _keep(lines):
    ledger.ledger_path().write_text("".join(lines), encoding="utf-8")


def _closed(ws, tid):
    return ledger.done_verification(ws, store.load(ws, tid)[1], closed=True)


def _cut(ws):
    return [f for f in run_checks(ws, emit_events=False) if f.code == "ledger-cut"]


def _decisions(ws, hops, human, put):
    """Two closed tickets and the widgets setting turned on: three chained decisions and a few other entries."""
    a, b = put("open"), put("open")
    hops.close(a, "one")
    hops.close(b, "two")
    Ops(ws, human).set_widgets_html(True)
    assert _closed(ws, a) == _closed(ws, b) == "verified"
    assert ledger.signed_setting(ws, ledger.WIDGETS_HTML) is True
    return a, b


def _all_off(ws, a, b):
    assert _closed(ws, a) == _closed(ws, b) == "unverified"
    assert ledger.signed_setting(ws, ledger.WIDGETS_HTML) is None
    assert ledger.widgets_html_state(ws) != "on"
    (f,) = _cut(ws)
    assert f.level == "error" and "removed or replaced" in f.message


def test_normal_appends_keep_the_head_in_step(ws, hops, human, put):
    a, b = _decisions(ws, hops, human, put)
    head = json.loads(ledger.head_path().read_text())
    assert head["count"] == len(_lines()) and head["last"] == json.loads(_lines()[-1])["mac"]
    assert head["broken"] is False and ledger.head_ok() and not _cut(ws)
    hops.reopen(a, "more")
    hops.close(a, "again")
    assert ledger.head_ok() and _closed(ws, a) == "verified" and not _cut(ws)


def test_removing_the_newest_line_is_noticed(ws, hops, human, put):
    a, b = _decisions(ws, hops, human, put)
    _keep(_lines()[:-1])
    _all_off(ws, a, b)


def test_removing_a_line_in_the_middle_is_noticed(ws, hops, human, put):
    a, b = _decisions(ws, hops, human, put)
    lines = _lines()
    _keep(lines[:1] + lines[2:])
    _all_off(ws, a, b)


def test_truncating_the_ledger_is_noticed(ws, hops, human, put):
    a, b = _decisions(ws, hops, human, put)
    _keep(_lines()[:2])
    _all_off(ws, a, b)
    _keep([])
    assert not ledger.head_ok() and _cut(ws)


def test_replacing_the_newest_line_with_an_older_one_is_noticed(ws, hops, human, put):
    a, b = _decisions(ws, hops, human, put)
    lines = _lines()
    _keep(lines[:-1] + lines[:1])
    _all_off(ws, a, b)


def test_a_cut_stays_noticed_after_later_appends(ws, hops, human, put):
    a, b = _decisions(ws, hops, human, put)
    _keep(_lines()[:-1])
    c = put("open")
    hops.close(c, "later")
    assert _closed(ws, c) == "unverified"
    _all_off(ws, a, b)


def test_a_damaged_head_record_fails_closed(ws, hops, human, put):
    a, b = _decisions(ws, hops, human, put)
    head = json.loads(ledger.head_path().read_text())
    ledger.head_path().write_text(json.dumps({**head, "count": head["count"] - 1}))  # not signed any more
    _all_off(ws, a, b)
    ledger.head_path().write_text("not json")
    assert not ledger.head_ok()


def _as_before_the_head_record():
    """The ledger as a version from before the head record wrote it: no numbered entries and no head."""
    key = ledger._key(create=False)
    out = []
    for line in _lines():
        e = json.loads(line)
        e.pop("n")
        e["mac"] = ledger._mac(key, e)
        out.append(json.dumps(e) + "\n")
    _keep(out)
    ledger.head_path().unlink()


def test_a_ledger_from_before_the_head_record_keeps_verifying_and_gets_one(ws, hops, human, put):
    a, b = _decisions(ws, hops, human, put)
    _as_before_the_head_record()
    assert ledger.head_ok() and not _cut(ws)
    assert _closed(ws, a) == _closed(ws, b) == "verified"
    assert ledger.signed_setting(ws, ledger.WIDGETS_HTML) is True
    c = put("open")
    hops.close(c, "next")
    head = json.loads(ledger.head_path().read_text())
    assert head["count"] == len(_lines()) == json.loads(_lines()[-1])["n"] and head["broken"] is False
    assert ledger.head_ok() and _closed(ws, a) == _closed(ws, c) == "verified"
    _keep(_lines()[:-1])
    assert not ledger.head_ok()


def test_removing_the_head_record_after_it_existed_fails_closed(ws, hops, human, put):
    a, b = _decisions(ws, hops, human, put)
    ledger.head_path().unlink()
    _all_off(ws, a, b)
    c = put("open")
    hops.close(c, "after")  # a later append does not quietly start over
    assert _closed(ws, c) == "unverified" and json.loads(ledger.head_path().read_text())["broken"] is True
    _all_off(ws, a, b)


def test_an_older_valid_head_record_put_back_fails_closed(ws, hops, human, put):
    a, b = _decisions(ws, hops, human, put)
    older = ledger.head_path().read_text()
    c = put("open")
    hops.close(c, "newer")
    ledger.head_path().write_text(older)  # signed and once true, but not for this ledger
    _all_off(ws, a, b)
    # Removing the newer entries as well rolls both files back together: that needs an anchor outside them.


def test_a_head_record_with_a_bad_signature_fails_closed(ws, hops, human, put):
    a, b = _decisions(ws, hops, human, put)
    head = json.loads(ledger.head_path().read_text())
    ledger.head_path().write_text(json.dumps({**head, "mac": "0" * 64}))
    _all_off(ws, a, b)


def test_a_renumbered_entry_fails_closed(ws, hops, human, put):
    a, b = _decisions(ws, hops, human, put)
    key = ledger._key(create=False)
    lines = _lines()
    e = json.loads(lines[1])
    e["n"] = 7
    e["mac"] = ledger._mac(key, e)
    _keep(lines[:1] + [json.dumps(e) + "\n"] + lines[2:])
    _all_off(ws, a, b)


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.test", "-C", str(cwd), *args], check=True)


def _cid(root):
    ledger._checkouts.clear()
    return ledger.checkout_id(SimpleNamespace(home=root / "orchestrator", root=root))


def test_a_nested_workspace_gets_its_own_checkout_id_and_worktrees_share_it(tmp_path):
    main, tree = tmp_path / "main", tmp_path / "tree"
    (main / "team" / "a").mkdir(parents=True)
    (main / "team" / "b").mkdir(parents=True)
    _git("init", "-q", cwd=main)
    (main / "team" / "a" / "x").write_text("x")
    (main / "team" / "b" / "x").write_text("x")
    _git("add", ".", cwd=main)
    _git("commit", "-q", "-m", "x", cwd=main)
    _git("worktree", "add", "-q", str(tree), cwd=main)
    top, a, b = _cid(main), _cid(main / "team" / "a"), _cid(main / "team" / "b")
    assert len({top, a, b}) == 3
    assert _cid(tree) == top and _cid(tree / "team" / "a") == a and _cid(tree / "team" / "b") == b


def test_the_guard_keeps_agents_away_from_the_head_record(ws):
    from orch.hooks.guard import evaluate
    path = str(ledger.head_path())
    for payload in ({"tool_name": "Write", "tool_input": {"file_path": path, "content": "{}"}},
                    {"tool_name": "Read", "tool_input": {"file_path": path}},
                    {"tool_name": "Bash", "tool_input": {"command": f"rm {path}"}},
                    {"tool_name": "Bash", "tool_input": {"command": "rm ~/.config/orch/ledger.head"}}):
        assert not evaluate(ws, payload).allow, payload


def test_an_old_unnumbered_line_copied_to_the_end_fails_closed(ws, hops, human, put):
    a, b = _decisions(ws, hops, human, put)
    key = ledger._key(create=False)
    first = json.loads(_lines()[0])
    first.pop("n")
    first["mac"] = ledger._mac(key, first)  # a valid line as an older version wrote it
    _keep(_lines() + [json.dumps(first) + "\n"])
    _all_off(ws, a, b)


def test_a_line_added_after_the_head_fails_closed(ws, hops, human, put):
    a, b = _decisions(ws, hops, human, put)
    _keep(_lines() + _lines()[-1:])  # a numbered line repeated: wrong place
    _all_off(ws, a, b)


def test_a_cut_ledger_backs_no_delegation_or_grant(ws, hops, human, put):
    from orch.core import epics, permits
    epic = put("open")
    ep = store.load(ws, epic)[1]
    Ops(ws, human)  # a signed pause and a charter-like entry stand in for the delegation records
    for kind, extra in (("charter", {"delegation": "D1", "delegate": {}, "epic_hash": "x", "hash_v": 3}),
                        ("grant", {"grant": "G1"})):
        ledger.record(ws, ticket=epic, kind=kind, actor=human, evidence=None, **extra)
    assert epics.latest_charter(ws, epic) is not None
    assert any(e.get("kind") == "grant" for e in permits._signed(ws, None))
    _keep(_lines()[:-1])
    assert epics.latest_charter(ws, epic) is None and epics.delegation(ws, ep) is None
    assert permits._signed(ws, None) == []


def test_a_failed_head_write_takes_the_entry_back(ws, hops, human, put, monkeypatch):
    import pytest
    from orch.errors import OrchError
    a, b = _decisions(ws, hops, human, put)
    before = ledger.ledger_path().read_bytes()
    head = ledger.head_path().read_text()

    def boom(*args, **kw):
        raise OSError("disk full")
    with monkeypatch.context() as m:
        m.setattr(ledger, "_write_head", boom)
        with pytest.raises(OrchError):
            ledger.record(ws, ticket=a, kind="close", actor=human, evidence=None, reason="x")
    assert ledger.ledger_path().read_bytes() == before and ledger.head_path().read_text() == head
    assert ledger.head_ok() and _closed(ws, a) == "verified"


def test_the_guard_keeps_agents_away_from_the_ledger_lock(ws):
    from orch.hooks.guard import evaluate
    path = str(ledger.base_dir() / ledger.LOCK_FILE)
    for payload in ({"tool_name": "Write", "tool_input": {"file_path": path, "content": ""}},
                    {"tool_name": "Bash", "tool_input": {"command": f"rm {path}"}}):
        assert not evaluate(ws, payload).allow, payload
