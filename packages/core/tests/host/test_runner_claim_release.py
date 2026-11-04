"""W31: when an agent run ends (stopped from the dock, exited, killed), the RUNNER releases that run's claim as the
agent, with that run's own grant and session, and the host records ``claim_released`` on the run."""

from __future__ import annotations

import os
import sys

import pytest

from orch.host.runners import new_run_id
from orch.runner import claimend
from orch.runner.grants import GrantStore
from tests.host import human_helpers
from tests.host.test_hostclient import hcli, hosted
from tests.host.test_runner_socket import Runner, payload, wait_for
from tests.ops.helpers import SESSION

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")
lab = human_helpers.lab

OTHER = "s_01J9ZP0000000000000000000T"
THIRD = "s_01J9ZP0000000000000000000V"
GRANT = "gr_" + "0" * 26
SECRET = GRANT + "." + "x" * 43


class Calls:
    """Stands in for the runner's ``orch`` child: records every call and runs it in-process as the agent whose
    ``ORCH_GRANT`` / ``ORCH_SESSION`` it was given (the lab's real agent grant stands behind the fake secret)."""

    def __init__(self, lab) -> None:
        self.lab, self.calls = lab, []

    def __call__(self, argv, env):
        self.calls.append((list(argv), dict(env)))
        assert env["ORCH_GRANT"] == SECRET
        r = hcli(self.lab)(*argv, session=env["ORCH_SESSION"])
        return r.code, r.out


@pytest.fixture
def world(lab, tmp_path):
    served = hosted(lab, runner_identity=lambda cred: cred.pid == os.getpid())
    rstate = tmp_path / "rs"
    rstate.mkdir(mode=0o700)
    runner = Runner(served, rstate)
    runner.link.claim_cli = Calls(lab)
    try:
        yield served, runner
    finally:
        runner.close()
        served.close()


def start(world, tmp_path, key, script, session=SESSION):
    served, runner = world
    run = new_run_id()
    runner.grants.put_run(
        {"run": run, "workspace_id": served.wid, "person": served.lab.owner, "grant": GRANT, "secret": SECRET,
         "until": "2099-01-01T00:00:00Z"}
    )  # fmt: skip
    pl = payload(tmp_path, ["/bin/sh", "-c", script])
    pl.update(kind="agent", grant=GRANT, session=session, person=served.lab.owner, run_grant=True,
              env_fixed={"ORCH_TICKET": key, "ORCH_WORKSPACE": str(served.lab.root)})  # fmt: skip
    pl["env_names"] = ["PATH", "HOME"]
    fields = {"kind": "agent", "person": served.lab.owner, "ticket": key, "harness": "codex", "run": run,
              "session": session}  # fmt: skip
    served.h.run(served.h.host.runners.spawn(served.wid, fields, pl))
    return run


def claim_of(lab, key):
    """The live claim of a ticket, or None (a released or lapsed claim shows as none)."""
    claim = hcli(lab).j("show", key).data["ticket"]["claim"]
    return claim if claim and claim["live"] else None


def two_tickets(lab):
    cli = hcli(lab)
    keys = []
    for title in ("one", "two"):
        keys.append(cli.j("new", title).doc["key"])
    return keys


def test_dock_stop_releases_the_claim_and_records_it(world, tmp_path, lab):
    served, runner = world
    one, _two = two_tickets(lab)
    assert hcli(lab)("claim", one).code == 0
    assert claim_of(lab, one)["session"] == SESSION
    run = start(world, tmp_path, one, "sleep 60")
    hub = served.h.host.runners
    served.h.run(hub.stop(run, 500))
    wait_for(lambda: hub.store.records[run].get("cleanup") == "complete")
    assert claim_of(lab, one) is None  # a new agent start is no longer blocked (launch.claimed)
    assert hub.store.records[run]["claim_released"] is True
    calls = runner.link.claim_cli.calls
    assert [c[0] for c in calls] == [["show", one, "--json"], ["release", one]]  # no agent-supplied text
    assert calls[1][1]["ORCH_SESSION"] == SESSION and "DYLD_INSERT_LIBRARIES" not in calls[1][1]
    assert runner.grants.secret_for_release(run) is None  # used once


def test_a_killed_agent_releases_the_claim(world, tmp_path, lab):
    served, _runner = world
    one, _two = two_tickets(lab)
    assert hcli(lab)("claim", one).code == 0
    run = start(world, tmp_path, one, "kill -9 $$")
    hub = served.h.host.runners
    wait_for(lambda: hub.store.records[run].get("claim_released"))
    assert claim_of(lab, one) is None


def test_an_agent_that_exits_by_itself_releases_the_claim(world, tmp_path, lab):
    served, _runner = world
    one, _two = two_tickets(lab)
    assert hcli(lab)("claim", one).code == 0
    run = start(world, tmp_path, one, "exit 0")
    hub = served.h.host.runners
    wait_for(lambda: hub.store.records[run].get("claim_released"))
    assert claim_of(lab, one) is None


def test_another_sessions_claims_are_untouched(world, tmp_path, lab):
    served, runner = world
    one, two = two_tickets(lab)
    assert hcli(lab)("claim", one).code == 0
    assert hcli(lab)("claim", two, session=OTHER).code == 0  # another session, another ticket
    run = start(world, tmp_path, one, "exit 0")
    hub = served.h.host.runners
    wait_for(lambda: hub.store.records[run].get("claim_released"))
    assert claim_of(lab, two)["session"] == OTHER


def test_a_claim_taken_over_by_another_session_is_left_alone(world, tmp_path, lab):
    served, runner = world
    one, _two = two_tickets(lab)
    assert hcli(lab)("claim", one, session=THIRD).code == 0  # the ticket is held by THIRD, the run is SESSION's
    run = start(world, tmp_path, one, "exit 0")
    hub = served.h.host.runners
    wait_for(lambda: hub.store.records[run].get("cleanup") == "complete")
    assert claim_of(lab, one)["session"] == THIRD
    assert "claim_released" not in hub.store.records[run]
    assert [c[0][0] for c in runner.link.claim_cli.calls] == ["show"]  # looked, did not release


def test_a_run_whose_grant_was_revoked_attempts_no_release(world, tmp_path, lab, caplog):
    served, runner = world
    one, _two = two_tickets(lab)
    assert hcli(lab)("claim", one).code == 0
    run = start(world, tmp_path, one, "sleep 60")
    assert runner.grants.drop(GRANT)  # what the host's grant.drop does
    hub = served.h.host.runners
    served.h.run(hub.stop(run, 500))
    wait_for(lambda: hub.store.records[run].get("cleanup") == "complete")
    assert runner.link.claim_cli.calls == []
    assert "claim_released" not in hub.store.records[run]
    assert claim_of(lab, one)["session"] == SESSION
    assert SECRET not in caplog.text


def test_the_default_child_gets_a_cleaned_environment(monkeypatch):
    import subprocess

    seen = {}

    def fake(argv, **kw):
        seen["argv"], seen["env"], seen["cwd"] = argv, kw["env"], kw.get("cwd")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(subprocess, "run", fake)
    claimend.run_cli(
        ["release", "A-1"], {"PATH": "/usr/bin", "PYTHONPATH": "rel", "LD_PRELOAD": "/x", "ORCH_GRANT": "g"}
    )
    assert seen["argv"][-2:] == ["release", "A-1"]
    assert "PYTHONPATH" not in seen["env"] and "LD_PRELOAD" not in seen["env"] and seen["env"]["ORCH_GRANT"] == "g"
    # Opus W31: the child never imports from the current folder (a planted orch/__main__.py) and runs from "/"
    assert seen["argv"][1:4] == ["-P", "-m", "orch"] and seen["cwd"] == "/"


def test_the_store_keeps_a_run_secret_for_release_once_and_forgets_it_on_drop():
    store = GrantStore(clock=lambda: 0)
    run = "r_" + "0" * 26
    args = {"run": run, "workspace_id": "a" * 32, "person": "p_" + "b" * 32, "grant": GRANT, "secret": SECRET,
            "until": "2030-01-01T00:00:00Z"}  # fmt: skip
    store.put_run(args)
    store.secret_for_run(run, GRANT)
    assert SECRET in store.secrets()
    assert store.secret_for_release(run) == SECRET and store.secret_for_release(run) is None
    store.put_run(args)
    store.secret_for_run(run, GRANT)
    store.drop(GRANT)
    assert store.secret_for_release(run) is None and store.secrets() == []
