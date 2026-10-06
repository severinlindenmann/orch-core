"""ticket-usage: Claude Code usage per ticket (cost estimate, output tokens per model, share of the weekly limit).

A provider reads Claude's transcripts and the limits log in the background; the ticket panel and the Usage page only
read its snapshot. The transcript format is Claude Code's own: what we cannot read is shown as unknown, never as 0.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from orch.addons.api import Snapshot
from orch.addons.widgets import (KV, Badge, Callout, Card, Chart, ChartSeries, Copy, Countdown, Link, MenuRow, MenuStatus, Table,
                                 Tabs, Text, Time)

from .data import (DEFAULT_LOG, FAMILIES, begin_fetch, open_cache, save_cache, to_epoch, claude_dir, cost_of, day_of, distribute, family, limit_history, limits_log_state,
                   monday_of, names, pace,
                   parse_file, read_limits, recorder_wired, subagents, transcripts, week_rises)

WEEK_S = 7 * 86400
HISTORY_DAYS = 90
UNKNOWN_PCT = "unknown, needs the status line recorder"


def _owner(store: dict, key: str) -> dict:
    return store.setdefault(key, {"main": {}, "sub": {}, "week": {}, "first": None, "last": None})


def build(claude, tickets, log_path, now) -> list[dict]:
    """The snapshot items. `tickets` is [(id, title, [session ids])]; `now` an epoch."""
    ids = [t[0] for t in tickets]
    by_session: dict = {}
    for tid, _, sids in tickets:
        for sid in sids:
            by_session.setdefault(sid, []).append(tid)
    log_state = limits_log_state(log_path)
    log = [] if log_state["state"] == "relative" else read_limits(log_path)
    rises = week_rises(log)
    resets = [r["week_reset"] for r in log if isinstance(r.get("week_reset"), (int, float))]
    win = (resets[-1] - WEEK_S) if resets else now - WEEK_S
    horizon = now - HISTORY_DAYS * 86400
    since = min([win, horizon] + [lo for lo, _, _, _ in rises])
    found = transcripts(claude)
    owners: dict = {}
    msgs: list = []
    costs: dict = {}  # owner -> cost record summed over its sessions
    running: set = set()
    daily: dict = {}  # local day -> output tokens per model family, every reply on this machine
    cost_days: dict = {}  # local day the session ended -> API-price estimate

    def take(owner, role, parsed):
        o = _owner(owners, owner)
        for ts, model, out in parsed["msgs"]:
            o[role][model] = o[role].get(model, 0) + out
            msgs.append((ts, owner, out))
            if ts >= horizon:
                daily.setdefault(day_of(ts), [0] * len(FAMILIES))[family(model)] += out
            o["first"] = ts if o["first"] is None else min(o["first"], ts)
            o["last"] = ts if o["last"] is None else max(o["last"], ts)
            if ts >= win:
                o["week"][model] = o["week"].get(model, 0) + out

    for sid, path in found.items():
        claimed = by_session.get(sid, [])
        try:
            recent = path.stat().st_mtime >= since
        except OSError:
            continue
        if not claimed and not recent:
            continue
        owner = claimed[0] if len(claimed) == 1 else f"shared:{sid}" if claimed else "unlinked"
        main = parse_file(path)
        take(owner, "main", main)
        cost = cost_of(main)
        if cost is not None:
            ended = main["msgs"][-1][0] if main["msgs"] else path.stat().st_mtime
            if ended >= horizon:
                cost_days[day_of(ended)] = cost_days.get(day_of(ended), 0.0) + cost["total"]
        if cost is None:
            if main["msgs"]:
                running.add(owner)
        else:
            c = costs.setdefault(owner, {"models": {}, "total": 0.0, "ms": 0, "added": 0, "removed": 0})
            for k in ("total", "ms", "added", "removed"):
                c[k] += cost[k]
            for m, usd in cost["models"].items():
                c["models"][m] = c["models"].get(m, 0.0) + usd
        for f, desc in subagents(path):
            named = names(desc, ids)
            # a session of one ticket owns all its subagents; in a shared one only a subagent naming a ticket is that ticket's
            take(named or owner, "sub" if named or len(claimed) == 1 else "main", parse_file(f))

    share = distribute(rises, msgs)
    items = []

    def pct(key):
        s = share.get(key)
        return None if s is None else {"all": s["all"], "week": s["week"]}

    def row(key, kind, label, **extra):
        o = owners.get(key) or _owner({}, key)
        c = costs.get(key)
        item = {"id": key if kind != "ticket" else f"ticket:{key}", "kind": kind, "label": label, "role": "neu",
                "text": "", "main": o["main"], "sub": o["sub"], "week": o["week"],
                "first": o["first"], "last": o["last"], "cost": c, "running": key in running, "pct": pct(key)}
        item.update(extra)
        items.append(item)

    for tid, title, sids in tickets:
        if not sids:
            continue
        shared = [{"session": s, "tickets": len(by_session[s]), "out": owners.get(f"shared:{s}", {}).get("main", {})}
                  for s in sids if len(by_session[s]) > 1 and s in found]
        row(tid, "ticket", tid, title=title, shared=shared, missing=[s for s in sids if s not in found])
    for key in sorted(k for k in owners if k.startswith("shared:")):
        row(key, "shared", "Shared orchestrator", session=key[7:], tickets=by_session.get(key[7:], []))
    if "unlinked" in owners:
        row("unlinked", "unlinked", "Not linked to a ticket")
    last = log[-1] if log else None
    items.append({"id": "limits", "kind": "limits", "label": "Limits", "role": "neu", "text": "", "window_start": win,
                  "log": log_state, "recorder": recorder_wired(claude), "last": {k: last.get(k) for k in ("at", "five", "five_reset", "week", "week_reset")} if last else None,
                  "pace": {k: pace(log, k) for k in ("five", "week")}})
    attrib = {"none": 0, "shared": 0, "ticket": 0}
    top: dict = {}
    for key, o in owners.items():
        n = total(o["week"])
        bucket = "none" if key == "unlinked" else "shared" if key.startswith("shared:") else "ticket"
        attrib[bucket] += n
        if bucket == "ticket" and n:
            top[key] = n
    items.append({"id": "stats", "kind": "stats", "label": "Stats", "role": "neu", "text": "", "built": now,
                  "files": len(found), "daily": daily, "cost_days": cost_days, "attrib": attrib,
                  "top": sorted(top.items(), key=lambda kv: -kv[1])[:8],
                  "history": limit_history(log, horizon)})
    return items


class UsageProvider:
    id = "usage"
    kind = "status"
    interval_s = 300

    def scopes(self, ctx):
        return ["workspace"]

    def fetch(self, ctx, scope, previous):
        try:
            tickets = []
            for e in ctx.addon.tickets():
                meta = e.meta if isinstance(e.meta, dict) else {}
                sids = [s["id"] for s in meta.get("sessions") or [] if isinstance(s, dict) and isinstance(s.get("id"), str)]
                tickets.append((e.id, str(meta.get("title") or ""), sids))
            log = ctx.settings.get("limits_log") or DEFAULT_LOG
            open_cache(ctx.addon.state_dir / "parse-cache.json")
            begin_fetch()
            try:
                items = tuple(build(claude_dir(), tickets, log, time.time()))
            finally:
                save_cache()
            return Snapshot(self.id, scope, ctx.now(), items=items)
        except Exception as e:  # the transcript format is Claude Code's own and may change: never raise into a page
            return Snapshot(self.id, scope, ctx.now(), health="error", message=f"could not read Claude's files ({type(e).__name__})")


# -- widgets: read the snapshot only --------------------------------------------------------------------------------

def tokens(n) -> str:
    n = int(n or 0)
    return f"{n / 1e6:.1f}M" if n >= 1_000_000 else f"{n / 1e3:.1f}k" if n >= 1000 else str(n)


def model_name(model: str) -> str:
    """claude-haiku-4-5-20251001 or haiku-4-5 -> Haiku 4.5, sonnet-5 -> Sonnet 5; anything else stays as it is."""
    m = re.fullmatch(r"(?:claude-)?([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?", model)
    return model if not m else f"{m[1].capitalize()} {m[2]}" + (f".{m[3]}" if m[3] else "")


def models_text(by_model: dict) -> str:
    return ", ".join(f"{model_name(m)} {tokens(n)}" for m, n in sorted(by_model.items(), key=lambda x: -x[1])) or "none"


def total(*dicts) -> int:
    return sum(sum(d.values()) for d in dicts)


def iso(epoch) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def pct_text(p, key: str) -> str:
    v = (p or {}).get(key)
    return UNKNOWN_PCT if v is None else f"about {v:.0f} % (estimate)" if v >= 1 else f"under 1 % (estimate)"


def cost_text(item, show: bool):
    c = item.get("cost")
    if item.get("kind") == "ticket" and not c and item.get("shared") and not total(item["main"]):
        return "—, shared session"
    if c is None:
        return "—, session still running" if item.get("kind") != "limits" else "—"
    return f"${c['total']:.2f}"


def duration(ms) -> str:
    m = int(ms or 0) // 60000
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def ticket_panel(item, show_cost: bool) -> list:
    rows = []
    if show_cost:
        rows.append(("Estimate at API list prices", cost_text(item, True)))
        c = item.get("cost")
        for m, usd in sorted((c or {}).get("models", {}).items(), key=lambda x: -x[1]):
            rows.append((f"  {model_name(m)}", f"${usd:.2f}"))
    rows.append(("Share of weekly limit", pct_text(item.get("pct"), "all")))
    c = item.get("cost")
    if c:
        rows.append(("Working time", duration(c["ms"])))
        rows.append(("Lines", f"+{c['added']:,} / -{c['removed']:,}"))
    if item.get("first") is not None:
        rows.append(("First message", Time(iso(item["first"]), "at")))
        rows.append(("Last message", Time(iso(item["last"]), "at")))
    body = [KV(tuple(rows))]
    by_model = sorted(set(item["main"]) | set(item["sub"]), key=lambda m: -(item["main"].get(m, 0) + item["sub"].get(m, 0)))
    if by_model:
        body.append(Table(("Model", "Main", "Subagents"), tuple(
            (model_name(m), tokens(item["main"].get(m, 0)) if m in item["main"] else "—",
             tokens(item["sub"].get(m, 0)) if m in item["sub"] else "—") for m in by_model),
            empty="No output tokens found for this ticket."))
    for s in item.get("shared") or []:
        body.append(Text(f"Shared orchestrator: {s['tickets']} tickets, {tokens(total(s['out']))} output tokens "
                         f"({models_text(s['out'])}). Only subagents that name this ticket count above."))
    for sid in item.get("missing") or []:
        body.append(Text(f"Session {sid[:8]}: not on this machine."))
    if not total(item["main"], item["sub"]) and not item.get("missing") and not item.get("shared"):
        body.append(Text("No output tokens found yet."))
    return [Card("Usage", tuple(body))]


RANGES = (("week", "Last 7 days"), ("month", "Last 30 days"), ("all", "Calendar weeks"))
PAGE_URL = "/addons/ticket-usage/"
SETUP_CMD = "orch addon setup ticket-usage"
RECORDER = (f"To install the recorder, run `{SETUP_CMD}` in your own terminal: it asks before it adds a status line to "
            "your user-global Claude settings (see this addon's README). Tokens and cost below do not need it.")


def _k(n) -> str:
    n = float(n)
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e4 else f"{n / 1e3:.1f}k" if n >= 1e3 else f"{n:.0f}"


def _d(day: str) -> str:
    return date.fromisoformat(day).strftime("%d %b")


def _wk(monday: str) -> str:
    return f"W{date.fromisoformat(monday).isocalendar()[1]} · {_d(monday)}"


def _limit_card(title, v, reset, now, pace_text):
    if not isinstance(v, (int, float)):
        return Card(title, (Text("unknown"),))
    past = isinstance(reset, (int, float)) and reset <= now
    body = [KV((("Used", "0 %, reset" if past else f"{v:.0f} %"),), layout="stats")]  # the card's own title says which
    if isinstance(reset, (int, float)):
        body.append(KV((("Resets" if not past else "Reset", Time(iso(reset), "at")),)))
    if pace_text and not past:
        body.append(Text(pace_text))
    return Card(title, tuple(body), role=None if past else _role(v))


def _pace_text(p, key, reset, now):
    """One sentence from the readings of the newest window. Five hours: needs two points. Week: a day of data."""
    def too_early(span):
        have = f"{span / 3600:.0f} h" if span >= 3600 else f"{max(round(span / 60), 0)} min"
        return f"Weekly pace needs a day of readings; there are {have} so far."

    if not _rising(p):
        if key == "week":
            return too_early(p["last_ts"] - p["first_ts"] if p else 0)
        return None
    span = p["last_ts"] - p["first_ts"]
    if key == "week" and span < 86400:
        return too_early(span)
    if p["last"] >= 100:
        return "The limit is reached; it frees up at the reset."
    eta = _eta(p, key)
    if eta is None:  # a tiny rise says nothing about when it fills
        return f"Up {p['last'] - p['first']:.0f} points in the newest window; too slow to say when it fills."
    dur = f"{span / 3600:.1f} h" if span >= 7200 else f"{round(span / 60)} min"
    if isinstance(reset, (int, float)) and eta > reset:  # the window resets first: "100 % after the reset" says nothing
        return f"Up {p['last'] - p['first']:.0f} points in {dur}. At that pace it will not fill before the reset."
    return (f"Up {p['last'] - p['first']:.0f} points in {dur}. At that pace it would reach 100 % around "
            f"{_clock(eta)}.")


def _rising(p) -> bool:
    return bool(p) and p["n"] >= 2 and p["last_ts"] > p["first_ts"] and p["last"] > p["first"]


def _eta(p, key):
    """When the newest window reaches 100 % at its pace (the pace sentence's projection), or None when it cannot say."""
    if not _rising(p) or (key == "week" and p["last_ts"] - p["first_ts"] < 86400):
        return None
    if p["last"] >= 100:
        return p["last_ts"]
    eta = p["last_ts"] + (100 - p["last"]) / ((p["last"] - p["first"]) / (p["last_ts"] - p["first_ts"]))
    return eta if eta < p["last_ts"] + 366 * 86400 else None


def _days(daily: dict, rng: str, today: str) -> tuple[list, list, str, str]:
    """(labels, rows of 5 family totals, first day, last day) of a range; calendar days with no output are zeros."""
    end = date.fromisoformat(today)
    if rng == "all":
        first = date.fromisoformat(min(daily)) if daily else end
        start = first - timedelta(days=first.weekday())
    else:
        start = end - timedelta(days=6 if rng == "week" else 29)
    days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    cols = len(FAMILIES)
    if rng != "all":
        return [_d(x) for x in days], [list(daily.get(x, [0] * cols)) for x in days], days[0], days[-1]
    weeks: dict = {}
    for x in days:
        w = weeks.setdefault(monday_of(x), [0] * cols)
        for i, n in enumerate(daily.get(x, [0] * cols)):
            w[i] += n
    first_day = min(daily) if daily else days[0]
    labels = [_wk(m) + (f" (from {date.fromisoformat(first_day).strftime('%a')})" if m == monday_of(first_day) and first_day != m else "")
              for m in weeks]
    return labels, list(weeks.values()), first_day, days[-1]


def _usage_charts(stats: dict, params: dict, show_cost: bool, now: float) -> list:
    daily = stats.get("daily") or {}
    out: list = []
    today = day_of(stats.get("built") or now)
    if len(daily) <= 1:  # one day of data: numbers only, week and month tabs wait for more
        row = next(iter(daily.values()), None)
        tot = sum(row) if row else 0
        out.append(Card("Output tokens by model", (
            KV((("Output tokens", _k(tot)),), layout="stats") if tot else Text("No output yet."),
            Text("Charts by week and month appear once there is more than one day of data."))))
    else:
        rng = params.get("range") if params.get("range") in dict(RANGES) else "month"
        labels, rows, first, last = _days(daily, rng, today)
        sums = [sum(r) for r in rows]
        tot = sum(sums)
        used = [i for i in range(len(FAMILIES)) if any(r[i] for r in rows)]
        body: list = [Tabs(tuple(Link(t, f"{PAGE_URL}?range={k}", current=k == rng) for k, t in RANGES), label="Range")]
        if tot:
            busiest = max(range(len(rows)), key=lambda i: sums[i])
            body.append(KV((("Output tokens", _k(tot)), ("On Opus", f"{round(100 * sum(r[0] for r in rows) / tot)} %"),
                            (f"Busiest {'week' if rng == 'all' else 'day'}, {labels[busiest]}", _k(sums[busiest]))),
                           layout="stats"))
            body.append(Chart("", tuple(labels), tuple(
                ChartSeries(FAMILIES[i].capitalize(), tuple(r[i] for r in rows), f"series-{n + 1}")
                for n, i in enumerate(used)), stacked=True, unit="tokens"))
        else:
            body.append(Text("No output in this range."))
        body.append(Text(f"Every reply Claude Code wrote on this machine, counted once, subagents included. {_d(first)} to {_d(last)}"
                         f"{', by calendar week' if rng == 'all' else ''}."))
        out.append(Card("Output tokens by model", tuple(body)))
    a = stats.get("attrib") or {}
    wk_total = sum(a.values())
    cards = []
    if wk_total:
        pc = lambda n: round(100 * n / wk_total)  # noqa: E731
        cards.append(Card("How much of this week's output is tied to a ticket", (
            Chart("", ("No ticket claimed", "Shared runs, not split", "Tied to one ticket"),
                  (ChartSeries("Output tokens", (a.get("none", 0), a.get("shared", 0), a.get("ticket", 0)), "series-1"),),
                  horizontal=True, unit="tokens"),
            Text(f"{pc(a.get('ticket', 0))} % can be tied to a single ticket. {pc(a.get('none', 0))} % comes from sessions that claimed "
                 "no ticket. That can still be ticket work done without a claim, so this measures attribution, not what the time was spent on."))))
    else:
        cards.append(Card("How much of this week's output is tied to a ticket", (Text("No Claude output found this week."),)))
    top = [(t, n) for t, n in (stats.get("top") or []) if n]
    cards.append(Card("Output tied to each ticket, this week", (
        Chart("", tuple(t for t, _ in top), (ChartSeries("Output tokens", tuple(n for _, n in top), "series-1"),),
              horizontal=True, unit="tokens") if top else Text("No ticket has output this week."),
        Text("Its own sessions plus subagents that name it. Output tokens, not effort or value."))))
    out += cards
    hist = stats.get("history") or []
    if len(hist) >= 2:
        t0 = hist[0][0]
        xs = tuple(round(h[0]) for h in hist)
        lim = Card("Limits history", (
            Chart("", xs, (ChartSeries("Week", tuple(h[2] for h in hist), "series-1"),
                                            ChartSeries("5-hour window", tuple(h[1] for h in hist), "series-2")),
                  style="line", x="time", unit="%"),
            Text(f"Status line readings since {_clock(t0)}, spaced by real time. Only new highs within a window are drawn.")))
    else:
        lim = Card("Limits history", (Text("Not enough status line readings yet. This fills as you work."),))
    cost = []
    if show_cost:
        weeks: dict = {}
        for day, usd in (stats.get("cost_days") or {}).items():
            weeks[monday_of(day)] = weeks.get(monday_of(day), 0.0) + usd
        if weeks:
            first = min(stats["cost_days"])
            note = ("Claude Code's own estimate at API list prices, written when a session ends: a long session lands in the week it "
                    f"ended. Not what your plan bills.{'' if monday_of(first) == first else f' Records start {_d(first)}, so the first week is partial.'}")
            cost = [Card("API-price equivalent per calendar week", (
                Chart("", tuple(_wk(m) for m in sorted(weeks)),
                      (ChartSeries("USD", tuple(round(weeks[m], 2) for m in sorted(weeks)), "series-1"),), unit="USD"),
                Text(note)))]
    return out + [lim] + cost


def page(snaps, show_cost: bool, params: dict | None = None, now: float | None = None) -> list:
    items = list(snaps[0].items) if snaps else []
    now = now if now is not None else time.time()
    if not items:
        return [Text("Reading transcripts… Usage shows up after the first fetch; press Refresh.")]
    limits = next((i for i in items if i["kind"] == "limits"), {})
    stats = next((i for i in items if i["kind"] == "stats"), None)
    last = limits.get("last")
    out: list = []
    log = limits.get("log") or {}
    if not last and log.get("state") == "missing" and log.get("path") == str(Path(DEFAULT_LOG).expanduser()):
        out.append(_recorder_card(limits.get("recorder") is True))
    elif not last and log.get("state") in ("missing", "relative"):  # TU-01: a configured file is gone or unusable
        out.append(Callout("warn", "Limits log not found", (
            f"File not found: {log.get('path')}. Change \"Limits log\" in this addon's settings on Workspace & addons."
            if log["state"] == "missing" else
            f"{log.get('path')} is a relative path, which depends on where Mission Control was started. Use an absolute "
            "path or one starting with ~/.") + " If the recorder is not installed yet: " + RECORDER))
    elif not last:
        out.append(Callout("neu", "No limits recorded", "Limits come from the status line. " + RECORDER))
    else:
        pc = limits.get("pace") or {}
        out.append(Card("Limits", (
            _limit_card("5-hour window", last.get("five"), last.get("five_reset"), now,
                        _pace_text(pc.get("five"), "five", last.get("five_reset"), now)),
            _limit_card("Week", last.get("week"), last.get("week_reset"), now,
                        _pace_text(pc.get("week"), "week", last.get("week_reset"), now)),
        ), layout="grid"))
        if last.get("at"):
            out.append(Text(f"Limits as of {_clock(to_epoch(last['at']))}."))
    if stats:
        out += _usage_charts(stats, params or {}, show_cost, now)
    rows = []
    order = {"ticket": 0, "shared": 1, "unlinked": 2}  # tickets first, then shared orchestrators, unlinked last
    for i in sorted(items, key=lambda i: (order.get(i["kind"], 3), -total(i.get("week") or {}))):
        if i["kind"] in ("limits", "stats") or not total(i["week"]):
            continue
        week = (i.get("pct") or {}).get("week")
        label = i["label"] if i["kind"] != "shared" else f"Shared orchestrator ({len(i['tickets'])} tickets)"
        if i["kind"] == "unlinked":
            label = "Not linked to a ticket"
        cells = [label, ", ".join(model_name(m) for m in i["week"]), tokens(total(i["week"]))]
        if show_cost:  # TU-03: no Estimate column of dashes while dollar figures are off
            cells.append(cost_text(i, True) if i["kind"] != "unlinked" else "—")
        cells.append("—" if week is None else f"about {week:.0f} %" if week >= 1 else "under 1 %")
        rows.append(tuple(cells))
    heads = ("Ticket", "Models", "Output this week") + (("Estimate",) if show_cost else ()) + ("Week share",)
    out.append(Card("Details", (Table(heads, tuple(rows),
                     empty="No Claude output found this week. Sessions on this machine show up here once they run."),)))
    return out


def _recorder_card(wired: bool):
    """No limits log at the default path: the recorder is not set up yet (or has not written yet). Info, not an error."""
    if wired:
        return Callout("info", "No limits recorded yet", "Your status line runs the recorder; limits appear after Claude "
                                                         "Code's next reply. Tokens and cost below do not need it.")
    return Card("Recorder not installed", (
        Text("The limit cards come from Claude Code's status line, through a small recorder script. Run this in your own "
             "terminal: it copies the script and, after you confirm, adds a status line to your user-global Claude "
             "settings (every Claude Code session on this machine). An existing status line is never replaced; you get "
             "the one line to add to it instead. Tokens and cost below do not need it."),
        Copy("Setup command", SETUP_CMD)), role="info")


def _clock(epoch) -> str:
    d, now = datetime.fromtimestamp(epoch), datetime.now()
    return d.strftime("%H:%M") if d.date() == now.date() else d.strftime("%d.%m. %H:%M")


def _role(v) -> str:
    return "ok" if v < 70 else "warn" if v < 90 else "err"


DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
STALE_MINUTES = 30
SEVERITY = {"neu": 0, "ok": 1, "warn": 2, "err": 3}


def _day_clock(epoch) -> str:
    d = datetime.fromtimestamp(epoch)
    return f"{DAYS[d.weekday()]} {d:%H:%M}"


def menu_chip(snaps, now: float, stale_minutes: float = STALE_MINUTES):
    """The menu entry: a labelled row per limit (meter, percent, reset), coloured by level or, when the pace sentence
    sees it fill before its reset, at least warn; the chip names the riskier limit. None without data."""
    limits = next((i for s in snaps[:1] for i in s.items if i["kind"] == "limits"), {})
    last = limits.get("last")
    if not last:
        return None
    pc = limits.get("pace") or {}
    parts, rows, risks = [], [], []
    for name, label, k in (("5-hour", "5h", "five"), ("week", "Week", "week")):
        v, reset = last.get(k), last.get(k + "_reset")
        has_reset = isinstance(reset, (int, float))
        if not isinstance(v, (int, float)):
            parts.append(f"{name} unknown")
            rows.append(MenuRow(label, "unknown", muted=True))
            continue
        past = has_reset and reset <= now  # that window has reset since the last record
        parts.append(f"{name} {0 if past else v:.0f} %" + (" · reset" if past else f" · resets {_clock(reset)}" if has_reset else ""))
        if past:
            rows.append(MenuRow(label, "reset", muted=True))
            risks.append(((0, 0), Badge("neu", f"{label} reset")))
            continue
        eta = _eta(pc.get(k), k)
        role = _role(v)
        if role == "ok" and has_reset and eta is not None and eta <= reset:  # ahead of pace: fills before the reset
            role = "warn"
        note = ()
        if has_reset:
            note = ((Countdown(iso(reset), prefix="resets in "),) if k == "five" else (Text(f"resets {_day_clock(reset)}"),))
        rows.append(MenuRow(label, f"{v:.0f} %", meter=v, role=role, note=note))
        risks.append(((SEVERITY[role], v), Badge(role, f"{label} {v:.0f} %")))
    if not risks:
        return None
    at = to_epoch(last.get("at")) if last.get("at") else None
    stale = at is not None and now - at > stale_minutes * 60
    title = " · ".join(parts) + (f" · as of {_clock(at)}" if stale else "")
    top = max(risks, key=lambda r: r[0])[1]
    return MenuStatus(Badge(top.role, top.text, title=title), line=(Text(f"as of {_clock(at)}"),) if stale else (),
                      rows=tuple(rows), stale=stale)


def check_settings(values: dict) -> tuple[list[str], list[str]]:
    """(errors, notes) for a settings save (called by Workspace & addons): a relative Limits log is refused, a file
    that is not there yet is saved with a note (the recorder creates it)."""
    state = limits_log_state(str(values.get("limits_log") or DEFAULT_LOG))
    if state["state"] == "relative":
        return [f"Limits log: {state['path']} is a relative path; use an absolute path or one starting with ~/"], []
    if state["state"] == "missing":
        return [], [f"File not found: {state['path']}. The Usage page shows no limits until it exists"]
    return [], []


class TicketUsage:
    def __init__(self, ctx):
        self.page = f"page.{ctx.name}"
        self.providers = [UsageProvider()]

    def widgets(self, slot, view):
        try:
            return self._widgets(slot, view)
        except Exception:  # a snapshot from another version of this addon, or a shape we do not know: never raise
            return [Text("Usage could not be read. Press Refresh.")] if slot in (self.page, "ticket.code") else []

    def check_settings(self, values):
        return check_settings(values)

    def menu_badge(self, view):
        try:
            stale = float(view.settings.get("stale_after") or STALE_MINUTES)
        except (TypeError, ValueError):
            stale = STALE_MINUTES
        return menu_chip(view.snapshots("usage"), time.time(), stale)

    def _widgets(self, slot, view):
        snaps = view.snapshots("usage")
        show = view.settings.get("show_cost") is not False
        if slot == self.page:
            return page(snaps, show, view.params)
        if slot == "ticket.code" and view.ticket is not None and snaps:
            item = next((i for i in snaps[0].items if i["id"] == f"ticket:{view.ticket.id}"), None)
            return ticket_panel(item, show) if item else []
        return []


def create(ctx):
    return TicketUsage(ctx)
