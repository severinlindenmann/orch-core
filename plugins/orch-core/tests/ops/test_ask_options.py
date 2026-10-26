"""orch ask takes plain-words option titles; the op derives the signed keys."""

from __future__ import annotations

import pytest

from orch.ops.plans import option_keys, option_slug
from tests.ops.helpers import Cli


@pytest.mark.parametrize(
    ("title", "key"),
    [
        ("json-drives-html", "json_drives_html"),
        ("Data-driven (JSON drives HTML)", "data_driven_json_drives_html"),
        ("Später", "spaeter"),
        ("1. Data", "o_1_data"),
        ("Café", "cafe"),
        ("日本語", "o_"[:-1] + "o"),
        ("x" * 40, "x" * 32),
        ("alpha beta gamma delta epsilon zeta", "alpha_beta_gamma_delta_epsilon"),
    ],
)
def test_slug(title, key):
    if title == "日本語":
        assert option_slug(title) == "o"
        return
    assert option_slug(title) == key


def test_collisions_get_suffix():
    assert option_keys(["a-b", "a_b", "A B"]) == ["a_b", "a_b_2", "a_b_3"]
    assert [len(k) for k in option_keys(["x" * 40, "x" * 40])] == [32, 32]


def test_ask_titles_and_rec(ws, cli: Cli):
    cli("new", "A ticket")
    cli("claim", "1")
    cli("ask", "Which?", "--options", "json-drives-html,Separate", "--rec", "Data")  # no match -> refused
    r = cli("ask", "Which?", "--options", "json-drives-html,Separate", "--rec", "json-drives-html")
    assert r.first.startswith("ok "), (r.out, r.err)
    qs = ws.ticket_json("1")["questions"]
    assert qs[-1]["options"] == [
        {"key": "json_drives_html", "label": "json-drives-html"},
        {"key": "separate", "label": "Separate"},
    ]
    assert qs[-1]["recommended"] == "json_drives_html"
    cli("ask", "By label?", "--options", "Yes now,Later", "--rec", "later")
    assert ws.ticket_json("1")["questions"][-1]["recommended"] == "later"


def test_ask_refusal_is_retryable_with_hint(ws, cli: Cli):
    cli("new", "A ticket")
    cli("claim", "1")
    r = cli("ask", "Which?", "--options", "A,B", "--rec", "Nothing")
    assert "retry:true" in r.err and "next: orch ask" in r.err


def test_ask_plain_keys_unchanged(ws, cli: Cli):
    cli("new", "A ticket")
    cli("claim", "1")
    cli("ask", "Which?", "--options", "csv,api", "--rec", "csv")
    assert ws.ticket_json("1")["questions"][-1]["options"] == [{"key": "csv", "label": "csv"}, {"key": "api", "label": "api"}]
