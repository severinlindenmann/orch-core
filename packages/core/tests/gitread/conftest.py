"""Fixture repositories built with the real git CLI in temp dirs, with no user or system config."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

GIT = shutil.which("git")


def git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "A",
        "GIT_AUTHOR_EMAIL": "a@example.org",
        "GIT_COMMITTER_NAME": "A",
        "GIT_COMMITTER_EMAIL": "a@example.org",
        "GIT_AUTHOR_DATE": "1700000000 +0000",
        "GIT_COMMITTER_DATE": "1700000000 +0000",
        "LC_ALL": "C",
    }


class Repo:
    """A working copy at ``path`` driven by the git CLI."""

    def __init__(self, path: Path, home: Path) -> None:
        self.path = path
        self.env = git_env(home)
        self._tick = 0

    def git(self, *args: str, check: bool = True, raw: bool = False) -> str | bytes:
        self._tick += 1
        env = dict(self.env)
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = f"{1700000000 + self._tick} +0000"
        done = subprocess.run(
            [GIT, "-C", str(self.path), *args], capture_output=True, env=env, check=False, stdin=subprocess.DEVNULL
        )
        if check and done.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {done.stderr.decode(errors='replace')}")
        return done.stdout if raw else done.stdout.decode().strip()

    def write(self, rel: str, data: bytes | str, mode: int | None = None) -> Path:
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_symlink():
            p.unlink()
        p.write_bytes(data.encode() if isinstance(data, str) else data)
        if mode is not None:
            p.chmod(mode)
        return p

    def commit(self, msg: str = "c") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg)
        return self.git("rev-parse", "HEAD")  # type: ignore[return-value]

    def sha(self, rev: str) -> str:
        return self.git("rev-parse", rev)  # type: ignore[return-value]

    @property
    def gitdir(self) -> Path:
        return self.path / ".git"


@pytest.fixture
def make_repo(tmp_path):
    base = tmp_path.resolve()
    home = base / "home"
    home.mkdir()

    def make(name: str = "repo", *, object_format: str = "sha1", branch: str = "main", extra: tuple = ()) -> Repo:
        path = base / name
        path.mkdir(parents=True)
        r = Repo(path, home)
        r.git("init", "-q", f"--object-format={object_format}", "-b", branch, *extra)
        return r

    return make


def git_version() -> tuple[int, ...]:
    out = subprocess.run([GIT, "version"], capture_output=True, text=True, check=True).stdout
    nums = out.split()[2].split(".")
    return tuple(int("".join(ch for ch in n if ch.isdigit()) or 0) for n in nums[:3])


def case_sensitive(r) -> None:
    r.git("config", "core.ignorecase", "false")


def untracked_by_git(r) -> set[str]:
    """Every path plain git lists as untracked or changed (``status --untracked-files=all``)."""
    out = subprocess.run([GIT, "--no-optional-locks", "-C", str(r.path), "status", "--porcelain=v1", "-z",
                          "--untracked-files=all"], capture_output=True, env=r.env, check=True,
                         stdin=subprocess.DEVNULL).stdout  # fmt: skip
    return {e[3:].decode() for e in out.split(b"\0") if e}


# -- Timing helpers: CPU time, never wall clock. A loaded machine stretches wall time but not the CPU a process uses,
# so these stay steady where `time.monotonic() - t < 2` flaked (load average ~286).


@contextmanager
def cpu_budget(seconds: float):
    """Fail if the block uses more than ``seconds`` of this process's CPU time (fixed-size inputs, where the old bug
    cost many times the budget)."""
    started = time.process_time()
    yield
    used = time.process_time() - started
    assert used < seconds, f"used {used:.2f} s of CPU, budget {seconds} s"


def assert_linear(run, sizes=(1, 2, 4), factor=2.5, repeats=3):
    """Assert ``run(n)`` takes about linear CPU time in n. Takes the best of ``repeats`` per size and requires
    time(largest) / time(smallest) < factor * (largest / smallest). With sizes 1x..4x a linear cost gives a ratio of
    about 4 (limit 10), a quadratic one 16, an exponential one far more: noise fits, a regression does not."""

    def best(n):
        runs = []
        for _ in range(repeats):
            started = time.process_time()
            run(n)
            runs.append(time.process_time() - started)
        return max(min(runs), 1e-4)  # the clock's resolution floor, so a tiny first run cannot inflate the ratio

    first, last = best(sizes[0]), best(sizes[-1])
    grown = sizes[-1] / sizes[0]
    assert last / first < factor * grown, f"x{grown:g} the input took x{last / first:.1f} the CPU time (not linear)"
