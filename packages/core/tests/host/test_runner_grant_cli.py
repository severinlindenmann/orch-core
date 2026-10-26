"""H11c, terminal paths of the runner grant (§12.4.2): ``orch grant --runner`` deposits instead of printing, and
``orch sign CODE`` of a runner grant asks "[r] hand to the orch runner  [s] show ORCH_GRANT here"."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from orch.host import handoff
from orch.host.control_client import new_idem
from orch.ops import human as ops_human
from orch.runner import enablement
from tests.host import human_helpers
from tests.host.human_helpers import KeyBackend, ok
from tests.host.lifecycle_helpers import UID
from tests.host.test_handoff import Shown, connect
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
    monkeypatch.setattr(ops_human, "review_prompt", lambda text, word: True)
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


def _sign(served, human, code, choose):
    shown = Shown()
    with connect(served) as conn:
        res = handoff.sign_code(
            conn.call, code, backend=human.backend, key_id="dk", workspace_id=served.wid, person=human.person.ref,
            device=human.person.device, workspace=lambda w: ("Acme", "DEMO") if w == served.wid else None,
            confirm=shown.confirm, show_secret=shown.secret, idem=new_idem(), choose=choose,
        )  # fmt: skip
    return res, shown


def test_orch_sign_hands_a_runner_grant_over_when_the_person_types_r(world, lab):
    served, runner = world
    human = human_helpers.Human(served)
    asked: list[str] = []
    p = human.prepare("grant", {"hours": 8, "runner": True}, handoff=True)
    res, shown = _sign(served, human, p["request"]["code"], lambda prompt: asked.append(prompt) or "r")
    assert asked == [handoff.DEPOSIT_PROMPT] and shown.secrets == []
    (held,) = runner.grants.report()
    assert any("is with your orch runner" in line for line in res["lines"]) and held["grant"] == res["data"]["grant"]
    assert ok(served.client().req("whoami", {}))["data"]["runner"]["grant"]["id"] == held["grant"]


def test_orch_sign_shows_it_on_s_and_never_asks_for_another_grant(world, lab):
    served, runner = world
    human = human_helpers.Human(served)
    asked: list[str] = []
    p = human.prepare("grant", {"hours": 8, "runner": True}, handoff=True)
    _, shown = _sign(served, human, p["request"]["code"], lambda prompt: asked.append(prompt) or "s")
    assert len(shown.secrets) == 1 and "ORCH_GRANT=gr_" in shown.secrets[0] and runner.grants.report() == []
    q = human.prepare("grant", {"hours": 1}, handoff=True)  # no runner label: shown, nothing asked
    asked.clear()
    _, shown = _sign(served, human, q["request"]["code"], lambda prompt: asked.append(prompt) or "r")
    assert asked == [] and len(shown.secrets) == 1
