"""Linux run cgroups (D-P2-29): file-level behaviour with a fake tree; the real thing only where delegated."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from orch.runner import cgroup
from orch.runner.supervise import Supervisor
from tests.runner.helpers import spec


def test_tree_files(tmp_path):
    tree = cgroup.CgroupTree(tmp_path)
    path = tree.create("r_0ABC")
    assert path == tmp_path / "run-r_0ABC"
    (path / "cgroup.procs").write_text("12\n34\n")
    (path / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    (path / "cgroup.kill").write_text("")
    assert tree.procs(path) == [12, 34]
    assert tree.populated(path) is True
    assert tree.kill(path) is True and (path / "cgroup.kill").read_text() == "1"
    (path / "cgroup.events").write_text("populated 0\n")
    assert tree.populated(path) is False
    assert tree.populated(tmp_path / "gone") is None
    with pytest.raises(ValueError):
        tree.path("../escape")


def test_own_cgroup_parsing():
    assert cgroup.own_cgroup("0::/user.slice/user-1000.slice/user@1000.service/app.slice/orch-runner.service\n") == (
        "/user.slice/user-1000.slice/user@1000.service/app.slice/orch-runner.service"
    )
    assert cgroup.own_cgroup("1:name=systemd:/x\n") is None


def test_detect_is_none_off_linux_or_without_delegation(tmp_path):
    if not sys.platform.startswith("linux"):
        assert cgroup.detect() is None
    else:
        assert cgroup.detect(tmp_path) is None  # no cgroup.kill under a fake root


@pytest.mark.skipif(cgroup.detect() is None, reason="needs a delegated cgroup v2 (the runner's systemd user unit)")
def test_real_cgroup_stop_is_complete(tmp_path: Path):  # pragma: no cover - only under a delegated unit
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    sup = Supervisor(state, scan_interval=0.1)
    sup.open()
    try:
        code = "import os, time\nif os.fork() == 0:\n    os.setsid()\n    time.sleep(60)\ntime.sleep(60)\n"
        s = spec(tmp_path, [sys.executable, "-I", "-c", code])
        sup.spawn(s)
        assert sup.stop(s.run, grace=0.5)["cleanup"] == "complete"  # the escapee was inside the cgroup
    finally:
        sup.close()
