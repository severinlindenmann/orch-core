"""orch addon preview (#251): an addon slot drawn with the dashboard's templates into a file, read-only, no token."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from orch import cli
from orch.dashboard import preview

TEMPLATE = Path(__file__).resolve().parents[1] / "addon-template"
FIXTURES = TEMPLATE / "tests" / "fixtures"

PANEL_ADDON = '''
from orch.addons.api import PendingDecision
from orch.addons.widgets import Action, Card, Table, Text, Tile


class Panel:
    def __init__(self, ctx):
        self.page = f"page.{ctx.name}"

    def widgets(self, slot, view):
        if slot == "ticket.external":
            tid = getattr(view.ticket, "id", "?")
            return [Card("Published", (Table(("Name", "Status"), (("report", "live"),),
                                             empty="Nothing published from this ticket."),
                                       Action("ping", "Ping it", tid)))]
        if slot == "today.summary":
            return [Tile("Live", 3, "neu", sub="2 apps · 1 share")]
        if slot == self.page:
            return [Card("Panel page", (Text(f"tab is {view.params.get('tab', 'none')}"),))]
        return []

    def decisions(self, view):
        return [PendingDecision(id="p1", title="Publish report?", body="A staged share waits.", ticket="DEMO-0001",
                                choices=(("yes", "Publish"), ("no", "Not now")))]

    def resolve(self, decision_id, choice, ctx):
        raise AssertionError("a preview never resolves a decision")

    def act(self, action_id, target, ctx):
        raise AssertionError("a preview never runs an action")


def create(ctx):
    return Panel(ctx)
'''


@pytest.fixture
def panel_addon(tmp_path):
    folder = tmp_path / "panel-addon"
    (folder / "panel_addon").mkdir(parents=True)
    (folder / "panel_addon" / "__init__.py").write_text(PANEL_ADDON)
    (folder / "README.md").write_text("test addon\n")
    (folder / "orch-addon.json").write_text(json.dumps({
        "name": "panel-addon", "title": "Panel", "version": "0.1.0", "requires_api": "2", "kind": "in-process",
        "capabilities": ["page", "panel", "decisions"], "slots": ["ticket.external", "today.summary"],
        "binaries": [], "env": [], "entry": "panel_addon:create", "menu": {"title": "Panel", "icon": "box"},
        "actions": [{"id": "ping", "label": "Ping", "confirm": "Ping it?"}]}))
    return folder


@pytest.fixture
def ticket(aops):
    return aops.new("A ticket to draw a panel for").id


def test_page_from_fixtures_looks_like_the_dashboard(ws):
    html = preview.render(ws, str(TEMPLATE), "page.hello-status", fixtures=FIXTURES, theme="dark", width=375)
    assert 'data-theme="dark"' in html and "width: 375px" in html
    assert "Hello status 0.1.0 · page.hello-status · 375 px · dark" in html
    assert "Branch" in html and 'class="addon-page"' in html  # the dashboard's own page body
    assert "--bg" in html  # the dashboard's tokens are inlined


def test_fixtures_and_fetch_never_touch_the_workspace_cache(ws):
    preview.render(ws, str(TEMPLATE), "page.hello-status", fixtures=FIXTURES)
    assert not (Path(ws.state_dir) / "addons" / "hello-status").exists()


def test_without_fetch_nothing_runs(ws, monkeypatch):
    def no_process(*a, **kw):
        raise AssertionError("a preview without --fetch started a process")
    monkeypatch.setattr(subprocess, "Popen", no_process)
    html = preview.render(ws, str(TEMPLATE), "page.hello-status")
    assert "Hello status" in html


def test_no_token_and_no_script(ws, panel_addon, ticket):
    html = preview.render(ws, str(panel_addon), "ticket.external", ticket=ticket)
    assert "Ping it" in html and "report" in html
    assert "token=" not in html and 'name="token"' not in html  # no dashboard token in a link or a form
    assert "<script" not in html.lower()
    assert "Nothing on this page does anything." in html


def test_ticket_panel_tiles_decisions_and_params(ws, panel_addon, ticket):
    aside = preview.render(ws, str(panel_addon), "ticket.external", ticket=ticket)
    assert 'class="ticket-aside"' in aside and "width: 360px" in aside
    tiles = preview.render(ws, str(panel_addon), "today.summary")
    assert "summary-addons" in tiles and "2 apps · 1 share" in tiles
    cards = preview.render(ws, str(panel_addon), "today.from_addons")
    assert "Publish report?" in cards and "Not now" in cards
    page = preview.render(ws, str(panel_addon), "page.panel-addon", params={"tab": "shares"})
    assert "tab is shares" in page


@pytest.mark.parametrize("args, says", [
    (["--slot", "page.nope"], "draws nothing in 'page.nope'"),
    (["--slot", "ticket.external"], "pass --ticket"),
    (["--slot", "ticket.external", "--ticket", "DEMO-9999"], "no ticket"),
    (["--slot", "page.panel-addon", "--theme", "sepia"], "light or dark"),
    (["--slot", "page.panel-addon", "--param", "tab"], "not key=value"),
])
def test_cli_refuses_clearly_with_exit_2(ws, panel_addon, tmp_path, capsys, args, says):
    code = cli.run(["addon", "preview", str(panel_addon), *args, "--out", str(tmp_path / "x.html")])
    assert code == 2
    assert says in capsys.readouterr().err


def test_cli_unknown_addon_and_bad_out(ws, tmp_path, capsys):
    assert cli.run(["addon", "preview", "no-such-addon", "--slot", "page.x", "--out", str(tmp_path / "x.html")]) == 2
    assert cli.run(["addon", "preview", str(TEMPLATE), "--slot", "page.hello-status", "--out", str(tmp_path / "x.pdf")]) == 2


def test_cli_writes_html_and_agents_may_run_it(ws, tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    out = tmp_path / "shots" / "hello.html"
    assert cli.run(["addon", "preview", str(TEMPLATE), "--slot", "page.hello-status", "--fixtures", str(FIXTURES),
                    "--out", str(out)]) == 0
    assert "Hello status" in out.read_text()


def test_png_without_playwright_says_how_to_get_it(ws, tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    assert cli.run(["addon", "preview", str(TEMPLATE), "--slot", "page.hello-status", "--out", str(tmp_path / "x.png")]) == 2
    assert "playwright install chromium" in capsys.readouterr().err
