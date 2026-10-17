"""B11 P4: Finish setup in words and read-only, custody failure reasons, terminal progress and ending lines."""

from __future__ import annotations

import json

from orch.setup import facts as fx
from orch.setup.facts import catalog
from orch.setup.server import SetupServer

from .conftest import PROFILE
from .fakes import FakePorts
from .test_create import CLICK, run_ok
from .test_finish import fake_signer as _fs  # noqa: F401
from .test_finish import PROF, finish_flow, mkfacts

INFO = {a.id: a for a in catalog()}


def test_name_is_not_prefilled_from_the_login(tmp):
    from orch.setup.flow import SetupFlow

    from .conftest import make_env

    f = SetupFlow(make_env(tmp))
    f.launch(str(tmp.work))
    st = f.dispatch("GET", "state", {}, None)[1]
    assert st["suggested"]["name"] == "" and st["suggested"]["prefix"]


def test_finish_state_has_current_values_and_words_and_never_custody(tmp):
    f = finish_flow(
        tmp,
        FakePorts(),
        [
            mkfacts(
                addons=frozenset({"dashboard", "start-agent"}),
                grant_hours=72,
                grant_hours_set=True,
                runner_enabled=True,
            )
        ],
    )
    st = f.dispatch("GET", "state", {}, None)[1]
    assert st["current"]["agent_hours"] == 72 and st["current"]["addons"] == ["dashboard", "start-agent"]
    assert st["current"]["runner"] == {"enabled": True, "terminals": False} and st["current"]["method"] == "terminal"
    assert "custody" not in st["missing"] and "settings" not in st["missing"] and "runner" not in st["missing"]
    assert st["missing"] == ["addon:terminals", "addon:worktrees", "runner_grant"]
    assert st["missing_words"] == ["Terminals add-on", "Worktrees add-on", "Agent access for this Mac's runner"]
    assert st["labels"]["runner_grant"] == "Agent access for this Mac's runner"
    assert not any(w.startswith(("addon:", "runner_")) for w in st["missing_words"])


def test_finish_with_a_set_agent_time_asks_for_no_typed_word(tmp):
    box = [mkfacts(addons=frozenset({"dashboard"}), grant_hours=72, grant_hours_set=True)]
    f = finish_flow(tmp, FakePorts(), box)
    f.dispatch(
        "POST", "profile", {}, {**PROF, "addons": ["dashboard", "terminals"], "agent_hours": 72, "agent_grant": False}
    )
    rv = f.dispatch("POST", "review", {}, {})[1]
    assert rv["typed_word"] is None and [i["id"] for i in rv["items"]] == ["addon:terminals"]


def test_labels_read_in_words():
    assert fx.label("addon:dashboard", INFO) == "Dashboard add-on"
    assert fx.label("runner_grant", INFO) == "Agent access for this Mac's runner"
    assert fx.label("nonsense", INFO) == "A setup step"


def test_progress_rows_are_words(tmp):
    f, ports, p, rv = run_ok(tmp, PROFILE)
    labels = [s["label"] for s in p["steps"]]
    assert "Dashboard add-on" in labels and not any(":" in label for label in labels)


def test_the_terminal_prints_progress_lines(tmp):
    f, ports, p, rv = run_ok(tmp, PROFILE)
    shown = "".join(ports.terminal.shown)
    assert "orch setup: working on it" in shown and "done: Making your keys" in shown


def test_custody_failure_gives_the_reason_on_both_sides_and_explains_the_passphrase(tmp):
    ports = FakePorts()
    ports.fail_stage = 2
    f, ports, p, _ = run_ok(tmp, CLICK, ports=ports)
    msg = p["error"]["message"]
    assert "one-click could not be set up because" in msg and "uses the terminal for now" in msg
    shown = [t for t in ports.terminal.shown if ports.passphrase_seen in t]
    assert len(shown) == 1
    assert "one-click could not be set up because" in shown[0] and "uses the terminal for now" in shown[0]
    assert "Write it down" in shown[0]
    assert ports.passphrase_seen not in json.dumps(p)


class _Flow:
    busy = False
    finished = False
    phase = "cancelled"

    def __init__(self, outcome):
        self.outcome = outcome

    def end(self, why):
        pass


def test_only_a_finished_setup_says_finished(tmp):
    lines = []
    s = SetupServer(tmp.root / "s.sock", _Flow("paused"), notify=lines.append)
    for why in ("done", "cancel", "idle", "shutdown"):
        assert "paused; run orch setup again to continue" in s.ending_line(why)
    assert SetupServer(tmp.root / "t.sock", _Flow("done")).ending_line("done") == "setup finished"
    assert "(done)" not in s.ending_line("done")
