"""#51: a crash between appending a ledger entry and rewriting the head cuts the ledger; a human, and only a human,
accepts exactly one trailing entry (valid signature, number head.count + 1) after seeing it."""
import json

import pytest

from orch import actor
from orch.cli import run
from orch.core import ledger, store
from orch.core.check import run_checks
from orch.errors import OrchError


def _cut(ws):
    return [f for f in run_checks(ws, emit_events=False) if f.code == "ledger-cut"]


def _crashed(ws, hops, put, appended=1):
    """A ticket closed (head in step), then `appended` more entries whose head rewrite never happened."""
    tid = put("open")
    hops.close(tid, "one")
    old_head = ledger.head_path().read_text(encoding="utf-8")
    for i in range(appended):
        hops.reopen(tid, f"r{i}")
        if i + 1 < appended:
            hops.close(tid, f"c{i}")
    ledger.head_path().write_text(old_head, encoding="utf-8")
    assert not ledger.head_ok() and _cut(ws)
    return tid


def _tail():
    return json.loads(ledger.ledger_path().read_text(encoding="utf-8").splitlines()[-1])


def test_a_human_repairs_the_crash_shape(ws, hops, put):
    tid = _crashed(ws, hops, put)
    tail = ledger.tail_to_repair()
    assert tail == _tail()
    hops.ledger_repair(tail["mac"][:8])
    assert ledger.head_ok() and not _cut(ws)
    assert json.loads(ledger.head_path().read_text())["count"] == tail["n"]
    hops.close(tid, "after")  # and the ledger keeps working
    assert ledger.head_ok()


def test_a_wrong_typed_id_changes_nothing(ws, hops, put):
    _crashed(ws, hops, put)
    before = ledger.head_path().read_text()
    with pytest.raises(OrchError, match="does not match"):
        hops.ledger_repair("deadbeef")
    assert ledger.head_path().read_text() == before and not ledger.head_ok()


def test_an_agent_cannot_repair(ws, aops, hops, put):
    _crashed(ws, hops, put)
    with pytest.raises(OrchError):
        aops.ledger_repair(_tail()["mac"][:8])
    assert not ledger.head_ok()


def test_two_trailing_entries_are_refused(ws, hops, put):
    _crashed(ws, hops, put, appended=2)
    with pytest.raises(OrchError, match="nothing to repair"):
        hops.ledger_repair(_tail()["mac"][:8])
    assert not ledger.head_ok()


def test_a_bad_signature_is_refused(ws, hops, put):
    _crashed(ws, hops, put)
    lines = ledger.ledger_path().read_text(encoding="utf-8").splitlines()
    e = json.loads(lines[-1])
    e["at"] = "2001-01-01T00:00Z"  # edited after signing
    lines[-1] = json.dumps(e)
    ledger.ledger_path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(OrchError, match="nothing to repair"):
        hops.ledger_repair(e["mac"][:8])


def test_a_wrong_number_is_refused(ws, hops, put):
    _crashed(ws, hops, put)
    lines = ledger.ledger_path().read_text(encoding="utf-8").splitlines()
    e = json.loads(lines[-1])
    e["n"] += 1
    e["mac"] = ledger._mac(ledger._key(create=False), e)  # validly signed, but not the next number
    lines[-1] = json.dumps(e)
    ledger.ledger_path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(OrchError, match="nothing to repair"):
        hops.ledger_repair(e["mac"][:8])
    assert not ledger.head_ok()


def test_a_ledger_in_step_has_nothing_to_repair(ws, hops, put):
    hops.close(put("open"), "one")
    with pytest.raises(OrchError, match="nothing to repair"):
        hops.ledger_repair("00000000")


def test_the_command_shows_the_entry_and_repairs_on_the_typed_id(ws, hops, put, monkeypatch, capsys):
    tid = _crashed(ws, hops, put)
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": _tail()["mac"][:8])
    assert run(["ledger", "repair"]) == 0
    out = capsys.readouterr().out
    assert "kind: reopen" in out and tid in out and "repaired" in out
    assert ledger.head_ok()
