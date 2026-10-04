import hashlib
import json

from orch.core.questions import build_questions, canonical_json, question_hash


def _q(**over):
    q = build_questions([{"text": "Which format?", "why": "export", "options": ["CSV", "JSON"], "recommended": "A"}],
                        [], "2026-10-02T09:00Z")[0]
    q.update(over)
    return q


def test_hash_is_sha256_of_canonical_core_fields():
    q = _q()
    core = {k: q[k] for k in ("id", "text", "why", "type", "options", "recommended", "blocking")}
    want = "sha256:" + hashlib.sha256(json.dumps(core, ensure_ascii=False, separators=(",", ":"),
                                                 sort_keys=True).encode("utf-8")).hexdigest()
    assert question_hash(q) == want


def test_answer_fields_do_not_change_the_hash():
    q = _q()
    before = question_hash(q)
    q.update(answer="A", note="x", answered="2026-10-02T10:00Z", via="tty", asked="2020-01-01T00:00Z")
    assert question_hash(q) == before


def test_text_option_and_blocking_change_the_hash():
    base = question_hash(_q())
    assert question_hash(_q(text="Which format now?")) != base
    assert question_hash(_q(blocking=False)) != base
    q = _q()
    q["options"][1]["label"] = "YAML"
    assert question_hash(q) != base


def test_canonical_json_matches_the_js_and_sharing_serialiser():
    assert canonical_json({"b": 1, "a": "ä"}) == '{"a":"ä","b":1}'.encode("utf-8")


def test_one_canonical_json_keeps_every_hash_unchanged():
    """Final review M4: questions, epics and the ledger share one canonical_json; the hashes they produced before
    (question hash, verdict hash, ledger MAC) stay byte-identical."""
    import hashlib

    from orch.core import canonical, epics, ledger, questions
    from orch.core.model import new_ticket
    assert questions.canonical_json is canonical.canonical_json
    assert not hasattr(epics, "_canonical") and not hasattr(ledger, "_canonical")
    obj = {"b": [1, "ä", {"z": None, "a": True}], "a": "ü‮x"}
    assert hashlib.sha256(canonical.canonical_json(obj)).hexdigest() == \
        "d06d965bbd354333c6003af5e1f199b7ce3cd018c3d71ed7cfb2e1002a045324"
    assert question_hash({"id": "Q1", "text": "Which?", "why": "w", "type": "single",
                          "options": [{"key": "A", "label": "x", "cost": None}], "recommended": "A"}) == \
        "sha256:a48e8e4ce049ff10a9315c434cf63396662592099dd03cf1c89eaf39f41983ca"
    assert ledger._mac(b"k" * 32, {"ticket": "L-1", "kind": "gate", "hash": "sha256:ab"}) == \
        "dad67b5b2c74db64179995eac3fa0bd6a6436c5bf4bfd735359d5bdfd588b031"
    t = new_ticket("L-0001", "Export", type="feature", priority="normal", size="m", created="2026-10-01T09:00Z")
    t.meta["status"] = "testing"
    t.set_section("Acceptance criteria", "- [ ] a")
    t.set_section("Verification", "- AC1: ok")
    assert epics.verdict_hash([t], None) == "sha256:e41cd2f3eb23420295abc361ddff706b1d92bd33f31a6d02e9adfd146c5c94d3"
