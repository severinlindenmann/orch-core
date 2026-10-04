import json
from pathlib import Path

import pytest

from orch.addons.manifest import load_manifest
from orch.testing import FakeRunner, ProviderContract
from orch_terminals import ARGV, SessionsProvider, count

ADDON = Path(__file__).resolve().parents[1]
MANIFEST = load_manifest(ADDON)
RECORDS = json.loads((Path(__file__).with_name("fixtures") / "list-sessions.json").read_text())


def test_counts_only_sessions_started_inside_the_workspace():
    assert count("/tmp/orch-demo\n/tmp/orch-demo/sub\n/elsewhere\n/tmp/orch-demo-2\n", "/tmp/orch-demo") == 2
    assert count("", "/tmp/orch-demo") == 0


def test_asks_orchs_own_tmux_server_only():
    assert ARGV[:3] == ["tmux", "-L", "orch"] and "list-sessions" in ARGV


def test_no_server_yet_is_zero_sessions_not_an_error(orch_workspace):
    runner = FakeRunner([{"argv": ARGV, "returncode": 1, "stdout": "", "stderr": "no server running"}])
    snap = SessionsProvider().fetch(orch_workspace.provider_context(MANIFEST, runner=runner), "workspace", None)
    assert snap.health != "error" and snap.items[0]["count"] == 0


class TestSessionsContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return SessionsProvider()

    @pytest.fixture
    def provider_ctx(self, orch_workspace):
        return orch_workspace.provider_context(MANIFEST, runner=FakeRunner(RECORDS, strict=False))
