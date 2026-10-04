"""AI Factory: two checkouts sharing one config dir cannot both auto-approve past the child limit."""
import shutil
import subprocess
import sys

import pytest

from orch.core.ops import Ops

CODE = """
import sys, time
from pathlib import Path
from orch.core import epics
from orch.core.events import Actor
from orch.core.ops import Ops
from orch.core.workspace import Workspace
real = epics.mark_delegated
epics.mark_delegated = lambda *x: (time.sleep(1.5), real(*x))[1]
try:
    Ops(Workspace.open(Path('.')), Actor('agent', 'claude-code', 'cli', '7f3c9a21-0000')).epic_auto_approve(sys.argv[1])
    print('ok')
except Exception:
    print("no")
"""


@pytest.fixture
def fws(configure):
    return configure(factory={"enabled": True})


def _refine(ops, tid):
    ops.set_section(tid, "Requirements", "r")
    ops.set_section(tid, "Acceptance criteria", "- [ ] a")
    ops.set_section(tid, "Plan", "1. do it")


def test_two_checkouts_auto_approving_at_the_limit_admit_one(fws, human, agent, ws_root, tmp_path):
    from conftest import human_ops
    a = Ops(fws, agent)
    e = a.new("Epic", type="epic")
    a.set_section(e.id, "Requirements", "r")
    a.set_section(e.id, "Acceptance criteria", "- [ ] a")
    human_ops(fws, human).approve(e.id, "requirements", delegate={"factory": True, "max_children": 1})
    kids = []
    for n in range(2):
        c = a.new(f"k{n}", epic=e.id)
        _refine(a, c.id)
        kids.append(c.id)
    other = tmp_path / "copy"
    shutil.copytree(ws_root, other)  # same customer and prefix, same ORCH_STATE_DIR: one ledger, one marker dir
    procs = [subprocess.Popen([sys.executable, "-c", CODE, k], cwd=d, stdout=subprocess.PIPE, text=True)
             for k, d in zip(kids, (ws_root, other))]
    outs = [p.communicate(timeout=60)[0].strip() for p in procs]
    assert sorted(outs) == ["no", "ok"], outs
