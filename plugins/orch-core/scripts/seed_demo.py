"""Create a demo workspace so every Mission Control page has something to show.

Run: uv run python scripts/seed_demo.py /tmp/mc-demo
Then, in your own terminal: cd /tmp/mc-demo && orch serve

The workspace is git-initialised (plus two small repositories) and holds about a dozen tickets in
every status: requirements and a plan waiting for approval, a question with options, a testing
ticket with Verification, a stale agent claim, finished work spread over the last weeks, and one
broken ticket file. Every change goes through `Ops`, so `events.jsonl` matches the tickets; the
clock is moved back while seeding so the Timeline and Reports pages have history.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import orch.clock
from orch.core import store
from orch.core.events import Actor
from orch.core.ops import Ops
from orch.core.workspace import Workspace

REAL_NOW = datetime.now(timezone.utc)
_at = [REAL_NOW]


def _fake_now() -> datetime:
    return _at[0]


def at(days: float = 0, hours: float = 0, minutes: float = 0) -> None:
    """Set the seeding clock to the given time before the real now."""
    _at[0] = REAL_NOW - timedelta(days=days, hours=hours, minutes=minutes)


def tick(minutes: float = 7) -> None:
    _at[0] += timedelta(minutes=minutes)


def _git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)


REQS = """- Show the monthly cost per workspace and per job cluster.
- Data comes from the `system.billing.usage` table, refreshed nightly.
- Filters: workspace, cost centre, month."""
ACS = """- [ ] The dashboard loads in under 5 s for 12 months of data.
- [ ] Totals match the Databricks invoice within 1 %.
- [ ] A finance user can open it without a Databricks login."""
PLAN = """1. Create the `billing.cost_daily` view over `system.billing.usage`.
2. Add a nightly job that refreshes the aggregate table.
3. Build the AI/BI dashboard with the three filters.
4. Share it with the finance group and check the invoice totals."""


def _ready(h: Ops, a: Ops, tid: str, *, plan: bool = True) -> None:
    """Requirements approved, claimed by the agent, plan approved: ready for work."""
    a.set_section(tid, "Requirements", REQS)
    tick()
    a.set_section(tid, "Acceptance criteria", ACS)
    tick(30)
    h.approve(tid, "requirements")
    tick(40)
    a.claim(tid)
    tick(20)
    if plan:
        a.set_section(tid, "Plan", PLAN)
        tick(45)
        h.approve(tid, "plan")
        tick(30)


def _finish(h: Ops, a: Ops, tid: str, *, work_hours: float, evidence: str) -> None:
    a.set_state(tid, "Implementation done; running the checks.")
    tick(work_hours * 60)
    a.set_section(tid, "Verification", evidence)
    tick(5)
    a.move(tid, "testing")
    tick(2)
    a.release(tid)
    tick(180)
    h.verdict(tid, "done")


def _done(h: Ops, a: Ops, days_ago: float, title: str, kind: str, size: str, hours: float, *,
          question: str | None = None) -> None:
    at(days=days_ago, hours=6)
    tid = h.new(title, type=kind, size=size, ask=f"{title}.").id
    tick(15)
    _ready(h, a, tid, plan=size not in ("xs", "s"))
    if question:  # asked and answered, so Reports has a "waiting on you" time
        a.ask(tid, [{"text": question, "type": "single", "options": ["Yes", "No"], "recommended": "A"}])
        tick(95)
        h.answer(tid, "Q1", "A", "fine for now")
        tick(10)
    _finish(h, a, tid, work_hours=hours, evidence="`pytest -q`: 48 passed.\nRe-ran the job on a copy of prod: OK.")


def seed(root: Path) -> Workspace:
    if (root / "orchestrator").exists():
        raise SystemExit(f"{root} already has an orchestrator folder; pick an empty directory")
    _git_init(root)
    _git_init(root / "pipelines")
    _git_init(root / "dashboards")
    home = root / "orchestrator"
    home.mkdir(parents=True)
    config = {
        "schema": 1,
        "customer": "Globex Energy",
        "id": {"prefix": "CE", "pad": 4},
        "external_trackers": [{"prefix": "FIN", "pattern": "FIN-\\d+", "url": "https://jira.example.com/browse/{key}"}],
        "git": {"repos": {"pipelines": {"path": "pipelines"}, "dashboards": {"path": "dashboards"}}},
        "gates": {"plan_skip_sizes": ["xs", "s"]},
        "claims": {"ttl_hours": 48},
    }
    (home / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    orch.clock.now = _fake_now  # stamp()/stamp_s() read the clock through the module
    ws = Workspace.open(root)
    h = Ops(ws, Actor("human", "you", "dashboard"))
    claude = Ops(ws, Actor("agent", "claude-code", "cli", "7f3c9a21-5d1e-4c2a-9b1e-0d6c3e8f1a42"))
    copilot = Ops(ws, Actor("agent", "copilot", "cli", "b2e4d6f8-1a3c-4e5f-8a7b-9c0d1e2f3a4b"))

    # -- finished work over the last six weeks (Reports: done per week, medians, time split) --
    done_specs = [
        (36, "Ingest smart-meter readings into bronze", "feature", "m", 9),
        (23, "Upgrade the job clusters to DBR 15.4 LTS", "chore", "s", 3),
        (15, "Alert when the nightly load is late", "feature", "m", 6, "Send the alert to the on-call Teams channel?"),
    ]
    for days_ago, title, kind, size, hours, *question in done_specs:
        _done(h, claude, days_ago, title, kind, size, hours, question=(question or [None])[0])

    # One ticket was sent back once before it was accepted.
    at(days=9, hours=3)
    tid = h.new("Repartition the meter readings by day", type="chore", size="s", ask="Queries on one day scan the whole table.").id
    tick(10)
    _ready(h, copilot, tid, plan=False)
    copilot.set_section(tid, "Verification", "OPTIMIZE ran; file count 4 812 → 312.")
    tick(5)
    copilot.move(tid, "testing")
    tick(240)
    h.verdict(tid, "follow-up", "Also add ZORDER BY meter_id, the dashboard filters on it.")
    tick(120)
    copilot.log(tid, "added ZORDER BY meter_id and re-ran OPTIMIZE")
    copilot.set_section(tid, "Verification", "OPTIMIZE ... ZORDER BY (meter_id): file count 4 812 → 298; dashboard query 41 s → 6 s.")
    tick(5)
    copilot.move(tid, "testing")
    tick(90)
    h.verdict(tid, "done")
    _done(h, claude, 4, "Mask IBAN columns for the analyst group", "feature", "s", 5)
    _done(h, copilot, 2.5, "Typo in the cost-centre lookup", "bug", "xs", 1,
          question="Also fix the same typo in the archived 2024 table?")

    # -- the decisions waiting for the human right now --
    # Requirements ready for approval.
    at(days=2, hours=5)
    tid = h.new("Cost dashboard for the finance team", type="feature", size="m", external="FIN-118",
                ask="Finance wants to see the Databricks cost per workspace each month.").id
    tick(30)
    claude.set_section(tid, "Requirements", REQS)
    tick(10)
    claude.set_section(tid, "Acceptance criteria", ACS)
    tick(5)
    claude.set_section(tid, "Out of scope", "- Forecasting.\n- Cost from other clouds.")

    # Plain backlog idea, nothing refined yet.
    at(days=1, hours=20)
    h.new("Archive raw files older than 2 years", type="chore", size="s",
          ask="The landing volume grows by 300 GB a month; most of it is never read again.")

    # Open: approved, nobody has started yet.
    at(days=1, hours=8)
    tid = h.new("Nightly backup of the Unity Catalog metastore", type="feature", size="m",
                ask="Requested in the weekly ops meeting.").id
    tick(10)
    claude.set_section(tid, "Requirements", "- Export catalog, schema and grant definitions every night.\n- Keep 30 days.")
    tick(5)
    claude.set_section(tid, "Acceptance criteria", "- [ ] A restore into a test metastore recreates every grant.")
    tick(20)
    h.approve(tid, "requirements")

    # In progress with a plan waiting for approval (size l).
    at(days=1, hours=3)
    tid = h.new("Migrate the ETL jobs to Lakeflow pipelines", type="feature", size="l", external="FIN-121",
                ask="Move the 14 notebook jobs to declarative pipelines.").id
    tick(10)
    _ready(h, claude, tid, plan=False)
    claude.link(tid, repo="pipelines", branch=f"feature/{tid}-lakeflow-migration")
    tick(15)
    claude.set_section(tid, "Plan", "1. Inventory the 14 jobs and their schedules.\n2. Convert bronze jobs first (6).\n"
                                    "3. Convert silver and gold (8).\n4. Run old and new side by side for a week.\n"
                                    "5. Switch the schedules and delete the old jobs.")
    tick(5)
    claude.set_state(tid, "Plan written; waiting for approval before converting the first job.")

    # Waiting on a question with options.
    at(hours=20)
    tid = h.new("Load the tariff table from the SFTP drop", type="feature", size="m",
                ask="Tariffs arrive as CSV on SFTP each Monday.").id
    tick(10)
    _ready(h, copilot, tid, plan=False)
    copilot.set_section(tid, "Plan", "1. Auto Loader on the SFTP volume.\n2. Merge into silver.tariffs by tariff_id and valid_from.")
    tick(30)
    h.approve(tid, "plan")
    tick(60)
    copilot.ask(tid, [{
        "text": "The CSV has no header in older files. How should those be loaded?",
        "type": "single",
        "options": [
            {"key": "schema", "label": "Use a fixed schema for every file"},
            {"key": "skip", "label": "Skip files without a header and report them"},
            {"key": "manual", "label": "Stop and ask me per file"},
        ],
        "recommended": "schema",
    }])

    # Testing, with Verification: waits for a verdict.
    at(hours=12)
    tid = h.new("Data quality checks for the meter readings", type="feature", size="m", external="FIN-104",
                ask="See the title; details are in the linked FIN ticket.").id
    tick(10)
    _ready(h, claude, tid)
    claude.link(tid, repo="pipelines", branch=f"feature/{tid}-dq-checks", pr="https://github.com/globex/pipelines/pull/42")
    claude.set_section(tid, "Verification",
                       "- `pytest tests/dq -q`: 23 passed.\n- Expectations on 7 days of prod data: 0 violations of not-null, "
                       "3 rows outside the plausible range (logged to quarantine).\n- Pipeline run: 4 min 12 s.")
    tick(5)
    claude.move(tid, "testing")

    # A stale claim: copilot started 7 hours ago and has been silent since.
    at(hours=7)
    tid = h.new("Investigate slow queries on the gold layer", type="investigation", size="s",
                ask="Requested in the weekly ops meeting.").id
    tick(10)
    _ready(h, copilot, tid, plan=False)
    copilot.set_state(tid, "Collected the 20 slowest queries from system.query.history.")

    # Agent working right now, with a non-blocking text question.
    at(hours=2, minutes=30)
    tid = h.new("Rotate the service principal secrets", type="chore", size="s", priority="high",
                ask="Requested in the weekly ops meeting.").id
    tick(5)
    _ready(h, claude, tid, plan=False)
    claude.log(tid, "rotated the secret for sp-ingest")
    tick(5)
    claude.ask(tid, [{"text": "Should the old secrets be revoked now or after a 24 h overlap?", "type": "text", "blocking": False}])

    # A broken ticket file (unparseable frontmatter) for the repair card.
    at(minutes=10)
    tid = h.new("Hand-edited ticket with a typo in the frontmatter", type="bug", size="xs").id
    store.resolve(ws, tid).path.write_text("---\nid: " + tid + "\ntitle: [unclosed\nstatus: backlog\n---\n\n## Ask\n\nOops.\n",
                                           encoding="utf-8")
    return ws


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python scripts/seed_demo.py <empty-dir>")
    root = Path(sys.argv[1]).expanduser().resolve()
    ws = seed(root)
    count = sum(1 for _ in store.scan(ws))
    print(f"demo workspace ready: {root} ({count} tickets)")
    print(f"open it in your own terminal: cd {root} && orch serve")


if __name__ == "__main__":
    main()
