import json
from pathlib import Path

import pytest

import orch_ticket_usage as T
from orch.addons.manifest import load_manifest
from orch.addons.runtime import SlotView
from orch.testing import AddonContract, ProviderContract
from orch.testing.workspace import fake_workspace
from orch_ticket_usage import data

ADDON = Path(__file__).resolve().parents[1]
MANIFEST = load_manifest(ADDON)
PAGE = f"page.{MANIFEST.name}"
SID, OTHER = "11111111-aaaa", "22222222-bbbb"


def line(kind, **kw):
    return json.dumps({"type": kind, **kw})


def reply(mid, out, ts, model="claude-opus-5-5"):
    return line("assistant", timestamp=ts, message={"id": mid, "model": model, "usage": {"output_tokens": out}})


def cost(start, usd, ms=1000, add=0, rem=0, models=None):
    return line("cost-state", startTime=start, totalCostUSD=usd, totalDuration=ms, totalLinesAdded=add,
                totalLinesRemoved=rem, modelUsage={m: {"costUSD": c} for m, c in (models or {}).items()})


def session(claude, sid, lines, subs=()):
    d = claude / "projects" / "-proj"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text("\n".join(lines) + "\n")
    for n, (desc, sub_lines) in enumerate(subs):
        s = d / sid / "subagents"
        s.mkdir(parents=True, exist_ok=True)
        (s / f"agent-{n}.jsonl").write_text("\n".join(sub_lines) + "\n")
        (s / f"agent-{n}.meta.json").write_text(json.dumps({"agentType": "general-purpose", "description": desc}))


def limits(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def build(claude, tickets, log="/nonexistent"):
    return {i["id"]: i for i in T.build(claude, tickets, str(log), 1_790_000_000)}


def test_a_streamed_reply_counts_once_from_its_last_line(tmp_path):
    session(tmp_path, SID, [reply("m1", 5, "2026-10-01T10:00:00Z"), reply("m1", 400, "2026-10-01T10:00:01Z"),
                            reply("m2", 10, "2026-10-01T10:01:00Z", "<synthetic>")])
    assert data.parse_file(tmp_path / "projects/-proj" / f"{SID}.jsonl")["msgs"][0][1:] == ("claude-opus-5-5", 400)
    assert len(data.parse_file(tmp_path / "projects/-proj" / f"{SID}.jsonl")["msgs"]) == 1


def test_cost_state_is_the_last_per_run_and_runs_add_up(tmp_path):
    session(tmp_path, SID, [cost(1, 1.0, 10, 5, 1, {"a": 1.0}), cost(1, 3.0, 30, 9, 2, {"a": 3.0}),
                            cost(2, 2.0, 5, 1, 0, {"b": 2.0})])
    c = data.cost_of(data.parse_file(tmp_path / "projects/-proj" / f"{SID}.jsonl"))
    assert (c["total"], c["ms"], c["added"], c["removed"], c["models"]) == (5.0, 35, 10, 2, {"a": 3.0, "b": 2.0})


def test_ticket_with_its_own_session_gets_main_and_named_subagents(tmp_path):
    session(tmp_path, SID, [reply("m1", 100, "2026-10-01T10:00:00Z"), cost(1, 2.5, 60000, 7, 3, {"claude-opus-5-5": 2.5})],
            subs=[("B-2914 UI builder", [reply("s1", 30, "2026-10-01T11:00:00Z", "claude-sonnet-5-5")]),
                  ("Security review of B-29140", [reply("s2", 8, "2026-10-01T11:00:00Z", "claude-sonnet-5-5")]),
                  ("Challenge a draft", [reply("s3", 4, "2026-10-01T11:00:00Z", "claude-sonnet-5-5")])])
    item = build(tmp_path, [("B-2914", "t", [SID]), ("B-29140", "t", [])])["ticket:B-2914"]
    assert item["main"] == {"claude-opus-5-5": 100}
    # its own session owns the unnamed subagent too; the one naming B-29140 belongs to that id (B-2914 is not inside it)
    assert item["sub"] == {"claude-sonnet-5-5": 30 + 4}
    assert item["cost"]["total"] == 2.5 and item["cost"]["added"] == 7 and not item["running"]


def test_a_session_without_a_cost_record_is_still_running(tmp_path):
    session(tmp_path, SID, [reply("m1", 100, "2026-10-01T10:00:00Z")])
    assert build(tmp_path, [("B-1", "t", [SID])])["ticket:B-1"]["running"] is True


def test_shared_session_is_not_split(tmp_path):
    session(tmp_path, SID, [reply("m1", 1000, "2026-10-01T10:00:00Z")],
            subs=[("B-2 builder", [reply("s1", 50, "2026-10-01T11:00:00Z", "claude-sonnet-5-5")])])
    items = build(tmp_path, [("B-1", "t", [SID]), ("B-2", "t", [SID])])
    assert items["ticket:B-1"]["main"] == {} and items["ticket:B-1"]["sub"] == {}
    assert items["ticket:B-2"]["sub"] == {"claude-sonnet-5-5": 50} and items["ticket:B-2"]["main"] == {}
    assert items[f"shared:{SID}"]["main"] == {"claude-opus-5-5": 1000}
    assert items["ticket:B-1"]["shared"][0]["tickets"] == 2
    panel = T.ticket_panel(items["ticket:B-1"], True)[0]
    assert any("Shared orchestrator: 2 tickets, 1.0k output tokens" in w.text for w in panel.body if hasattr(w, "text"))


def test_transcript_not_on_this_machine(tmp_path):
    item = build(tmp_path, [("B-1", "t", ["33333333-cccc"])])["ticket:B-1"]
    assert item["missing"] == ["33333333-cccc"]
    assert any("not on this machine" in getattr(w, "text", "") for w in T.ticket_panel(item, True)[0].body)


def test_limit_share_is_spread_by_output_tokens_and_a_reset_starts_over(tmp_path):
    session(tmp_path, SID, [reply("a", 300, "2026-10-01T10:30:00Z"), reply("c", 100, "2026-10-02T10:30:00Z")])
    session(tmp_path, OTHER, [reply("b", 100, "2026-10-01T10:30:00Z")])
    t = lambda s: data.to_epoch(s)  # noqa: E731
    log = tmp_path / "limits.jsonl"
    limits(log, [
        {"at": "2026-10-01T10:00:00Z", "week": 10, "week_reset": 1000},
        {"at": "2026-10-01T11:00:00Z", "week": 14, "week_reset": 1000},   # +4 over a (300) and b (100): 3 and 1
        {"at": "2026-10-01T11:05:00Z", "week": 12, "week_reset": 1000},   # a stale reading: ignored
        {"at": "2026-10-02T10:00:00Z", "week": 2, "week_reset": 2000},    # reset: starts over
        {"at": "2026-10-02T11:00:00Z", "week": 4, "week_reset": 2000},    # +2 all to c's session
    ])
    assert t("2026-10-01T10:00:00Z") < t("2026-10-01T11:00:00Z")
    items = build(tmp_path, [("B-1", "t", [SID]), ("B-2", "t", [OTHER])], log)
    assert items["ticket:B-1"]["pct"] == pytest.approx({"all": 3 + 2, "week": 2})
    assert items["ticket:B-2"]["pct"] == pytest.approx({"all": 1, "week": 0})


def test_no_log_data_is_unknown_not_zero(tmp_path):
    session(tmp_path, SID, [reply("a", 300, "2026-10-01T10:30:00Z")])
    item = build(tmp_path, [("B-1", "t", [SID])])["ticket:B-1"]
    assert item["pct"] is None
    assert T.pct_text(item["pct"], "all").startswith("unknown")


def test_sessions_without_a_ticket_are_one_muted_row(tmp_path):
    session(tmp_path, SID, [reply("a", 300, "2026-10-01T10:30:00Z")])
    session(tmp_path, OTHER, [reply("b", 200, "2026-10-01T10:30:00Z")])
    items = T.build(tmp_path, [], "/nonexistent", data.to_epoch("2026-10-02T00:00:00Z"))
    [row] = [i for i in items if i["kind"] == "unlinked"]
    assert row["main"] == {"claude-opus-5-5": 500}


def test_provider_reads_tickets_and_the_page_renders(tmp_path):
    claude = tmp_path / "home" / ".claude"
    session(claude, SID, [reply("a", 2000, "2026-10-01T10:00:00Z"), cost(1, 4.2, 120000, 3, 1, {"claude-opus-5-5": 4.2})])
    ws = fake_workspace(tmp_path / "ws", tickets=[{"title": "x", "meta": {"sessions": [{"id": SID, "harness": "claude-code"}]}}])
    addon = ws.load(ADDON)
    snap = T.UsageProvider().fetch(addon.ctx.provider_context(), "workspace", None)
    assert snap.health == "ok"
    [ticket] = [i for i in snap.items if i["kind"] == "ticket"]
    assert ticket["main"] == {"claude-opus-5-5": 2000} and ticket["cost"]["total"] == 4.2
    ws.cache(MANIFEST.name, snap)
    page = addon.obj.widgets(PAGE, SlotView(ws.ws, addon, PAGE))
    assert page[0].kind == "callout"  # no limits log: one callout at the top
    panel = addon.obj.widgets("ticket.code", SlotView(ws.ws, addon, "ticket.code", ticket=ws.ws and _ticket(ws)))
    assert panel[0].title == "Usage" and "$4.20" in repr(panel)


def _ticket(ws):
    from orch.core import store
    return store.load(ws.ws, ws.tickets[0])[1]


def test_show_cost_off_hides_dollars(tmp_path):
    session(tmp_path, SID, [reply("a", 5, "2026-10-01T10:00:00Z"), cost(1, 9.99, models={"claude-opus-5-5": 9.99})])
    item = build(tmp_path, [("B-1", "t", [SID])])["ticket:B-1"]
    card = lambda show: repr(T.ticket_panel(item, show))  # noqa: E731
    assert "$9.99" in card(True) and "$9.99" not in card(False)


def test_widgets_render_nothing_for_other_slots_and_no_snapshot(orch_workspace):
    addon = orch_workspace.load(ADDON)
    assert addon.obj.widgets("ticket.sync", SlotView(orch_workspace.ws, addon, "ticket.sync")) == []
    assert addon.obj.widgets("ticket.code", SlotView(orch_workspace.ws, addon, "ticket.code")) == []


def test_a_workspace_that_did_not_enable_it_renders_nothing(orch_workspace):
    from orch.addons.runtime import AddonRuntime
    runtime = AddonRuntime(orch_workspace.ws)
    assert runtime.page(MANIFEST.name) is None
    assert runtime.slot("ticket.code", _ticket(orch_workspace)) == []


class TestProvider(ProviderContract):
    @pytest.fixture
    def provider(self):
        return T.UsageProvider()

    @pytest.fixture
    def provider_ctx(self, orch_workspace, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "none"))
        return orch_workspace.provider_context(MANIFEST)


class TestAddon(AddonContract):
    addon_dir = ADDON
