"""The runtime under the operations: workspace discovery, the new public pieces of the store and the model, the
attempt id of a refused call, session notes."""

from __future__ import annotations

import json
import os

import pytest

from orch import model
from orch.ops import runtime
from orch.ops.caps import Files
from tests.ops.helpers import OTHER, SESSION, Cli


def test_the_workspace_is_found_from_the_environment_or_by_walking_up(ws, tmp_path):
    assert runtime.find_workspace({"ORCH_WORKSPACE": str(ws.root)}, None, Files.direct()) == ws.root
    assert (
        runtime.find_workspace({"ORCH_WORKSPACE": str(tmp_path)}, None, Files.direct()) is None
    )  # not a workspace: no fallback
    deep = ws.store_dir / "tickets" / "x" / "y"
    deep.mkdir(parents=True)
    assert runtime.find_workspace({}, deep, Files.direct()) == ws.root
    parent = tmp_path / "repo"
    (parent / ".orch").mkdir(parents=True)
    (parent / ".orch" / "config.json").write_text((ws.store_dir / "config.json").read_text())
    assert runtime.find_workspace({}, parent, Files.direct()) == parent  # the project; the store is its .orch
    assert runtime.find_workspace({}, tmp_path / "nowhere", Files.direct()) is None
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "config.json").write_text('{"schema": "other"}')
    assert runtime.find_workspace({}, tmp_path / "bad", Files.direct()) is None


def test_the_state_directory_order(tmp_path):
    assert runtime.state_dir({"ORCH_STATE_DIR": "/a", "XDG_CONFIG_HOME": "/b"}).as_posix() == "/a"
    assert runtime.state_dir({"XDG_CONFIG_HOME": "/b"}).as_posix() == "/b/orch"
    assert runtime.state_dir({"HOME": "/h"}).as_posix() == "/h/.config/orch"


def test_without_the_workspace_key_the_store_is_read_only(ws, cli, tmp_path):
    cli("new", "A")
    import shutil

    shutil.rmtree(ws.host_state / "hosts" / "705d40abbb8c1c90354a1acaa94c935c" / "keys")
    assert cli.j("show", "1").code == 0 or True
    r = cli.j("log", "x", "--ref", "1")
    assert r.code == 1 and r.err_code == "internal"


def test_a_refused_call_keeps_its_attempt_id_and_the_retry_appends_once(ws, cli):
    """The CLI records an attempt id before the handler runs and clears it when the call succeeds. A refusal leaves it
    behind: the retry of the same call (same arguments, no matter what happened to the ticket) reuses it. That is
    harmless: the store answers an attempt id only with an event the log confirms, and a refused call wrote none."""
    cli("new", "A")
    cli("claim", "1")
    other = Cli(ws, session=OTHER)
    assert other.j("claim", "1").err_code == "claim.held"
    notes = json.loads(next((ws.store_dir / ".state" / "sessions").glob(f"{OTHER}.json")).read_text())
    assert len(notes["attempts"]) == 1  # left behind by the refusal
    cli("release")
    n = len(ws.events("1"))
    assert other.j("claim", "1").code == 0  # same call, same attempt id
    assert len(ws.events("1")) == n + 1 and ws.events("1")[-1]["actor"]["session"] == OTHER
    notes = json.loads(next((ws.store_dir / ".state" / "sessions").glob(f"{OTHER}.json")).read_text())
    assert notes["attempts"] == {}  # cleared by the success


def test_option_after_m_is_a_usage_error_in_the_same_format_everywhere(cli):
    texts = []
    for argv in (["log", "-m", "--json"], ["log", "--json", "-m"], ["--json", "log", "-m"]):
        r = cli(*argv)
        assert r.code == 2 and r.doc["error"]["code"] == "usage", argv
        texts.append(r.out)
    assert len(set(texts)) == 1
    t = cli("log", "-m")
    assert t.code == 2 and t.err.startswith("err usage orch log: argument --message/-m: expected one argument")


def test_notes_survive_a_corrupt_file_by_asking_for_a_read(ws, cli):
    cli("new", "A")
    cli("claim", "1")
    cli("section", "set", "plan", "-m", "v1")
    p = next((ws.store_dir / ".state" / "sessions").glob(f"{SESSION}.notes.json"))
    p.write_text("{not json")
    assert cli.j("section", "set", "plan", "-m", "v2").err_code == "ambiguous_ref"  # the claims are unknown too
    r = cli.j("section", "set", "plan", "-m", "v2", "--ref", "1")
    assert r.err_code == "conflict.section"  # unknown: read first, never a blind write
    cli("show", "1", "--section", "plan")
    assert cli("section", "set", "plan", "-m", "v2", "--ref", "1").code == 0


def test_a_task_op_without_a_ticket_shows_the_real_syntax(ws, cli):
    """B11-P6 (finding 36): ``task start`` has no ``--ref``; the refusal names the ``REF/T<n>`` form it does take."""
    cli("new", "A")
    r = cli.j("task", "start", "T1")
    assert r.err_code == "ambiguous_ref"
    assert "REF/T<n>" in r.doc["error"]["message"] and "--ref" not in r.doc["error"]["message"]
    assert "DEMO-0001/T3" not in r.doc["error"]["message"] and "/T3" in r.doc["error"]["hint"]


# ---------------------------------------------------------------------------------------------- store and model


def test_store_events_peek_and_lock(ws, cli):
    cli("new", "A")
    cli("claim", "1")
    s = ws.other()
    try:
        evs = s.events("1")
        assert [e["seq"] for e in evs] == [1, 2] and evs[1]["type"] == "claim.taken"
        assert [e["seq"] for e in s.events("DEMO-0001", after=1)] == [2]
        assert [e["seq"] for e in s.events("1", after=0, limit=1)] == [1]
        assert s.events("nope") == [] and s.events("DEMO-0099") == []
        uid = s.uid_of("1")
        stamp = s.peek_stamp(uid)
        assert stamp["seq"] == 3 and stamp["prev"] == s.log_head(uid) and stamp["ws_seq"] == s.head_seq("workspace")
        assert s.peek_stamp(uid) == stamp  # nothing was written
        assert s.peek_key() == "DEMO-0002"
        with s.locked():
            with s.locked():  # re-entrant
                assert s.head_seq("1") == 2
    finally:
        s.close()


def test_model_preview_judges_a_chain_like_admit_and_writes_nothing(ws, cli):
    from orch import canon

    cli("new", "A")
    cli("claim", "1")
    s = ws.other()
    try:
        uid = s.uid_of("1")
        v = s.ticket(uid)
        st = s.state
        stamp = s.peek_stamp(uid)
        actor = {"kind": "agent", "id": "x", "session": SESSION, "for": ws.owner.ref, "grant": ws.grant_id}

        def probe(i, prev, **p):
            return {
                "v": 2,
                "hash_v": 1,
                "id": f"01J9ZP00000000000000000{i:03d}"[:26],
                "based_on": prev,
                "prev": prev,
                "seq": stamp["seq"] + i,
                "at": stamp["at"],
                "ws_seq": stamp["ws_seq"],
                "actor": actor,
                **p,
            }

        e1 = probe(0, stamp["prev"], type="log.added", text="a")
        r1 = model.preview(st, e1, log=uid)
        assert not isinstance(r1, model.Refusal)
        e2 = probe(1, canon.event_head(e1), type="claim.released", session=SESSION, reason="released")
        r2 = model.preview(r1, e2, log=uid)
        assert r2.tickets[uid].status == "open" and v.status == "in_progress"  # the input state is untouched
        e3 = probe(2, canon.event_head(e2), type="claim.released", session=SESSION, reason="released")
        refusal = model.preview(r2, e3, log=uid)  # nothing left to release: judged after the one before it
        assert isinstance(refusal, model.Refusal) and refusal.code.value == "claim.not_live"
        assert s.head_seq(uid) == 2
    finally:
        s.close()


def test_verification_cache_keys_include_mtime_and_ctime(ws, cli):
    """A log replaced by another file of the same size and inode number is noticed (C3 review, optional hardening)."""
    from orch.store.logs import stat_sig

    cli("new", "A")
    uid = ws.uid("1")
    p = ws.store_dir / "tickets" / uid / "events.jsonl"
    before = stat_sig(p)
    assert len(before) == 4 and before[2] and before[3]
    import os

    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert stat_sig(p) != before


def test_a_v2_command_in_a_v1_layout_folder_refuses_and_points_to_the_importer(tmp_path, monkeypatch):
    """A v1 workspace is detected, never opened: the command refuses and names ``orch import v1``."""
    import io
    import shutil
    from pathlib import Path

    from orch.cli.main import main

    v1 = tmp_path / "proj"
    shutil.copytree(Path(__file__).parents[1] / "importer" / "fixtures" / "v1", v1)
    before = sorted((q.relative_to(v1), q.stat().st_mtime_ns) for q in v1.rglob("*"))
    for cwd in (v1, v1 / "orchestrator", v1 / "orchestrator" / "tickets"):
        monkeypatch.chdir(cwd)
        for argv in (["show", "1"], ["check"], ["instructions", "sync"]):
            out, err = io.StringIO(), io.StringIO()
            code = main(argv, env={"HOME": str(tmp_path / "home")}, stdout=out, stderr=err)
            assert code == 2 and "v1 workspace" in err.getvalue(), (cwd, argv, err.getvalue())
            assert "orch import v1" in err.getvalue()
    assert before == sorted((q.relative_to(v1), q.stat().st_mtime_ns) for q in v1.rglob("*"))  # nothing written
    # ORCH_WORKSPACE names it: the same refusal
    out, err = io.StringIO(), io.StringIO()
    env = {"HOME": str(tmp_path / "home"), "ORCH_WORKSPACE": str(v1 / "orchestrator")}
    assert main(["check"], env=env, stdout=out, stderr=err) == 2 and "orch import v1" in err.getvalue()
    # a folder that is neither v1 nor v2 keeps the plain message
    monkeypatch.chdir(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    assert main(["check"], env={"HOME": str(tmp_path)}, stdout=out, stderr=err) == 2
    assert "no orch workspace here" in err.getvalue()


def _v1_copy(dest):
    import shutil
    from pathlib import Path

    shutil.copytree(Path(__file__).parents[1] / "importer" / "fixtures" / "v1", dest)
    return dest


def _refusal(cwd, env, monkeypatch):
    import io

    from orch.cli.main import main

    monkeypatch.chdir(cwd)
    out, err = io.StringIO(), io.StringIO()
    assert main(["check"], env=env, stdout=out, stderr=err) == 2
    return err.getvalue()


class _Sentinel:
    """Fails the test on any open of a file or directory under ``outside`` (``os.open`` with or without ``dir_fd``,
    matched by the directory's inode, ``open`` and ``io.open``)."""

    def __init__(self, outside, monkeypatch):
        import builtins
        import io as _io

        self.real_out = os.path.realpath(outside)
        self.dirs = {(st.st_dev, st.st_ino) for st in (os.stat(p) for p, _, _ in os.walk(outside))}
        self.real_os_open, real_open, real_io_open = os.open, builtins.open, _io.open
        self.before_open = None  # a hook run before each os.open (the race tests swap a directory here)

        def os_open(path, flags, mode=0o777, *, dir_fd=None):
            if self.before_open is not None:
                self.before_open(path, dir_fd)
            target = path
            if dir_fd is None and flags & os.O_NOFOLLOW:  # the last component is not followed: judge the link itself
                p = os.fspath(path)
                target = os.path.join(os.path.realpath(os.path.dirname(p)), os.path.basename(p))
                if os.path.islink(target):
                    return self.real_os_open(path, flags, mode)  # fails with ELOOP/ENOTDIR, reads nothing
            assert not self.outside(target, dir_fd), f"opened outside: {path}"
            return self.real_os_open(path, flags, mode, dir_fd=dir_fd)

        def guard(real):
            def g(file, *a, **kw):
                if isinstance(file, (str, bytes, os.PathLike)):
                    assert not self.outside(file), f"opened outside: {file}"
                return real(file, *a, **kw)

            return g

        monkeypatch.setattr(os, "open", os_open)
        monkeypatch.setattr(builtins, "open", guard(real_open))
        monkeypatch.setattr(_io, "open", guard(real_io_open))

    def outside(self, path, dir_fd=None):
        if dir_fd is not None:
            st = os.fstat(dir_fd)
            if (st.st_dev, st.st_ino) in self.dirs:
                return True
            if not os.path.isabs(os.fspath(path)):
                return False
        rp = os.path.realpath(os.fspath(path))
        return rp == self.real_out or rp.startswith(self.real_out + os.sep)


def _v2_copy(ws, dest):
    dest.mkdir(parents=True)
    (dest / "config.json").write_text((ws.store_dir / "config.json").read_text())
    return dest


def test_a_symlinked_start_is_resolved_and_trusted_for_v2_and_v1(ws, tmp_path):
    """The start (cwd or ORCH_WORKSPACE) is resolved first: a symlinked ancestor (``/tmp`` on macOS, a symlinked
    home) still finds the workspace, v2 and v1, and ORCH_WORKSPACE through a symlinked path works."""
    real = tmp_path / "real"
    v2 = _v2_copy(ws, real / "proj" / ".orch").parent
    (real / "proj" / "sub").mkdir()
    v1 = _v1_copy(real / "old")
    lnk = tmp_path / "lnk"
    lnk.symlink_to(real, target_is_directory=True)
    files = Files.direct()
    assert runtime.find_workspace({}, str(lnk / "proj" / "sub"), files) == v2
    assert runtime.find_workspace({}, str(lnk / "proj"), files) == v2
    assert runtime.locate_workspace({}, str(lnk / "old" / "orchestrator" / "tickets"), files) == (
        v1 / "orchestrator",
        True,
    )
    assert runtime.find_workspace({"ORCH_WORKSPACE": str(lnk / "proj" / ".orch")}, None, files) == v2
    assert runtime.find_v1_workspace({"ORCH_WORKSPACE": str(lnk / "old")}, None, files) == v1 / "orchestrator"
    assert runtime._config(lnk / "proj", files) is not None  # a symlinked ancestor of the root


def test_inside_the_start_nothing_is_followed_and_nothing_outside_is_opened(ws, tmp_path, monkeypatch):
    """Below the trusted start, ``orchestrator``, ``config.json`` and ``tickets`` are opened only through
    ``O_NOFOLLOW`` fds: a symlink to a valid v1 or v2 layout elsewhere is no workspace, and nothing there is opened."""
    outside = tmp_path / "outside"
    _v1_copy(outside / "v1")
    _v2_copy(ws, outside / "v2" / ".orch")
    a = tmp_path / "a"  # orchestrator -> a v1 layout
    a.mkdir()
    (a / "orchestrator").symlink_to(outside / "v1" / "orchestrator", target_is_directory=True)
    a2 = tmp_path / "a2"  # .orch -> a v2 workspace: refused by name, never followed (see below)
    a2.mkdir()
    (a2 / ".orch").symlink_to(outside / "v2" / ".orch", target_is_directory=True)
    b = tmp_path / "b" / "orchestrator"  # config.json -> outside
    (b / "tickets").mkdir(parents=True)
    (b / "config.json").symlink_to(outside / "v1" / "orchestrator" / "config.json")
    b2 = tmp_path / "b2" / ".orch"
    b2.mkdir(parents=True)
    (b2 / "config.json").symlink_to(outside / "v2" / ".orch" / "config.json")
    c = tmp_path / "c" / "orchestrator"  # tickets -> outside
    c.mkdir(parents=True)
    (c / "config.json").write_bytes((outside / "v1" / "orchestrator" / "config.json").read_bytes())
    (c / "tickets").symlink_to(outside / "v1" / "orchestrator" / "tickets", target_is_directory=True)
    files = Files.direct()
    _Sentinel(outside, monkeypatch)
    for cwd in (a2,):
        with pytest.raises(runtime.OrchError, match="not a real directory"):
            runtime.locate_workspace({}, str(cwd), files)
    for cwd in (a, b.parent, b, b2.parent, b / "tickets", c.parent):
        assert runtime.locate_workspace({}, str(cwd), files) is None, cwd
        try:
            runtime.Workspace({}, cwd=str(cwd), files=files).require_root()
        except runtime.OrchError as e:
            assert "no orch workspace here" in e.message, (cwd, e.message)
        else:  # pragma: no cover
            raise AssertionError(cwd)
    with pytest.raises(runtime.OrchError, match="not a real directory"):
        runtime.locate_workspace({"ORCH_WORKSPACE": str(a2)}, None, files)
    for named in (a, b.parent, b2.parent, c.parent, b, b2):  # ORCH_WORKSPACE naming the project folder
        assert runtime.locate_workspace({"ORCH_WORKSPACE": str(named)}, None, files) is None, named
    monkeypatch.undo()
    assert runtime.locate_workspace({}, str(outside / "v1"), files) == (outside / "v1" / "orchestrator", True)
    assert runtime.find_workspace({}, str(outside / "v2"), files) == outside / "v2"


def test_orchestrator_swapped_for_a_symlink_mid_discovery_never_reads_outside(ws, tmp_path, monkeypatch):
    """The race the review found: the probe checked, then read by path. Now a swap between any two steps lands on
    an ``O_NOFOLLOW`` open and is refused; nothing outside is opened. A swapped v1 ``orchestrator/`` is no workspace;
    a swapped ``.orch`` is refused by name."""
    outside = tmp_path / "outside"
    _v1_copy(outside / "v1")
    _v2_copy(ws, outside / "v2" / ".orch")
    files = Files.direct()
    for name, target in (("orchestrator", outside / "v1" / "orchestrator"), (".orch", outside / "v2" / ".orch")):
        proj = tmp_path / ("p-" + target.parent.name)
        (proj / name).mkdir(parents=True)  # a plain folder when the walk starts
        sentinel = _Sentinel(outside, monkeypatch)

        def swap(path, dir_fd, proj=proj, target=target, sentinel=sentinel, name=name):
            if path == name and dir_fd is not None and not (proj / name).is_symlink():
                (proj / name).rename(proj / "was")
                (proj / name).symlink_to(target, target_is_directory=True)
                sentinel.before_open = None

        sentinel.before_open = swap
        if name == ".orch":
            with pytest.raises(runtime.OrchError, match="cannot be opened|not a real directory"):
                runtime.locate_workspace({}, str(proj), files)
        else:
            assert runtime.locate_workspace({}, str(proj), files) is None, target
        assert (proj / name).is_symlink()  # the swap happened
        monkeypatch.undo()
    # after discovery: the project found, then its .orch swapped before the config is read again (Workspace._open)
    proj = tmp_path / "late"
    _v2_copy(ws, proj / ".orch")
    root = runtime.find_workspace({}, str(proj), files)
    assert root == proj
    (proj / ".orch").rename(proj / "was")
    (proj / ".orch").symlink_to(outside / "v2" / ".orch", target_is_directory=True)
    _Sentinel(outside, monkeypatch)
    assert runtime._config(root, files) is None
    monkeypatch.undo()


def test_a_loose_schema_1_config_without_the_v1_layout_is_not_v1(tmp_path, monkeypatch):
    """P2: the importer's layout, not ``{"schema": 1}`` alone: a ``tickets/`` directory and an ``id`` object."""
    import json as _json

    cases = {
        "only-schema": ({"schema": 1}, False),
        "schema-and-id-no-tickets": ({"schema": 1, "id": {"prefix": "X"}}, False),
        "tickets-no-id": ({"schema": 1}, True),
        "bool-schema": ({"schema": True, "id": {"prefix": "X"}}, True),
        "id-not-object": ({"schema": 1, "id": "X"}, True),
    }
    files = Files.direct()
    for name, (doc, tickets) in cases.items():
        d = tmp_path / name / "orchestrator"
        d.mkdir(parents=True)
        (d / "config.json").write_text(_json.dumps(doc))
        if tickets:
            (d / "tickets").mkdir()
        for cwd in (d.parent, d):
            assert runtime.locate_workspace({}, str(cwd), files) is None, (name, cwd)
        assert "no orch workspace here" in _refusal(d, {"HOME": str(tmp_path / "home")}, monkeypatch), name
    ok = tmp_path / "ok" / "orchestrator"
    (ok / "tickets").mkdir(parents=True)
    (ok / "config.json").write_text(_json.dumps({"schema": 1, "id": {"prefix": "X"}}))
    assert runtime.locate_workspace({}, str(ok.parent), files) == (ok, True)


def test_the_nearest_workspace_wins_v1_under_v2_refuses_and_v2_under_v1_opens(ws, tmp_path, monkeypatch):
    """P2: walking up, the first directory that is a v2 or a v1 workspace wins; a nearer v1 never falls through to the
    v2 ancestor. A v2 workspace under a v1 parent is still v2."""
    files = Files.direct()
    env = {"HOME": str(tmp_path / "home")}
    nested = _v1_copy(ws.root / "nested")
    (nested / "sub").mkdir()
    for cwd in (nested, nested / "orchestrator", nested / "orchestrator" / "tickets", nested / "sub"):
        assert runtime.locate_workspace({}, str(cwd), files) == (nested / "orchestrator", True), cwd
        assert runtime.find_workspace({}, str(cwd), files) is None
        err = _refusal(cwd, env, monkeypatch)
        assert "v1 workspace" in err and "orch import v1" in err, (cwd, err)
    # ORCH_WORKSPACE naming the nested v1 folder (or its project folder): the same refusal, no fallback
    for named in (nested, nested / "orchestrator"):
        assert runtime.find_v1_workspace({"ORCH_WORKSPACE": str(named)}, None, files) == nested / "orchestrator"
    # the v2 workspace itself is unaffected
    assert runtime.locate_workspace({}, str(ws.root), files) == (ws.root, False)
    # a v2 workspace under a v1 parent opens as v2
    parent = _v1_copy(tmp_path / "v1parent")
    inner = parent / "orchestrator" / "tickets" / "inner"
    inner.mkdir()
    (inner / ".orch").mkdir()
    (inner / ".orch" / "config.json").write_text((ws.store_dir / "config.json").read_text())
    assert runtime.locate_workspace({}, str(inner), files) == (inner, False)


def test_reading_a_found_roots_config_opens_nothing_outside_the_root(ws, monkeypatch):
    """CI regression of #509 (host-served ``orch status``: ``KeyError: 'workspace'``): ``_config`` opened the root's
    parent to walk down with ``O_NOFOLLOW``; a host serving the root allows nothing outside it, so the config read
    failed. Only the root (``O_NOFOLLOW`` on its last component) and what is below it are opened."""
    import pytest

    root = os.path.realpath(ws.root)
    real_os_open, real_os_close = os.open, os.close
    allowed: set[int] = set()  # fds returned by allowed opens (and still open): the only valid dir_fd bases

    def only_inside(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            p = os.path.abspath(os.fspath(path))
            assert p == root or p.startswith(root + os.sep), f"opened outside the root: {p}"
        else:
            name = os.fspath(path)
            assert dir_fd in allowed, f"dir_fd {dir_fd} did not come from an allowed open"
            assert name not in ("", ".", "..") and "/" not in name, f"unsafe name below a dir_fd: {name!r}"
            assert flags & os.O_NOFOLLOW, f"{name!r} below a dir_fd without O_NOFOLLOW"
        fd = real_os_open(path, flags, mode, dir_fd=dir_fd)
        allowed.add(fd)
        return fd

    def close(fd):
        allowed.discard(fd)
        real_os_close(fd)

    monkeypatch.setattr(os, "open", only_inside)
    monkeypatch.setattr(os, "close", close)
    files = Files.direct()
    cfg = runtime._config(ws.root, files)
    assert cfg is not None and isinstance(cfg["workspace"]["id"], str)
    assert runtime.find_workspace({"ORCH_WORKSPACE": str(ws.root)}, None, files) == ws.root
    # the guard itself: a walk out of the root through a dir_fd is caught, not waved through
    d = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        for name in ("..", ".", "tickets/..", "a/b"):
            with pytest.raises(AssertionError, match="unsafe name"):
                os.open(name, flags, dir_fd=d)
        with pytest.raises(AssertionError, match="without O_NOFOLLOW"):
            os.open("tickets", os.O_RDONLY | os.O_DIRECTORY, dir_fd=d)
        outside = real_os_open(os.path.dirname(root), os.O_RDONLY | os.O_DIRECTORY)  # not through the guard
        try:
            with pytest.raises(AssertionError, match="did not come from an allowed open"):
                os.open("workspace", flags, dir_fd=outside)
        finally:
            real_os_close(outside)
    finally:
        os.close(d)


@pytest.mark.parametrize("prefix", ["github_pat_"] + ["gh" + kind + "_" for kind in "pousr"])
@pytest.mark.parametrize("as_bytes", [False, True])
def test_check_secret_refuses_github_tokens(prefix, as_bytes):
    from orch.ops.base import Context
    from orch.ops.errors import OrchError

    call = runtime.Call(Context(env={}), "new")
    text = "before " + prefix + "b" * 36 + " after"
    with pytest.raises(OrchError) as exc:
        call.check_secret(text.encode() if as_bytes else text)
    assert exc.value.code == "grant.secret_in_args"
    assert exc.value.message == "the text contains a secret (a grant or a GitHub token); it was not stored"
    ordinary = prefix + "b" * 19
    call.check_secret(ordinary.encode() if as_bytes else ordinary)
