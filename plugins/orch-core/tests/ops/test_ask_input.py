"""orch ask authoring (design #1123 section 3): --option/--fact/--rec/--allow-other/--details/--text-question and the
whole-question JSON file, one parser for ``ask`` and a batch ``ask``; every limit comes from validate_question."""

from __future__ import annotations

import json

import pytest

from tests.ops.helpers import Cli


def claimed(cli):
    cli("new", "A ticket")
    cli("claim", "1")


def stored(ws, n=0):
    return ws.ticket_json("1")["questions"][n]


def test_options_with_titles_text_facts_details_and_other(ws, cli):
    claimed(cli)
    r = cli(
        "ask",
        "Which source?",
        "--option",
        "csv | Monthly CSV | Complete but a month old",
        "--option",
        " api |Live API",
        "--fact",
        "api | Limit | 100 per hour",
        "--fact",
        "api | Cost | free",
        "--rec",
        "csv",
        "--allow-other",
        "--details",
        "Line one\nLine two",
        "--why",
        "they differ",
    )
    assert r.code == 0, r.out + r.err
    assert stored(ws) == {
        "id": "Q1",
        "to": "ticket_owner",
        "text": "Which source?",
        "kind": "choice",
        "why": "they differ",
        "details": "Line one\nLine two",
        "options": [
            {"key": "csv", "label": "Monthly CSV", "text": "Complete but a month old"},
            {"key": "api", "label": "Live API", "facts": [["Limit", "100 per hour"], ["Cost", "free"]]},
        ],
        "recommended": "csv",
        "allow_other": True,
        "blocking": True,
    }


def test_text_question_and_details_file(ws, cli):
    claimed(cli)
    assert cli("ask", "What is the name?", "--text-question", "--details-file", "-", stdin="Some background").code == 0
    assert stored(ws) == {
        "id": "Q1",
        "to": "ticket_owner",
        "text": "What is the name?",
        "kind": "text",
        "details": "Some background",
        "blocking": True,
    }


def test_question_file_holds_the_whole_question(ws, cli):
    claimed(cli)
    doc = {
        "text": "Pick one",
        "why": "because",
        "options": [
            {"key": "a", "title": "Alpha", "text": "first", "facts": [["Size", "S"]], "cost": "1 day"},
            {"key": "b", "title": "Beta"},
        ],
        "rec": "b",
        "allow_other": True,
        "details": "more",
        "blocking": False,
        "to": "assignees",
    }
    assert cli("ask", "--question-file", "-", stdin=json.dumps(doc)).code == 0
    assert stored(ws) == {
        "id": "Q1",
        "to": "assignees",
        "text": "Pick one",
        "kind": "choice",
        "why": "because",
        "details": "more",
        "options": [
            {"key": "a", "label": "Alpha", "text": "first", "facts": [["Size", "S"]], "cost": "1 day"},
            {"key": "b", "label": "Beta"},
        ],
        "recommended": "b",
        "allow_other": True,
        "blocking": False,
    }


def test_question_file_without_options_is_a_text_question(ws, cli):
    claimed(cli)
    assert cli("ask", "--question-file", "-", stdin=json.dumps({"text": "Name?"})).code == 0
    assert stored(ws)["kind"] == "text"


A = ["--option", "a | Alpha", "--option", "b | Beta"]


@pytest.mark.parametrize(
    "argv, words",
    [
        (["--option", "a | Alpha"], "2 to 6"),  # one option is not a choice
        ([], "--option"),  # no options and no --text-question: the old keys-only call is gone
        (["--option", "a | Alpha", "--option", "a | Again"], "have the key"),
        (["--option", "a | Alpha", "--option", "b | ALPHA"], "same title"),
        (["--option", "a"], "key | title"),
        (["--option", "A b | Title", "--option", "b | Beta"], "key must be"),
        ([*A, "--rec", "c"], "recommended"),
        ([*A, "--fact", "c | Limit | 1"], "not one of the --option keys"),
        ([*A, "--fact", "a | Limit"], "key | Label | value"),
        ([*A, "--fact", "a | L | 1", "--fact", "a | l | 2"], "same label"),
        ([*A, "--details", ""], "details"),
        ([*A, "--details", "x" * 1501], "details"),
        ([*A, "--details", "x", "--details-file", "-"], "exactly one of"),
        (["--text-question", *A], "takes no options"),
        (["--text-question", "--rec", "a"], "takes no options"),
        (
            [
                *A,
                "--option",
                "c | C",
                "--option",
                "d | D",
                "--option",
                "e | E",
                "--option",
                "f | F",
                "--option",
                "g | G",
            ],
            "2 to 6",
        ),
        ([*A, "--why", ""], "why"),
        ([*A, "--to", "someone"], "--to is a person id"),
    ],
)
def test_ask_refuses_in_plain_words(ws, cli, argv, words):
    claimed(cli)
    r = cli.j("ask", "q?", *argv, stdin="")
    assert (r.code, r.err_code) == (5, "invalid.input"), r.out
    assert words in r.doc["error"]["message"], r.doc["error"]["message"]
    assert ws.ticket_json("1")["questions"] == []


def test_question_text_limit_and_hidden_characters(ws, cli):
    claimed(cli)
    r = cli.j("ask", "x" * 141, *A)
    assert (r.code, r.err_code) == (5, "invalid.input") and "140" in r.doc["error"]["message"]
    r = cli.j("ask", "q?", "--option", "a | Al\u202epha", "--option", "b | Beta")
    assert r.code != 0 and r.err_code in ("parse.text", "invalid.input")
    assert ws.ticket_json("1")["questions"] == []


def test_question_file_is_exclusive_and_strict(ws, cli):
    claimed(cli)
    ok = json.dumps({"text": "Name?"})
    for argv, words in (
        (["Name?", "--question-file", "-"], "exactly one of"),
        (["--question-file", "-", "--rec", "a"], "remove --rec"),
        (["--question-file", "-", "--non-blocking"], "blocking"),
    ):
        r = cli.j("ask", *argv, stdin=ok)
        assert (r.code, r.err_code) == (5, "invalid.input") and words in r.doc["error"]["message"], argv


@pytest.mark.parametrize(
    "body, code",
    [
        ('{"text": "a", "text": "b"}', "parse.text"),  # duplicate keys
        ("[1]", "invalid.input"),
        ('{"text": "a", "colour": "red"}', "invalid.input"),
        ('{"options": []}', "invalid.input"),
        ('{"text": "a", "options": [{"key": "a", "label": "A"}, {"key": "b", "title": "B"}]}', "invalid.input"),
        (
            '{"text": "a", "allow_other": false, "options": [{"key": "a", "title": "A"}, {"key": "b", "title": "B"}]}',
            "invalid.input",
        ),
        (
            '{"text": "a", "kind": "text", "options": [{"key": "a", "title": "A"}, {"key": "b", "title": "B"}]}',
            "invalid.input",
        ),
    ],
)
def test_question_file_is_strict(ws, cli, body, code):
    claimed(cli)
    r = cli.j("ask", "--question-file", "-", stdin=body)
    assert (r.code, r.err_code) == ((6 if code == "parse.text" else 5), code), (body, r.out)
    assert ws.ticket_json("1")["questions"] == []


def test_marker_heuristic_only_warns(ws, cli):
    claimed(cli)
    r = cli("ask", "Which? a) csv b) api", *A)
    assert r.code == 0 and "warning:" in r.out and "--option" in r.out
    assert stored(ws)["text"] == "Which? a) csv b) api"
    r = cli("ask", "Which source?", *A)
    assert r.code == 0 and "warning:" not in r.out


def test_batch_ask_uses_the_same_parser(ws, cli):
    claimed(cli)
    item = {
        "op": "ask",
        "text": "Which?",
        "option": ["a | Alpha | first", "b | Beta"],
        "fact": ["a | Size | S"],
        "rec": "a",
    }
    r = cli.j(
        "apply",
        "--file",
        "-",
        stdin=json.dumps({"ref": "1", "ops": [item, {"op": "ask", "text": "Name?", "text_question": True}]}),
    )
    assert r.code == 0, r.out
    q1, q2 = ws.ticket_json("1")["questions"]
    assert q1["options"] == [
        {"key": "a", "label": "Alpha", "text": "first", "facts": [["Size", "S"]]},
        {"key": "b", "label": "Beta"},
    ]
    assert (q1["id"], q2["id"], q2["kind"]) == ("Q1", "Q2", "text")
    for bad in (
        {"op": "ask", "text": "Which?"},  # no options
        {"op": "ask", "text": "Which?", "option": ["a | A"]},
        {"op": "ask", "text": "Which?", "options": ["a", "b"]},  # the keys-only form is gone
        {"op": "ask", "text": "Which?", "question_file": "x.json"},
    ):
        r = cli.j("apply", "--file", "-", stdin=json.dumps({"ref": "1", "ops": [bad]}))
        assert (r.code, r.err_code) == (5, "invalid.input"), bad
    assert len(ws.ticket_json("1")["questions"]) == 2


def test_options_flag_is_gone(ws, cli):
    claimed(cli)
    r = cli.j("ask", "q?", "--options", "a,b")
    assert r.code != 0
    assert ws.ticket_json("1")["questions"] == []


def test_unattended_ask_takes_options_too(ws, anon):
    Cli(ws)("new", "A ticket")
    r = anon.j("ask", "can I?", "--ref", "1", *A)
    assert r.code == 0, r.out
