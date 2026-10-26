"""``waiting`` on a run (run-9 feedback 3.6): hook marker, else a quiet terminal; new output clears it."""

from __future__ import annotations

import time
from types import SimpleNamespace

from orch.host import runners
from orch.instructions import marker_doc, marker_path
from orch.layout import Layout
from orch.ops.runtime import records_dir


def _setup(tmp_path):
    layout = Layout.new_at(tmp_path)
    d = records_dir(layout)
    d.mkdir(parents=True)
    service = SimpleNamespace(store=SimpleNamespace(layout=layout))
    hub = SimpleNamespace(activity={})
    rec = {"run": "r_1", "kind": "agent", "state": "running", "ticket": "DEMO-1", "session": "s_a"}
    return service, hub, rec, d


def test_a_quiet_terminal_is_waiting_and_output_clears_it(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_TERMINAL_QUIET_SECONDS", "90")
    service, hub, rec, _ = _setup(tmp_path)
    assert runners.waiting_of(service, hub, rec) is None  # no output seen yet
    hub.activity["r_1"] = (10, time.time() - 30)
    assert runners.waiting_of(service, hub, rec) is None  # printed 30 s ago
    hub.activity["r_1"] = (10, time.time() - 100)
    got = runners.waiting_of(service, hub, rec)
    assert got is not None and got["source"] == "quiet"
    hub.activity["r_1"] = (20, time.time())
    assert runners.waiting_of(service, hub, rec) is None


def test_the_hook_marker_shows_at_once_and_new_output_clears_it(tmp_path):
    service, hub, rec, d = _setup(tmp_path)
    marker_path(d, "s_a").write_bytes(marker_doc("s_a", time.time() - 5))
    got = runners.waiting_of(service, hub, rec)
    assert got is not None and got["source"] == "hook"
    hub.activity["r_1"] = (5, time.time())  # output after the marker
    assert runners.waiting_of(service, hub, rec) is None


def test_only_a_live_agent_run_on_a_ticket_can_wait(tmp_path):
    service, hub, rec, d = _setup(tmp_path)
    marker_path(d, "s_a").write_bytes(marker_doc("s_a", time.time()))
    assert runners.waiting_of(service, hub, {**rec, "state": "exited"}) is None
    assert runners.waiting_of(service, hub, {**rec, "kind": "shell"}) is None
    assert runners.waiting_of(service, hub, {**rec, "ticket": None}) is None
