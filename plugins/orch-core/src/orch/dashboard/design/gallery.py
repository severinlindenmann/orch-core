"""Fixtures for the /design gallery (design-system spec §5.2): every addon widget in its variants and states, the
health lines, and the token scales. Plain data, built from the same widget classes an addon returns; the tests
check that every specimen passes `widget_problems` and the design lint, so the gallery is also the lint's
reference. No addon code runs here."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from orch.addons.runtime import HEALTH_ROLE, Banner
from orch.addons.widgets import (KV, QR, Action, Badge, Callout, Card, Chart, ChartSeries, Chips, Copy, Link, Search,
                                 Table, Tabs, Text, Tile, Time)

ADDON = "design-gallery"  # never a real addon: the specimen forms post nowhere useful
ACTIONS = (("rerun", "Rerun checks"), ("ignore", "Ignore"), ("close", "Close local"))
ROLES = ("ok", "info", "warn", "err", "neu")
FRAMES = (("Page", 960), ("Aside", 320), ("Phone", 375))
EMPTY = "No run failed in the last 24 hours. New failures show up here after the next refresh."


@dataclass(frozen=True)
class Specimen:
    title: str
    widgets: tuple
    slot: str = "page.design-gallery"
    note: str = ""
    warns: tuple = ()  # design warnings this specimen shows on purpose (a core rendering rule the lint discourages)


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    specimens: tuple = field(default_factory=tuple)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(minutes_ago: int) -> str:
    return (_now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sections(prefix: str = "DEMO") -> tuple[Section, ...]:
    key = f"{prefix}-0004"
    runs = Table(("Run", "State", "Took", "When"), (
        (Link("Nightly build", "https://ci.example.com/runs/1"), Badge("err", "failed"), "4 min", Time(_iso(12))),
        (Link("PR #22 checks", "https://ci.example.com/runs/2"), Badge("info", "1 running 3 h"), "3 h", Time(_iso(190))),
        (Link("Docs deploy", "https://ci.example.com/runs/3"), Badge("ok", "passed"), None, Time(_iso(1500))),
    ), empty=EMPTY)
    wide = Table(("Pull request", "Repo", "Author", "Checks", "Review", "Size", "Ticket", "Updated"), (
        (Link("#21", "https://github.com/acme/data/pull/21"), "acme/data", "sam", Badge("err", "failed"),
         Badge("neu", "pending"), "M", Link(key, f"/t/{key}"), Time(_iso(40))),
        (Link("#91", "https://github.com/acme/web/pull/91"), "acme/web", "kim", Badge("ok", "passed"),
         Badge("ok", "approved"), "S", None, Time(_iso(300))),
    ), empty=EMPTY)
    xl = Table(tuple(f"Metric {i}" for i in range(1, 11)), (tuple(str(i * 7) for i in range(1, 11)),), empty=EMPTY)
    return (
        Section("text", "Text, badge, link, copy, time", (
            Specimen("Text", (Text(f"The nightly build failed twice since Monday; {key} tracks the fix."),),
                     note="Plain fact in one to three sentences; ticket keys are linked by core."),
            Specimen("Badge", tuple(Badge(r, w) for r, w in zip(ROLES, ("passed", "running", "login needed", "failed", "draft")))),
            Specimen("Link", (Link("Open run", "/addons/design-gallery/"), Link("PR #22", "https://github.com/acme/web/pull/22")),
                     note="External links get the arrow and open in a new tab."),
            Specimen("Copy", (Copy("Copy command", "gh auth refresh -s read:org"),)),
            Specimen("Time", (Time(_iso(2)), Time(_iso(600), style="at")), note="Relative or wall-clock, full time as tooltip."),
        )),
        Section("actions", "Actions", (
            Specimen("Action, quiet and grouped", (Action("rerun", "Rerun checks", "acme/data#21"),
                                                   Action("ignore", "Ignore", "acme/data#21", quiet=True)),
                     note="Consecutive actions share one row; it stacks full width in a narrow container."),
        )),
        Section("callouts", "Callouts", tuple(
            Specimen(f"Callout {r}", (Callout(r, t, x),)) for r, t, x in (
                ("ok", "All repositories reachable", "Every configured repository answered on the last fetch."),
                ("info", "Simulated data", "This page shows demo data until a workspace is connected."),
                ("warn", "Login needed for 1 repo", "gh has no access to acme/infra. Run gh auth refresh, then Refresh."),
                ("err", "Sync failed", "The last fetch could not reach the server. The data below is from 10:42."),
                ("neu", "Partial access", "Two of five repositories are private to another team."),
            ))),
        Section("kv", "Key-value lists and stats", (
            Specimen("KV", (KV((("Branch", "feature/login-retry"), ("Owner", "claude-code"), ("Last run", Time(_iso(30))),
                                ("Checks", Badge("ok", "passed")))),)),
            Specimen("KV stats", (KV((("Open", 12), ("Failing", Badge("err", "2")), ("Drafts", 3)), layout="stats"),)),
        )),
        Section("tables", "Tables by column count", (
            Specimen("Table, 4 columns (stacks below 560)", (runs,)),
            Specimen("Table, 8 columns (stacks below 800)", (wide,), warns=("W1",),
                     note="Core can draw it; the lint warns above 5 columns on a page."),
            Specimen("Table, 10 columns (scrolls, sticky key column)", (xl,), warns=("W1",),
                     note="Never stacks; scrolls in its box. The lint warns."),
            Specimen("Table, empty", (Table(("Run", "State"), (), empty=EMPTY),)),
            Specimen("Table in a card (flat, bleeds to the edges)", (Card("Failed runs", (runs,)),)),
        )),
        Section("cards", "Cards", (
            Specimen("Card", (Card("Repository health", (KV((("Open", 4), ("Stale", 1)), layout="stats"),
                                                          Text("Counts include drafts."))),)),
            Specimen("Card with a warn rail", (Card("Stale repositories", (Badge("warn", "stale"),
                                                                           Text("Two repositories were not fetched today.")),
                                                    role="warn"),)),
            Specimen("Card with an err rail", (Card("Failed runs", (Badge("err", "2 failed"),), role="err"),)),
            Specimen("Linked card title", (Card("Code reviews", (Text("3 need your review."),), href="/addons/design-gallery/"),)),
            Specimen("Card grid", (Card("Repositories", tuple(
                Card(name, (KV((("Open", n), ("Failing", f)), layout="stats"),)) for name, n, f in
                (("acme/data", 4, 1), ("acme/web", 7, 0), ("acme/infra", 2, 0))), layout="grid"),)),
        )),
        Section("filters", "Filters and views", (
            Specimen("Chips", (Chips((Link("Needs review 3", "/addons/design-gallery/?s=review", current=True),
                                      Link("Mine 5", "/addons/design-gallery/?s=mine"),
                                      Link("Failing 1", "/addons/design-gallery/?s=failing"),
                                      Link("All 12", "/addons/design-gallery/?s=all")), label="Show"),)),
            Specimen("Tabs", (Tabs((Link("Files", "/addons/design-gallery/", current=True),
                                    Link("Messages", "/addons/design-gallery/?v=messages"),
                                    Link("Devices", "/addons/design-gallery/?v=devices")), label="View"),)),
            Specimen("Search", (Search("q", placeholder="Title or text"),)),
        )),
        Section("qr", "QR", (
            Specimen("QR with its link", (QR("https://example.com/pair/abc", "Scan with the phone"),
                                          Link("Open the pairing page", "https://example.com/pair/abc"))),
        )),
        Section("charts", "Charts", (
            Specimen("Stacked bars", (Chart("Runs per day", ("Mon", "Tue", "Wed", "Thu"),
                                            (ChartSeries("Passed", (12, 9, 14, 11)), ChartSeries("Failed", (1, 3, 0, 2))),
                                            stacked=True, unit="runs"),)),
            Specimen("Line", (Chart("Open reviews", ("W1", "W2", "W3", "W4"), (ChartSeries("Open", (4, 6, 3, 5)),),
                                    style="line", unit="reviews"),)),
        )),
        Section("tiles", "Today tiles", (
            Specimen("Tile", (Tile("Failing checks", 4, "err", sub="in 2 repos"),), slot="today.summary"),
            Specimen("Tile, unknown", (Tile("Open reviews", None, "neu", sub="login needed"),), slot="today.summary"),
        )),
    )


def banners() -> tuple[Banner, ...]:
    """One health line per state (spec §3.1 Health banner)."""
    now = _now()
    msg = {"auth_required": "gh auth login", "error": "server answered 502", "offline": "no network"}
    return tuple(Banner(h, HEALTH_ROLE.get(h, "err"), now - timedelta(minutes=12), now + timedelta(minutes=20) if
                        h == "rate_limited" else None, msg.get(h, ""))
                 for h in ("ok", "stale", "auth_required", "offline", "rate_limited", "error", "never_fetched"))


def confirms() -> dict:
    return {a: f"{label}?" for a, label in ACTIONS}


def ticket_cards(prefix: str) -> list[dict]:
    """Three ticket_card() shapes (data.cards) for the S/M/L specimens: the human's move, an agent working with a
    PR, a stale claim. Plain dicts in the card's shape; the gallery never reads a workspace."""
    from orch.dashboard.data.cards import CHECKS, GATE_GLYPH, ICONS

    def gate(state):
        glyph, role, word = GATE_GLYPH[state]
        return {"state": state, "glyph": glyph, "role": role, "word": word}

    def tasks(done, doing, todo):
        from orch.dashboard.data import tasks as tasks_data
        s = {"total": done + doing + todo, "done": done, "doing": doing, "todo": todo, "skipped": 0, "blocked": 0,
             "closed": done}
        return {**s, "human": 0, "open": [], "progress": tasks_data.progress(s),
                "segments": ["done"] * done + ["doing"] * doing + ["todo"] * todo, "bar": tasks_data.bar(s)}

    def code(n, checks):
        glyph, role = CHECKS[checks]
        return {"label": f"PR #{n}", "url": None, "checks": checks, "draft": False, "state": "open", "glyph": glyph,
                "role": role, "others": 0}

    def move(what, label, role, who):
        return {"who": who, "what": what, "ref": None, "label": label, "role": role, "icon": ICONS[role], "since": None}

    now = _now()
    base = {"status": "in-progress", "status_label": "In progress", "priority": "normal", "hot": False, "type": "feature",
            "externals": [], "parent": None, "epic": None, "rollup": None, "labels": [], "updated": "", "updated_at": None, "blockers": [],
            "broken": False, "left": ""}
    return [
        {**base, "id": f"{prefix}-0004", "title": "Add coverage reporting to CI", "size": "s",
         "external": {"key": "GH-4", "url": None}, "move": move("approve-plan", "Approve plan", "you", "you"),
         "gates": {"requirements": gate("approved"), "plan": gate("you")}, "tasks": None,
         "ac": {"proven": 0, "total": 3}, "code": code(22, "failed"), "agent": None},
        {**base, "id": f"{prefix}-0021", "title": "Add a meter_type dimension to the dbt marts", "size": "m",
         "external": None, "move": move("working", "Working · claude-code 12 min", "info", "agent"),
         "gates": {"requirements": gate("approved"), "plan": gate("approved")}, "tasks": tasks(1, 1, 1),
         "ac": {"proven": 1, "total": 2}, "code": code(31, "passed"),
         "agent": {"harness": "claude-code", "status": "working", "since": now - timedelta(minutes=40),
                   "last": now - timedelta(minutes=12), "last_action": "started T2"}},
        {**base, "id": f"{prefix}-0019", "title": "Alert when the suspect rate exceeds the threshold", "size": "l",
         "external": None, "move": move("stale", "Stale · claude-code 3 h", "warn", "agent"),
         "gates": {"requirements": gate("approved"), "plan": gate("approved")}, "tasks": tasks(2, 1, 1),
         "ac": {"proven": 0, "total": 3}, "code": None,
         "agent": {"harness": "claude-code", "status": "stale", "since": now - timedelta(hours=5),
                   "last": now - timedelta(hours=3), "last_action": None}},
    ]
