"""The decisions of model routing, free of orch: which tier a start gets, which model that is, what failed twice."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

TIERS = ("light", "standard", "strong")
MODES = ("refine", "work", "fix-checks", "continue")
CONTINUE = "continue"
# Variables that silently change what a model name or a subagent means in Claude Code (model-config docs)
FORCE_VARS = ("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
              "ANTHROPIC_DEFAULT_HAIKU_MODEL")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]:/-]{0,63}")
_TASK = re.compile(r"T[1-9][0-9]*")
_TICKET = re.compile(r"[A-Z][A-Z0-9]*-[0-9]+")  # ASCII only: [0-9], never \d (which matches other scripts' digits)
_RECEIPT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
LABELS = {"light": "Light", "standard": "Standard", "strong": "Strong"}


class ModelNameError(ValueError):
    """A setting that is not a model name: the launch is refused with this sentence, never started on a guess."""


def model_of(settings: dict, tier: str) -> str | None:
    """The model name saved for `tier`, None when empty. A name Claude Code could not take as an argument raises."""
    raw = settings.get(f"tier_{tier}")
    value = raw.strip() if isinstance(raw, str) else ""
    if not value:
        return None
    if not _NAME.fullmatch(value):
        raise ModelNameError(f"{LABELS[tier]} model {value!r} is not a model name: use an alias such as opus, sonnet, "
                             "haiku or opusplan, or a full model name (letters, digits and . _ [ ] : / -)")
    return value


def next_tier(tier: str | None) -> str | None:
    """One tier up from `tier` (None counts as Standard, the usual start); None above Strong."""
    order = list(TIERS)
    at = order.index(tier) if tier in order else 1
    return order[at + 1] if at + 1 < len(order) else None


@dataclass(frozen=True)
class Choice:
    tier: str | None
    model: str | None
    reason: str
    note: str = ""


def _resolved(settings: dict, tier: str, why: str) -> Choice:
    model = model_of(settings, tier)
    if model is None and tier == "light":  # Light is optional: without a model of its own it is Standard
        std = model_of(settings, "standard")
        if std:
            return Choice("standard", std, f"{why}: Light has no model, so Standard ({std})")
    if model is None:
        return Choice(tier, None, f"{why}: {LABELS[tier]} has no model set, so the harness default")
    return Choice(tier, model, f"{why}: {LABELS[tier]} ({model})")


def choose(settings: dict, mode: str, *, strong_next: bool, escalation: dict | None, last_tier: str | None) -> Choice:
    """Tier and model for one start. A human's "next start on Strong" wins, then an accepted escalation, then the
    tier saved for the mode ("same" repeats the last start on the ticket)."""
    if strong_next:
        return _resolved(settings, "strong", "You marked this ticket's next start")
    if escalation and escalation.get("tier") in TIERS:
        c = _resolved(settings, escalation["tier"], f"Escalated after {escalation.get('task', 'a task')} failed its verify")
        task, receipt = str(escalation.get("task") or ""), str(escalation.get("receipt") or "")
        if c.model and _TASK.fullmatch(task) and _RECEIPT.fullmatch(receipt):
            c = Choice(c.tier, c.model, c.reason,
                       f"The previous sessions on this ticket failed the verify of task {task}; the latest receipt "
                       f"is the artifact {receipt} on this ticket.")
        return c
    setting = settings.get(f"mode_{mode.replace('-', '_')}") or ("same" if mode == CONTINUE else "none")
    if setting == "same":
        if last_tier in TIERS:
            return _resolved(settings, last_tier, "Same as the last start on this ticket")
        setting = "standard"
    if setting in TIERS:
        return _resolved(settings, setting, f"{mode} runs on {setting}")
    return Choice(None, None, f"No tier is set for {mode}: the harness default")


class State:
    """What the addon remembers on this machine, in its own state folder: the tier of each ticket's last start, the
    accepted escalations waiting for the next start, and how many failing sessions the human already answered."""

    def __init__(self, folder):
        self.path = Path(folder) / "state.json"

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        return {k: (data.get(k) if isinstance(data.get(k), dict) else {}) for k in ("starts", "escalations", "handled")}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def last_tier(self, ticket: str) -> str | None:
        tier = (self._read()["starts"].get(ticket) or {}).get("tier")
        return tier if tier in TIERS else None

    def record_start(self, ticket: str, tier: str, mode: str, session: str) -> None:
        data = self._read()
        data["starts"][ticket] = {"tier": tier, "mode": mode, "session": session}
        if len(data["starts"]) > 2000:
            data["starts"] = dict(list(data["starts"].items())[-2000:])
        self._write(data)

    def escalation(self, ticket: str) -> dict | None:
        e = self._read()["escalations"].get(ticket)
        return e if isinstance(e, dict) else None

    def set_escalation(self, ticket: str, tier: str, task: str, receipt: str, sessions: int) -> None:
        data = self._read()
        data["escalations"][ticket] = {"tier": tier, "task": task, "receipt": receipt, "sessions": sessions}
        self._write(data)

    def consume_escalation(self, ticket: str) -> None:
        data = self._read()
        if data["escalations"].pop(ticket, None) is not None:
            self._write(data)

    def handled(self, ticket: str, task: str) -> int:
        n = self._read()["handled"].get(f"{ticket}/{task}")
        return n if isinstance(n, int) else 0

    def set_handled(self, ticket: str, task: str, sessions: int) -> None:
        data = self._read()
        data["handled"][f"{ticket}/{task}"] = sessions
        self._write(data)


def _failed(run) -> bool:
    return isinstance(run, dict) and (run.get("timed_out") is True or (run.get("exit") is not None and run.get("exit") != 0))


def _tail(addon_ctx, ticket: str, name: str) -> str:
    """The last lines of a receipt log. `name` comes from the ticket's frontmatter, which an agent can write: it is
    checked against a strict name pattern and the file must resolve inside the ticket's own artifact folder."""
    if not (_TICKET.fullmatch(ticket) and _RECEIPT.fullmatch(name)):
        return ""
    try:
        root = Path(addon_ctx.ws.artifacts_dir)
        folder = root / ticket
        base = folder.resolve()
        path = base / name
        if folder.is_symlink() or not base.is_relative_to(root.resolve()) or path.is_symlink() or not path.resolve().is_relative_to(base) or not path.is_file():
            return ""
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 2000))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [_CONTROL.sub("", ln).strip() for ln in text.splitlines()]
    return " | ".join(ln for ln in lines if ln)[-500:] if any(lines) else ""


def failures_items(addon_ctx) -> list[dict]:
    """One item per open task whose verify (a failing receipt) failed in two or more separate agent sessions."""
    out = []
    for entry in addon_ctx.tickets():
        if entry.status == "done" or not isinstance(entry.meta, dict) or not _TICKET.fullmatch(str(entry.id)):
            continue
        failing: dict[str, list[dict]] = {}
        for a in entry.meta.get("artifacts") or []:
            if isinstance(a, dict) and a.get("kind") == "receipt" and isinstance(a.get("task"), str) \
                    and _failed(a.get("run")) and isinstance(a.get("by"), str) and a["by"].count(":") >= 2:
                failing.setdefault(a["task"], []).append(a)
        failing = {t: rs for t, rs in failing.items() if len({r["by"] for r in rs}) >= 2}
        if not failing:
            continue
        try:
            tasks = {t.get("id"): t.get("state") for t in addon_ctx.document(entry.id)["tasks"]["tasks"]}
        except Exception:
            continue
        for task, receipts in sorted(failing.items()):
            if tasks.get(task) not in ("todo", "doing", "blocked"):
                continue  # done or skipped: it passed in the end
            sessions = len({r["by"] for r in receipts})
            name = str(receipts[-1].get("name") or "")
            out.append({"id": f"{entry.id}-{task}", "label": f"{entry.id} {task}", "role": "warn",
                        "text": f"{task} failed its verify in {sessions} separate sessions.",
                        "ticket": entry.id, "task": task, "sessions": sessions,
                        "receipt": name if _RECEIPT.fullmatch(name) else "", "tail": _tail(addon_ctx, entry.id, name)})
    return out
