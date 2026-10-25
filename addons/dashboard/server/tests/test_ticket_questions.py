"""The ticket document carries a question's options and spec exactly as the host sent them (design-question-options.md
section 7). Test data: the frozen read_item forms of core's f1 question vectors (task A)."""

import json
from pathlib import Path

from orch_dashboard.adapters.tickets import document

ITEMS = json.loads((Path(__file__).parent / "data" / "f1_question_items.json").read_text())


def data_for(item: dict) -> dict:
    doc = {
        "uid": "01J9ZK4Q7M3R8T2V6X0B5N1C9D",
        "key": "DEMO-0001",
        "title": "Tariff seeds",
        "type": "feature",
        "status": "open",
        "questions": [{"id": item["id"], "to": "ticket_owner", "text": item["text"], "blocking": item["blocking"]}],
        "tasks": [],
        "acceptance": [],
    }
    return {
        "doc": doc,
        "log": [{"seq": 1, "at": "2026-10-23T12:00:00Z", "type": "ticket.created"}],
        "questions": [item],
        "gates": {},
        "criteria": [],
        "status": "open",
    }


def question(name: str) -> dict:
    out = document(data_for(ITEMS[name]), "p_x", {}, False)
    assert [q["id"] for q in out["questions_state"]] == ["Q1"]
    assert out["questions"][0] == {k: v for k, v in out["questions_state"][0].items() if k in out["questions"][0]}
    return out["questions_state"][0]


def test_choice_passes_options_and_spec_verbatim():
    item = ITEMS["choice_full"]
    q = question("choice_full")
    assert q["options"] == item["options"]  # every key of every option, facts as pairs, nothing dropped
    assert q["spec"] == item["spec"]
    assert q["kind"] == "choice" and q["allow_other"] is True and q["recommended"] == "csv"
    assert q["why"] == "The seeds are stale." and q["details"].startswith("If you pick CSV")
    assert q["text"] == item["text"]


def test_text_kind_has_no_options_and_keeps_why_and_details():
    q = question("text_with_why_and_details")
    assert "options" not in q and q["kind"] == "text"
    assert q["spec"] == ITEMS["text_with_why_and_details"]["spec"]
    assert q["why"] == "For the invoice." and q["details"] == "IBAN or name is fine."


def test_a_question_the_host_did_not_describe_has_no_kind():
    data = data_for(ITEMS["choice_full"])
    data["questions"] = []
    out = document(data, "p_x", {}, False)
    q = out["questions_state"][0]
    assert "kind" not in q and "options" not in q and "spec" not in q


def test_the_card_carries_the_hosts_question_hash():
    for name in ("choice_full", "text_with_why_and_details"):
        item = ITEMS[name]
        assert item["hash"].startswith("sha256:")
        q = question(name)
        assert q["hash"] == item["hash"]  # what an answer binds; without it one click can never arm


def test_no_hash_from_the_host_means_no_hash_on_the_card():
    data = data_for(ITEMS["choice_full"])
    del data["questions"][0]["hash"]
    assert "hash" not in document(data, "p_x", {}, False)["questions_state"][0]


def test_the_ticket_documents_text_is_never_used_for_the_question():
    data = data_for(ITEMS["choice_full"])
    data["doc"]["questions"][0]["text"] = "UNBOUND copy from the ticket document"
    data["questions"][0]["text"] = ""
    q = document(data, "p_x", {}, False)["questions_state"][0]
    assert q["text"] == ""
    data["questions"] = []
    assert document(data, "p_x", {}, False)["questions_state"][0]["text"] == ""
