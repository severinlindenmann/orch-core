"""H11c, terminal paths of the runner grant (§12.4.2): ``orch grant --runner`` deposits instead of printing, and
``orch sign CODE`` of a runner grant asks "[r] hand to the orch runner  [s] show ORCH_GRANT here"."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from orch.ops import human as ops_human
from orch.runner import enablement
from tests.host import human_helpers
from tests.host.human_helpers import KeyBackend
from tests.host.lifecycle_helpers import UID
from tests.host.test_hostclient import hcli, hosted
from tests.host.test_runner_socket import Runner

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")
lab = human_helpers.lab


@pytest.fixture
def world(lab, tmp_path):
    served = hosted(lab, runner_identity=lambda cred: cred.pid == os.getpid())
    rstate = tmp_path / "rs"
    rstate.mkdir(mode=0o700)
    runner = Runner(served, rstate)
    enablement.set_enabled(Path(lab.state), served.wid, True, UID, terminals=False, text="t")
    try:
        yield served, runner
    finally:
        runner.close()
        served.close()


def test_orch_grant_runner_hands_the_secret_to_the_runner_and_shows_nothing(world, lab, monkeypatch):
    served, runner = world
    owner = lab.ws.owner

    class Backend(KeyBackend):
        def public_key(self, key_id):
            return owner.sig_pub

    shown: list[str] = []
    monkeypatch.setattr(ops_human, "open_backend", lambda directory: Backend(owner))
    monkeypatch.setattr(ops_human, "review_prompt", lambda text: True)
    monkeypatch.setattr(ops_human, "show_secret", lambda text: shown.append(text))
    keys = ops_human.keys_dir(lab.state, served.wid)
    keys.mkdir(parents=True)
    (keys / ops_human.KEY_FILE).write_text("{}")
    r = hcli(lab, grant=False)("grant", "--runner", "--hours", "8")
    assert r.code == 0, r.out + r.err
    assert shown == [] and "ORCH_GRANT=gr_" not in r.out + r.err
    (held,) = runner.grants.report()
    assert held["grant"] in r.out and "is with your orch runner" in r.out
    tail = runner.grants.secret_for(served.wid, owner.ref, held["grant"]).partition(".")[2]
    assert tail not in r.out + r.err
    bad = hcli(lab, grant=False).j("grant", "--runner", "--hours", "13")
    assert (bad.code, bad.err_code) == (5, "invalid.input")
    bad = hcli(lab, grant=False).j("grant", "--runner", "--label", "mine")
    assert (bad.code, bad.err_code) == (5, "invalid.input")


def test_orch_grant_runner_needs_the_host(lab):
    from tests.ops.helpers import Cli

    r = Cli(lab.ws, grant=False).j("grant", "--runner")
    assert (r.code, r.err_code) == (5, "invalid.input") and "host" in r.doc["error"]["message"]
