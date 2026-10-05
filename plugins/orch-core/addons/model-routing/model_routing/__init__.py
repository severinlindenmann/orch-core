"""model-routing: start each agent session on a model chosen by its Start agent mode (issue #47).

Off until a human enables the addon in a workspace. Then, for every session Mission Control starts from Start agent
(a terminal, or Terminals), core asks this addon what to add (the `launch` capability): a model for the harness's
model argument, the subagent model as an environment variable of the launched process, and the model requested, kept
as ORCH_MODEL. Nothing here starts anything or changes a ticket. The settings are saved per user and workspace by a
human on Workspace & addons: an agent cannot change which model a session runs on.

Escalation is two things, both a human's: "Next start on the Strong model" on a ticket, and a decision card when a
task failed its verify in two separate sessions. Neither runs by itself: the card only arranges that the next start
you choose is one tier up, and that its prompt names the failing receipt.
"""
from __future__ import annotations

import os
import re

from orch.addons.api import LaunchPlan, PendingDecision, Snapshot
from orch.addons.widgets import Callout, Card, KV, Text

from .routing import (CONTINUE, FORCE_VARS, MODES, TIERS, ModelNameError, State, choose, failures_items, model_of,
                      next_tier)

ESCALATE = "escalate"
IGNORE = "ignore"
_ID = re.compile(r"escalate-([A-Z][A-Z0-9]*-\d+)-(T[1-9][0-9]*)")
_MODE_LABELS = {"refine": "Refine", "work": "Work on ticket", "fix-checks": "Fix failing checks",
                "continue": "Continue after feedback"}
TIER_LABELS = {"light": "Light", "standard": "Standard", "strong": "Strong"}


class FailuresProvider:
    """Tasks whose verify failed in two or more separate sessions, from the tickets' receipts. Cheap: it only looks
    at tickets that carry a failing receipt, and reads a task list for those."""
    id = "failures"
    kind = "status"
    interval_s = 30

    def __init__(self, ctx):
        self.ctx = ctx

    def scopes(self, ctx):
        return ["workspace"]

    def fetch(self, ctx, scope, previous):
        items = failures_items(ctx.addon)
        return Snapshot(self.id, scope, ctx.now(), items=tuple(items))


class ModelRouting:
    def __init__(self, ctx):
        self.ctx = ctx
        self.providers = [FailuresProvider(ctx)]

    # -- launch ---------------------------------------------------------------------------------------------------

    def launch(self, req, ctx):
        settings = ctx.settings
        if req.harness != "claude":
            return LaunchPlan(reason=f"Model routing sets Claude Code models only: {req.harness} starts on its own "
                                     "default")
        state = State(ctx.state_dir)
        c = choose(settings, req.mode, strong_next=ctx.ticket_option(req.ticket, "strong_next"),
                   escalation=state.escalation(req.ticket), last_tier=state.last_tier(req.ticket))
        model = c.model
        sub = _clean(settings.get("subagent_model"), "Subagent model")
        env = {}
        if model:
            env["ORCH_MODEL"] = model
            env["ORCH_MODEL_TIER"] = c.tier
        if sub:
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = sub
        reason = c.reason + (f"; subagents on {sub}" if sub else "")
        warns = [f"{v} is set in this environment: it silently changes what a model name means" for v in FORCE_VARS
                 if os.environ.get(v)]
        note = c.note
        label = f"{model} ({TIER_LABELS[c.tier]})" if model and c.tier else ""
        return LaunchPlan(model=model, env=env, note=note, label=label, reason=reason, warnings=warns)

    def launched(self, req, routing, ctx, session):
        state = State(ctx.state_dir)
        env = dict(routing.env)
        tier = env.get("ORCH_MODEL_TIER")
        if tier in TIERS:
            state.record_start(req.ticket, tier, req.mode, session)
        if "ORCH_MODEL" in env:
            # one-shot: the human's "next start on Strong" and an accepted escalation apply to this start only
            state.consume_escalation(req.ticket)
            if ctx.ticket_option(req.ticket, "strong_next"):
                try:
                    ctx.ops().relay_ticket_option(req.ticket, "strong_next", False)
                except Exception:
                    pass

    # -- the Workspace page ----------------------------------------------------------------------------------------

    def widgets(self, slot, view):
        if slot != "workspace.settings":
            return []
        s = view.settings
        rows = []
        for mode in MODES:
            setting = s.get(f"mode_{mode.replace('-', '_')}") or "none"
            tier = None if setting in ("none", "same") else setting
            if setting == "same":
                shown = "Same as the last start on the ticket"
            elif tier is None:
                shown = "Not routed: the harness default"
            else:
                try:
                    m = model_of(s, tier)
                except ModelNameError as e:
                    m = None
                    shown = f"{TIER_LABELS[tier]}: {e}"
                else:
                    shown = f"{TIER_LABELS[tier]}: {m}" if m else f"{TIER_LABELS[tier]}: no model set"
            rows.append((_MODE_LABELS[mode], shown))
        sub = (s.get("subagent_model") or "").strip()
        rows.append(("Subagents", sub or "Not set: Claude Code decides"))
        body = [KV(rows)]
        for v in FORCE_VARS:
            if os.environ.get(v):
                body.append(Callout("warn", f"{v} is set",
                                    "It overrides or changes what the model names above mean for sessions started "
                                    "from this dashboard. Unset it, or expect a different model than the one chosen."))
        if not any(isinstance(s.get(f"tier_{t}"), str) and s.get(f"tier_{t}").strip() for t in TIERS):
            body.append(Text("No tier has a model yet, so every session still starts on the harness default."))
        return [Card("Where each Start agent mode runs", body)]

    # -- escalation cards ------------------------------------------------------------------------------------------

    def _cards(self, snapshots_of, state):
        out = []
        for snap in snapshots_of:
            for item in snap.items:
                ticket, task = item.get("ticket"), item.get("task")
                if not (isinstance(ticket, str) and isinstance(task, str)):
                    continue
                n = int(item.get("sessions") or 0)
                if n <= state.handled(ticket, task):
                    continue
                up = next_tier(state.last_tier(ticket))
                if up is None:
                    continue  # already on Strong: there is no tier above
                out.append((ticket, task, n, up, item))
        return out

    def decisions(self, view):
        state = State(view.state_dir)
        cards = []
        for ticket, task, n, up, item in self._cards(view.snapshots("failures"), state):
            body = f"{item.get('text', '')} The next session you start on {ticket} will use {TIER_LABELS[up]}."
            if item.get("tail"):
                body += f" Last lines of the log ({item.get('receipt')}): {item['tail']}"
            cards.append(PendingDecision(
                id=f"escalate-{ticket}-{task}", title=f"{task} on {ticket} failed its verify in {n} sessions",
                body=body[:2000], ticket=ticket, role="warn",
                choices=((ESCALATE, f"Next start on {TIER_LABELS[up]}"), (IGNORE, "Not now"))))
        return cards

    def resolve(self, decision_id, choice, ctx):
        m = _ID.fullmatch(decision_id)
        if m is None or choice not in (ESCALATE, IGNORE):
            return None
        ticket, task = m.groups()
        state = State(ctx.addon.state_dir)
        found = next((c for c in self._cards(ctx.snapshots("failures"), state) if c[0] == ticket and c[1] == task),
                     None)
        if found is None:
            return "Nothing to do: this card is no longer current."
        _, _, n, up, item = found
        state.set_handled(ticket, task, n)
        if choice == IGNORE:
            return f"Left {ticket} as it is. The card returns only after another session fails {task}."
        state.set_escalation(ticket, up, task, str(item.get("receipt") or ""), n)
        return (f"The next session you start on {ticket} uses {TIER_LABELS[up]} and is told where the failing "
                f"receipt of {task} is. Open the ticket and use Start agent.")


def _clean(value, what: str) -> str:
    value = (value or "").strip() if isinstance(value, str) else ""
    if value:
        try:
            LaunchPlan(model=value)
        except ValueError as e:
            raise ModelNameError(f"{what} {value!r}: {e}") from None
    return value


def create(ctx):
    return ModelRouting(ctx)
