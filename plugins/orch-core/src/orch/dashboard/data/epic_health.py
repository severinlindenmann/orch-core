"""The epic page at a glance: which of six buckets each child is in, the progress sentence/bar/legend counted from
those buckets, the state chip, the filter and the "nothing is waiting" test. Pure functions over the child cards the
Children list is drawn from, so every number on the page adds up."""
from __future__ import annotations

# (key, group heading, bar/legend label, role colour). Order = the order of the groups: what needs a person first.
BUCKETS = (
    ("you", "Waiting on you", "Waiting on you", "you"),
    ("idle", "No progress", "No progress", "warn"),
    ("working", "Working", "Working", "info"),
    ("testing", "Testing", "Testing", "neu"),
    ("ready", "Ready", "Ready", "line"),
    ("done", "Done", "Done", "ok"),
)
KEYS = tuple(b[0] for b in BUCKETS)
FILTERS = ("all", "you", "idle")
STOPPED_STATES = ("suspended", "budget used up")


def bucket(card) -> str:
    """The one bucket a child card is counted in. Done first, then the human's move, then testing; a claim whose
    agent is silent (or an in-progress ticket with no agent attached) is "no progress"."""
    if card["status"] == "done":
        return "done"
    move = card["move"]
    if move["who"] == "you":
        return "you"
    if card["status"] == "testing":
        return "testing"
    if move["what"] == "stale" or (card["status"] == "in-progress" and move["what"] in ("ready", "blocked")):
        return "idle"
    return "working" if move["what"] == "working" else "ready"


def sub_line(card, key: str) -> str:
    """One quiet line under a child: why it waits, or why it counts as no progress."""
    if key == "idle":
        return ("Claimed, but the agent has been silent." if card["move"]["what"] == "stale"
                else "In progress, but no agent is attached.")
    if key == "ready" and card["status"] == "open" and card["move"]["what"] == "ready":
        return "Waiting for an agent to start."
    return str(card["move"].get("why") or "")


def counts(keys) -> dict:
    out = {k: 0 for k in KEYS}
    for k in keys:
        out[k] += 1
    return out


def progress(n: dict) -> dict | None:
    """{total, sentence, aria, segments, legend} for the bucket counts `n`; None for an epic with no children.
    Segments and legend list only the non-zero buckets."""
    total = sum(n.values())
    if not total:
        return None
    sentence = f"{n['done']} of {total} done"
    if n["you"]:
        sentence += f" · {n['you']} waiting on you"
    if n["idle"]:
        sentence += f" · {n['idle']} with no progress"
    parts = [{"key": k, "label": label, "role": role, "n": n[k]} for k, _h, label, role in BUCKETS if n[k]]
    aria = f"{sentence}. " + ", ".join(f"{p['n']} {p['label'].lower()}" for p in parts)
    return {"total": total, "sentence": sentence, "aria": aria, "segments": parts, "legend": parts}


def state_chip(*, total: int, n: dict, approved: bool, factory_state: str | None, needs: int) -> tuple[str, str]:
    """(role, label) of the epic's state chip. `needs` is how many things wait on the human in the top slot."""
    if factory_state == "paused":
        return "neu", "Paused"
    if factory_state in STOPPED_STATES:
        return "you", "Stopped"
    if not approved:
        return "you", "Not approved"
    if needs:
        return "you", f"Needs you ({needs})"
    if not total:
        return "neu", "No tickets yet"
    if n["idle"] and not n["working"] and n["done"] < total:
        return "warn", "Stuck: nothing is running"
    return "ok", "On track"


def filter_view(n: dict, show: str) -> dict:
    """The single-choice filter above the list: {options: [{key, label, n, disabled}], show}. An unknown or empty
    choice falls back to "all"."""
    total = sum(n.values())
    show = show if show in FILTERS and (show == "all" or n[show]) else "all"
    options = [{"key": "all", "label": "All", "n": total, "disabled": False},
               {"key": "you", "label": "Needs you", "n": n["you"], "disabled": not n["you"]},
               {"key": "idle", "label": "No progress", "n": n["idle"], "disabled": not n["idle"]}]
    return {"options": options, "show": show}


def groups(rows: list[dict], show: str) -> list[dict]:
    """[{key, title, n, note, rows}] in bucket order. `n` counts the whole group; `rows` follow the filter. A note
    is set when every row of the group has the same sub-line (said once, not on each row)."""
    out = []
    for key, title, _label, _role in BUCKETS:
        mine = [r for r in rows if r["bucket"] == key]
        if not mine:
            continue
        subs = {r["sub"] for r in mine}
        note = subs.pop() if len(subs) == 1 and len(mine) > 1 else ""
        shown = mine if show in ("all", key) else []
        if note:
            shown = [{**r, "sub": ""} for r in shown]
        if shown:
            out.append({"key": key, "title": title, "n": len(mine), "note": note, "rows": shown,
                        "role": "neu" if _role == "line" else _role})
    return out


def nothing_waiting(*, gate: bool, asks: int, permits: int, stopped: bool, ready: bool, needs_you: int,
                    move_human: bool = False) -> bool:
    """True only when every source of "yours to do" is empty: the approve gate, open questions, permission cards
    (and used-up budgets), a stopped factory, the epic verdict, children waiting on you, and any other human move."""
    return not (gate or asks or permits or stopped or ready or needs_you or move_human)
