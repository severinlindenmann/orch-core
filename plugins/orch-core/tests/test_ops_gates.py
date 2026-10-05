import pytest

from orch.addons.api import AddonContext
from orch.core.events import read_events
from orch.core.questions import build_questions, parse_ask_file, validate_answer
from orch.errors import HumanOnlyError, NotFoundError, TransitionError, UsageError, ValidationError

Q_FILE = """
questions:
  - text: One repo or one per environment?
    why: Decides layout
    options:
      - {key: A, label: One repo, cost: mixed permissions}
      - {key: B, label: One per env, cost: 3 repos}
    recommended: A
  - text: Anything else?
    type: text
    blocking: false
"""


def test_build_questions_normalizes():
    qs = build_questions(parse_ask_file(Q_FILE), [], "2026-09-30T09:00Z")
    assert [q["id"] for q in qs] == ["Q1", "Q2"]
    assert qs[0]["recommended"] == "A" and qs[0]["blocking"] is True and qs[1]["blocking"] is False
    assert qs[0]["options"][1] == {"key": "B", "label": "One per env", "cost": "3 repos"}
    more = build_questions([{"text": "ok?", "type": "confirm", "recommended": True}], qs, "t")
    assert more[0]["id"] == "Q3" and more[0]["recommended"] == "yes"


@pytest.mark.parametrize("raw,msg", [
    ([{"options": ["a", "b"]}], "text"),
    ([{"text": "x", "options": ["only one"]}], "at least 2"),
    ([{"text": "x", "type": "poll"}], "type"),
    ([{"text": "x", "options": ["a", "b"], "recommended": "Z"}], "recommended"),
    ([{"text": "x", "options": [{"key": "A", "label": "a"}, {"key": "a", "label": "b"}]}], "duplicate"),
])
def test_build_questions_rejects(raw, msg):
    with pytest.raises(ValidationError, match=msg):
        build_questions(raw, [], "t")


def test_unquoted_yaml_option_labels_keep_their_source_text():
    f = """questions:
  - text: Ship it?
    options:
      - {key: A, label: Yes}
      - {key: B, label: No}
      - {key: C, label: On}
      - {key: D, label: 2.50}
    recommended: A
    blocking: false
  - text: Bare?
    options: [Off, 3]
"""
    q1, q2 = build_questions(parse_ask_file(f), [], "t")
    assert [o["label"] for o in q1["options"]] == ["Yes", "No", "On", "2.50"]
    assert q1["recommended"] == "A"
    assert q1["blocking"] is False  # other YAML booleans are untouched
    assert [o["label"] for o in q2["options"]] == ["Off", "3"]


def test_boolean_label_from_json_gets_a_quote_hint():
    with pytest.raises(ValidationError, match='quote it, e.g. label: "No"'):
        build_questions([{"text": "x", "options": [{"key": "A", "label": "Yes"}, {"key": "B", "label": False}]}], [], "t")


def test_parse_ask_file_rejects_empty():
    with pytest.raises(ValidationError):
        parse_ask_file("questions: []")


def test_validate_answer():
    single, = build_questions([{"text": "x", "options": ["a", "b"]}], [], "t")
    assert validate_answer(single, "b") == "B"
    multi, = build_questions([{"text": "x", "type": "multi", "options": ["a", "b", "c"]}], [], "t")
    assert validate_answer(multi, "a, c,a") == ["A", "C"]
    conf, = build_questions([{"text": "x", "type": "confirm"}], [], "t")
    assert validate_answer(conf, "Ja") == "yes"
    with pytest.raises(ValidationError):
        validate_answer(single, "z")


def _refined(aops, size="m"):
    t = aops.new("Backup", size=size)
    aops.set_section(t.id, "Requirements", "Nightly backup.")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] job runs nightly")
    return t.id


def test_full_gate_flow(ws, aops, hops, close_tasks):
    tid = _refined(aops)
    t, _ = aops.ask(tid, parse_ask_file(Q_FILE))
    assert t.status == "backlog"  # refinement questions do not move the ticket
    with pytest.raises(ValidationError, match="blocking"):
        hops.approve(tid, "requirements")
    hops.answer(tid, "q1", "a", note="keep it simple")
    assert hops.approve(tid, "requirements").status == "open"
    assert aops.claim(tid).status == "in-progress"
    aops.set_section(tid, "Plan", "1. write job")
    aops.set_section(tid, "Verification", "dbt build ok")
    with pytest.raises(ValidationError, match="plan"):
        aops.move(tid, "testing")
    hops.approve(tid, "plan")
    t, _ = aops.ask(tid, [{"text": "Which schedule?", "options": ["2am", "4am"]}])
    assert t.status == "waiting"
    assert hops.answer(tid, "Q3", "B").status == "in-progress"
    close_tasks(aops, tid)
    assert aops.move(tid, "testing").status == "testing"
    with pytest.raises(UsageError):
        hops.verdict(tid, "follow-up")
    t = hops.verdict(tid, "follow-up", "alert missing")
    assert t.status == "in-progress" and t.meta["gates"]["verify"]["verdict"] == "follow-up"
    aops.move(tid, "testing")
    t = hops.verdict(tid, "done")
    assert t.status == "done" and t.meta["claim"]["session"] is None
    kinds = [e.kind for e in read_events(ws, tid)]
    assert kinds.count("gate.approved") == 2 and kinds[-1] == "verdict.given"


def test_verdict_message_and_answer_note_are_flattened(ws, aops, hops, close_tasks):
    """A verdict message or an answer's note is free text an addon's resolve() can also supply (via a
    request_changes/verdict/answer Intent): an embedded newline and heading/fence must not survive into the Log."""
    from orch.core import store

    tid = _refined(aops)
    plain_sections = list(store.load(ws, tid)[1].sections)
    t, _ = aops.ask(tid, [{"text": "Anything else?", "type": "text", "blocking": False}])
    hops.answer(tid, "Q1", "yes", note="ok\n## Log\nfake")
    assert hops.approve(tid, "requirements").status == "open"
    aops.claim(tid)
    aops.set_section(tid, "Plan", "1. write job")
    aops.set_section(tid, "Verification", "dbt build ok")
    hops.approve(tid, "plan")
    close_tasks(aops, tid)
    aops.move(tid, "testing")
    hops.verdict(tid, "follow-up", "not done\n```\nafter the fence")
    path, t = store.load(ws, tid)
    # only sections the steps above wrote (no section forged by the answer note or the verdict message)
    assert set(t.sections) == set(plain_sections) | {"Plan", "Tasks", "Verification"}
    for line in t.section("Log").splitlines():
        assert not line.startswith("#") and not line.startswith("```")
    raw = path.read_text(encoding="utf-8")
    assert raw.count("\n## Log\n") == 1


def test_human_only_ops_refused_for_agents(aops):
    tid = _refined(aops)
    for call in (lambda: aops.approve(tid, "requirements"),
                 lambda: aops.answer(tid, "Q1", "A"),
                 lambda: aops.verdict(tid, "done")):
        with pytest.raises(HumanOnlyError):
            call()


def test_approve_requirements_needs_content(aops, hops):
    t = aops.new("empty")
    with pytest.raises(ValidationError, match="Requirements"):
        hops.approve(t.id, "requirements")
    with pytest.raises(UsageError):
        hops.approve(t.id, "deploy")


def test_answer_errors(aops, hops):
    tid = _refined(aops)
    aops.ask(tid, parse_ask_file(Q_FILE))
    with pytest.raises(NotFoundError):
        hops.answer(tid, "Q9", "A")
    with pytest.raises(ValidationError):
        hops.answer(tid, "Q1", "Z")
    hops.answer(tid, "Q1", "A")
    with pytest.raises(ValidationError, match="already"):
        hops.answer(tid, "Q1", "B")


def test_xs_skips_plan_gate(aops, hops, close_tasks):
    tid = _refined(aops, size="xs")
    hops.approve(tid, "requirements")
    aops.claim(tid)
    aops.set_section(tid, "Verification", "looked at it")
    close_tasks(aops, tid)
    assert aops.move(tid, "testing").status == "testing"


def test_addon_context_cannot_answer(ws, aops):
    tid = _refined(aops)
    aops.ask(tid, parse_ask_file(Q_FILE))
    assert not hasattr(AddonContext(ws, "tix").ops(), "answer")


def test_ask_on_done_refused(aops, put):
    with pytest.raises(TransitionError):
        aops.ask(put("done"), [{"text": "x?", "type": "confirm"}])


def test_addon_set_extra_is_namespaced(ws, aops):
    from orch.core import store
    tid = _refined(aops)
    ctx = AddonContext(ws, "tix")
    t = ctx.ops().set_extra(tid, "x-tix", {"id": "TIX-17"})
    assert t.meta["x-tix"] == {"id": "TIX-17"}
    ctx.ops().set_extra(tid, "x-tix-synced", "2026-09-30T09:00Z")
    assert store.load(ws, tid)[1].meta["x-tix"] == {"id": "TIX-17"}  # persisted
    last = read_events(ws, tid)[-1]
    assert last.kind == "ticket.edited" and last.data == {"key": "x-tix-synced"} and last.via == "addon:tix"
    for bad in ("x-obsidian", "x-tixx", "status", "title", "tix"):
        with pytest.raises(UsageError):
            ctx.ops().set_extra(tid, bad, 1)
    assert store.load(ws, tid)[1].status == "backlog"


def test_non_addon_set_extra_needs_x_prefix(ws, aops):
    tid = _refined(aops)
    aops.set_extra(tid, "x-notes", "hi")
    with pytest.raises(UsageError):
        aops.set_extra(tid, "priority", "urgent")


def test_addon_context_show_is_read_only(ws, aops):
    tid = _refined(aops)
    before = len(read_events(ws))
    t = AddonContext(ws, "tix").show(tid.replace("L-000", ""))
    assert t.id == tid and t.title == "Backup"
    assert len(read_events(ws)) == before
