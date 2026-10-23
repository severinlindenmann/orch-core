"""The signer binds the WHOLE question it shows (#1123, invariant 1): it recomputes the hash with the one raw-question
entry, so a changed why, details, kind, fact, title or option order in the material is refused, and an invalid
question cannot be signed at all. Routing (to, blocking) is not bound."""

import copy
import json
from pathlib import Path

import pytest

from orch import canon
from orch.host.signer import SignerRefused, _question

F = json.loads((Path(__file__).parent.parent / "vectors" / "f1" / "hashes.json").read_text(encoding="utf-8"))
FULL = next(v for v in F["question_hash"] if v["name"] == "choice_full")
WS = "0123456789abcdef0123456789abcdef"
UID = FULL["ticket"]


def _ev(q):
    qid = canon.question_id(WS, UID, q["id"])
    return {"type": "question.answered", "question": q["id"], "hash": canon.question_hash(qid, UID, q), "option": "csv"}


def test_the_shown_question_signs():
    q = copy.deepcopy(FULL["question"])
    _question({"question": q}, _ev(q), WS, UID)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda q: q.update(why="another reason"),
        lambda q: q.update(details="apply to production"),
        lambda q: q.pop("allow_other"),
        lambda q: q.update(recommended="api"),
        lambda q: q["options"][0]["facts"][1].__setitem__(1, "CHF 1"),
        lambda q: q["options"][0].update(label="Monthly file"),
        lambda q: q["options"][0].update(text="Weekly"),
        lambda q: q["options"].reverse(),
    ],
)
def test_a_material_that_differs_from_the_hashed_question_is_refused(tamper):
    q = copy.deepcopy(FULL["question"])
    ev = _ev(q)
    tamper(q)
    with pytest.raises(SignerRefused, match="not the hash of the question shown"):
        _question({"question": q}, ev, WS, UID)


def test_routing_is_not_bound():
    q = copy.deepcopy(FULL["question"])
    ev = _ev(q)
    _question({"question": {**q, "blocking": False, "to": "assignees"}}, ev, WS, UID)


@pytest.mark.parametrize("bad", [v for v in F["question_hash_refused"] if isinstance(v["question"], dict)][:12],
                         ids=lambda v: v["name"])  # fmt: skip
def test_an_invalid_question_cannot_be_signed(bad):
    q = {**bad["question"], "id": "Q1"}
    with pytest.raises(SignerRefused, match="cannot be hashed"):
        _question({"question": q}, {"question": "Q1", "hash": "sha256:" + "0" * 64}, WS, UID)
