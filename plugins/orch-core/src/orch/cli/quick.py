"""``orch quick add TITLE`` (H11f, #308, D-P2-35): the one composite quick-task command.

A quick task is an ordinary ticket (``type: chore``, label ``quick``), so adding one is two registry operations the
CLI chains, in direct and in host mode alike (each step is an ordinary ``orch`` call: grant, session, stop rule,
host idem and retries are the steps' own):

1. ``new TITLE --type chore``;
2. ``set KEY labels=quick``.

A failure between the two leaves an ordinary chore ticket. The same command run again finds it (the newest chore with
this exact title that nothing has touched since ``ticket.created``: no label, no other event) and labels it instead
of creating a second one; the failure itself is ``quick.unlabelled`` (exit 9, retryable) and names the key.
``quick.list`` is a registry read and needs no code here.
"""

from __future__ import annotations

import io
import json
import time
from collections.abc import Callable, Mapping
from typing import Any, TextIO

from orch.cli import render
from orch.ops import Result
from orch.ops import quick as core
from orch.ops.errors import OrchError

__all__ = ["main"]

USAGE = 'usage: orch quick add "TITLE" [--json]   (one line, at most 120 characters; the list is orch quick list)'
CANDIDATES = 50  # newest tickets looked at for an unlabelled one from an earlier attempt
SHOWN = 5  # exact-title matches opened at most


def _step(argv: list[str], run: Callable[..., int]) -> tuple[int, dict[str, Any]]:
    out, err = io.StringIO(), io.StringIO()
    code = run([*argv, "--json"], out, err)
    text = (out.getvalue() or err.getvalue()).strip()
    try:
        return code, json.loads(text)
    except ValueError:
        return code or 1, {"ok": False, "error": {"code": "internal", "message": "unreadable answer of " + argv[0]}}


def _failure(code: int, doc: dict[str, Any], key: str | None) -> OrchError:
    e = doc.get("error") or {"code": "internal", "message": "failed"}
    if key is None:
        return OrchError(e["code"], e["message"], hint=e.get("hint"))
    return OrchError(
        "quick.unlabelled",
        f"{key} was created but not labelled: {e['code']} {render.clean_line(e['message'])[:120]}; "
        f"run the same quick add again, or label it yourself: orch set {key} labels=quick",
    )


ADOPT_WITHIN_S = 600  # an earlier attempt's ticket is adopted only within this many seconds of its creation


def _mine(created: dict[str, Any] | None, env: Mapping[str, str], now: float) -> bool:
    """Was the ticket created by this caller (the same grant and, when the call has one, the same session) within the
    last :data:`ADOPT_WITHIN_S` seconds? Anything else is somebody else's ticket (or an old one) and is never taken."""
    import calendar
    import time

    grant = (env.get("ORCH_GRANT") or "").partition(".")[0]
    if not created or not grant:
        return False
    actor = created.get("actor") or {}
    if actor.get("kind") != "agent" or actor.get("grant") != grant:
        return False
    if actor.get("session") != (env.get("ORCH_SESSION") or None):
        return False
    try:
        at = calendar.timegm(time.strptime(created["at"], "%Y-%m-%dT%H:%M:%SZ"))
    except (KeyError, ValueError, TypeError):
        return False
    return 0 <= now - at <= ADOPT_WITHIN_S


def _earlier(title: str, run: Callable[..., int], env: Mapping[str, str], now: float) -> str | None:
    """The key of a ticket an earlier ``quick add`` of this caller created in the last 10 minutes and did not get to
    label, if there is one: an untouched chore (exact title, no label, no claim, one event) of the caller's own grant
    and session."""
    code, doc = _step(["list", "--limit", str(CANDIDATES)], run)
    if code != 0:
        return None
    same = [t["key"] for t in doc["data"]["tickets"] if t["title"] == title][:SHOWN]
    for key in same:
        code, doc = _step(["show", key], run)
        if code != 0:
            continue
        t = doc["data"]["ticket"]
        if not (t["type"] == "chore" and t["labels"] == [] and t["claim"] is None):
            continue
        if doc.get("seq") != 1 or len(t["events"]) != 1:
            continue
        code, log = _step(["show", key, "--log", "--since", "0"], run)
        if code == 0 and _mine(log["data"]["ticket"].get("created"), env, now):
            return key
    return None


def main(
    argv: list[str],
    *,
    env: Mapping[str, str],
    out: TextIO,
    err: TextIO,
    run: Callable[..., int],
    now: Callable[[], float] = time.time,
) -> int:
    """``run(argv, out, err)`` runs one ordinary ``orch`` call; the caller binds the mode, records and clock."""
    as_json = "--json" in argv or env.get("ORCH_OUTPUT") == "json"
    rest = [a for a in argv if a != "--json"]

    def fail(e: OrchError) -> int:
        envelope = render.error_envelope(e)
        text = render.dumps(envelope) if as_json else render.error_text(envelope, render.use_color(err, as_json, env))
        (out if as_json else err).write(text + "\n")
        return render.error_exit(e.code)

    if rest[1:] in (["--help"], ["-h"]):
        out.write(USAGE + "\n")
        return 0
    if len(rest) != 2 or rest[0] != "add":
        return fail(OrchError("usage", USAGE))
    title = rest[1]
    if "\n" in title or "\r" in title or not 1 <= len(title.strip()) <= core.TITLE_MAX:
        return fail(OrchError("invalid.input", f"a quick task's title is one line of 1 to {core.TITLE_MAX} characters"))
    code, doc = _step(["addon", "list"], run)
    if code == 0 and not any(
        a.get("name") == core.ADDON and a.get("state") not in ("disabled", "purged", "not_granted")
        for a in doc["data"].get("addons", [])
    ):
        return fail(OrchError("quick.off", core.MESSAGE))
    key = _earlier(title, run, env, now())
    created = key is None
    if created:
        code, doc = _step(["new", title, "--type", "chore"], run)
        if code != 0:
            return fail(_failure(code, doc, None))
        key = doc["key"]
    code, doc = _step(["set", key, f"labels={core.LABEL}"], run)
    if code != 0:
        return fail(_failure(code, doc, key))
    res = Result(
        data={"key": key, "title": title, "type": "chore", "labels": [core.LABEL], "created": created},
        key=key,
        seq=int(doc.get("seq") or 0),
        hints=[f"orch claim {key}"],
    )
    if as_json:
        out.write(render.dumps(render.result_envelope(res)) + "\n")
    else:
        out.write(f"ok {key} quick.add {'created' if created else 'labelled'} seq={res.seq}\nnext: {res.hints[0]}\n")
    return 0
