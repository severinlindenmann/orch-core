"""Ticket anatomy v2 (ticket design review §3.1): a file holds only the sections someone wrote, Summary is a
section, Proposal and Decisions are legacy, and the human's Ask is protected and imported from the tracker."""
import pytest

from orch.core import store
from orch.core.model import new_ticket, parse_ticket, render_ticket
from orch.errors import UsageError


def _t():
    return new_ticket("L-0042", "Back up nightly config", type="feature", priority="normal", size="m",
                      created="2026-09-30T08:12Z")


def test_a_new_ticket_writes_no_empty_sections(aops, ws):
    t = aops.new("Back up the config")
    text = store.resolve(ws, t.id).path.read_text(encoding="utf-8")
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == ["## Log"]


def test_an_ask_given_at_creation_is_written(aops, ws):
    t = aops.new("Back up the config", ask="Please back it up.")
    text = store.resolve(ws, t.id).path.read_text(encoding="utf-8")
    assert [line for line in text.splitlines() if line.startswith("## ")] == ["## Ask", "## Log"]


def test_render_drops_empty_sections_and_keeps_the_rest_in_place():
    old = ("---\nid: L-1\ntitle: t\n---\n\n## Ask\n\nhi\n\n## Context\n\n## Requirements\n\nr\n\n"
           "## Plan\n\np\n\n## Verification\n\n## Log\n\n- x\n")
    out = render_ticket(parse_ticket(old))
    headings = [line for line in out.splitlines() if line.startswith("## ")]
    assert headings == ["## Ask", "## Requirements", "## Plan", "## Log"]


def test_summary_is_written_right_after_the_ask():
    t = _t()
    t.set_section("Requirements", "r")
    t.set_section("Summary", "- one line")
    t.set_section("Ask", "hi")
    out = render_ticket(t)
    assert out.index("## Ask") < out.index("## Summary") < out.index("## Requirements")


def test_agents_write_the_summary(aops):
    t = aops.new("x")
    assert aops.set_section(t.id, "summary", "- Move the download into the ingest service").section("Summary")


@pytest.mark.parametrize("name", ["Proposal", "Decisions"])
def test_legacy_sections_are_not_written_any_more(aops, name):
    t = aops.new("x")
    with pytest.raises(UsageError) as e:
        aops.set_section(t.id, name, "text")
    assert "Summary" in (e.value.hint or "") and name not in (e.value.hint or "")


def test_guard_protects_the_ask(ws, put):
    from orch.hooks.guard import evaluate
    tid = put("backlog", sections={"Ask": "the human's words", "Context": "old"})
    path = str(store.resolve(ws, tid).path)
    edit = lambda old, new: {"tool_name": "Edit", "tool_input": {"file_path": path, "old_string": old, "new_string": new}}
    d = evaluate(ws, edit("the human's words", "an agent's words"))
    assert not d.allow and "Ask" in d.reason
    assert evaluate(ws, edit("old", "new")).allow


def test_schema_lists_summary_and_drops_the_legacy_sections():
    from orch.core.schema import SCHEMA_VERSION, ticket_schema
    props = ticket_schema()["properties"]["sections"]["properties"]
    assert "Summary" in props and "Proposal" not in props and "Decisions" not in props
    assert SCHEMA_VERSION == "1.6.0"  # E2: type epic and sprint; 1.3: the verdict hash; 1.4: together (F2); 1.5: artifact_items


# -- Ask from the tracker -------------------------------------------------------------------------------------------

def test_an_import_intent_fills_the_ask_from_the_issue_text(ws, human):
    from orch.addons.api import Intent
    from orch.addons.intents import execute
    intent = Intent("import", ref="GH-7", value="Retry downloads", reason="Imported from https://x.test/7",
                    data={"ask": "Downloads fail on 503.\n## Not a heading"})
    msg = execute(ws, intent, allowed_ref="GH-7", tickets=True, actor=human, source="act")
    tid = msg.rsplit(" ", 1)[-1]
    t = store.load(ws, tid)[1]
    assert t.section("Ask").startswith("Downloads fail on 503.")
    assert "\\## Not a heading" in t.section("Ask")  # neutral_text: never a section boundary
    assert set(t.sections) >= {"Ask", "Log"} and "Requirements" not in t.sections


def test_an_import_intent_without_text_leaves_the_ask_empty(ws, human):
    from orch.addons.api import Intent
    from orch.addons.intents import execute
    msg = execute(ws, Intent("import", ref="GH-8", value="t"), allowed_ref="GH-8", tickets=True, actor=human,
                  source="act")
    assert store.load(ws, msg.rsplit(" ", 1)[-1])[1].section("Ask") == ""
