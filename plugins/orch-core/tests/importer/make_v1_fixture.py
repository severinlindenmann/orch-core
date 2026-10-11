"""Writes the v1 fixture workspace of the import tests with v1's own code (``orch.core.model`` renders the tickets).

Run it once with a checkout of v1 (``origin/main``) when the fixture should change; the result is committed under
``tests/importer/fixtures/v1/`` so the tests need no v1 code::

    git worktree add --detach /tmp/v1-ref origin/main
    uv run --project /tmp/v1-ref/plugins/orch-core python tests/importer/make_v1_fixture.py tests/importer/fixtures/v1
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

from orch.core.model import new_ticket, render_ticket


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main(out: Path) -> None:
    shutil.rmtree(out, ignore_errors=True)
    home = out / "orchestrator"
    (home / ".state").mkdir(parents=True)
    for d in ("backlog", "open", "in-progress", "waiting", "testing", "done"):
        (home / "tickets" / d).mkdir(parents=True)
    (home / "config.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "customer": "Globex Energy",
                "id": {"prefix": "DEMO", "pad": 4},
                "git": {"repos": {"pipelines": {"path": "pipelines"}}},
            },
            indent=2,
        )
        + "\n"
    )
    events: list[dict] = []
    seq = [0]

    def ev(ticket: str, kind: str, actor: str, at: str, **data) -> None:
        seq[0] += 1
        events.append(
            {"seq": seq[0], "at": at, "ticket": ticket, "kind": kind, "actor": actor, "via": "cli", "data": data}
        )

    def write(key: str, status: str, slug: str, t, files: dict[str, bytes] | None = None) -> None:
        t.meta["status"] = status
        (home / "tickets" / status / f"{key}-{slug}.md").write_text(render_ticket(t))
        for name, data in (files or {}).items():
            p = home / "artifacts" / key / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)

    def mk(n: int, title: str, **meta):
        t = new_ticket(
            f"DEMO-{n:04d}",
            title,
            type=meta.pop("type", "feature"),
            priority=meta.pop("priority", "normal"),
            size=meta.pop("size", "m"),
            created=meta.pop("created", f"2026-09-0{n}T09:00Z"),
        )
        t.meta.update(meta)
        return t

    # 1 backlog: bare ask
    t = mk(1, "Ingest smart-meter readings", labels=["customer:globex", "data"])
    t.sections["Ask"] = "Ingest the readings nightly."
    write("DEMO-0001", "backlog", "ingest", t)
    ev("DEMO-0001", "ticket.created", "human:you", "2026-09-01T09:00:00Z", title=t.title)

    # 2 epic with a child
    t = mk(2, "Billing dashboard", type="epic", size="l")
    t.sections["Summary"] = "One dashboard for billing."
    t.sections["Requirements"] = "- Monthly cost per workspace."
    write("DEMO-0002", "open", "billing", t)

    # 3 in progress: tasks, ac, artifacts, open + answered question, parent, link, external, due
    shot = b"\x89PNG\r\n\x1a\nfake image bytes"
    log = b"all green\n"
    t = mk(
        3,
        "Cost view",
        parent="DEMO-0002",
        due="2026-10-30",
        priority="high",
        size="s",
        repos=["pipelines", "elsewhere"],
        branches={"pipelines": "feature/DEMO-0003-cost", "elsewhere": "x"},
        external=[{"key": "FIN-12", "url": "https://jira.example.com/browse/FIN-12"}],
        prs=[{"repo": "pipelines", "url": "https://github.com/acme/pipelines/pull/7"}],
        blocked_by=["DEMO-0001", "DEMO-0099"],
        labels=["Needs Review!"],
        questions=[
            {
                "id": "Q1",
                "text": "Which month?",
                "why": "Seeds differ.",
                "type": "single",
                "blocking": True,
                "options": [
                    {"key": "jan", "label": "January", "cost": None},
                    {"key": "feb", "label": "February", "cost": None},
                ],
                "recommended": "jan",
                "asked": "2026-09-03T10:00Z",
            },
            {
                "id": "Q2",
                "text": "Use the API?",
                "type": "confirm",
                "blocking": False,
                "answer": "yes",
                "answered": "2026-09-03T11:00Z",
                "options": [{"key": "yes", "label": "Yes"}, {"key": "no", "label": "No"}],
            },
        ],
        artifacts=[
            {
                "name": "after.png",
                "kind": "screenshot",
                "sha256": sha(shot),
                "size": len(shot),
                "ac": 1,
                "label": "After",
            },
            {"name": "sub/out.log", "kind": "receipt", "sha256": sha(log), "size": len(log), "task": "T2"},
            {"name": "gone.csv", "kind": "dataset", "sha256": sha(b"x"), "size": 1},
            {"url": "https://ci.example.com/run/1", "kind": "build", "label": "CI"},
        ],
    )
    t.meta["gates"]["requirements"] = {"approved": "2026-09-03T12:00Z", "via": "human", "hash": "sha256:abc"}
    t.sections["Context"] = (
        "See ![after](artifact:after.png) and ![gone](artifact:gone.csv).\n\n```text\n## not a heading\n```"
    )
    t.sections["Requirements"] = "- Show cost by month."
    t.sections["Acceptance criteria"] = "- [ ] Loads in 5 s.\n- [x] Totals match the invoice\n  within 1 percent.\n"
    t.sections["Out of scope"] = "Forecasts."
    t.sections["Plan"] = "1. View.\n2. Dashboard."
    t.sections["Tasks"] = (
        "- [x] T1 Create the view\n  - verify: cmd: pytest -q\n  - ref: ac:1\n"
        "- [/] T2 Build the dashboard\n  - owner: human\n  - ref: ac:2\n  - note: working\n"
        "- [ ] T3 Share it\n  - needs: T2\n"
    )
    t.sections["Current state"] = "Dashboard half done."
    t.sections["Verification"] = "- AC1: ![after](artifact:after.png)"
    t.sections["Log"] = "- 2026-09-03T09:00Z [you] created"
    t.sections["Findings"] = "A finding."
    write("DEMO-0003", "in-progress", "cost-view", t, {"after.png": shot, "sub/out.log": log})
    ev("DEMO-0003", "ticket.created", "human:you", "2026-09-03T09:00:00Z", title=t.title)
    ev("DEMO-0003", "claim.taken", "agent:claude-code:7f3c", "2026-09-03T09:05:00Z")
    ev("DEMO-0003", "gate.approved", "human:you", "2026-09-03T12:00:00Z", gate="requirements")

    # 4 testing: spike (v1 investigation) with findings and a control character in a section
    t = mk(4, "Why is it slow", type="investigation", size="xs")
    t.sections["Requirements"] = "Find the cause.\x1b[31m red ‮evil‬"
    t.sections["Findings"] = "The join."
    t.sections["Plan"] = "Look."
    t.sections["Out of scope"] = "Fixing it."
    write("DEMO-0004", "testing", "slow", t)

    # 5 done, completed
    t = mk(5, "Fix the typo", type="bug", size="xs")
    t.sections["Requirements"] = "Typo gone."
    t.sections["Verification"] = "Done."
    t.meta["resolution"] = "completed"
    write("DEMO-0005", "done", "typo", t)

    # 6 done, duplicate of 5 (superseded_by)
    t = mk(6, "Fix the typo again", type="bug", size="xs", resolution="duplicate", superseded_by="DEMO-0005")
    write("DEMO-0006", "done", "typo-again", t)

    # 7 done, wont-do
    t = mk(7, "Rewrite in Rust", type="chore", size="xl", resolution="wont-do")
    t.sections["Plan"] = "Rewrite."
    write("DEMO-0007", "done", "rust", t)

    # 8 waiting with the Ask
    t = mk(8, "Needs an answer", status="waiting")
    t.sections["Ask"] = "Pick one."
    t.sections["Context"] = "Some context."
    write("DEMO-0008", "waiting", "answer", t)
    ev("DEMO-0008", "question.asked", "agent:claude-code:aa", "2026-09-08T09:00:00Z", question="Q1")
    (home / ".state" / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (home / ".state" / "counter.json").write_text('{"next": 9}\n')


if __name__ == "__main__":
    main(Path(sys.argv[1]))
