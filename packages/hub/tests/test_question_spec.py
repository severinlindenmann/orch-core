"""#1123 D-hub: the raw-question hash over ``{question_id, ticket, text, options, spec}`` against the frozen core
fixtures (``tests/data/core_f1``, copied from core PR #1135 commit e0525503, see PROVENANCE.md), the per-question
"unverifiable" degradation (the ticket stays), verbatim ``options`` + ``spec`` in the needs content, the hub-built ask and
the per-kind custodial answer rules. Pure units first; the needs tests use the D15 fake host."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from custodial_helpers import LOG, WK, WS
from test_view_service import make_seals
from view_helpers import OWNER, Host, build_view_services

from orch_hub.contract import phase4 as P4
from orch_hub.contract.hub import Refused
from orch_hub.view import project as PR

DATA = Path(__file__).resolve().parent / "data" / "core_f1"
HASHES = json.loads((DATA / "hashes.json").read_text())
FORMS = {f["name"]: f for f in json.loads((DATA / "questions.json").read_text())["forms"]}
USER = "cd" * 16


def test_the_old_label_only_hash_is_gone():
    import inspect

    assert list(inspect.signature(PR.question_hash).parameters) == ["qid", "ticket_uid", "question"]


@pytest.mark.parametrize("v", HASHES["question_hash"], ids=lambda v: v["name"])
def test_question_hash_matches_every_core_vector(v):
    assert PR.question_hash(v["qid"], v["ticket"], v["question"]) == v["hash"]
    assert PR.question_id(HASHES["question_id"][0]["workspace_id"], v["ticket"], "Q1") == v["qid"]
    # the read-view form: the same bytes from options + spec as given (never rebuilt from other keys)
    h = v["hashed"]
    view = {"text": h["text"], "options": h["options"], **h["spec"]}
    assert PR.question_hash(h["question_id"], h["ticket"], view) == v["hash"]


def test_re_ask_changing_only_routing_keeps_the_hash():
    by = {v["name"]: v["hash"] for v in HASHES["question_hash"]}
    assert by["choice_full_reask_to"] == by["choice_full_reask_blocking"] == by["choice_full"]


# What only core checks, each with the reason it is not mirrored here (the host re-checks on admission, so a question with
# one of these never reaches a ticket; the hub only has to be no more permissive on what it can cheaply see).
_CORE_ONLY: dict[
    str, str
] = {}  # L2/L3 are ported (unicodedata M* stands in for core's 16.0 tables); nothing is core-only


@pytest.mark.parametrize("v", HASHES["question_hash_refused"], ids=lambda v: v["name"])
def test_everything_core_refuses_is_unverifiable_here(v):
    if v["name"] in _CORE_ONLY:
        pytest.skip(_CORE_ONLY[v["name"]])
    assert not PR.renderable(v["question"]), v["name"]
    q = v["question"]
    if type(q) is dict:
        ask = {k: x for k, x in q.items() if k != "id"}
        if v["name"] not in (
            "missing_blocking",
            "question_not_nfc",
            "question_crlf",
            "option_title_tab",
            "facts_object_form",
            # the contract's early refusal (contract/phase4.py _question) has no combining-mark rule: only `renderable` (the
            # reader) and core's admission do, so a question with a line-start mark is listed here as unverifiable
            "option_title_leading_mark",
            "details_line_starts_with_combining_mark",
            "question_line_starts_with_combining_mark",
        ):
            assert not P4._question(ask) or v["name"].startswith(("titles_", "fact_labels_")), v["name"]


@pytest.mark.parametrize("name", list(FORMS))
def test_the_fixture_questions_are_renderable(name):
    assert PR.renderable(FORMS[name]["raw_event"]["question"])


# --- the view: per-question degradation, verbatim content ---------------------------------------------------------------


class Reg:
    def mode(self, ws):
        return "full"

    def owner_alias(self, ws):
        return USER


class Acc:
    def role_of(self, user, ws):
        return "edit"


@pytest.fixture
def kit(tmp_path):
    seals = make_seals(tmp_path, WS, {1: WK})
    (tmp_path / "v").mkdir(mode=0o700)
    vs = build_view_services(tmp_path / "v", wk_seals=seals, registry=Reg(), access=Acc())
    h = Host()
    h.genesis()
    h.ticket()
    k = type("K", (), {})()
    k.vs, k.host, k.r = vs, h, vs.reader
    k.push = lambda: h.push_all(vs)
    yield k
    vs.close()
    seals.close()


def ask_raw(kit, q, qn="Q1", *, hash_=None, qid=None):
    q = {**copy.deepcopy(q), "id": qn, "to": OWNER}
    qid = qid or PR.question_id(WS, LOG, qn)
    kit.host.append(LOG, "question.asked", question=q, qid=qid, hash=hash_ or PR.question_hash(qid, LOG, q), roster_v=1)
    return qid


FULL = FORMS["choice_full"]["raw_event"]["question"]
TEXTQ = FORMS["text_with_why_and_details"]["raw_event"]["question"]


def test_needs_content_carries_options_and_spec_verbatim_in_the_hub_needs_form(kit):
    for name, form in FORMS.items():
        want = form["hub_needs"]["content"]
        qid = ask_raw(kit, form["raw_event"]["question"], qn="Q1" if name == "choice_full" else "Q2")
        kit.push()
        got = kit.r.item(WS, USER, qid)["content"]
        qn_want = {
            **want,
            "question": got["question"],
            "short_hash": got["short_hash"],
            "ticket_title": got["ticket_title"],
        }
        assert got == qn_want, name  # same keys, same options and spec, byte for byte, nothing added or dropped
        assert "unverifiable" not in got
    h = PR.question_hash(qid, LOG, TEXTQ)
    assert kit.r.needs_you(WS, USER)[1]["hash"] == h


def test_a_hash_mismatch_keeps_the_ticket_and_marks_only_that_question(kit):
    ask_raw(kit, TEXTQ, "Q1")
    bad = ask_raw(kit, FULL, "Q2", hash_="sha256:" + "11" * 32)
    kit.push()
    rows = kit.r.needs_you(WS, USER)
    assert [r["item_id"] for r in rows] == [PR.question_id(WS, LOG, "Q1"), bad]
    ok, un = (kit.r.item(WS, USER, r["item_id"])["content"] for r in rows)
    assert "unverifiable" not in ok and ok["spec"]["kind"] == "text"
    assert un["unverifiable"] is True and "options" not in un and "spec" not in un
    assert un["text"] == FULL["text"]  # still shown


@pytest.mark.parametrize(
    "mutate",
    [
        lambda q: q.pop("kind"),
        lambda q: q.update(kind="confirm"),
        lambda q: q.update(future_key="x"),
        lambda q: q["options"][0].update(preview="p"),
        lambda q: q["options"][0].update(facts=[{"label": "Effort", "value": "x"}]),
        lambda q: q.update(allow_other=False),
    ],
)
def test_a_question_it_cannot_render_stays_but_is_not_answerable(kit, mutate):
    q = copy.deepcopy(FULL)
    mutate(q)
    ask_raw(kit, q)  # the host's hash is right for these bytes; the hub just does not know them
    kit.push()
    (row,) = kit.r.needs_you(WS, USER)
    assert kit.r.item(WS, USER, row["item_id"])["content"]["unverifiable"] is True


def test_a_wrong_qid_also_only_marks_the_question(kit):
    ask_raw(kit, TEXTQ, qid="0" * 32)
    kit.push()
    (row,) = kit.r.needs_you(WS, USER)
    assert row["item_id"] == PR.question_id(WS, LOG, "Q1")
    assert kit.r.item(WS, USER, row["item_id"])["content"]["unverifiable"] is True


def test_a_question_without_text_is_unverifiable_not_a_dropped_ticket(kit):
    kit.host.append(
        LOG, "question.asked", question={"id": "Q1", "to": OWNER}, qid="0" * 32, hash="sha256:" + "0" * 64, roster_v=1
    )
    kit.push()
    (row,) = kit.r.needs_you(WS, USER)
    assert kit.r.item(WS, USER, row["item_id"])["content"] == {
        "ticket_title": "Load tariffs", "question": "Q1", "blocking": False, "short_hash": "0000 0000 0000",
        "text": "", "unverifiable": True,
    }  # fmt: skip


def test_only_a_malformed_event_drops_the_ticket_view(kit):
    kit.host.append(
        LOG,
        "question.asked",
        question={"id": "bad", "to": OWNER, "text": "x"},
        qid="0" * 32,
        hash="sha256:" + "0" * 64,
        roster_v=1,
    )
    kit.push()
    assert kit.r.needs_you(WS, USER) == []


# --- hub-built asks and answers ---------------------------------------------------------------------------------------


ASK = {"to": OWNER, "text": "Which?", "blocking": True, "kind": "choice",
       "options": [{"key": "a", "label": "A"}, {"key": "b", "label": "B", "text": "Bee", "facts": [["Cost", "1"]], "cost": "1 d"}]}  # fmt: skip


@pytest.fixture
def env(kit):
    return kit


def _built(kit, action):
    kit.push()
    ev = kit.vs.workspace_view.build(bytearray(WK), 1, WS, USER, "01J9ZP00000000000000000EV1", action)
    return ev.core


def test_hub_built_ask_hashes_the_raw_question(kit):
    core = _built(kit, {"type": "question.asked", "log": LOG, "question": ASK})
    assert core["question"] == {"id": "Q1", **ASK}
    assert core["hash"] == PR.question_hash(core["qid"], LOG, core["question"])
    assert PR.question_spec(core["question"]) == {"kind": "choice"}


@pytest.mark.parametrize(
    "ask",
    [
        {k: v for k, v in ASK.items() if k != "kind"},
        {**ASK, "options": ASK["options"][:1]},
        {**ASK, "options": [{"key": "a", "label": "A"}, {"key": "b", "label": "B", "preview": "x"}]},
        {**ASK, "kind": "text"},
        {**ASK, "allow_other": False},
        {**ASK, "text": "x" * 141},
        {**ASK, "details": ""},
    ],
)
def test_hub_ask_without_the_new_shape_is_refused(ask):
    ask = {k: v for k, v in ask.items() if v is not None}
    assert not P4._question(ask)


def test_hub_ask_accepts_text_kind_details_and_recommended():
    assert P4._question(
        {"to": OWNER, "text": "Bank?", "blocking": False, "kind": "text", "details": "a\n\tb", "why": "w"}
    )
    assert P4._question({**ASK, "recommended": "b", "allow_other": True, "details": "d"})


def _answered(kit, ask, **answer):
    qid = ask_raw(kit, ask)
    kit.push()
    h = kit.r.needs_you(WS, USER)[0]["hash"]
    del qid
    return _built(kit, {"type": "question.answered", "log": LOG, "question": "Q1", "hash": h, **answer})


@pytest.mark.parametrize(
    ("ask", "answer", "ok"),
    [
        (FULL, {"option": "csv"}, True),
        (FULL, {"option": "csv", "text": "after the audit"}, True),
        (FULL, {"text": "something else"}, True),  # Other: allow_other
        (FULL, {"option": "nope"}, False),
        (TEXTQ, {"text": "Acme Bank"}, True),
        (TEXTQ, {"option": "csv"}, False),
        (TEXTQ, {"option": "csv", "text": "x"}, False),
    ],
)
def test_answer_rules_per_kind(kit, ask, answer, ok):
    if ok:
        assert _answered(kit, ask, **answer)["hash"]
    else:
        with pytest.raises(Refused):
            _answered(kit, ask, **answer)


def test_text_alone_to_a_choice_without_allow_other_is_refused(kit):
    q = {k: v for k, v in FULL.items() if k != "allow_other"}
    with pytest.raises(Refused):
        _answered(kit, q, text="other")


def test_an_unverifiable_question_is_not_answered_through_the_hub(kit):
    ask_raw(kit, FULL, hash_="sha256:" + "22" * 32)
    kit.push()
    h = kit.r.needs_you(WS, USER)[0]["hash"]
    with pytest.raises(Refused) as e:
        _built(kit, {"type": "question.answered", "log": LOG, "question": "Q1", "hash": h, "option": "csv"})
    assert e.value.code == "unavailable"


def test_custodial_answer_text_limit_is_1000_code_points():
    base = {"type": "question.answered", "log": LOG, "question": "Q1", "hash": "sha256:" + "ab" * 32}
    P4.check_action({**base, "text": "x" * 1000}, role="edit")
    P4.check_action({**base, "text": "\U0001f4c8" * 1000}, role="edit")  # 4000 bytes, 1000 code points
    for t in ("x" * 1001, "\U0001f4c8" * 1001):
        with pytest.raises(Refused):
            P4.check_action({**base, "text": t}, role="edit")


def test_item_content_passes_a_verified_question_verbatim_even_with_a_tab():
    from orch_hub.webapi.api import _item_content

    form = FORMS["choice_full"]["hub_needs"]["content"]
    assert "\t" in form["spec"]["details"]
    out = _item_content(form)
    assert out["text"] == form["text"] and out["options"] == form["options"] and out["spec"] == form["spec"]
    hostile = {"ticket_title": "a\u202eb", "question": "Q1", "text": "x\u202e", "unverifiable": True}
    assert _item_content(hostile)["text"] == "x<U+202E>" and _item_content(hostile)["ticket_title"] == "a<U+202E>b"
