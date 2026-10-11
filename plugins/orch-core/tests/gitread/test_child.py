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
    """100 ``diff`` calls through ``Capabilities`` (25 principals): never more than 4 children, one per principal at
    a time; what does not fit the bounded queue is answered ``quota.host`` (Busy), never queued without bound."""
    from orch.gitread.errors import Busy
    from orch.ops.caps import Capabilities

    r = make_repo()
    r.write("a.txt", "1\n")
    r.commit()
    r.git("checkout", "-q", "-b", "feat")
    r.write("a.txt", "2\n")
    r.commit()
    pool = ReaderPool()
    reader = HostGitReader(pool, PinStore())
    reader.sync([r.path])
    caps = Capabilities.host((str(tmp_path.resolve()),), git=reader)
    live: list = []
    live_peak = [0]
    mu = threading.Lock()
    real_spawn = pool._spawn

    def counting_spawn():
        p = real_spawn()
        with mu:  # every child started so far that has not exited yet
            live.append(p)
            live[:] = [q for q in live if q.poll() is None]
            live_peak[0] = max(live_peak[0], len(live))
        return p

    pool._spawn = counting_spawn
    ok: list[int] = []
    busy: list[int] = []
    errors: list[BaseException] = []

    def one(i: int) -> None:
        mine = caps.with_principal(f"agent-{i % 25}")
        try:
            got = mine.git.diff(r.path, name="r", branch="feat", default_branch=None, remote=None)
            assert got["files"][0]["path"] == "a.txt"
            ok.append(i)
        except Busy:
            busy.append(i)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]
    assert len(ok) + len(busy) == 100 and len(ok) >= 64
    assert pool.active == 0 and pool.waiting == 0
    assert pool.peak == 4  # the burst filled the pool, and never more
    assert live_peak[0] <= 4


class _GatedPool(ReaderPool):
    """A pool whose "children" wait for a gate, so overlap is decided by the test, never by a sleep."""

    def __init__(self, **kw):
        super().__init__(spawn=lambda: None, **kw)
        self.gate = threading.Event()
        self.events: list[tuple[str, str]] = []
        self.started_ev = threading.Event()

    def _run(self, hello, req, deadline=None):
        with self._mu:
            self.events.append((req["who"], "start"))
        self.started_ev.set()
        self.gate.wait(10)
        with self._mu:
            self.events.append((req["who"], "end"))
        return {"ok": {}}


def test_one_child_per_principal_at_a_time():
    """Two requests of one principal never overlap; another principal's do."""
    pool = _GatedPool()
    ts = [threading.Thread(target=pool.call, args=(who, {}, {"who": who})) for who in ("a", "a", "b")]
    for t in ts:
        t.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:  # both principals have a child running, the second "a" waits
        with pool._mu:
            if pool.active == 2 and pool.waiting == 1:
                break
        time.sleep(0.01)
    assert (pool.active, pool.waiting) == (2, 1)
    pool.gate.set()
    for t in ts:
        t.join()
    a = [k for who, k in pool.events if who == "a"]
    assert a == ["start", "end", "start", "end"]  # serialised
    assert pool.peak == 2


def test_the_queue_is_bounded_per_principal_and_host_wide():
    from orch.gitread.errors import Busy

    pool = _GatedPool(queue_per_principal=2, queue_max=3)
    results: list[str] = []

    def call(who):
        try:
            pool.call(who, {}, {"who": who})
            results.append("ok")
        except Busy:
            results.append("busy")

    first = threading.Thread(target=call, args=("a",))
    first.start()
    pool.started_ev.wait(10)
    second = threading.Thread(target=call, args=("a",))  # a's second: queued (1 running + 1 waiting = 2)
    second.start()
    deadline = time.monotonic() + 10
    while pool.waiting < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    with pytest.raises(Busy):  # a's third: over its bound of 2
        pool.call("a", {}, {"who": "a"})
    pool.gate.set()
    for t in (first, second):
        t.join()
    assert results == ["ok", "ok"]


def test_the_deadline_starts_when_the_request_is_queued():
    """A request that waited for a slot gets only what is left of its 30 s, not a fresh 30 s."""
    from orch.gitread.errors import Busy

    pool = _GatedPool(max_children=1, wall=0.5)
    holder = threading.Thread(target=pool.call, args=("a", {}, {"who": "a"}))
    holder.start()
    pool.started_ev.wait(10)
    t = time.monotonic()
    with pytest.raises(Busy, match="within"):
        pool.call("b", {}, {"who": "b"})  # never gets the one slot before its deadline
    assert time.monotonic() - t < 2
    pool.gate.set()
    holder.join()


def test_a_principal_is_required_through_capabilities(make_repo, tmp_path):
    from orch.ops.caps import Capabilities, CapabilityDenied

    r = make_repo()
    r.write("a.txt", "1\n")
    r.commit()
    reader = HostGitReader(ReaderPool(), PinStore())
    reader.sync([r.path])
    caps = Capabilities.host((str(tmp_path.resolve()),), git=reader)
    with pytest.raises(CapabilityDenied, match="principal"):
        caps.git.head(r.path)
    seen = []
    real = reader.pool.call
    reader.pool.call = lambda principal, *a: (seen.append(principal), real(principal, *a))[1]
    assert caps.with_principal("person:ada").git.head(r.path) == r.sha("HEAD")
    assert seen == ["person:ada"]


def test_a_child_over_its_memory_limit_is_killed():
    """The host watches each child's resident memory (macOS has no RLIMIT_AS) and kills its group above the limit."""
    import sys as _sys

    if _sys.platform not in ("darwin", "linux"):
        pytest.skip("no RSS probe on this platform")

    def spawn():
        code = "import time\nx = []\nfor _ in range(40):\n"
        code += '    x.append(bytearray(b"x" * (8 << 20)))  # touched pages: resident\n    time.sleep(0.05)\n'
        code += "time.sleep(30)\n"
        return subprocess.Popen(
            [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True
        )

    pool = ReaderPool(spawn=spawn, rss_max=96 << 20, wall=20)
    t = time.monotonic()
    with pytest.raises(Unavailable, match="memory limit"):
        pool.call("p", {}, {})
    assert time.monotonic() - t < 10


@pytest.mark.parametrize(
    "bad",
    [
        {"head_sha": "zz" * 20},
        {"refs": {"refs/heads/main": "ab" * 32}},  # a sha256-length id in a sha1 answer
        {"refs": {"refs/heads/other": None}},  # not the ref asked for
        {"head_ref": "refs/tags/v1"},
        {"origin_urls": "x"},
        {"object_format": "md5"},
    ],
)
def test_a_malformed_child_answer_never_reaches_the_caller(bad):
    good = {
        "head_ref": "refs/heads/main",
        "head_sha": "ab" * 20,
        "head_error": None,
        "refs": {"refs/heads/main": "cd" * 20},
        "origin_urls": [],
        "object_format": "sha1",
    }

    class Fake(ReaderPool):
        def call(self, principal, hello, req):
            return {**good, **bad}

    reader = HostGitReader(Fake(spawn=lambda: None), PinStore())
    reader._pins = lambda path: type("P", (), {"to_json": lambda self: {}})()
    with pytest.raises(Unavailable):
        reader.observe("/x", ["refs/heads/main"])
    Fake.call = lambda self, principal, hello, req: good
    assert reader.observe("/x", ["refs/heads/main"])["head_sha"] == "ab" * 20
