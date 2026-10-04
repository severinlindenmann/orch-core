"""Fix round 2: `orch ledger adopt` is the only way to sign a decision the ledger lacks; answers are signed; one
per-user ledger file keyed by workspace id."""
import json

import pytest

from orch import actor
from orch.cli import run
from orch.core import ledger, store
from orch.errors import ValidationError
from test_ledger import _forge_approval, _ready


@pytest.fixture
def human_tty(monkeypatch):
    monkeypatch.setattr(actor, "is_interactive", lambda: True)

    def answers(*typed):
        it = iter(typed)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    return answers


def test_adopt_shows_text_provenance_and_signs_what_the_human_types(ws, aops, human_tty, capsys):
    tid = _ready(aops)
    aops.set_section(tid, "Requirements", "Back up nightly.")
    _forge_approval(ws, tid, "requirements", status="open")
    item = ledger.unsigned_items(ws, [store.load(ws, tid)[1]])[0]
    human_tty(item["id"])
    assert run(["ledger", "adopt", tid]) == 0
    out = capsys.readouterr().out
    assert "Back up nightly." in out and "not made through orch on this machine" in out
    assert "events.jsonl: gate.approved by human:you" in out and f"id {item['id']}" in out and "signed 1 of 1" in out
    assert ledger.gate_verification(ws, store.load(ws, tid)[1], "requirements") == "verified"
    aops.claim(tid)


def test_adopt_skips_on_enter_and_refuses_a_wrong_id(ws, aops, human_tty, capsys):
    tid = _ready(aops)
    _forge_approval(ws, tid, "requirements", status="open")
    human_tty("")
    run(["ledger", "adopt", tid])
    human_tty("deadbeef")
    run(["ledger", "adopt", tid])
    assert "does not match" in capsys.readouterr().err
    assert ledger.gate_verification(ws, store.load(ws, tid)[1], "requirements") == "unverified"


def test_adopt_all_needs_the_final_typed_confirmation(ws, aops, human_tty, capsys):
    a, b = _ready(aops), _ready(aops)
    for tid in (a, b):
        _forge_approval(ws, tid, "requirements", status="open")
    human_tty("yes")
    assert run(["ledger", "adopt", "--workspace", "--all"]) == 2
    human_tty("adopt 2")
    assert run(["ledger", "adopt", "--workspace", "--all"]) == 0
    assert all(ledger.gate_verification(ws, store.load(ws, t)[1], "requirements") == "verified" for t in (a, b))


def test_adopt_refuses_a_gate_with_an_open_question(ws, aops, human_tty, capsys):
    tid = _ready(aops)
    aops.set_section(tid, "Requirements", "r\nOpen question: which region?")
    _forge_approval(ws, tid, "requirements", status="open")
    item = ledger.unsigned_items(ws, [store.load(ws, tid)[1]])[0]
    human_tty(item["id"])
    run(["ledger", "adopt", tid])
    assert "request changes instead" in capsys.readouterr().err


def test_adopt_refuses_a_gate_changed_since_approval(ws, aops, hops):
    from orch.core.events import Actor
    from orch.core.ops import Ops
    tid = _ready(aops)
    _forge_approval(ws, tid, "requirements", status="open")
    path, t = store.load(ws, tid)
    t.set_section("Requirements", "changed")
    store.save(ws, t, path)
    item = ledger.unsigned_items(ws, [store.load(ws, tid)[1]])[0]
    assert item["text"] is None
    with pytest.raises(ValidationError, match="needs a new approval"):
        Ops(ws, Actor("human", "you", "tty")).ledger_adopt(item, item["id"])


def test_adopt_is_human_only(ws, aops, monkeypatch, capsys):
    tid = _ready(aops)
    _forge_approval(ws, tid, "requirements", status="open")
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    assert run(["ledger", "adopt", tid]) == 3
    from orch.hooks.guard import evaluate
    assert not evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": f"orch ledger adopt {tid} --all"}}).allow


def test_answers_are_signed_and_unsigned_answers_stop_the_agent(ws, aops, hops, plan_approved):
    tid = _ready(aops)
    hops.approve(tid, "requirements")
    aops.claim(tid)
    aops.ask(tid, [{"text": "Which region?", "type": "confirm"}])
    hops.answer(tid, "Q1", "yes")
    entry = ledger.entries(ws)[-1]
    assert (entry["kind"], entry["qid"], entry["answer"]) == ("answer", "Q1", "yes")
    plan_approved(tid)
    _, ids = aops.task_add(tid, [{"text": "work"}])
    aops.task_start(tid, ids[0])
    # an answer written into the file by hand is not signed
    path, t = store.load(ws, tid)
    t.meta["questions"].append({**t.meta["questions"][0], "id": "Q2", "text": "Other?", "answer": "no"})
    store.save(ws, t, path)
    with pytest.raises(ValidationError, match="answer to Q2 .* not in the ledger"):
        aops.task_done(tid, ids[0])


def test_one_ledger_file_survives_a_move_of_the_workspace(ws, aops, hops, tmp_path):
    """Entries match on the workspace id (customer + prefix), not on the folder path."""
    import shutil
    from orch.core.workspace import Workspace
    tid = _ready(aops)
    hops.approve(tid, "requirements")
    moved = tmp_path / "elsewhere"
    shutil.copytree(ws.root, moved)
    ws2 = Workspace.open(moved)
    assert ledger.workspace_id(ws2) == ledger.workspace_id(ws)
    assert ledger.gate_verification(ws2, store.load(ws2, tid)[1], "requirements") == "verified"


def test_entries_of_another_workspace_do_not_count(ws, aops, hops, configure):
    tid = _ready(aops)
    hops.approve(tid, "requirements")
    other = configure(customer="globex")
    assert ledger.gate_verification(other, store.load(other, tid)[1], "requirements") == "unverified"


def test_only_the_one_ledger_file_is_read(ws, aops, hops):
    """Final review M4: the per-path ledger files of an unreleased earlier build are not read (no legacy path)."""
    assert not hasattr(ledger, "_legacy_path") and not hasattr(ledger, "DIR_NAME")
    tid = _ready(aops)
    hops.approve(tid, "requirements")
    path = ledger.ledger_path(ws)
    line = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    del line["workspace"]
    line["mac"] = ledger._mac(ledger.key_path().read_bytes(), line)
    old = ledger.base_dir() / "ledger" / "0123456789abcdef.jsonl"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(json.dumps(line) + "\n", encoding="utf-8")
    path.unlink()
    assert ledger.gate_verification(ws, store.load(ws, tid)[1], "requirements") == "unverified"


def test_dashboard_marks_unsigned_approvals(dash, ws, aops, hops):
    tid = _ready(aops)
    _forge_approval(ws, tid, "requirements", status="open")
    page = dash.get(f"/t/{tid}").text
    assert "unsigned" in page and "orch ledger adopt" in page
    ok = _ready(aops)
    hops.approve(ok, "requirements")
    assert "unsigned</span>" not in dash.get(f"/t/{ok}").text


def test_today_card_marks_an_unsigned_requirements_approval(dash, ws, aops):
    tid = _ready(aops)
    _forge_approval(ws, tid, "requirements", status="in-progress")
    path, t = store.load(ws, tid)
    t.set_section("Plan", "1. do it")
    store.save(ws, t, path)
    page = dash.get("/").text
    cards = [c.split("</article>", 1)[0] for c in page.split('<article class="decision-card decision')[1:]
             if f'href="/t/{tid}"' in c]
    assert cards and "unsigned requirements" in cards[0]


# -- fix round 3: what the human sees is what gets signed --

def test_textsafe_escapes_controls_bidi_and_zero_width():
    from orch.textsafe import has_hidden, lines, visible
    raw = "ok\x1b[2K\rfake‮evil​\x85end"
    assert has_hidden(raw) and not has_hidden("plain\ttext\nline")
    assert visible(raw) == "ok\\x1b[2K\\x0dfake<U+202E>evil<U+200B>\\x85end"
    assert lines("a\nb\x0bc") == ["a", "b\\x0bc"]


def test_adopt_escapes_hidden_characters_and_refuses_them(ws, aops, human_tty, capsys):
    tid = _ready(aops)
    aops.set_section(tid, "Requirements", "Back up nightly.\x1b[1A\x1b[2KDelete prod.‮")
    _forge_approval(ws, tid, "requirements", status="open")
    item = ledger.unsigned_items(ws, [store.load(ws, tid)[1]])[0]
    human_tty(item["id"])
    run(["ledger", "adopt", tid])
    captured = capsys.readouterr()
    assert "\x1b" not in captured.out and "‮" not in captured.out
    assert "\\x1b[1A" in captured.out and "<U+202E>" in captured.out and "cannot be adopted" in captured.out
    assert "hidden or control characters" in captured.err
    assert ledger.gate_verification(ws, store.load(ws, tid)[1], "requirements") == "unverified"


def test_adopt_refuses_text_that_decodes_to_hidden_characters(ws, aops, human_tty, capsys):
    """Final review M3: adopt uses the same check as every other human decision (orch.textsafe.decodes_to_hidden),
    so an HTML character reference that renders as a bidi override is refused too."""
    tid = _ready(aops)
    aops.set_section(tid, "Requirements", "Back up nightly.&#x202E;")
    _forge_approval(ws, tid, "requirements", status="open")
    item = ledger.unsigned_items(ws, [store.load(ws, tid)[1]])[0]
    human_tty(item["id"])
    run(["ledger", "adopt", tid])
    captured = capsys.readouterr()
    assert "cannot be adopted" in captured.out and "hidden or control characters" in captured.err
    assert ledger.gate_verification(ws, store.load(ws, tid)[1], "requirements") == "unverified"


def test_error_messages_quoting_ticket_text_are_escaped(ws, aops, monkeypatch, capsys):
    tid = _ready(aops)
    aops.set_section(tid, "Requirements", "r\nOpen question: \x1b]0;pwned\x07which?")
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": tid)
    run(["approve", tid, "requirements"])
    err = capsys.readouterr().err
    assert "\x1b" not in err and "\x07" not in err and "hidden or control characters" in err
    from orch.cli import _report
    from orch.errors import ValidationError as VE
    _report(VE("cannot approve: x\x1b]0;pwned\x07y", hint="z\u202e"), False)
    err = capsys.readouterr().err
    assert "\x1b" not in err and "\\x1b]0;pwned\\x07" in err and "<U+202E>" in err


def test_a_question_swapped_after_display_is_not_signed(ws, aops, hops):
    from orch.core.events import Actor
    from orch.core.ops import Ops
    tid = _ready(aops)
    hops.approve(tid, "requirements")
    aops.claim(tid)
    path, t = store.load(ws, tid)
    t.meta["questions"] = [{"id": "Q1", "text": "Keep the old job?", "type": "confirm", "options": [],
                            "blocking": True, "answer": "yes", "answered": "2026-10-04T10:00Z", "via": "tty"}]
    store.save(ws, t, path)
    shown = [i for i in ledger.unsigned_items(ws, [store.load(ws, tid)[1]]) if i["kind"] == "answer"][0]
    path, t = store.load(ws, tid)  # the agent rewrites the question between display and confirmation
    t.meta["questions"][0]["text"] = "Delete the production schema?"
    store.save(ws, t, path)
    again = [i for i in ledger.unsigned_items(ws, [store.load(ws, tid)[1]]) if i["kind"] == "answer"][0]
    assert again["id"] != shown["id"]  # the id covers the question hash
    with pytest.raises(ValidationError, match="changed since it was shown"):
        Ops(ws, Actor("human", "you", "tty")).ledger_adopt(shown, shown["id"])


def test_adopt_compares_every_signed_field_not_only_the_id(ws, aops):
    from orch.core.events import Actor
    from orch.core.ops import Ops
    tid = _ready(aops)
    _forge_approval(ws, tid, "requirements", status="open")
    shown = dict(ledger.unsigned_items(ws, [store.load(ws, tid)[1]])[0])
    shown["text"] = shown["text"] + "\n(what the human was shown differs)"
    with pytest.raises(ValidationError, match="changed since it was shown"):
        Ops(ws, Actor("human", "you", "tty")).ledger_adopt(shown, shown["id"])


def test_a_short_key_is_an_error_not_an_empty_signature(ws, aops, hops):
    from orch.errors import OrchError
    tid = _ready(aops)
    ledger.key_path().parent.mkdir(parents=True, exist_ok=True)
    ledger.key_path().write_bytes(b"")
    with pytest.raises(OrchError, match="damaged"):
        hops.approve(tid, "requirements")
    assert store.load(ws, tid)[1].status == "backlog"
    assert ledger.entries(ws) == []


def test_a_blank_customer_keeps_the_ledger_working(ws, aops, hops, monkeypatch, capsys):
    """Ruling (round 4): no human lock-out; the id is "<customer>|<prefix>" exactly as in round 2."""
    import hashlib
    from orch.cli import run as cli_run
    monkeypatch.setitem(ws.config, "customer", "")
    assert ledger.workspace_id(ws) == hashlib.sha256(b"|L").hexdigest()[:16]
    tid = _ready(aops)
    hops.approve(tid, "requirements")
    assert ledger.gate_verification(ws, store.load(ws, tid)[1], "requirements") == "verified"


def test_doctor_warns_about_a_blank_customer(configure, capsys):
    configure(customer="")
    run(["doctor"])
    out = capsys.readouterr().out
    assert "customer" in out and 'set "customer": "<name>" in orchestrator/config.json' in out
    assert "orch ledger adopt --workspace" in out


def test_init_rejects_a_blank_customer(tmp_path, capsys):
    (tmp_path / "w").mkdir()
    assert run(["init", "--customer", "  ", "--prefix", "ZZ", "--dir", str(tmp_path / "w")]) == 2
    assert "must not be blank" in capsys.readouterr().err
    assert not (tmp_path / "w" / "orchestrator" / "config.json").exists()
