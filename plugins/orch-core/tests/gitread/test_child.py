"""The reader child through the H3a launcher, the pool of at most four children (F10) and the pin store."""

from __future__ import annotations

import base64
import json
import stat
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from orch.gitread.client import HostGitReader, PinStore, ReaderPool
from orch.gitread.errors import Unavailable

from .test_diff import GIT_DIFF, build_fixture


@pytest.fixture
def host(tmp_path):
    pool = ReaderPool()
    return HostGitReader(pool, PinStore(tmp_path.resolve() / "git-pins.json"))


def test_child_pins_observes_and_diffs_like_git(make_repo, host, tmp_path):
    r = make_repo()
    base, head = build_fixture(r)
    assert host.sync([r.path]) == {str(r.path): None}
    pin_file = tmp_path.resolve() / "git-pins.json"
    assert stat.S_IMODE(pin_file.stat().st_mode) == 0o600
    pins = json.loads(pin_file.read_text())["repos"][str(r.path)]
    assert pins["gitdir"]["path"] == str(r.gitdir) and pins["gitdir"]["ino"] == r.gitdir.stat().st_ino
    got = host.observe(r.path, ["refs/heads/main", "refs/heads/feat", "refs/heads/nope"])
    assert got["head_ref"] == "refs/heads/feat" and got["head_sha"] == head
    assert got["refs"] == {"refs/heads/main": base, "refs/heads/feat": head, "refs/heads/nope": None}
    d = host.diff(r.path, branch="feat", default_branch=None)
    assert base64.b64decode(d["patch"]) == r.git(*GIT_DIFF, base, head, raw=True)
    # a fresh PinStore reads the same pins back
    again = HostGitReader(host.pool, PinStore(pin_file))
    assert again.observe(r.path)["head_sha"] == head


def test_an_unpinned_path_is_unavailable_and_sync_never_repins(make_repo, host):
    r = make_repo()
    r.write("a", "1\n")
    r.commit()
    with pytest.raises(Unavailable, match="not pinned"):
        host.observe(r.path)
    host.sync([r.path])
    before = host.pins.get(str(r.path))
    host.sync([r.path])
    assert host.pins.get(str(r.path)) is before
    host.sync([])  # the repo left settings.repos: its pin goes
    assert host.pins.get(str(r.path)) is None


def test_a_child_that_overruns_the_wall_clock_is_killed():
    class Slow:
        pid = 0

    def spawn():
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            start_new_session=True,
        )

    pool = ReaderPool(spawn=spawn, wall=0.5)
    t = time.monotonic()
    with pytest.raises(Unavailable, match="longer than"):
        pool.call("p", {}, {})
    assert time.monotonic() - t < 5


def test_the_child_alarm_ends_a_request_that_overruns(tmp_path):
    """The wall clock is enforced inside the child too (SIGALRM), not only by the host."""
    script = textwrap.dedent(
        """
        import io, sys, time
        from orch.gitread import child, limits, ops
        limits.WALL_SECONDS = 1
        ops.run = lambda *a: time.sleep(30)
        sys.stdin = io.TextIOWrapper(io.BytesIO(b'{"profile":"pin","roots":["/x"]}\\n{"op":"pin"}\\n'))
        child.main()
        """
    )
    t = time.monotonic()
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60, check=False)
    assert time.monotonic() - t < 10
    assert done.returncode != 0 and done.stdout == b""


def test_limits_are_set_before_any_repository_byte_is_read(make_repo, tmp_path):
    """The child's rlimits are in place when the first pinned directory is opened."""
    r = make_repo()
    r.write("a", "1\n")
    r.commit()
    script = textwrap.dedent(
        """
        import io, json, resource, sys
        from orch.gitread import child, fs
        seen = []
        real = fs.open_walk
        def spy(path):
            seen.append((resource.getrlimit(resource.RLIMIT_CPU)[0], resource.getrlimit(resource.RLIMIT_NOFILE)[0]))
            return real(path)
        fs.open_walk = spy
        import orch.gitread.pinning as pinning
        pinning.open_walk = spy
        req = json.dumps({"profile": "pin", "roots": [sys.argv[1]]}) + "\\n"
        req += json.dumps({"op": "pin", "args": {"path": sys.argv[1]}}) + "\\n"
        out = io.BytesIO()
        child.sandbox.apply_limits()
        child.serve(io.BytesIO(req.encode()), out)
        print(json.dumps({"seen": seen, "resp": json.loads(out.getvalue())}))
        """
    )
    done = subprocess.run([sys.executable, "-c", script, str(r.path)], capture_output=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    res = json.loads(done.stdout)
    assert "ok" in res["resp"] and res["seen"]
    assert all(cpu == 10 and nofile <= 64 for cpu, nofile in res["seen"])


@pytest.mark.slow
def test_at_most_four_children_under_a_burst_of_100_diffs(make_repo, tmp_path):
    r = make_repo()
    r.write("a.txt", "1\n")
    r.commit()
    r.git("checkout", "-q", "-b", "feat")
    r.write("a.txt", "2\n")
    r.commit()
    pool = ReaderPool()
    reader = HostGitReader(pool, PinStore())
    reader.sync([r.path])
    live: list = []
    live_peak = [0]
    mu = threading.Lock()
    real_spawn = pool._spawn

    def counting_spawn():
        p = real_spawn()
        with mu:  # every child started so far that has not exited yet (poll() through its own Popen)
            live.append(p)
            live[:] = [q for q in live if q.poll() is None]
            live_peak[0] = max(live_peak[0], len(live))
        return p

    pool._spawn = counting_spawn
    errors: list[BaseException] = []

    def one(i: int) -> None:
        try:
            got = reader.diff(r.path, branch="feat", default_branch=None, principal=f"agent-{i % 10}")
            assert got["files"][0]["path"] == "a.txt"
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]
    assert pool.started == 101 and pool.active == 0  # 100 diffs and the pin at sync
    assert pool.peak == 4  # the burst filled the pool, and never more
    assert live_peak[0] <= 4


def test_one_child_per_principal_at_a_time():
    """Two requests of one principal never overlap; another principal's do."""
    events: list[tuple[str, str, float]] = []
    mu = threading.Lock()

    class FakePool(ReaderPool):
        def _run(self, hello, req):
            with mu:
                events.append((req["who"], "start", time.monotonic()))
            time.sleep(0.2)
            with mu:
                events.append((req["who"], "end", time.monotonic()))
            return {"ok": {}}

    pool = FakePool(spawn=lambda: None)
    ts = [threading.Thread(target=pool.call, args=(who, {}, {"who": who})) for who in ("a", "a", "b")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    a = sorted((t, kind) for who, kind, t in events if who == "a")
    assert [k for _, k in a] == ["start", "end", "start", "end"]  # serialised
    assert pool.peak == 2
