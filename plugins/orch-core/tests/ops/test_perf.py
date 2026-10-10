"""1000 tickets: the commands an agent runs all the time stay fast (review of #351, finding 2)."""

from __future__ import annotations

import os
import time

import pytest

from tests.ops.helpers import Cli, Ws

pytestmark = pytest.mark.slow
SLACK = 3 if os.environ.get("CI") else 1  # shared runners are slower; the budgets are for a laptop


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    w = Ws(tmp_path_factory.mktemp("big"))
    w.bootstrap()
    s = w.store
    for i in range(1000):
        r = s.create_ticket(
            actor=w.agent, ticket_type="feature", title=f"Ticket number {i} about seeds", owner=w.owner.ref
        )
        w.tick() if i % 50 == 0 else None
        assert r.event["seq"] == 1
    s.close()
    w.store = None
    return w


def timed(cli, *argv, budget):
    t = time.perf_counter()
    r = cli(*argv)
    dt = time.perf_counter() - t
    assert r.code == 0, (argv, r.err)
    assert dt < budget * SLACK, f"{' '.join(argv)} took {dt:.2f} s (budget {budget} s)"
    return r


def test_the_commands_of_a_task_loop_meet_their_budgets(big):
    cli = Cli(big)
    assert cli("claim", "DEMO-0500").code == 0
    for argv in (
        ["show"],
        ["status"],
        ["task", "list"],
        ["log", "a note"],
        ["ac", "add", "a criterion"],
        ["task", "next"],
    ):
        timed(cli, *argv, budget=0.4)
    timed(cli, "wait", "--timeout", "1", budget=2.0)  # the timeout plus one second
    timed(cli, "list", budget=0.8)
    timed(cli, "list", "--status", "open", "--limit", "20", budget=0.8)
    timed(cli, "inbox", budget=0.8)
    timed(cli, "next", budget=0.8)
    r = timed(cli, "search", "number 777", budget=1.5)
    assert "DEMO-0778" in r.out


def test_a_hidden_ticket_is_not_counted_at_scale(big):
    anon = Cli(big, grant=False)
    r = timed(anon, "list", "--limit", "5", budget=0.8)
    assert "50 or more not shown" in r.out
