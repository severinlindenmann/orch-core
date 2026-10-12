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
        f"{key} was created but not labelled: {e['code']} {render.clean_line(e['message'])[:120]}",
    )


def _earlier(title: str, run: Callable[..., int]) -> str | None:
    """The key of a ticket an earlier ``quick add`` of this title created and did not get to label, if there is one."""
    code, doc = _step(["list", "--limit", str(CANDIDATES)], run)
    if code != 0:
        return None
    same = [t["key"] for t in doc["data"]["tickets"] if t["title"] == title][:SHOWN]
    for key in same:
        code, doc = _step(["show", key], run)
        if code != 0:
            continue
        t = doc["data"]["ticket"]
        untouched = doc.get("seq") == 1 and len(t["events"]) == 1
        if t["type"] == "chore" and t["labels"] == [] and t["claim"] is None and untouched:
            return key
    return None


def main(argv: list[str], *, env: Mapping[str, str], out: TextIO, err: TextIO, run: Callable[..., int]) -> int:
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
    key = _earlier(title, run)
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
