"""H7 parity of the parts other groups serve. Both waited as ``xfail(strict=True)`` for their group and lost the mark
when it merged.

* (#413, merged) ``verdict`` and ``show`` on a ticket with a linked repo need git in the served workspace and the
  clean-tree check (H10a ``tree.check``). ``approve code`` binds the same source list through the same check; its gate
  must be independent (D59), so it needs a second person, and in tier A one UID maps to one person: its parity is
  the H10a host tests' (a clean tree allows it, a dirty one refuses it) rather than a two-mode script here.
* (#417, merged) the ``diff`` op the dashboard's Changes tab reads, served and equal to git.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from orch.ops import human
from tests.host.dispatch_helpers import SESSION, Served, ok
from tests.host.lifecycle_helpers import Lab
from tests.host.test_golden_host import served
from tests.ops.helpers import Cli
from tests.ops.humans import HumanWs, make_ticket, to_testing
from tests.ops.test_golden import _direct, norm

LINKS = 'links={"repos":["proj"],"branches":{"proj":"feat/x"}}'


def _mask(text: str) -> str:
    text = norm(text)
    text = re.sub(r"\bp_[0-9a-f]{6}\b", "p_SHORT", text)  # each world draws its own owner key
    text = re.sub(r"\b[0-9a-f]{40}\b", "SHA", text)
    return re.sub(r"\b[0-9a-f]{7,12}\b", "HEX", text)


def _script(tmp: Path, mode, monkeypatch) -> list[tuple[int, str, str]]:
    """The same steps in either mode on a fresh workspace with a linked repo: to testing, then the verdict as the
    person, then ``show``."""
    ws = HumanWs(tmp)
    ws.bootstrap()
    repo = ws.repo()
    ws.store.close()
    ws.store = None
    monkeypatch.setattr(human, "open_backend", ws.backend)
    monkeypatch.setattr(human, "review_prompt", lambda text, expect: True)
    monkeypatch.chdir(repo)  # task done --run runs the verify command in the linked repo's working copy
    out = []
    with mode(ws) as extra:
        agent, me = Cli(ws, **extra), Cli(ws, grant=False, session=None, **extra)
        key = make_ticket(agent)
        assert agent("set", key, LINKS).code == 0
        to_testing(agent, me, ws.tmp, key)
        for cli, argv in ((me, ("verdict", "pass", "--ref", key)), (agent, ("show", key))):
            r = cli(*argv)
            out.append((r.code, _mask(r.out), _mask(r.err)))
    return out


def _fake_free(ws):  # host mode with the host's own git (no test reader): what production serves
    return served(ws, fake_git=False)


@pytest.mark.slow
def test_verdict_and_show_on_a_linked_repo_match_direct_mode(tmp_path, monkeypatch):
    real = Path(os.path.realpath(tmp_path))
    want = _script(real / "d", _direct, monkeypatch)
    assert [c for c, _, _ in want] == [0, 0], want
    got = _script(real / "h", _fake_free, monkeypatch)
    assert got == want


@pytest.fixture
def lab(tmp_path):
    value = Lab(tmp_path)
    yield value
    value.close()


def test_diff_is_served_for_a_registered_workspace_and_equals_git(lab):
    ws = lab.ws
    ws.store = ws.other()
    path = ws.repo()
    ws.store.close()
    ws.store = None
    ws.git("branch", "main", "HEAD")
    (path / "impl.txt").write_text("fixed\n")
    ws.git("commit", "-qam", "fix")
    s = Served(lab)
    try:
        _, key = s.ticket("Linked")
        c = s.client()
        for op, args in (("claim", {"ref": key}), ("set", {"ref": key, "pairs": [LINKS]})):
            ok(c.req(op, args, grant=s.grant, session=SESSION, idem=True))
        assert "diff" in c.welcome["ops"]
        got = ok(c.req("diff", {"ref": key}, grant=s.grant, session=SESSION))["data"]
        want = subprocess.run(
            ["git", "-C", str(path), "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "main", "feat/x"],
            capture_output=True, check=True,
        ).stdout.decode()  # fmt: skip
        assert got["unified"] == want and got["truncated"] is False
    finally:
        s.close()
