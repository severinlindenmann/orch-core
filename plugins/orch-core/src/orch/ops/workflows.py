"""The workflows ``orch help <workflow>`` explains: an ordered list of registry operations, each with the
call to make. The text is generated from the registry (the summary of each operation), so it is never stale."""

from __future__ import annotations

# workflow -> (one line, [(operation, example call)])
WORKFLOWS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "work": (
        "pick up a ticket, do its tasks, prove the acceptance criteria, hand over",
        [
            ("status", "orch status"),
            ("claim", "orch claim --next"),
            ("task.next", "orch task next"),
            ("task.done", "orch task done T3 --run --artifact out.log --ac AC2 -m 'what ran'"),
            ("handoff", "orch handoff -m 'where it stands'"),
            ("submit", "orch submit"),
        ],
    ),
    "ask": (
        "ask a person instead of guessing, then wait for the answer",
        [
            ("ask", "orch ask 'question' --options a,b --rec a"),
            ("wait", "orch wait"),
        ],
    ),
    "refine": (
        "turn a rough ticket into requirements, acceptance criteria and tasks",
        [
            ("show", "orch show --section requirements"),
            ("section.set", "orch section set requirements --file -"),
            ("ac.add", "orch ac add 'text'"),
            ("task.add", "orch task add 'text' --verify 'cmd' --proves AC1"),
            ("ask", "orch ask 'question' --options a,b --rec a"),
        ],
    ),
    "parallel": (
        "work as a subagent: one lease on one task of the manager's claim",
        [
            ("task.next", "ORCH_SESSION=<yours>.<n> orch task next"),
            ("task.start", "orch task start T3"),
            ("task.done", "orch task done T3 --run"),
            ("log", "orch log 'note'"),
        ],
    ),
}
