"""AI Factory on the dashboard (#2, phase 2): what the permission cards, the epic's factory status and the Board
group show. Reads only; every answer goes through orch.core.permits (human only, signed). Off unless
`factory.enabled`: every function answers "nothing" then. Agent text (a command, a reason) is shown escaped."""
from __future__ import annotations

from datetime import timedelta

from orch import clock
from orch.core import epics, factory_report, permits, store


def _raw(text) -> str:
    """Text for a card: only characters outside printable ASCII (and newlines) are escaped, so a grantable command
    (single line, printable ASCII) reads exactly as the raw text the grant binds, quotes and backslashes included."""
    return "".join(c if 32 <= ord(c) < 127 else c.encode("unicode_escape").decode("ascii") for c in str(text))


def _card(r: dict, profile: bool = False) -> dict:
    return {"id": r["id"], "epic": r["epic"], "ticket": r["ticket"], "sha": r["sha"], "short": r["sha"][7:15],
            "command": _raw(r["command"]), "reason": _raw(r["reason"]),
            "asked_by": _raw(r["actor"]), "source": _raw(r["source"]), "at": r["at"], "profile": profile}


def _dark_epics(ws, reqs) -> set[str]:
    """Upper-case ids of the epics of Dark cards that are active Dark epics now (Dark on): only their cards offer "Add
    to the Dark profile" (dark_profile.add_from_request refuses the others)."""
    out = set()
    if not permits.dark_on(ws):
        return out
    for eid in {str(r["epic"]) for r in reqs if r["source"] == "dark"}:
        try:
            if permits.dark_delegation(ws, store.read_ticket(store.resolve(ws, eid).path)) is not None:
                out.add(eid.upper())
        except Exception:
            continue
    return out


def permit_view(ws, epic_id: str | None = None) -> dict | None:
    """{requests: open permission cards, grants: live standing grants for an epic, budget: used-up-budget cards}, all
    for `epic_id` when given. None while the factory is off."""
    if not permits.enabled(ws):
        return None
    keep = (lambda e: e.upper() == epic_id.upper()) if epic_id else (lambda e: True)
    raw = [r for r in permits.open_requests(ws) if keep(str(r["epic"]))]
    dark = _dark_epics(ws, raw)
    reqs = [_card(r, r["source"] == "dark" and str(r["epic"]).upper() in dark) for r in raw]
    grants = [{"grant": g["grant"], "epic": g["epic"], "scope": g["scope"], "command": _raw(g["command"])}
              for g in permits.grants(ws) if g["live"] and keep(str(g["epic"]))]
    report = factory_report.cards(ws)
    ready = [r for r in report["ready"] if keep(r["epic"])]
    stopped = [_stopped_card(s) for s in report["stopped"] if keep(s["epic"])]
    # a Stopped card says the budget is used up itself: one card for it, not two
    suspect = [x for x in report["suspect"] if keep(x["epic"])]
    cards = [c for c in permits.budget_cards(ws) if keep(str(c["epic"])) and not any(s["epic"] == c["epic"] for s in stopped)]
    return {"requests": reqs, "grants": grants, "budget": cards, "ready": ready, "stopped": stopped, "suspect": suspect,
            "cards_n": len(reqs) + len(cards) + len(ready) + len(stopped) + len(suspect), "any": bool(reqs or grants or cards or ready or stopped or suspect)}


_CAN = {  # what the human can do, per reason (rule text, never agent prose)
    "budget": "Approve the epic again (Start as an AI Factory) for a new budget, or give verdicts on what is in testing.",
    "sent-back": "Open the child and say what is missing in its requirements, or take it out of the epic.",
    "denied": "Open the child: change its text so it does without that command, or take it out of the epic.",
    "ledger-cut": "Run `orch check` on this machine: nothing the factory did counts until the ledger is whole again.",
}


def _stopped_card(s: dict) -> dict:
    return {**s, "reasons": [{**r, "can": _CAN[r["code"]]} for r in s["reasons"]]}


def epic_status(ws, epic, d: dict | None, events) -> dict | None:
    """The factory chip of an epic page: {enabled, factory, state, children, max_children, hours_left, max_hours}.
    `factory` is whether the signed charter is a factory one; None-free only while the switch is on."""
    if not permits.enabled(ws):
        return None
    limits = {"max_children": epics.FACTORY_DEFAULTS["max_children"], "max_size": epics.FACTORY_DEFAULTS["max_size"],
              "max_hours": epics.FACTORY_DEFAULTS["max_hours"]}
    dark_on = permits.dark_on(ws)  # the epic page offers a Dark start only then (Ops refuses it otherwise too)
    if not d or not d.get("factory"):
        return {"factory": False, "limits": limits, "dark_on": dark_on}
    used = epics.delegated_count(ws, epic.id, d["id"], events)
    left = None
    try:
        end = clock.parse_stamp(str(d["at"])) + timedelta(hours=d["max_hours"])
        left = max(0.0, (end - clock.now()).total_seconds() / 3600)
    except (ValueError, TypeError):
        left = 0.0  # no readable time: the budget counts as used up
    if d["paused"]:
        state = "paused"
    elif d["epic_changed"]:
        state = "suspended"
    elif d.get("expired") or used >= d["max_children"]:
        state = "budget used up"
    else:
        state = "running"
    from orch.core import factory_runner, factory_sessions
    blocker = (factory_runner.user_settings_blocker() if factory_sessions.armed(ws, d["id"]) and state == "running"
               else None)
    return {"factory": True, "limits": limits, "dark_on": dark_on, "dark": bool(d.get("dark")), "state": state, "runner_blocker": blocker, "children": used, "max_children": d["max_children"],
            "hours_left": left, "max_hours": d["max_hours"]}


# -- the run view and the factory list (phase 5, dashboard) --------------------------------------------------------
# Five steps, each lit only from records orch already keeps: no step claims a test, review, merge or release, because
# no record of those exists yet.

STEPS = ("Understand", "Plan", "Build", "Evidence", "Done")
_PLANNED = ("covered", "delegated", "approved", "done")  # epics.child_state


def _arc(i: int) -> str:
    """The SVG path of ring segment `i` (the 18 of every 20 units its circle draws), for the moving dash."""
    import math
    a0, a1 = math.radians(-90 + 72 * i), math.radians(-90 + 72 * i + 64.8)
    return (f"M {60 + 52 * math.cos(a0):.2f} {60 + 52 * math.sin(a0):.2f} "
            f"A 52 52 0 0 1 {60 + 52 * math.cos(a1):.2f} {60 + 52 * math.sin(a1):.2f}")


STEP_ARCS = tuple(_arc(i) for i in range(5))
# state: (chip role, list rank: needs you first, then working, then the rest, finished last, chip words). The chip
# says the state in a word or two; the headline says it once, in a sentence.
_STATES = {"waiting": ("you", 0, "Needs you"), "stopped": ("warn", 0, "Stopped"), "budget": ("warn", 0, "Budget used up"),
           "working": ("info", 1, "Working"), "planning": ("info", 1, "Working"), "paused": ("neu", 2, "Paused"), "changed": ("warn", 2, "Edited, start again"),
           "blocked": ("warn", 2, "Blocked"), "unarmed": ("neu", 2, "Not running"), "nokids": ("neu", 2, "No children"),
           "idle": ("neu", 2, "Idle"), "finished": ("ok", 3, "Finished")}
NEEDS_YOU = ("waiting", "stopped", "budget")


def span(seconds) -> str:
    """A duration in plain words ("3 hours 12 minutes")."""
    m = int(max(0.0, seconds) // 60)
    if m < 1:
        return "less than a minute"
    d, h, mm = m // 1440, m % 1440 // 60, m % 60

    def n(x, w):
        return f"{x} {w}{'' if x == 1 else 's'}"
    if d:
        return n(d, "day") + (f" {n(h, 'hour')}" if h else "")
    return n(h, "hour") + (f" {n(mm, 'minute')}" if mm else "") if h else n(mm, "minute")


def _tasks(t) -> tuple[int, bool]:
    """(tasks done, every task closed and there is at least one); an unreadable task list is not done."""
    from orch.core import tasks
    try:
        s = tasks.summary(tasks.ticket_tasks(t))
    except Exception:
        return 0, False
    return s["done"], bool(s["total"]) and s["closed"] == s["total"]


def _steps(ws, epic, d, kids, signed, events, entries) -> int:
    """How many of the five steps the records back, in order (a step counts only once those before it do). `kids`:
    None when a child cannot be read (nothing past the first step is claimed then)."""
    if factory_report.finished(ws, epic, signed, events) == "done":
        return 5
    if d["epic_changed"] or not kids:
        return 0
    # a status word in a ticket file counts only with the record behind it (factory_report.finished)
    finished = {t.id: factory_report.finished(ws, t, signed, events) for _, t in kids}
    if any(epics.child_state(ws, epic, t, signed, events, entries) not in _PLANNED
           or (t.status == "done" and finished[t.id] != "done") for _, t in kids):
        return 1
    if any(finished[t.id] is None and not _tasks(t)[1] for _, t in kids):
        return 2
    if factory_report.ready(ws, epic, entries=entries, signed=signed, events=events) is None:
        return 3
    return 4


def _at(stamp):
    try:
        return clock.parse_stamp(str(stamp))
    except (ValueError, TypeError):
        return None


def run_status(ws, epic, d, view, *, signed, events, entries, blocker=None, bound=()) -> dict:
    """One factory epic as the run view and the list show it, from records only: {epic, title, dark, look_dark, state,
    role, rank, chip, headline, steps, current, step, hot, live, elapsed, ...}. `view` is permit_view(ws) (all epics),
    `blocker` factory_runner.user_settings_blocker() read once (it counts only for an armed epic), `bound` the live runner bindings of this workspace."""
    from orch.core import factory_runner, factory_sessions
    kids = factory_report._kids(ws, epic, entries)
    n = _steps(ws, epic, d, kids, signed, events, entries)
    kids = kids or []
    eid = epic.id.upper()
    mine = {k: [x for x in view[k] if str(x["epic"]).upper() == eid]
            for k in ("requests", "ready", "stopped", "budget", "suspect")}
    dark = bool(d.get("dark"))
    look_dark = dark and permits.dark_on(ws)  # a Dark charter with the switch off runs (and looks) as an AI Factory
    name = "Dark AI Factory" if look_dark else "AI Factory"
    running = [b for b in bound if b["delegation"] == d["id"] and str(b["epic"]).upper() == eid]
    if n == 5:
        state, headline = "finished", "Finished: you gave the verdict"
    elif d["paused"]:
        state, headline = "paused", "Paused: you stopped the run"
    elif mine["stopped"]:
        only_budget = all(r["code"] == "budget" for s in mine["stopped"] for r in s["reasons"])
        state, headline = (("budget", "The budget is used up: agents stopped on this epic") if only_budget
                           else ("stopped", "Stopped: the agents cannot go on by themselves"))
    elif d["epic_changed"]:
        state, headline = "changed", "Suspended: the epic's text changed since you started it"
    elif mine["requests"] or mine["ready"] or mine["budget"]:
        state, headline = "waiting", "Your answer is needed on the cards below"
    elif not factory_sessions.armed(ws, d["id"]):
        state, headline = "unarmed", ("Not running: the dashboard's start did not arm it (for example, it was approved "
                                      "in a terminal)")
    elif blocker:
        state, headline = "blocked", "The runner starts nothing"
    elif not kids and any(factory_sessions.is_planner(b) for b in running):
        state, headline = "planning", "A planner session is splitting the epic into children."
    elif not kids:
        state, headline = "nokids", "Waiting for children"
    elif running or any(factory_runner._launchable(ws, epic, d, t, signed) for _, t in kids):
        state, headline = "working", f"{name} is working"
    else:
        state, headline = "idle", "Nothing is running: no child can start now"
    role, rank, chip = _STATES[state]
    start = _at(d.get("at"))
    end = clock.now()
    if state == "finished":
        end = max((_at(e.at) for e in events if str(e.ticket).upper() == eid and e.kind == "verdict.given"
                   and _at(e.at)), default=end)
    # the ring: done = solid thin, the current step thick (now), dashed (waiting for you) or amber (stopped)
    here = {"working": "now", "planning": "now", "waiting": "wait", "unarmed": "todo", "nokids": "todo",
            "idle": "todo"}.get(state, "stop")
    marks = ["done" if i < n else here if i == n else "todo" for i in range(len(STEPS))]
    current = min(n, len(STEPS) - 1)
    live = state in ("working", "planning")  # motion and glow only while it really works
    built = bool(running) or any(_tasks(t)[0] for _, t in kids)
    # why no child is there yet (the planner's own launch markers, never ticket text)
    planner = None
    if state == "nokids":
        used = factory_sessions.planner_runs(ws, d["id"])
        planner = ("spent" if used >= factory_sessions.PLANNER_LAUNCHES else "parked" if used else "next")
    return {"epic": epic.id, "title": epic.title, "dark": dark, "look_dark": look_dark, "name": name,
            "state": state, "role": role, "rank": rank, "chip": chip, "headline": headline, "blocker": blocker,
            "steps": n, "current": current, "step": STEPS[current], "live": live, "arc": STEP_ARCS[current],
            "hot": look_dark and live and built, "marks": marks,
            "elapsed": span((end - start).total_seconds()) if start else None,
            "active": bool(d["active"]) and epic.status != "done", "kids": kids, "mine": mine, "planner": planner}


_LOG_PHRASE = {"permit.requested": "asked for a permission", "permit.granted": "granted a permission",
               "permit.denied": "denied a permission", "permit.revoked": "revoked a grant",
               "permit.used": "used a grant", "gate.delegated": "auto-approved it under your charter"}


def _epic_events(events, ids) -> list[dict]:
    """The read-only log in plain words: time, ticket, who (you or an agent) and a fixed phrase per event kind
    (timeline.action_phrase). Event data (command text, hashes, grant ids, notes) and agent session ids never reach
    the page."""
    from orch.dashboard.data.timeline import action_phrase
    keep = {i.upper() for i in ids}
    rows = []
    for e in events:
        if not e.ticket or str(e.ticket).upper() not in keep:
            continue
        who = "You" if str(e.actor).startswith("human") else "An agent"
        rows.append({"at": e.at, "ticket": e.ticket, "text": f"{who} {_LOG_PHRASE.get(e.kind) or action_phrase(e)}"})
    return rows[::-1][:200]


def _bound(ws) -> list[dict]:
    from orch.core import factory_sessions
    try:
        return factory_sessions.bindings(ws)
    except Exception:
        return []


def _blocker():
    from orch.core import factory_runner
    return factory_runner.user_settings_blocker()


def run_view(ws, epic) -> dict | None:
    """The run view of factory epic `epic`, or None when it is not one (or the factory is off)."""
    if not permits.enabled(ws):
        return None
    from orch.core import dark_profile, ledger
    from orch.core.events import read_events
    signed, events, entries = ledger.entries(ws), read_events(ws), store.scan(ws)
    d = permits.factory_delegation(ws, epic, signed)
    if d is None:
        return None
    view = permit_view(ws)
    r = run_status(ws, epic, d, view, signed=signed, events=events, entries=entries,
                   blocker=_blocker(), bound=_bound(ws))
    r["log"] = _epic_events(events, [epic.id] + [t.id for _, t in r["kids"]])
    r["profile_empty"] = r["look_dark"] and r["state"] != "finished" and not dark_profile.rules(ws, signed)
    r["permits"] = {**view, **r["mine"], "grants": [g for g in view["grants"] if str(g["epic"]).upper() == epic.id.upper()]}
    if r["state"] == "finished":
        reqs = [x for x in permits.requests(ws, events).values() if str(x["epic"]).upper() == epic.id.upper()]
        answered = permits.decisions(ws, signed)
        listed = dark_profile.rules(ws, signed)

        def added_after(x) -> bool:  # a rule signed after the card, covering exactly its command
            rule = dark_profile.match(ws, x["command"], listed)
            return rule is not None and _at(rule.get("at")) is not None and _at(x["at"]) is not None \
                and _at(rule["at"]) >= _at(x["at"])
        card = sum(1 for x in reqs if (x["id"], x["sha"]) in answered)
        profile = sum(1 for x in reqs if (x["id"], x["sha"]) not in answered and x["source"] == "dark"
                      and added_after(x))
        r["summary"] = {"children": len(r["kids"]), "tasks": sum(_tasks(t)[0] for _, t in r["kids"]),
                        "requests": len(reqs), "card": card, "profile": profile, "open": len(reqs) - card - profile}
    return r


def factory_list(ws) -> list[dict] | None:
    """Every factory epic of this workspace (AI Factory and Dark), needs-you first, then working, stopped, finished;
    None while the factory is off."""
    if not permits.enabled(ws):
        return None
    from orch.core import ledger
    from orch.core.events import read_events
    signed, events, entries = ledger.entries(ws), read_events(ws), store.scan(ws)
    view, out, bound = permit_view(ws), [], _bound(ws)
    blocker = _blocker()  # read once for the whole list
    for e in entries:
        if e.meta is None or not epics.is_epic(e.meta):
            continue
        try:
            epic = store.read_ticket(e.path)
        except Exception:
            continue
        d = permits.factory_delegation(ws, epic, signed)
        if d is not None:
            out.append(run_status(ws, epic, d, view, signed=signed, events=events, entries=entries,
                                  blocker=blocker, bound=bound))
    return sorted(out, key=lambda r: (r["rank"], r["epic"]))


def factory_epic_ids(ws, entries) -> set[str]:
    """Upper-case ids of the epics whose signed charter is a factory one (the Board's factory group); empty off."""
    if not permits.enabled(ws):
        return set()
    from orch.core import ledger
    signed, out = ledger.entries(ws), set()
    for e in entries:
        if e.meta is None or not epics.is_epic(e.meta):
            continue
        try:
            if permits.factory_delegation(ws, store.read_ticket(e.path), signed):
                out.add(e.id.upper())
        except Exception:
            continue
    return out
