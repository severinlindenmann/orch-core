"""ticket-usage: Claude Code usage per ticket (cost estimate, output tokens per model, share of the weekly limit).

A provider reads Claude's transcripts and the limits log in the background; the ticket panel and the Usage page only
read its snapshot. The transcript format is Claude Code's own: what we cannot read is shown as unknown, never as 0.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from orch.addons.api import Snapshot
from orch.addons.widgets import KV, Badge, Callout, Card, Countdown, MenuStatus, Table, Text, Time

from .data import (claude_dir, cost_of, distribute, names, parse_file, read_limits, subagents, transcripts,
                   week_rises)

WEEK_S = 7 * 86400
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
    log = read_limits(log_path)
    rises = week_rises(log)
    resets = [r["week_reset"] for r in log if isinstance(r.get("week_reset"), (int, float))]
    win = (resets[-1] - WEEK_S) if resets else now - WEEK_S
    since = min([win] + [lo for lo, _, _, _ in rises])
    found = transcripts(claude)
    owners: dict = {}
    msgs: list = []
    costs: dict = {}  # owner -> cost record summed over its sessions
    running: set = set()

    def take(owner, role, parsed):
        o = _owner(owners, owner)
        for ts, model, out in parsed["msgs"]:
            o[role][model] = o[role].get(model, 0) + out
            msgs.append((ts, owner, out))
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
                  "last": {k: last.get(k) for k in ("at", "five", "five_reset", "week", "week_reset")} if last else None})
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
            log = ctx.settings.get("limits_log") or "~/.claude/orch-usage/limits.jsonl"
            return Snapshot(self.id, scope, ctx.now(), items=tuple(build(claude_dir(), tickets, log, time.time())))
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


def page(snaps, show_cost: bool) -> list:
    items = list(snaps[0].items) if snaps else []
    if not items:
        return [Text("Usage shows up after the first fetch; press Refresh.")]
    limits = next((i for i in items if i["kind"] == "limits"), {})
    last = limits.get("last")
    out: list = []
    if not last:
        out.append(Callout("neu", "No limits recorded", "Install the status line recorder from this addon's README so the "
                           "5-hour and weekly percentages are logged. Tokens and cost below do not need it."))
    else:
        five, week = last.get("five"), last.get("week")
        out.append(KV((("5-hour limit", f"{five:.0f} %" if isinstance(five, (int, float)) else "unknown"),
                       ("Weekly limit", f"{week:.0f} %" if isinstance(week, (int, float)) else "unknown")), layout="stats"))
        times = [(label, last.get(k)) for label, k in (("5-hour limit resets", "five_reset"), ("Weekly limit resets", "week_reset"))]
        out.append(KV(tuple((label, Time(iso(v), "at")) for label, v in times if isinstance(v, (int, float)))))
    rows = []
    order = {"ticket": 0, "shared": 1, "unlinked": 2}  # tickets first, then shared orchestrators, unlinked last
    for i in sorted(items, key=lambda i: (order.get(i["kind"], 3), -total(i.get("week") or {}))):
        if i["kind"] == "limits" or not total(i["week"]):
            continue
        week = (i.get("pct") or {}).get("week")
        label = i["label"] if i["kind"] != "shared" else f"Shared orchestrator ({len(i['tickets'])} tickets)"
        if i["kind"] == "unlinked":
            label = "Not linked to a ticket"
        rows.append((label, ", ".join(model_name(m) for m in i["week"]), tokens(total(i["week"])),
                     cost_text(i, True) if show_cost and i["kind"] != "unlinked" else "—",
                     "—" if week is None else f"about {week:.0f} %" if week >= 1 else "under 1 %"))
    out.append(Table(("Ticket", "Models", "Output this week", "Estimate", "Week share"), tuple(rows),
                     empty="No Claude output found this week. Sessions on this machine show up here once they run."))
    return out


def _clock(epoch) -> str:
    d, now = datetime.fromtimestamp(epoch), datetime.now()
    return d.strftime("%H:%M") if d.date() == now.date() else d.strftime("%d.%m. %H:%M")


def _role(v) -> str:
    return "ok" if v < 70 else "warn" if v < 90 else "err"


def menu_chip(snaps, now: float):
    """The menu entry: a chip with the weekly percent and, under the label, the 5-hour percent with a live countdown to
    its reset; coloured by how little is left. None without data."""
    last = next((i for s in snaps[:1] for i in s.items if i["kind"] == "limits"), {}).get("last")
    if not last:
        return None
    parts, vals = [], {}
    for name, k in (("5-hour", "five"), ("week", "week")):
        v, reset = last.get(k), last.get(k + "_reset")
        if not isinstance(v, (int, float)):
            parts.append(f"{name} unknown")
            continue
        past = isinstance(reset, (int, float)) and reset <= now  # that window has reset since the last record
        vals[k] = (0 if past else v, past)
        parts.append(f"{name} {0 if past else v:.0f} %" + (" · reset" if past else f" · resets {_clock(reset)}" if isinstance(reset, (int, float)) else ""))
    if not vals:
        return None
    week = vals.get("week")
    badge = Badge(_role(week[0]), f"{week[0]:.0f} %", title=" · ".join(parts)) if week else None
    line = ()
    if "five" in vals:
        v, past = vals["five"]
        if past:
            line = (Text("5 h · reset"),)
        else:
            line = (Text("5 h"), Badge(_role(v), f"{v:.0f} %"))
            if isinstance(last.get("five_reset"), (int, float)):
                line += (Text("· resets"), Countdown(iso(last["five_reset"])))
    return MenuStatus(badge, line)


class TicketUsage:
    def __init__(self, ctx):
        self.page = f"page.{ctx.name}"
        self.providers = [UsageProvider()]

    def widgets(self, slot, view):
        try:
            return self._widgets(slot, view)
        except Exception:  # a snapshot from another version of this addon, or a shape we do not know: never raise
            return [Text("Usage could not be read. Press Refresh.")] if slot in (self.page, "ticket.code") else []

    def menu_badge(self, view):
        return menu_chip(view.snapshots("usage"), time.time())

    def _widgets(self, slot, view):
        snaps = view.snapshots("usage")
        show = view.settings.get("show_cost") is not False
        if slot == self.page:
            return page(snaps, show)
        if slot == "ticket.code" and view.ticket is not None and snaps:
            item = next((i for i in snaps[0].items if i["id"] == f"ticket:{view.ticket.id}"), None)
            return ticket_panel(item, show) if item else []
        return []


def create(ctx):
    return TicketUsage(ctx)
