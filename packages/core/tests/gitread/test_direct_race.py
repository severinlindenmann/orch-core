"""#565's probe, for every moved operation: a thread flips the repository's own ``.git/config`` between a benign
config and the same plus ``filter.x.clean=<marker program>``, with an untracked ``.gitattributes`` of ``* filter=x``
and stat-dirty tracked files (so git would hash them through the filter). Before #565 the gate approved the benign
config and ``git status`` started later, under the hostile one: the filter ran (Opus probe 5, within ~0.4 s).

Every direct-mode read now runs in orch's reader in-process, so the marker must run **0 times** under each of them.
The positive control is plain ``git status`` (fixed identity, rc 0) under the same flipper: the specific marker must
fire at least once, so the test would catch a read that still runs git.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from orch.gitread.errors import Unavailable
from orch.gitread.local import READER
from orch.store import observe

from .conftest import GIT

#: rounds of each operation while the flipper runs
ROUNDS = 40


def _setup(make_repo, tmp_path: Path):
    r = make_repo()
    for i in range(5):
        r.write(f"f{i}.txt", f"file {i}\n")
    r.write("d/g.txt", "g\n")
    r.commit("base")
    r.git("branch", "feat")
    r.git("remote", "add", "origin", "https://github.com/acme/x")
    for p in (*(r.path / f"f{i}.txt" for i in range(5)), r.path / "d" / "g.txt"):
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 7 * 10**9))  # stat-dirty: git must read the bytes
    (r.path / ".gitattributes").write_text("* filter=x\n")
    marker = tmp_path / "filter-ran"
    prog = tmp_path / "filter.sh"
    prog.write_text(f'#!/bin/sh\necho ran >> "{marker}"\nexec cat\n')
    prog.chmod(0o755)
    benign = (r.gitdir / "config").read_bytes()
    hostile = benign + f'[filter "x"]\n\tclean = {prog}\n'.encode()
    return r, marker, benign, hostile


class Flipper:
    """Rewrites ``.git/config`` in place, as fast as it can, benign <-> hostile, until stopped."""

    def __init__(self, config: Path, benign: bytes, hostile: bytes) -> None:
        self.config, self.benign, self.hostile = config, benign, hostile
        self.stop = threading.Event()
        self.flips = 0
        self.started = threading.Event()  # set once the first rewrite is done
        self.t = threading.Thread(target=self.run, daemon=True)

    def run(self) -> None:
        while not self.stop.is_set():
            for data in (self.benign, self.hostile):
                with open(self.config, "wb") as f:  # same file, same inode: an in-place rewrite
                    f.write(data)
                self.flips += 1
                self.started.set()

    def wait_for_flip(self, timeout: float = 10) -> None:
        """Block until at least one more rewrite has happened: the caller's next operation then provably starts
        after (and so overlaps) the flipper's activity, whatever the scheduler or the GIL does."""
        seen = self.flips
        deadline = time.monotonic() + timeout
        while self.flips == seen and time.monotonic() < deadline and self.t.is_alive():
            time.sleep(0.0005)
        assert self.flips > seen, "the flipper made no progress"

    def __enter__(self) -> Flipper:
        self.t.start()
        assert self.started.wait(10), "the flipper never started"  # no timing guess: wait for the first rewrite
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()
        self.t.join(5)
        with open(self.config, "wb") as f:
            f.write(self.benign)


def _ops(r):
    """Every moved direct-mode read (#565), by name: observe's functions (caps.Git, hostclient, runner, D58), the
    reader's own calls (orch worktree's revisions and clean check, observe.observe's look, the dev host's diff)."""
    p = r.path

    def quiet(fn):
        def call():
            try:
                fn()
            except Unavailable:
                pass  # a config read mid-rewrite is unknown: fine, the point is that nothing runs

        return call

    return {
        "observe.head": lambda: observe.head(p),
        "observe.symbolic_head": lambda: observe.symbolic_head(p),
        "observe.toplevel": lambda: observe.toplevel(p / "d"),
        "observe.dirty": lambda: observe.dirty(p),
        "observe.dirty_refusal": lambda: observe.dirty_refusal(p),
        "observe.refusal": lambda: observe.refusal(p),
        "observe.repo_identity": lambda: observe.repo_identity(p, "x"),
        "observe.look": quiet(lambda: observe._LOCAL.look(p, "x", "refs/heads/feat")),
        "worktree.rev": quiet(lambda: READER.commit(p, "refs/heads/feat")),
        "worktree.dirty": quiet(lambda: READER.dirty(p / "d")),
        "devhost.diff": quiet(lambda: READER.diff(p, branch="feat", default_branch=None)),
    }


OPS = sorted(_ops(type("R", (), {"path": Path("/")})()))


@pytest.mark.parametrize("op", OPS)
def test_a_moved_operation_never_runs_the_racing_filter(make_repo, tmp_path, op):
    r, marker, benign, hostile = _setup(make_repo, tmp_path)
    fn = _ops(r)[op]
    with Flipper(r.gitdir / "config", benign, hostile) as flip:
        for _ in range(ROUNDS):
            flip.wait_for_flip()  # every round runs against a config that was just rewritten
            fn()
        assert flip.flips >= ROUNDS
    assert not marker.exists(), f"{op} ran the filter {marker.read_text().count('ran')} times"


def test_positive_control_plain_git_status_runs_the_racing_filter(make_repo, tmp_path):
    """The control: under the same flipper, plain ``git status`` (fixed identity, no optional locks so the files stay
    stat-dirty) succeeds and runs this marker at least once."""
    r, marker, benign, hostile = _setup(make_repo, tmp_path)
    env = {**r.env, "GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t"}
    deadline = time.monotonic() + 20
    runs = ok = 0
    with Flipper(r.gitdir / "config", benign, hostile):
        while not marker.exists() and time.monotonic() < deadline:
            done = subprocess.run([GIT, "-c", "user.name=t", "-c", "user.email=t@t", "--no-optional-locks", "-C",
                                   str(r.path), "status", "--porcelain"], capture_output=True, env=env, check=False,
                                  stdin=subprocess.DEVNULL)  # fmt: skip
            runs += 1
            ok += done.returncode == 0
    assert ok >= 1, "plain git status never succeeded under the flipper"
    assert marker.exists() and marker.read_text().count("ran") >= 1, f"no fire in {runs} runs"
    # and with the flipper stopped on the benign config, a clean rc 0 status that runs nothing
    marker.unlink()
    done = subprocess.run([GIT, "--no-optional-locks", "-C", str(r.path), "status", "--porcelain"],
                          capture_output=True, env=env, check=False)  # fmt: skip
    assert done.returncode == 0 and not marker.exists()
