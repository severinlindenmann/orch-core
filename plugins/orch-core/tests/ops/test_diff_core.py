"""The ``diff`` core on a real workspace, in direct mode (in-process reader) and host mode (reader children), and
host-mode observation through the reader producing the same ``branch.pushed`` as P1's git CLI."""

from __future__ import annotations

import subprocess

import pytest

from orch.gitread.client import HostGitReader, PinStore, ReaderPool
from orch.ops.caps import Capabilities
from orch.ops.diff_core import diff_result
from orch.ops.errors import OrchError
from tests.ops.test_task_ac import claimed

LINKS = 'links={"repos":["proj"],"branches":{"proj":"feat/x"}}'


@pytest.fixture
def linked(ws, cli):
    path = ws.repo()
    ws.git("branch", "main", "HEAD")
    (path / "impl.txt").write_text("fixed\n")
    ws.git("commit", "-qam", "fix")
    claimed(cli)
    assert cli("set", "1", LINKS).code == 0
    return path


def both_caps(ws):
    host = HostGitReader(ReaderPool(), PinStore())
    return Capabilities.direct({}), Capabilities.host((str(ws.root),), git=host).with_principal("agent-1")


def test_diff_core_in_both_modes_equals_git(ws, linked):
    want = subprocess.run(
        ["git", "-C", str(linked), "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "main", "feat/x"],
        capture_output=True,
        check=True,
    ).stdout.decode()
    s = ws.other()
    try:
        view = s.ticket("1")
        direct, host = both_caps(ws)
        host.git.reader.sync([linked])
        for caps in (direct, host):
            got = diff_result(caps, s, view)
            assert got["unified"] == want and got["truncated"] is False
            assert got["sources"] == [
                {"repo": "proj", "branch": "feat/x", "base_sha": ws.git("rev-parse", "main"),
                 "head_sha": ws.git("rev-parse", "feat/x"), "base_source": "local_ref", "base_moved": 0,
                 "commits": [{"sha": ws.git("rev-parse", "feat/x"), "subject": "fix"}], "commits_more": 0}
            ]  # fmt: skip
            assert got["files"] == [
                {"repo": "proj", "path": "impl.txt", "old_path": None, "status": "M", "add": 1, "del": 1,
                 "binary": False, "too_large": False}
            ]  # fmt: skip
        with pytest.raises(OrchError) as e:
            diff_result(direct, s, view, repo="other")
        assert e.value.code == "not_found"
    finally:
        s.close()


def test_host_observe_through_the_reader_equals_direct(ws, linked):
    _, host = both_caps(ws)
    s = ws.other()
    try:
        problems: list[str] = []
        host.git.reader.sync([linked])  # pins are the workspace service's job (H10a), never an observe's
        out = host.git.observe(s, ws.uid("1"), problems)  # reads the pinned repo in a child
        assert problems == [] and len(out) == 1
        assert out[0]["type"] == "branch.pushed" and out[0]["sha"] == ws.git("rev-parse", "feat/x")
        assert out[0]["repo_id"] == "local:proj"
        assert host.git.symbolic_head(linked) == "refs/heads/feat/x"
    finally:
        s.close()


def test_a_declared_remote_that_differs_is_a_mismatch_in_both(ws, linked):
    ws.git("remote", "add", "origin", "https://github.com/evil/proj.git")
    s = ws.store
    s.append(
        ws.person_event(
            ws.owner,
            "workspace",
            "settings.changed",
            set={"repos": {"proj": {"path": str(linked), "remote": "https://github.com/acme/proj"}}},
        ),
        log="workspace",
    )
    s2 = ws.other()
    try:
        view = s2.ticket("1")
        direct, host = both_caps(ws)
        for caps in (direct, host):
            if caps is host:
                caps.git.reader.sync([linked])
            with pytest.raises(OrchError) as e:
                diff_result(caps, s2, view)
            assert e.value.code == "repo.remote_mismatch"
        problems: list[str] = []
        assert host.git.observe(s2, view.uid, problems) == []
        assert any("repo.remote_mismatch" in p for p in problems)
    finally:
        s2.close()
