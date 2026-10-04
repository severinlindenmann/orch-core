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


def _limits_snap(**last):
    from datetime import datetime, timezone
    from orch.addons.api import Snapshot
    item = {"id": "limits", "kind": "limits", "label": "Limits", "role": "neu", "text": "", "last": last or None}
    return [Snapshot("usage", "workspace", datetime(2026, 10, 4, tzinfo=timezone.utc), items=(item,))]


def test_menu_chip_is_the_weekly_percent_coloured_by_it_with_a_five_hour_line():
    now = 1_000_000.0
    mk = lambda five, week: T.menu_chip(_limits_snap(five=five, week=week, five_reset=now + 3600, week_reset=now + 86400), now)  # noqa: E731
    assert (mk(30, 39).badge.text, mk(30, 39).badge.role) == ("39 %", "ok")
    assert mk(10, 70).badge.role == "warn" and mk(10, 89).badge.role == "warn" and mk(10, 90).badge.role == "err"
    t = mk(30, 39).badge.title
    assert t.startswith("5-hour 30 % · resets ") and " · week 39 % · resets " in t
    line = mk(35, 39).line
    assert [type(p).__name__ for p in line] == ["Badge", "Countdown"]
    assert line[0].role == "ok" and line[0].text == "35%"
    assert mk(75, 1).line[0].role == "warn" and mk(95, 1).line[0].role == "err"


def test_menu_chip_reset_five_hour_window_no_week_and_no_data():
    now = 1_000_000.0
    st = T.menu_chip(_limits_snap(five=95, week=40, five_reset=now - 5, week_reset=now + 99), now)
    assert st.badge.text == "40 %" and "5-hour 0 % · reset · week 40 %" in st.badge.title
    assert [p.text for p in st.line] == ["5h reset"]
    st = T.menu_chip(_limits_snap(five=35, week=None, five_reset=now + 60), now)
    assert st.badge is None and st.line[0].text == "35%"
    assert T.menu_chip(_limits_snap(), now) is None and T.menu_chip([], now) is None
    assert T.menu_chip(_limits_snap(five=None, week=None), now) is None


def test_model_names_are_human_labels():
    assert [T.model_name(m) for m in ("opus-5-5", "sonnet-5", "haiku-4-5", "claude-haiku-4-5-20251001", "claude-opus-5-5",
                                      "claude-sonnet-5", "weird")] == ["Opus 5.5", "Sonnet 5", "Haiku 4.5", "Haiku 4.5",
                                                                       "Opus 5.5", "Sonnet 5", "weird"]


def test_page_rows_are_tickets_by_output_then_shared_then_unlinked():
    def it(kind, label, out, **kw):
        return {"id": label, "kind": kind, "label": label, "role": "neu", "text": "", "week": {"claude-opus-5-5": out},
                "main": {}, "sub": {}, "cost": None, "pct": None, "tickets": [], **kw}
    from datetime import datetime, timezone
    from orch.addons.api import Snapshot
    items = (it("unlinked", "u", 9999), it("shared", "s", 5000), it("ticket", "B-1", 10), it("ticket", "B-2", 300),
             {"id": "limits", "kind": "limits", "label": "Limits", "role": "neu", "text": "", "last": None})
    out = T.page([Snapshot("usage", "workspace", datetime(2026, 10, 4, tzinfo=timezone.utc), items=items)], True)
    table = next(w for c in out if c.kind == "card" and c.title == "Details" for w in c.body)
    assert [r[0] for r in table.rows] == ["B-2", "B-1", "Shared orchestrator (0 tickets)", "Not linked to a ticket"]
    assert table.rows[0][1] == "Opus 5.5"


# -- charts page --------------------------------------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402

from orch.addons.api import Snapshot  # noqa: E402
from orch.addons.widgets import widget_problems  # noqa: E402

NOW = data.to_epoch("2026-10-04T12:00:00Z")


def _walk(ws):
    for w in ws:
        yield w
        for c in getattr(w, "body", ()) or ():
            yield from _walk([c])
        if w.kind == "kv":
            yield from _walk([v for _, v in w.rows if not isinstance(v, str)])


def _snap(**stats):
    base = {"id": "stats", "kind": "stats", "label": "Stats", "role": "neu", "text": "", "built": NOW, "files": 3,
            "daily": {}, "cost_days": {}, "attrib": {"none": 0, "shared": 0, "ticket": 0}, "top": [], "history": []}
    base.update(stats)
    lim = {"id": "limits", "kind": "limits", "label": "Limits", "role": "neu", "text": "", "last": None, "pace": {}}
    return [Snapshot("usage", "workspace", datetime(2026, 10, 4, tzinfo=timezone.utc), items=(lim, base))]


def _charts(ws):
    return [w for w in _walk(ws) if w.kind == "chart"]


def _valid(ws):
    for w in _walk(ws):
        assert widget_problems(w, slot=PAGE, manifest=MANIFEST) == [], w


def test_daily_output_per_family_counts_each_reply_once_incl_subagents_and_weeks_cost_and_highs(tmp_path):
    session(tmp_path, SID, [reply("a", 100, "2026-10-01T10:00:00Z"), reply("a", 150, "2026-10-01T10:00:01Z"),
                            reply("b", 40, "2026-10-02T10:00:00Z", "claude-sonnet-5"),
                            reply("c", 7, "2026-10-02T11:00:00Z", "mystery-1"),
                            cost(1, 3.0, models={"claude-opus-5-5": 3.0})],
            subs=[("x", [reply("s", 10, "2026-10-01T11:00:00Z", "claude-haiku-4-5")])])
    session(tmp_path, OTHER, [reply("d", 5, "2026-10-01T12:00:00Z")])
    stats = build(tmp_path, [])["stats"]
    assert stats["daily"] == {"2026-10-01": [155, 0, 10, 0, 0], "2026-10-02": [0, 40, 0, 0, 7]}
    assert stats["cost_days"] == {"2026-10-02": 3.0}  # booked on the day the session ended
    assert data.monday_of("2026-10-04") == "2026-09-28" and data.monday_of("2026-10-05") == "2026-10-05"


def test_limit_history_keeps_new_highs_per_window_with_real_timestamps(tmp_path):
    log = [{"at": f"2026-10-04T10:0{i}:00Z", "five": five, "five_reset": 9000, "week": week, "week_reset": 99000}
           for i, (five, week) in enumerate([(10, 40), (12, 40), (11, 41), (13, 41)])]
    rows = data.read_limits(str(_write_log(tmp_path, log)))
    hist = data.limit_history(rows, 0)
    assert [(h[1], h[2]) for h in hist] == [(10, 40), (12, 40), (12, 41), (13, 41)]  # the stale 11 is not a new high
    assert hist[1][0] - hist[0][0] == 60


def _write_log(tmp_path, rows):
    p = tmp_path / "limits.jsonl"
    limits(p, rows)
    return p


def test_range_tabs_each_render_a_stacked_chart_within_eight_series():
    daily = {f"2026-09-{d:02d}": [1000 * d, 50, 0, 0, 5] for d in range(10, 31)} | {"2026-10-03": [9000, 0, 0, 1, 0]}
    for rng, n in (("week", 7), ("month", 30), ("all", None), (None, 30), ("bogus", 30)):
        ws = T.page(_snap(daily=daily), True, {"range": rng} if rng else {}, NOW)
        _valid(ws)
        main = _charts(ws)[0]
        assert main.stacked and 1 <= len(main.series) <= 8
        if n:
            assert len(main.labels) == n
        tabs = next(w for w in _walk(ws) if w.kind == "tabs")
        assert [t.current for t in tabs.items].count(True) == 1
    caption = [w.text for w in _walk(T.page(_snap(daily=daily), True, {"range": "all"}, NOW)) if w.kind == "text"]
    assert any("to 04 Oct, by calendar week" in t for t in caption)
    all_labels = _charts(T.page(_snap(daily=daily), True, {"range": "all"}, NOW))[0].labels
    assert all_labels[0].startswith("W37 · 07 Sep (from ")  # the first week is marked partial


def test_zero_total_and_one_day_and_not_ready_states():
    zero = T.page(_snap(daily={"2026-10-03": [0] * 5, "2026-10-04": [0] * 5}), True, {}, NOW)
    _valid(zero)
    assert any(w.kind == "text" and w.text == "No output in this range." for w in _walk(zero))
    assert not _charts(zero)
    one = T.page(_snap(daily={"2026-10-04": [500, 0, 0, 0, 0]}), True, {}, NOW)
    _valid(one)
    assert not any(w.kind == "tabs" for w in _walk(one)) and not _charts(one)
    assert "Reading transcripts" in T.page([], True)[0].text
    assert T.page(_snap(), True, {}, NOW)  # no data at all still renders


def test_limit_cards_pace_and_recorder_off():
    off = T.page(_snap(), True, {}, NOW)
    assert off[0].kind == "callout" and "statusLine" in off[0].text
    snap = _snap()
    reading = {"first_ts": NOW - 2400, "last_ts": NOW, "first": 25.0, "last": 36.0, "n": 5, "reset": NOW + 3600}
    snap[0].items[0].update(last={"at": "2026-10-04T12:00:00Z", "five": 36, "five_reset": NOW + 3600, "week": 41,
                                  "week_reset": NOW + 86400 * 3},
                            pace={"five": reading, "week": dict(reading, last_ts=NOW - 100, first_ts=NOW - 7200)})
    out = T.page(snap, True, {}, NOW)
    _valid(out)
    texts = [w.text for w in _walk(out) if w.kind == "text"]
    assert any(t.startswith("Up 11 points in 40 min. At that pace it would reach 100 % around ") and t.endswith("after the reset.") for t in texts)
    assert any("no weekly pace yet" in t for t in texts)
    assert any(w.kind == "time" for w in _walk(out))
    assert T._pace_text({"n": 1, "first_ts": 0, "last_ts": 0, "first": 5, "last": 5}, "five", NOW + 1, NOW) is None


def test_attribution_top_history_and_cost_charts():
    snap = _snap(daily={"2026-10-03": [10, 0, 0, 0, 0], "2026-10-04": [10, 0, 0, 0, 0]},
                 attrib={"none": 800, "shared": 100, "ticket": 100}, top=[("B-1", 70), ("B-2", 30)],
                 history=[[NOW - 7200, 10.0, 30.0], [NOW - 3600, 20.0, 31.0], [NOW, 25.0, 32.0]],
                 cost_days={"2026-09-30": 10.0, "2026-10-02": 5.0, "2026-10-06": 1.5})
    out = T.page(snap, True, {}, NOW)
    _valid(out)
    by = {c.series[0].name: c for c in _charts(out)}
    attrib, topc = [c for c in _charts(out) if c.horizontal]
    assert attrib.labels == ("No ticket claimed", "Shared runs, not split", "Tied to one ticket") and topc.labels == ("B-1", "B-2")
    line = by["Week"]
    assert line.style == "line" and line.x == "time" and line.labels == (round(NOW - 7200), round(NOW - 3600), round(NOW))
    cost = by["USD"]
    assert cost.labels == ("W40 · 28 Sep", "W41 · 05 Oct") and cost.series[0].values == (15.0, 1.5)
    assert any("10 % can be tied to a single ticket. 80 % comes from sessions" in w.text for w in _walk(out) if w.kind == "text")
    assert not any(c.series[0].name == "USD" for c in _charts(T.page(snap, False, {}, NOW)))


def test_days_and_weeks_follow_the_machine_zone(monkeypatch):
    import time as _t
    monkeypatch.setenv("TZ", "Pacific/Auckland")  # UTC+13 in October: 2026-10-03 23:00Z is already 04 Oct
    _t.tzset()
    assert data.day_of(data.to_epoch("2026-10-03T23:00:00Z")) == "2026-10-04"
    assert data.monday_of(data.day_of(data.to_epoch("2026-10-04T12:00:00Z"))) == "2026-10-05"  # Sunday 12:00Z is Monday there
