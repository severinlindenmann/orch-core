"""``orch update`` is a person's command: refused for an agent, with the catalog code and exit."""

from __future__ import annotations

import io

import pytest

from orch.cli import update as cli
from orch.ops.errors import ERRORS

from .world import World


@pytest.mark.parametrize("var", ["ORCH_GRANT", "ORCH_SESSION"])
@pytest.mark.parametrize("args", [[], ["--check"], ["--use", "2.0.0.dev41"], ["--channel", "develop", "--yes"]])
def test_agents_are_refused(tmp_path, var, args):
    w = World(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(args, env={**w.env, var: "x"}, out=out, err=err, seams=w.seams())
    assert rc == ERRORS["human_only"].exit
    assert w.services.events == []
    assert w.layout.current_version() == "2.0.0.dev41"


def test_usage_errors(tmp_path):
    w = World(tmp_path)
    for args in (["--bogus"], ["--use"], ["--finish", "--rollback"], ["--use", "2.0.0", "--check"]):
        err = io.StringIO()
        assert cli.main(args, env=w.env, out=io.StringIO(), err=err, seams=w.seams()) == 2
        assert "usage" in err.getvalue()


def test_a_channel_switch_needs_a_yes(tmp_path):
    w = World(tmp_path)
    err = io.StringIO()
    assert cli.main(["--channel", "develop"], env=w.env, out=io.StringIO(), err=err, seams=w.seams()) == 3
    assert w.services.events == []
