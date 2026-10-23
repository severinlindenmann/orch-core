"""The question contract (ticket-format §5.6.1, #1123): one shape with a kind, one hash entry over the raw question,
one validator, the answer rules, the pinned fold and blank sets, and the three-form fixtures."""

import copy
import json
from pathlib import Path

import pytest

from orch import canon
from orch.canon import question as cq
from orch.canon import text

from . import oracle_f1_questions as oq

DIR = Path(__file__).parent.parent / "vectors" / "f1"
H = json.loads((DIR / "hashes.json").read_text(encoding="utf-8"))
QS = json.loads((DIR / "questions.json").read_text(encoding="utf-8"))
BY_NAME = {v["name"]: v for v in H["question_hash"]}


def _hash_of_parts(qid, ticket, txt, options, spec):
    """What a reader does with a view: rebuild nothing, hash the verbatim text, options and spec. Routing keys are
    outside the hash, so any id/to/blocking will do."""
    raw = {**spec, "id": "Q1", "to": "ticket_owner", "blocking": True, "text": txt}
    if options:
        raw["options"] = options
    return canon.question_hash(qid, ticket, raw)


# --- invariant 1: one hash entry over the whole raw question ---------------------------------------------------


def test_the_old_signature_is_gone():
    with pytest.raises(TypeError):
        canon.question_hash("a" * 32, oq.UID, "text", [])  # type: ignore[call-arg]


@pytest.mark.parametrize("v", H["question_hash"], ids=lambda v: v["name"])
def test_the_hash_covers_text_options_and_spec(v):
    assert canon.question_spec(v["question"]) == v["hashed"]["spec"]
    assert v["hashed"]["spec"]["kind"] == v["question"]["kind"]
    assert not {"id", "to", "blocking", "text", "options"} & set(v["hashed"]["spec"])
    assert (
        _hash_of_parts(v["qid"], v["ticket"], v["hashed"]["text"], v["hashed"]["options"], v["hashed"]["spec"])
        == v["hash"]
    )


def test_every_tamper_changes_the_hash_and_routing_does_not():
    full = BY_NAME["choice_full"]["hash"]
    tampers = [v for n, v in BY_NAME.items() if n.startswith("choice_full_tamper_")]
    assert len(tampers) >= 19
    assert all(v["hash"] != full for v in tampers)
    assert len({v["hash"] for v in tampers}) == len(tampers)
    for name in ("choice_full_reask_to", "choice_full_reask_blocking"):
        assert BY_NAME[name]["hash"] == full and BY_NAME[name]["same_as"] == "choice_full"
    assert BY_NAME["choice_options_swapped"]["hash"] != BY_NAME["choice_minimal"]["hash"]


def test_a_text_question_hashes_options_as_empty():
    v = BY_NAME["text_minimal"]
    assert "options" not in v["question"] and v["hashed"]["options"] == [] and v["hashed"]["spec"] == {"kind": "text"}


# --- invariant 2 and 7: one validator, a closed kind registry --------------------------------------------------


def test_the_kind_registry_is_closed():
    assert set(canon.KINDS) == {"choice", "text"}


@pytest.mark.parametrize("v", H["question_hash_refused"], ids=lambda v: v["name"])
def test_the_validator_refuses(v):
    with pytest.raises(canon.QuestionError):
        canon.validate_question(v["question"])


def test_the_validator_does_not_mutate():
    q = copy.deepcopy(oq.FULL)
    canon.validate_question(q)
    assert q == oq.FULL


def test_a_read_view_is_not_a_question():
    item = QS["forms"][0]["read_item"]
    with pytest.raises(canon.HashError):
        canon.question_hash(item["qid"], oq.UID, item)


# --- invariant 4: text checks ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "s,one_line,ok",
    [
        ("Pick one", True, True),
        ("a\tb", True, False),
        ("a\tb", False, True),
        ("a\nb", True, False),
        ("a\nb", False, True),
        ("a\r\nb", False, False),
        ("cafe\u0301", False, False),
        ("a\u200bb", False, False),
        ("a\ufeffb", False, False),
        ("\u0301a", False, False),
        ("a\u202eb", False, False),
        ("a\ue000", False, False),
        ("\U0001f4c8 up", True, True),
        ("", True, False),
        ("\u00a0\u3000", False, False),
        (" a", True, False),
        ("a\u00a0", True, False),
        (" a ", False, True),
        ("a\n\u0301b", False, False),
    ],
)
def test_check_plain(s, one_line, ok):
    if ok:
        assert text.check_plain(s, one_line=one_line, limit=10) == s
    else:
        with pytest.raises(text.TextError):
            text.check_plain(s, one_line=one_line, limit=10)


def test_check_plain_counts_code_points_and_bytes():
    assert text.check_plain("\U0001f600" * 5, one_line=True, limit=5)
    with pytest.raises(text.TextError):
        text.check_plain("a" * 6, one_line=True, limit=5)
    with pytest.raises(text.TextError):
        text.check_plain("\u20ac" * 1400, one_line=False, limit=1500)  # 4200 bytes


def test_check_plain_never_normalises():
    with pytest.raises(text.TextError):
        text.check_plain("A\u030a", one_line=True, limit=5)  # NFD of U+00C5


@pytest.mark.parametrize("v", H["question_fold"], ids=lambda v: v["text"].encode("unicode_escape").decode())
def test_the_pinned_fold(v):
    assert canon.fold(v["text"]) == v["folded"]


@pytest.mark.parametrize("v", H["question_blank"], ids=lambda v: v["text"].encode("unicode_escape").decode())
def test_blank_is_unicode_white_space(v):
    assert canon.is_blank(v["text"]) is v["blank"]


def test_the_bundled_fold_matches_the_runtime_on_every_code_point():
    import unicodedata

    if unicodedata.unidata_version != "16.0.0":
        pytest.skip("needs a Unicode 16.0 runtime")
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        c = chr(cp)
        assert text._CASEFOLD.get(cp, c) == c.casefold(), hex(cp)


def test_white_space_is_the_property():
    assert len(text.WHITE_SPACE) == 25
    assert "\u200b" not in text.WHITE_SPACE and "\u180e" not in text.WHITE_SPACE


# --- invariant 5: answers --------------------------------------------------------------------------------------


@pytest.mark.parametrize("v", H["question_answers"], ids=lambda v: v["name"])
def test_answer_rules(v):
    if v["ok"]:
        canon.validate_answer(v["question"], v.get("option"), v.get("text"))
    else:
        with pytest.raises(canon.QuestionError):
            canon.validate_answer(v["question"], v.get("option"), v.get("text"))


# --- the three forms -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("f", QS["forms"], ids=lambda f: f["name"])
def test_the_three_forms_carry_one_hash(f):
    raw = f["raw_event"]
    q = raw["question"]
    assert canon.question_id(oq.W, f["ticket"], q["id"]) == raw["qid"]
    h = canon.question_hash(raw["qid"], f["ticket"], q)
    assert h == raw["hash"]
    item = f["read_item"]
    assert item["hash"] == h and item["qid"] == raw["qid"]
    assert item["spec"] == canon.question_spec(q) and item.get("options") == q.get("options")
    assert _hash_of_parts(item["qid"], f["ticket"], item["text"], item.get("options", []), item["spec"]) == h
    assert not {"why", "recommended", "kind", "details", "allow_other"} & set(item)
    needs = f["hub_needs"]
    c = needs["content"]
    assert needs["row"]["hash"] == h and needs["row"]["item_id"] == raw["qid"]
    assert _hash_of_parts(needs["row"]["item_id"], needs["row"]["log"], c["text"], c.get("options", []), c["spec"]) == h


def test_the_forms_cover_both_kinds():
    assert {f["raw_event"]["question"]["kind"] for f in QS["forms"]} == set(canon.KINDS)


def test_limits_are_the_documented_ones():
    assert cq.LIMITS["question"] == (1, 140) and cq.LIMITS["options"] == (2, 6) and cq.LIMITS["details"] == (1, 1500)
    assert cq.ANSWER_MAX == 1000 and cq.ROUTING == {"id", "to", "blocking"}
