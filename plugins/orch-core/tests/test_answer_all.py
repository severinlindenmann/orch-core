"""#219: `orch answer <id> --all` (human only, terminal): one prompt per open question, one typed-id confirmation,
every answer bound to its own question_hash. No agent draft, no pre-fill."""
import pytest

from orch import actor
from orch.cli import run
from orch.core import store
from orch.core.events import Actor, read_events
from orch.core.ops import Ops
from orch.core.questions import find_question, question_hash
from orch.errors import HumanOnlyError, ValidationError

QUESTIONS = ("questions:\n"
             "  - text: Which region?\n    options:\n      - {key: A, label: Frankfurt}\n      - {key: B, label: Zurich}\n"
             "  - text: Keep the old endpoint?\n    type: confirm\n"
             "  - text: Anything else?\n    type: text\n    blocking: false\n")


@pytest.fixture
def switch(monkeypatch, ws_root):
    class Switch:
        def agent(self):
            monkeypatch.setenv("ORCH_HARNESS", "test-agent")
            monkeypatch.setenv("ORCH_SESSION", "s-1")
            monkeypatch.setattr(actor, "is_interactive", lambda: False)

        def human(self):
            monkeypatch.delenv("ORCH_HARNESS", raising=False)
            monkeypatch.setattr(actor, "is_interactive", lambda: True)

    s = Switch()
    s.agent()
    return s


def _ok(capsys, *args):
    code = run(list(args))
    out = capsys.readouterr()
    assert code == 0, (args, out)
    return out.out


def _ws(ws_root):
    from orch.core.workspace import Workspace
    return Workspace.open(ws_root)


@pytest.fixture
def asked(switch, ws_root, capsys, tmp_path):
    _ok(capsys, "new", "--title", "Export")
    qf = tmp_path / "q.yaml"
    qf.write_text(QUESTIONS, encoding="utf-8")
    _ok(capsys, "ask", "L-0001", "--file", str(qf))
    switch.human()
    return ws_root


def _typed(monkeypatch, replies, during=None):
    """Feed the prompts in order; `during()` runs just before the last reply (the confirmation)."""
    queue = list(replies)
    prompts = []

    def fake(prompt=""):
        prompts.append(prompt)
        if during and len(queue) == 1:
            during()
        return queue.pop(0)
    monkeypatch.setattr("builtins.input", fake)
    return prompts


def _qs(ws_root):
    return store.load(_ws(ws_root), "L-0001")[1].meta["questions"]


def _unanswered(ws_root):
    return all(q.get("answer") in (None, "") for q in _qs(ws_root))


def test_all_prompts_each_question_then_one_confirmation(asked, capsys, monkeypatch):
    prompts = _typed(monkeypatch, ["B", "yes", "", "L-0001"])
    assert run(["answer", "L-0001", "--all"]) == 0
    out = capsys.readouterr()
    for text in ("Which region?", "Frankfurt", "Zurich", "Keep the old endpoint?", "Anything else?"):
        assert text in out.out + out.err, text
    assert len(prompts) == 4 and prompts[-1].startswith("Type L-0001")
    q1, q2, q3 = _qs(asked)
    assert (q1["answer"], q2["answer"], q3.get("answer")) == ("B", "yes", None)
    assert q1["via"] == "tty"


def test_each_answer_is_bound_to_its_own_question_hash(asked):
    ws = _ws(asked)
    hashes = {q["id"]: question_hash(q) for q in _qs(asked)}
    ops = Ops(ws, Actor("human", "you", "tty"))
    # another question's hash does not bind this one, and then nothing is applied
    with pytest.raises(ValidationError, match="changed"):
        ops.answer_many("L-0001", [{"qid": "Q1", "value": "A", "expected_hash": hashes["Q1"]},
                                   {"qid": "Q2", "value": "yes", "expected_hash": hashes["Q1"]}])
    assert _unanswered(asked)
    ops.answer_many("L-0001", [{"qid": "Q1", "value": "A", "expected_hash": hashes["Q1"]},
                               {"qid": "Q2", "value": "no", "expected_hash": hashes["Q2"]}])
    assert [q.get("answer") for q in _qs(asked)] == ["A", "no", None]
    done = [e for e in read_events(ws) if e.kind == "question.answered"]
    assert sorted(e.data["qid"] for e in done) == ["Q1", "Q2"]


def test_a_question_that_changed_before_the_confirmation_refuses_the_whole_batch(asked, capsys, monkeypatch):
    def change():
        ws = _ws(asked)
        path, t = store.load(ws, "L-0001")
        find_question(t, "Q2")["text"] = "Keep the old endpoint for a year?"
        store.save(ws, t, path)
    _typed(monkeypatch, ["A", "yes", "note", "L-0001"], during=change)
    assert run(["answer", "L-0001", "--all"]) != 0
    out = capsys.readouterr()
    assert "changed" in out.out + out.err
    assert _unanswered(asked)


def test_agents_are_refused_and_never_prompted(switch, asked, monkeypatch):
    switch.agent()
    prompts = _typed(monkeypatch, ["A"])
    assert run(["answer", "L-0001", "--all"]) != 0
    assert prompts == [] and _unanswered(asked)
    with pytest.raises(HumanOnlyError):
        Ops(_ws(asked), Actor("agent", "x", "cli", "s-1")).answer_many(
            "L-0001", [{"qid": "Q1", "value": "A", "expected_hash": "sha256:x"}])


def test_wrong_confirmation_applies_nothing(asked, monkeypatch):
    _typed(monkeypatch, ["A", "yes", "", "L-9999"])
    assert run(["answer", "L-0001", "--all"]) != 0
    assert _unanswered(asked)


def test_an_invalid_answer_is_asked_again_and_empty_leaves_it_open(asked, capsys, monkeypatch):
    prompts = _typed(monkeypatch, ["Z", "A", "", "", "L-0001"])
    assert run(["answer", "L-0001", "--all"]) == 0
    assert len(prompts) == 5
    assert [q.get("answer") for q in _qs(asked)] == ["A", None, None]
    assert "not an option" in capsys.readouterr().err


def test_all_does_not_mix_with_a_single_answer(asked):
    assert run(["answer", "L-0001", "Q1", "A", "--all"]) != 0
    assert run(["answer", "L-0001"]) != 0
