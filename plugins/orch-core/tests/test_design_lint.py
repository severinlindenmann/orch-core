"""`orch addon check` design warnings W1-W18 (design-system spec §4.5). Each rule has a case that warns and one that
does not; the errors E1-E3 are widget_problems (test_ds_widgets.py, test_addon_widgets.py)."""
import json

import pytest

from addon_fixtures import GOOD, make_addon
from orch.addons.design_lint import manifest_warnings, own_names, widget_warnings
from orch.addons.manifest import parse_manifest
from orch.testing.pytest_plugin import orch_user_dir  # noqa: F401
from orch.addons.widgets import (KV, QR, Action, Badge, Callout, Card, Chips, Copy, Link, Search, Table, Text, Tile,
                                 Time)

PAGE = "page.hello-status"
OK_EMPTY = "No runs failed in the last day."


def ids(widgets, slot=PAGE):
    return sorted({w.split(" ", 1)[0] for w in widget_warnings(list(widgets), slot)})


def test_a_well_made_page_has_no_warnings():
    page = [
        Callout("warn", "Login needed for 1 repo", "gh has no access to acme/infra."),
        Chips((Link("Mine", "/addons/x/?s=mine", current=True), Link("All", "/addons/x/")), label="Show"),
        Card("Failed runs", (
            KV((("Open", 3), ("Failing", Badge("err", "1 failed"))), layout="stats"),
            Table(("Run", "State", "When"), (
                (Link("Nightly build", "https://ci.example/1"), Badge("err", "failed"), Time("2026-10-03T12:00:00Z")),
                (Link("PR #22", "https://ci.example/2"), Badge("info", "1 running 3 h"), None),
            ), empty=OK_EMPTY),
            Action("rerun", "Rerun checks"), Action("ignore", "Ignore", quiet=True),
        ), role="err"),
        Copy("Copy command", "gh auth refresh"),
        QR("https://example.com", "Scan to open"),
        Search("q", placeholder="Title or text"),
    ]
    assert widget_warnings(page, PAGE) == []


@pytest.mark.parametrize("slot, cols, warn", [
    (PAGE, 5, False), (PAGE, 6, True), ("board.external", 6, True),
    ("ticket.code", 3, False), ("ticket.code", 4, True), ("today.from_addons", 4, True),
])
def test_w1_too_many_columns_for_the_slot(slot, cols, warn):
    names = tuple(f"Column {i}" for i in range(cols))
    assert ("W1" in ids([Table(names, (), empty=OK_EMPTY)], slot)) == warn


def test_w2_long_table_needs_a_filter():
    rows = tuple((f"r{i}",) for i in range(101))
    assert "W2" in ids([Table(("Run",), rows, empty=OK_EMPTY)])
    assert "W2" not in ids([Search("q"), Table(("Run",), rows, empty=OK_EMPTY)])
    assert "W2" not in ids([Chips((Link("All", "/x", current=True),)), Card("Runs", (Table(("Run",), rows, empty=OK_EMPTY),))])


@pytest.mark.parametrize("empty, warn", [("Nothing here.", True), ("No runs.", True), (OK_EMPTY, False)])
def test_w3_empty_text_says_why(empty, warn):
    assert ("W3" in ids([Table(("Run",), (), empty=empty)])) == warn


@pytest.mark.parametrize("text, warn", [("failed", False), ("1 running 3 h", False), ("login needed", False),
                                        ("this has far too many words", True), ("averyveryverylongsingleword!", True)])
def test_w4_badge_is_short(text, warn):
    assert ("W4" in ids([Badge("neu", text)])) == warn


@pytest.mark.parametrize("w, warn", [(Text("✓ all good"), True), (Badge("ok", "✔ passed"), True),
                                     (Text("Deployed 🎉"), True), (Text("All 4 checks passed."), False),
                                     (Text(""), False), (Table(("A", "B"), (("x", ""),), empty=OK_EMPTY), False)])
def test_w5_no_glyphs_or_emoji(w, warn):
    assert ("W5" in ids([w])) == warn


@pytest.mark.parametrize("w", [Badge("err", "failed!"), Callout("warn", "Heads up!"), Card("Runs!"),
                               Action("rerun", "Rerun now!")])
def test_w6_no_exclamation_marks(w):
    assert "W6" in ids([w])


@pytest.mark.parametrize("w, warn", [(Card("FAILED runs"), True), (Card("Open PR and CI state"), False),
                                     (Card("Runs:"), True), (Table(("Repo:",), (), empty=OK_EMPTY), True),
                                     (Table(("ID", "Repo"), (), empty=OK_EMPTY), False), (Card("Blocked by DEMO-12"), False)])
def test_w7_sentence_case_and_no_trailing_colons(w, warn):
    assert ("W7" in ids([w])) == warn


@pytest.mark.parametrize("w, warn", [(Badge("ok", "updated 2 min ago"), True), (Badge("ok", "synced"), True),
                                     (Badge("neu", "synced"), False), (Badge("ok", "passed"), False)])
def test_w8_freshness_is_core_s(w, warn):
    assert ("W8" in ids([w])) == warn


@pytest.mark.parametrize("w, warn", [(Badge("ok", "failed"), True), (Badge("err", "failed"), False),
                                     (Badge("info", "login needed"), True), (Badge("warn", "login needed"), False),
                                     (Badge("ok", "running"), True), (Badge("info", "in progress"), False),
                                     (Callout("info", "Sync failed"), True)])
def test_w9_role_matches_the_words(w, warn):
    assert ("W9" in ids([w])) == warn


def test_w10_one_callout_at_the_top():
    assert "W10" in ids([Callout("warn", "Partial data"), Callout("info", "Simulated")])
    assert "W10" in ids([Card("Runs", (Table(("Run",), (), empty=OK_EMPTY), Callout("warn", "Partial data")))])
    assert "W10" not in ids([Callout("warn", "Partial data"), Card("Runs", (Callout("info", "Simulated"),))])
    assert "W10" not in ids([Callout("warn", "a"), Callout("info", "b")], "ticket.code")  # pages only


def test_w11_chips_count_and_labels():
    many = Chips(tuple(Link(f"Filter {i}", f"/x?f={i}") for i in range(9)), label="Show")
    assert "W11" in ids([many])
    two = Card("Runs", (Chips((Link("a", "/a"),)), Chips((Link("b", "/b"),), label="Repo")))
    assert "W11" in ids([two])
    labelled = Card("Runs", (Chips((Link("a", "/a"),), label="Show"), Chips((Link("b", "/b"),), label="Repo")))
    assert "W11" not in ids([labelled])


@pytest.mark.parametrize("w, warn", [(Text("Last run 2026-10-03T12:00:00Z"), True), (Text("at 14:00 UTC"), True),
                                     (Table(("When",), (("2026-10-03T12:00",),), empty=OK_EMPTY), True),
                                     (Text("Last run 2 h ago"), False)])
def test_w12_timestamps_use_time(w, warn):
    assert ("W12" in ids([w])) == warn


@pytest.mark.parametrize("w, warn", [(Link("here", "/x"), True), (Link("Click here", "/x"), True),
                                     (Link("https://a.example/b", "https://a.example/b"), True), (Link("Open run", "/x"), False)])
def test_w13_link_text_says_where(w, warn):
    assert ("W13" in ids([w])) == warn


def test_w14_action_budget():
    four = Card("Runs", tuple(Action("rerun", f"Rerun {i}") for i in range(4)))
    assert "W14" in ids([four])
    row = Table(("Run", "Do", "Also"), (("r", Action("rerun", "Rerun"), Action("ignore", "Ignore", quiet=True)),), empty=OK_EMPTY)
    assert "W14" in ids([row])
    assert "W14" not in ids([Card("Runs", (Action("rerun", "Rerun"), Action("ignore", "Ignore")))])


def test_w15_no_table_in_a_nested_card():
    nested = Card("Repos", (Card("a/b", (Table(("Run",), (), empty=OK_EMPTY),)),))
    assert "W15" in ids([nested])
    assert "W15" not in ids([Card("Repos", (Table(("Run",), (), empty=OK_EMPTY),))])


@pytest.mark.parametrize("w, warn", [(Card("Runs", (Text("x"),), role="err"), True),
                                     (Card("Failed runs", (Text("x"),), role="err"), False),
                                     (Card("Runs", (Badge("err", "2 failed"),), role="err"), False),
                                     (Card("Runs", (Table(("Run", "State"), (("r", Badge("warn", "stale")),), empty=OK_EMPTY),), role="warn"), False),
                                     (Card("Runs", (Text("x"),), role="info"), False)])
def test_w16_role_card_is_not_colour_only(w, warn):
    assert ("W16" in ids([w])) == warn


@pytest.mark.parametrize("tile, warn", [(Tile("Failing checks", 4, "err", sub="in 2 repos"), False),
                                        (Tile("Failing checks", 4, "err"), True),
                                        (Tile("Failing checks", "many", "err", sub="x"), True),
                                        (Tile("Failing checks", "4", "err", sub="x"), False),
                                        (Tile("Failing checks", None, "neu", sub="unknown"), False)])
def test_w17_tile_has_a_sub_and_a_number(tile, warn):
    assert ("W17" in ids([tile], "today.summary")) == warn


@pytest.mark.parametrize("title, menu, warn", [("Hello status", "Hello status", False),
                                               ("GitHub code reviews", "Code reviews", False),
                                               ("Hello Status", "Hello status", True),
                                               ("Hello status", "A menu title that is much too long", True)])
def test_w18_manifest_titles(title, menu, warn):
    m = parse_manifest({**GOOD, "title": title, "menu": {"title": menu, "icon": "status"}})
    assert (any(w.startswith("W18") for w in manifest_warnings(m))) == warn


def test_the_contract_sample_payload_counts_as_one_word():
    payload = "<script>alert('orch')</script>"
    assert "W4" in ids([Badge("warn", f"stale: {payload}")])
    assert widget_warnings([Badge("warn", f"stale: {payload}")], PAGE, sample=payload) == []


def test_warnings_name_the_place():
    out = widget_warnings([Card("Runs", (Badge("ok", "failed"),))], PAGE)
    assert out == ["W9 Card.body[0]: Badge role 'ok' does not match its words 'failed' (failed, error and broken are err)"]


# ---------- the CLI: △ warnings, --strict, --format v2 ----------

LOUD = '''
from orch.addons.widgets import Badge, Card
class Addon:
    def __init__(self, ctx): self.ctx = ctx
    def widgets(self, slot, view): return [Card("Status", (Badge("ok", "updated just now"),))]
def create(ctx): return Addon(ctx)
'''


def _loud(tmp_path):
    manifest = {**GOOD, "capabilities": ["page", "settings"]}
    folder = make_addon(tmp_path, manifest=manifest)
    (folder / "hello_status" / "__init__.py").write_text(LOUD, encoding="utf-8")
    return folder


def test_cli_prints_warnings_and_passes_without_strict(tmp_path, capsys, orch_user_dir):
    from orch import cli
    assert cli.run(["addon", "check", str(_loud(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "△ W8" in out and "passes orch addon check" in out and "1 design warning" in out


def test_cli_strict_fails_on_warnings(tmp_path, capsys, orch_user_dir):
    from orch import cli
    assert cli.run(["addon", "check", str(_loud(tmp_path)), "--strict"]) == 5
    assert "△ W8" in capsys.readouterr().out


def test_cli_json_v2_splits_errors_and_warnings(tmp_path, capsys, orch_user_dir):
    from orch import cli
    folder = str(_loud(tmp_path))
    assert cli.run(["addon", "check", folder, "--json", "--format", "v2"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["errors"] == [] and any(w.startswith("W8") for w in doc["warnings"])
    assert cli.run(["addon", "check", folder, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []  # the flat v1 list stays the default (ruling F4)
    assert cli.run(["addon", "check", folder, "--json", "--format", "v2", "--strict"]) == 5
    assert json.loads(capsys.readouterr().out)["warnings"]


# ---------- the default addons and the template are the examples: they pass --strict ----------

PLUGIN = __import__("pathlib").Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("folder", ["addons/databricks", "addons/github-issues", "addons/github-reviews", "addons/wiki",
                                    "addon-template"])
def test_default_addons_and_template_pass_strict(folder):
    from orch.testing import FakeRunner, run_addon_check
    path = PLUGIN / folder
    recordings = []
    for f in sorted((path / "tests" / "fixtures").glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        recordings.extend(data if isinstance(data, list) else [data])
    errors, warnings = run_addon_check(path, runner=FakeRunner(recordings, strict=False))
    assert errors == [] and warnings == []


@pytest.mark.parametrize("w", [Badge("ok", "0 failed"), Badge("ok", "no errors"), Badge("neu", "none failing"),
                               Callout("ok", "No failed runs"), Badge("ok", "nothing stale"),
                               Badge("ok", "not running")])
def test_w9_negations_and_zero_counts_are_not_a_mismatch(w):
    assert "W9" not in ids([w])


@pytest.mark.parametrize("title", ["Open in Databricks", "Pages in Confluence", "GitHub issues", "Runs on Azure DevOps"])
def test_w18_accepts_proper_nouns(title):
    m = parse_manifest({**GOOD, "title": title, "menu": {"title": "Hello status", "icon": "status"}})
    assert manifest_warnings(m) == []


def test_an_addon_may_write_its_own_name_in_capitals():
    m = parse_manifest({**GOOD, "name": "orch-tix", "title": "TIX phone tickets", "menu": {"title": "TIX", "icon": "status"}})
    assert not any(w.startswith("W18") for w in manifest_warnings(m))
    other = parse_manifest({**GOOD, "title": "TIX phone tickets"})
    assert any(w.startswith("W18") for w in manifest_warnings(other))
    tile = [Tile("TIX inbox", "4", "neu", sub="from your phone")]
    assert not any(w.startswith("W7") for w in widget_warnings(tile, "today.summary", names=own_names(m)))
    assert any(w.startswith("W7") for w in widget_warnings(tile, "today.summary"))
