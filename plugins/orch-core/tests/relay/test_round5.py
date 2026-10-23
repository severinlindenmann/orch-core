"""R2 round 5 (Opus delta 4 of 3d519c71): N4-1's invariant, enforced in one place and pinned structurally; N4-2's
pinned read age bound; the alarm-cap alarm (N4-3); orch-52's #499 (the socket Error's closed per-code ``detail``:
``ticket.key_not_next {next_key}``); tests for the delta's low survivors X05, X06/X07, X08, X24.

N4-1's invariant: **a fence or session error from any relay POST stops the worker; it is never counted as an item
failure.** :attr:`Bridge.post` wraps every poster (:class:`bridge._FenceStops`): a ``SessionError`` becomes
:class:`WorkerStop`, a ``BaseException`` that no ``except Exception`` of an item, a rid or an aux step catches; only
``BridgeLoop._guard`` does. The AST tests below fail when a future handler could swallow it."""

from __future__ import annotations

import ast
import secrets
import threading
import warnings
from pathlib import Path

import pytest

from orch.net.http import TransportError
from orch.relay import bridge as B
from orch.relay import rids
from orch.relay.bridge import Bridge, BridgeLoop, Item, WorkerStop
from orch.relay.retry import RelayRefused
from orch.relay.session import Fenced, RelayMisbehaving, SessionError
from orch.wsproto import wire

from .bridgekit import World, open_chunk
from .helpers import WS

RELAY_SRC = Path(B.__file__).resolve().parent


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def _rid(env: bytes) -> str:
    return wire.parse_header(env[: wire.HEADER_LEN]).rid.hex()


def _metas(w, chunks):
    return [open_chunk(c, w.wk, w.key.public_key)[1] for c in chunks]


def _loop(w):
    return BridgeLoop(w.bridge, session=None, conn=lambda: None, workspace_id=WS)  # type: ignore[arg-type]


class _Fence:
    """A poster whose every POST meets the session's end; counts the requests that reached it."""

    def __init__(self, exc: SessionError):
        self.exc, self.calls = exc, []

    def __call__(self, device, rid, envelope):
        self.calls.append(rid)
        raise self.exc


SESSION_ENDS = [Fenced("fenced: writes stopped"), SessionError("lease lost")]


def _aux(loop, what, fn):
    """One aux step as the aux thread runs it (inside the thread guard)."""
    loop._guard(lambda: loop._aux_step(what, fn))()  # noqa: SLF001


# -- N4-1: the three aux-thread sites of delta 4, each stops the worker at the first fenced POST --------------------


@pytest.mark.parametrize("exc", SESSION_ENDS)
def test_n4_1_expire_reads_stops_at_the_first_fenced_post(w, exc):
    for _ in range(3):
        assert w.bridge.receive(w.item(w.phone.request(w.wk, {"op": "whoami", "args": {}}))) is not None
    w.clock.ms += B.READ_ADMITTED_MS + 1
    fence = _Fence(exc)
    w.bridge.post = fence
    loop = _loop(w)
    _aux(loop, "sweep", w.bridge.sweep)
    assert len(fence.calls) == 1  # not 3: the step ended at the first fence
    assert loop.error is exc and loop._stop.is_set() and w.alarms == []  # noqa: SLF001 - at once, never an alarm


@pytest.mark.parametrize("exc", SESSION_ENDS)
def test_n4_1_retry_stuck_settle_stops_at_the_fenced_post(w, monkeypatch, exc):
    envs = [w.phone.request(w.wk, w.new_ticket(title=f"t{i}")) for i in range(2)]
    adms = [w.bridge.receive(w.item(e)) for e in envs]
    real = w.host.submit
    monkeypatch.setattr(w.host, "submit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    for a in adms:
        w.bridge.run(a)  # both stuck
    monkeypatch.setattr(w.host, "submit", real)
    fence = _Fence(exc)
    w.bridge.post = fence
    w.clock.ms += B.STUCK_BACKOFF_MS[0] + 1
    loop = _loop(w)
    _aux(loop, "retry", w.bridge.retry_stuck)
    assert len(fence.calls) == 1 and loop.error is exc and loop._stop.is_set()  # noqa: SLF001
    assert w.alarms == []


@pytest.mark.parametrize("exc", SESSION_ENDS)
def test_n4_1_rotate_stops_at_the_fenced_post(w, exc):
    envs = []
    for _ in range(2):
        w.post.script = {1: "retry"}  # chunk 0 posted, chunk 1 attempted, chunk 2 stored: an open answer
        env = w.phone.request(w.wk, {"op": "ticket.text", "args": {"size": 3 * B.CHUNK_ROOM}})
        w.send(env)
        envs.append(env)
    w.epochs.rotate()
    fence = _Fence(exc)
    w.bridge.post = fence
    loop = _loop(w)
    loop._guard(w.bridge.rotate)()  # noqa: SLF001 - R6's rotation on whichever thread signals it
    assert len(fence.calls) == 1 and loop.error is exc and loop._stop.is_set()  # noqa: SLF001
    assert w.alarms == []


@pytest.mark.parametrize("exc", SESSION_ENDS)
def test_n4_1_the_real_responder_and_job_paths_stop_too(w, exc):
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}})
    w.send(env)
    w.rids.set_cursor(1)
    w.clock.ms += B.REPLAY_EVERY_MS
    fence = _Fence(exc)
    w.bridge.post = fence
    loop = _loop(w)
    h = wire.parse_header(env[: wire.HEADER_LEN])
    for n in (7, 8):
        loop._queue.put(Item(n, "bridge", h.device.hex(), h.rid.hex(), env))  # noqa: SLF001
        loop._queued_bytes += len(env)  # noqa: SLF001
    loop._guard(loop._resp_main)()  # noqa: SLF001
    assert loop.error is exc and w.rids.cursor() == 1 and len(fence.calls) == 1  # item 7 not acknowledged
    env2 = w.phone.request(w.wk, w.new_ticket())
    adm = w.bridge.receive(w.item(env2))
    loop2 = _loop(w)
    w.bridge.post = fence2 = _Fence(exc)
    loop2._jobs.put(adm)  # noqa: SLF001
    t = threading.Thread(target=loop2._guard(loop2._job_main))  # noqa: SLF001
    t.start()
    t.join(5)
    assert not t.is_alive() and loop2.error is exc and len(fence2.calls) == 1
    rec = w.rids.get(w.phone.device, _rid(env2))
    assert rec.state == "answered" and [c.state for c in rec.chunks] == ["attempted"]  # T2 precedes the POST (J17)


def test_n4_1_after_a_fence_the_poster_sends_nothing_more(w):
    """Whatever caught the first WorkerStop, the same poster never makes a second request."""
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}})
    w.bridge.receive(w.item(env))
    fence = _Fence(Fenced("fenced"))
    w.bridge.post = fence
    with pytest.raises(WorkerStop) as first:
        w.bridge.post("d", "r", b"x")
    for _ in range(3):
        with pytest.raises(WorkerStop) as again:
            w.bridge.post("d", "r", b"x")
        assert again.value.cause is first.value.cause
    assert fence.calls == ["r"]


def test_n4_1_every_poster_is_wrapped_constructor_and_assignment(w):
    assert isinstance(w.bridge.post, B._FenceStops)  # the kit's poster, passed to the constructor
    w.bridge.post = lambda d, r, e: "posted"
    assert isinstance(w.bridge.post, B._FenceStops) and w.bridge.post("d", "r", b"") == "posted"
    once = w.bridge.post
    w.bridge.post = once
    assert w.bridge.post is once  # never wrapped twice


def test_n4_1_a_poster_error_that_is_not_the_session_stays_an_item_failure(w):
    """Only the session's end is WorkerStop: anything else a poster raises is still one item's failure (D2-1)."""
    for _ in range(2):
        w.bridge.receive(w.item(w.phone.request(w.wk, {"op": "whoami", "args": {}})))
    w.clock.ms += B.READ_ADMITTED_MS + 1
    calls = []

    def boom(device, rid, envelope):
        calls.append(rid)
        raise RuntimeError("poster bug")

    w.bridge.post = boom
    loop = _loop(w)
    _aux(loop, "sweep", w.bridge.sweep)
    assert len(calls) == 2 and loop.error is None and not loop._stop.is_set()  # noqa: SLF001


def test_n4_1_workerstop_is_no_exception_and_the_session_errors_are_not_transport_errors():
    assert issubclass(WorkerStop, BaseException) and not issubclass(WorkerStop, Exception)
    for cls in (Fenced, SessionError):
        assert not issubclass(cls, (TransportError, RelayRefused))  # the poster's own handlers never take them


# -- N4-1, structural: no handler in the relay package can swallow the worker's end -------------------------------


def _handlers(tree: ast.AST):
    """(handler, enclosing function names) for every ``except`` in ``tree``."""
    out = []

    def walk(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                walk(child, [*stack, child.name])
            else:
                if isinstance(child, ast.ExceptHandler):
                    out.append((child, tuple(stack)))
                walk(child, stack)

    walk(tree, [])
    return out


def _names(t: ast.expr | None) -> set[str]:
    if t is None:
        return {"<bare>"}
    if isinstance(t, ast.Tuple):
        return set().union(*(_names(e) for e in t.elts))
    if isinstance(t, ast.Name):
        return {t.id}
    if isinstance(t, ast.Attribute):
        return {t.attr}
    return {"<expr>"}


def _flow(stmts: list[ast.stmt]) -> list[ast.stmt]:
    """Every ``return``/``break``/``continue`` and every ``raise`` with an exception in ``stmts``, at any depth outside
    nested defs and classes (a ``break``/``continue`` of a loop nested inside counts too: conservative)."""
    out: list[ast.stmt] = []

    def walk(node: ast.AST) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            return
        if isinstance(node, ast.Return | ast.Break | ast.Continue) or (isinstance(node, ast.Raise) and node.exc):
            out.append(node)  # type: ignore[arg-type]
        for child in ast.iter_child_nodes(node):
            walk(child)

    for st in stmts:
        walk(st)
    return out


def _only_reraises(h: ast.ExceptHandler) -> bool:
    """The handler ends in a bare ``raise`` and cannot leave any other way: no ``return``/``break``/``continue`` and no
    ``raise X`` anywhere in it (a dead ``raise`` after a ``continue`` is not a re-raise: delta 5 Y15)."""
    last = h.body[-1]
    return isinstance(last, ast.Raise) and last.exc is None and not _flow(h.body)


ROOTS = {"BaseException", "WorkerStop"}


def _aliases(tree: ast.AST) -> set[str]:
    """Names bound (at any scope) to ``BaseException``/``WorkerStop`` or to a tuple holding one, transitively:
    ``E = BaseException; except E`` is a stopper too."""
    found = set(ROOTS)
    while True:
        more = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
                v = node.value
                if isinstance(v, ast.Name | ast.Attribute | ast.Tuple) and _names(v) & found:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    more |= set().union(*(_names(t) for t in targets)) - {"<expr>"}
            if isinstance(node, ast.NamedExpr) and _names(node.value) & found:
                more |= _names(node.target)
        if more <= found:
            return found
        found |= more


GUARD = ("BridgeLoop", "_guard", "run")  # the one handler that ends the worker
SESSION = {"Fenced", "SessionError", "NotHolder", "TakeoverRefused", "RelayMisbehaving"}


def _violations(src: str, name: str = "<src>") -> list[str]:
    """N4-1's structural pin over one module (round 6, F5-1 hardened):

    * a handler that can catch ``WorkerStop`` (``BaseException``, ``WorkerStop``, an alias of either, bare, or a
      computed type) only re-raises (:func:`_only_reraises`), unless it is ``BridgeLoop._guard``'s;
    * no ``return``/``break``/``continue`` in any ``finally`` (it would drop an exception in flight);
    * no ``suppress(...)`` of a stopper (a context manager that swallows it).
    """
    with warnings.catch_warnings():  # a snippet's "return in finally" is a SyntaxWarning on 3.14: the point
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(src)
    stoppers = _aliases(tree) | {"<bare>", "<expr>"}
    bad = [
        f"{name}:{h.lineno} handler in {'.'.join(where)}"
        for h, where in _handlers(tree)
        if _names(h.type) & stoppers and where != GUARD and not _only_reraises(h)
    ]
    for node in ast.walk(tree):
        if isinstance(node, ast.Try | ast.TryStar) and node.finalbody:
            bad += [f"{name}:{x.lineno} {type(x).__name__.lower()} in finally" for x in _flow(node.finalbody)
                    if not isinstance(x, ast.Raise)]  # fmt: skip
        if isinstance(node, ast.Call) and "suppress" in _names(node.func):
            if not node.args or set().union(*(_names(a) for a in node.args)) & stoppers:
                bad.append(f"{name}:{node.lineno} suppress of a stopper")
    return bad


@pytest.mark.parametrize("path", sorted(RELAY_SRC.glob("*.py")), ids=lambda p: p.name)
def test_n4_1_no_handler_can_swallow_workerstop(path):
    assert _violations(path.read_text(), path.name) == []


def test_n4_1_bridge_handlers_of_the_session_errors_only_reraise():
    """In bridge.py a handler naming a session error only re-raises (a takeover is never one item's failure); only
    the poster wrapper converts it, and only the guard ends the worker with it."""
    tree = ast.parse((RELAY_SRC / "bridge.py").read_text())
    allowed = {("_FenceStops", "__call__"), GUARD}
    bad = [
        f"bridge.py:{h.lineno} in {'.'.join(where)}"
        for h, where in _handlers(tree)
        if _names(h.type) & SESSION and where not in allowed and not _only_reraises(h)
    ]
    assert bad == []


def test_n4_1_the_poster_is_only_set_through_the_wrapping_property():
    """``Bridge.post`` is a property whose setter wraps; nothing in the relay package writes ``_post`` (or reaches
    the instance dict) around it."""
    prop = Bridge.__dict__["post"]
    assert isinstance(prop, property) and prop.fset is not None
    for path in RELAY_SRC.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "_post" and isinstance(node.ctx, ast.Store):
                fn = [
                    f.name
                    for f in ast.walk(tree)
                    if isinstance(f, ast.FunctionDef) and f.lineno <= node.lineno <= (f.end_lineno or 0)
                ]
                assert path.name == "bridge.py" and fn == ["post"], f"{path.name}:{node.lineno}"
        assert "__dict__" not in path.read_text() and "object.__setattr__" not in path.read_text(), path.name


BAD_SNIPPETS = {
    "pass": "def f():\n    try:\n        g()\n    except BaseException:\n        pass\n",
    "dead raise after continue (delta 5 Y15)": (
        "def f(xs):\n    for x in xs:\n        try:\n            g(x)\n        except BaseException:\n"
        "            if True:\n                continue\n            raise\n"
    ),
    "return in a stopper handler": (
        "def f():\n    try:\n        g()\n    except WorkerStop:\n"
        "        if h():\n            return None\n        raise\n"
    ),
    "break in a stopper handler": (
        "def f():\n    while True:\n        try:\n            g()\n"
        "        except:\n            break\n            raise\n"
    ),
    "converted to an Exception": (
        "def f():\n    try:\n        g()\n    except BaseException as e:\n        raise RuntimeError() from e\n"
    ),
    "alias of BaseException": "_Any = BaseException\ndef f():\n    try:\n        g()\n    except _Any:\n        pass\n",
    "tuple alias, transitive": (
        "_A = WorkerStop\n_T = (ValueError, _A)\ndef f():\n    try:\n        g()\n    except _T:\n        log()\n"
    ),
    "return in finally (delta 5 Y12)": "def f():\n    try:\n        g()\n    finally:\n        return 1\n",
    "continue in finally": (
        "def f(xs):\n    for x in xs:\n        try:\n            g(x)\n        finally:\n            continue\n"
    ),
    "suppress(BaseException)": (
        "import contextlib\ndef f():\n    with contextlib.suppress(BaseException):\n        g()\n"
    ),
}


@pytest.mark.parametrize("label", sorted(BAD_SNIPPETS))
def test_f5_1_the_structural_check_rejects_each_evasion(label):
    """The checker itself (round 6, F5-1): every intentionally bad snippet is rejected."""
    assert _violations(BAD_SNIPPETS[label]) != []


def test_f5_1_the_structural_check_accepts_cleanup_then_reraise():
    ok = (
        "def f(xs):\n    for x in xs:\n        try:\n            g(x)\n        except BaseException:\n"
        "            close()\n            raise\n        except Exception:\n            continue\n"
        "        finally:\n            release()\n"
    )
    assert _violations(ok) == []


# -- F5-1: the aux flush step meets a session error (the delta's R7, adopted) -----------------------------------------


@pytest.mark.parametrize("exc", [Fenced("fenced"), RelayMisbehaving("relay misbehaving")])
def test_f5_1_flush_stops_the_worker_at_the_first_session_error(w, exc):
    for _ in range(2):
        w.post.script = {0: "retry"}  # two rids with an attempted chunk: the aux flush posts them again
        w.send(w.phone.request(w.wk, {"op": "whoami", "args": {}}))
    w.clock.ms += 120_000  # past the POST backoff
    fence = _Fence(exc)
    w.bridge.post = fence
    loop = _loop(w)
    _aux(loop, "flush", w.bridge.flush)
    assert loop.error is exc and loop._stop.is_set() and len(fence.calls) == 1  # noqa: SLF001
    assert w.alarms == []


# -- N4-2: the read age bound is pinned (X06, X07) and the host budget honours it (X08) --------------------------


def test_n4_2_read_admitted_ms_is_pinned():
    assert B.READ_ADMITTED_MS == 960_000
    assert B.READ_ADMITTED_MS >= B.RID_RETENTION_MS + 60_000  # never inside the relay's 900 s deadline (X07)


def test_n4_2_a_read_inside_the_relays_deadline_is_never_closed(w):
    adm = w.bridge.receive(w.item(w.phone.request(w.wk, {"op": "whoami", "args": {}})))
    rid = adm.header.rid.hex()
    w.clock.ms += 959_999  # past 900 s, still inside 960 s (halving the bound would close it: X07)
    w.bridge.sweep()
    assert w.rids.get(w.phone.device, rid).state == "admitted"
    w.clock.ms += 2  # 960 001 ms: closed (doubling the bound would keep it: X06)
    w.bridge.sweep()
    assert w.rids.get(w.phone.device, rid).state == "answered"


def test_n4_2_the_host_wide_budget_ignores_reads_past_the_bound(w, monkeypatch):
    for _ in range(8):
        assert w.bridge.receive(w.item(w.phone.request(w.wk, {"op": "whoami", "args": {}}))) is not None
    monkeypatch.setattr(B, "DEVICE_CHUNK_BYTES", 1 << 40)  # only the host-wide budget can refuse
    monkeypatch.setattr(B, "CHUNK_BYTES", 4 << 20)
    monkeypatch.setattr(B, "SHARED_CHUNK_BYTES", 4 << 20)
    assert not w.bridge._bytes_ok(w.phone.device)  # noqa: SLF001 - 8 young reads hold 16 MiB host-wide
    w.clock.ms += B.READ_ADMITTED_MS + 1
    assert w.bridge._bytes_ok(w.phone.device)  # noqa: SLF001 - past the bound they hold nothing (X08)


# -- N4-3: the alarm cap raises one alarm of its own; X05 ----------------------------------------------------------


def test_n4_3_the_alarm_cap_raises_one_alarm_when_reached(w, monkeypatch):
    monkeypatch.setattr(B, "ALARM_CLASSES", 2)
    loop = _loop(w)
    loop._item_failed("flush", OSError())  # noqa: SLF001
    loop._item_failed("flush", ValueError())  # noqa: SLF001 - a second class in the SAME step alarms (X05)
    assert w.alarms == [("relay_item_failed", "flush: OSError"), ("relay_item_failed", "flush: ValueError")]
    loop._item_failed("run", MemoryError())  # noqa: SLF001 - past the cap: one "exhausted" alarm
    loop._item_failed("receive", KeyError())  # noqa: SLF001 - then silence
    loop._item_failed("flush", OSError())  # noqa: SLF001 - a known class: silence
    assert w.alarms[2:] == [("relay_alarm_classes_exhausted", "run: MemoryError")]


# -- X24: the tombstone over-cap drains in one sweep --------------------------------------------------------------


def test_x24_the_tombstone_over_cap_drains_in_one_sweep(w, monkeypatch):
    monkeypatch.setattr(rids, "SWEEP_BATCH", 8)
    monkeypatch.setattr(rids, "MAX_TOMBSTONES", 10)
    db, now = w.rids._db, w.clock.ms  # noqa: SLF001
    dev = secrets.token_hex(16)
    with w.rids._lock:  # noqa: SLF001
        db.execute("BEGIN")
        for i in range(40):
            db.execute(
                "INSERT INTO rid(device, rid, first_ms, req_digest, epoch, seq, state, principal, idem, kind)"
                " VALUES(?,?,?,'sha256:x',2,?,'tombstone',NULL,NULL,'refusal')",
                (dev, secrets.token_hex(16), now - i * 1000, i + 1),
            )
        db.execute("COMMIT")
        w.rids._load_counts()  # noqa: SLF001
    w.rids.sweep(now)
    assert db.execute("SELECT COUNT(*) FROM rid WHERE state='tombstone'").fetchone()[0] == 10


# -- #499 (orch-52): the socket Error's closed per-code detail --------------------------------------------------------


def test_499_a_stored_key_not_next_answers_its_next_key_from_error_detail(w):
    env = w.phone.request(w.wk, w.new_ticket())
    adm = w.bridge.receive(w.item(env))
    rec = w.rids.get(w.phone.device, adm.header.rid.hex())
    got = {"state": "failed", "error": {"code": "ticket.key_not_next", "detail": {"next_key": "DEMO-9"}}}
    w.bridge._answer_outcome(rec, w.bridge._header_of(rec), got)  # noqa: SLF001
    (meta,) = _metas(w, w.post.for_rid(_rid(env)))
    assert meta["refusal"] == "ticket.key_not_next" and meta["next_key"] == "DEMO-9"


def test_499_another_codes_detail_never_reaches_the_device(w):
    env = w.phone.request(w.wk, w.new_ticket())
    adm = w.bridge.receive(w.item(env))
    rec = w.rids.get(w.phone.device, adm.header.rid.hex())
    got = {"state": "failed", "error": {"code": "role.denied", "detail": {"next_key": "DEMO-9"}}}
    w.bridge._answer_outcome(rec, w.bridge._header_of(rec), got)  # noqa: SLF001
    (meta,) = _metas(w, w.post.for_rid(_rid(env)))
    assert meta["refusal"] == "role.denied" and "next_key" not in meta


def test_499_a_settled_key_not_next_carries_the_stored_next_key(w, monkeypatch):
    """End to end on the kit host: a stuck write is rolled forward, the host refuses the key and stores the Error
    with its detail; the relay answer is built from the stored outcome (``_settle``) and carries ``next_key``."""
    env = w.phone.request(w.wk, w.new_ticket(key="DEMO-0007"))
    adm = w.bridge.receive(w.item(env))
    real = w.host.submit
    monkeypatch.setattr(w.host, "submit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    w.bridge.run(adm)
    monkeypatch.setattr(w.host, "submit", real)
    w.clock.ms += B.STUCK_BACKOFF_MS[0] + 1
    w.bridge.retry_stuck()
    metas = _metas(w, w.post.for_rid(_rid(env)))
    assert metas and metas[-1]["refusal"] == "ticket.key_not_next" and metas[-1]["next_key"] == "DEMO-42"
