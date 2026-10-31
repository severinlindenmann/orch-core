"""P1-B2: the hub opens its state dir only at the right format (``format.json``), holds the shared format lock while it
serves, and the three ``dataformat.py`` copies (hub, relay, publish) are one file."""

from __future__ import annotations

import fcntl
import io
import json
import os
import threading
import time
from pathlib import Path

import pytest
from format_helpers import write_format
from publish_helpers import FakeUpstream

from orch_hub import cli
from orch_hub import dataformat as DF
from orch_hub import serve as SV
from orch_hub.backup import hubop
from orch_hub.dataformat import DataFormatError, open_data
from orch_hub.keys import HubKeys

PACKAGES = Path(__file__).resolve().parents[2]
COPIES = (
    PACKAGES / "hub" / "src" / "orch_hub" / "dataformat.py",
    PACKAGES / "relay" / "src" / "orch_relay" / "dataformat.py",
    PACKAGES / "publish" / "pubsrv" / "dataformat.py",
)
EXCLUSIVE = fcntl.LOCK_EX | fcntl.LOCK_NB


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "data"
    write_format(d)
    return d


def code(path, current=1, env=None, kind="hub") -> str:
    with pytest.raises(DataFormatError) as e:
        open_data(path, kind, current, {} if env is None else env)
    return e.value.code


def lock_is_shared(path) -> bool:
    """True when somebody holds format.lock shared: an exclusive lock (what `orch update` takes) is refused."""
    try:
        fd = os.open(Path(path) / "format.lock", os.O_RDWR)
    except FileNotFoundError:  # refused before the lock file was even made
        return False
    try:
        fcntl.flock(fd, EXCLUSIVE)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def journal(tmp_path, doc) -> dict:
    p = tmp_path / "update.json"
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return {"ORCH_UPDATE_JOURNAL": str(p)}


# --- the happy path and the lock ---------------------------------------------------------------------------------------


def test_opens_at_format_1_and_holds_the_shared_lock_until_closed(data):
    h = open_data(data, "hub", 1, {})
    assert (h.kind, h.format) == ("hub", 1) and lock_is_shared(data)
    assert (data / "format.lock").stat().st_mode & 0o777 == 0o600
    h.close()
    h.close()  # twice is fine
    assert not lock_is_shared(data)
    with open_data(data, "hub", 1, {}) as again:  # shared locks stack: a CLI beside a running service
        with open_data(data, "hub", 1, {}):
            assert lock_is_shared(data)
        assert again.format == 1


def test_an_update_in_progress_holds_the_lock_exclusive_and_refuses_the_open(data):
    fd = os.open(data / "format.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, EXCLUSIVE)
    try:
        assert code(data) == "update.busy"
    finally:
        os.close(fd)
    open_data(data, "hub", 1, {}).close()


def test_the_os_environment_is_the_default_for_the_journal(data, tmp_path, monkeypatch):
    for k, v in journal(tmp_path, {"v": 1, "phase": "installed"}).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(DataFormatError) as e:
        open_data(data, "hub", 1)
    assert e.value.code == "update.interrupted"


# --- every refusal ---------------------------------------------------------------------------------------------------


def test_not_a_directory_is_format_missing(tmp_path):
    assert code(tmp_path / "nothing") == "format.missing"
    f = tmp_path / "file"
    f.write_text("x")
    assert code(f) == "format.missing"


def test_no_format_json_says_to_install_again(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    with pytest.raises(DataFormatError) as e:
        open_data(d, "hub", 1, {})
    assert e.value.code == "format.missing"
    assert str(e.value) == "this folder has no format.json: run `orch install hub` again"
    assert not lock_is_shared(d)  # a refused open does not keep the lock


@pytest.mark.parametrize(
    "raw",
    [
        b'{"orch": "relay", "format": 1}',  # another kind
        b'{"orch": "hub", "format": true}',  # a bool is not an int
        b'{"orch": "hub", "format": 1.0}',
        b'{"orch": "hub", "format": "1"}',
        b'{"orch": "hub", "format": 0}',
        b'{"orch": "hub", "format": 1, "extra": 1}',
        b'{"orch": "hub"}',
        b'{"format": 1}',
        b"[1]",
        b"not json",
        b"",
        b'{"orch": "hub", "format": 1}' + b" " * 5000,
    ],
)
def test_a_damaged_or_foreign_format_json_is_format_missing(tmp_path, raw):
    d = tmp_path / "d"
    write_format(d, raw=raw)
    assert code(d) == "format.missing"
    assert not lock_is_shared(d)


def test_older_and_newer(data):
    with pytest.raises(DataFormatError) as e:
        open_data(data, "hub", 2, {})  # the code is at 2, the data at 1
    assert e.value.code == "format.older"
    assert str(e.value) == "this data was made by an older orch: run `orch update`"
    write_format(data, fmt=3)
    with pytest.raises(DataFormatError) as e:
        open_data(data, "hub", 2, {})
    assert e.value.code == "format.newer" and "format 3" in str(e.value)
    assert not lock_is_shared(data)


def test_a_symlinked_format_json_or_lock_file_is_refused(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"orch": "hub", "format": 1}))
    d = tmp_path / "d"
    d.mkdir()
    (d / "format.json").symlink_to(real)
    assert code(d) == "format.missing"
    d2 = tmp_path / "d2"
    write_format(d2)
    (tmp_path / "elsewhere").write_text("")
    (d2 / "format.lock").symlink_to(tmp_path / "elsewhere")
    assert code(d2) == "format.missing"


def test_a_format_json_that_is_not_a_regular_file_is_refused(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "format.json").mkdir()
    assert code(d) == "format.missing"
    d2 = tmp_path / "d2"
    d2.mkdir()
    os.mkfifo(d2 / "format.json")
    assert code(d2) == "format.missing"  # no hang on a pipe


def test_a_pending_restore_refuses_before_anything_else(tmp_path):
    d = tmp_path / "hub"
    side = tmp_path / ".hub.restore"
    side.mkdir()
    (side / "record.json").write_text(json.dumps({"aside": str(tmp_path / "hub.aside")}))
    with pytest.raises(DataFormatError) as e:  # the folder itself is gone, as in the middle of a restore
        open_data(d, "hub", 1, {})
    assert e.value.code == "update.interrupted" and "your data is in 'hub.aside'" in str(e.value)
    write_format(d)
    assert code(d) == "update.interrupted"
    (side / "record.json").write_text("garbage")  # an unreadable record still refuses
    with pytest.raises(DataFormatError) as e:
        open_data(d, "hub", 1, {})
    assert e.value.code == "update.interrupted" and "see the folders beside it" in str(e.value)
    link = tmp_path / "link"
    link.symlink_to(d)  # a symlinked root: the sidecar sits beside the real folder
    assert code(link) == "update.interrupted"
    (side / "record.json").unlink()
    open_data(link, "hub", 1, {}).close()


# --- the update journal (ORCH_UPDATE_JOURNAL) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc",
    [
        {"v": 1, "phase": "stopped"},
        {"v": 1, "phase": "backed-up"},
        {"v": 1, "phase": "installed"},
        {"v": 1, "phase": "migrated:/some/root"},
        {"v": 1, "phase": "committed", "restoring": True},
        {"v": 1, "phase": "wat"},  # an unknown phase is damaged
        {"v": 2, "phase": "committed"},
        {"phase": "committed"},
        {"v": 1},
        [],
        "{broken",
        "",
    ],
)
def test_an_unfinished_or_damaged_journal_refuses(data, tmp_path, doc):
    env = journal(tmp_path, doc)
    with pytest.raises(DataFormatError) as e:
        open_data(data, "hub", 1, env)
    assert e.value.code == "update.interrupted"
    assert str(e.value) == "an update was interrupted: run `orch update` to finish or roll back"
    assert not lock_is_shared(data)


@pytest.mark.parametrize(
    "doc",
    [{"v": 1, "phase": "committed"}, {"v": 1, "phase": "committed", "restoring": False, "update_id": "ab"}],
)
def test_a_committed_journal_opens(data, tmp_path, doc):
    open_data(data, "hub", 1, journal(tmp_path, doc)).close()


def test_no_journal_variable_or_no_such_file_skips_the_check(data, tmp_path):
    open_data(data, "hub", 1, {}).close()
    open_data(data, "hub", 1, {"ORCH_UPDATE_JOURNAL": ""}).close()
    open_data(data, "hub", 1, {"ORCH_UPDATE_JOURNAL": str(tmp_path / "missing" / "update.json")}).close()


def test_a_journal_that_cannot_be_read_counts_as_unfinished(data, tmp_path):
    assert code(data, env={"ORCH_UPDATE_JOURNAL": str(tmp_path)}) == "update.interrupted"  # a directory


def test_the_journal_is_looked_at_again_under_the_lock(data, monkeypatch):
    seen = iter([False, True])  # clean before the lock, an update started meanwhile
    monkeypatch.setattr(DF, "_unfinished", lambda env: next(seen))
    assert code(data) == "update.interrupted"
    assert not lock_is_shared(data)


# --- the hub wiring ------------------------------------------------------------------------------------------------------


def test_hold_state_keeps_the_handle_and_gives_up_the_old_one(data, tmp_path):
    other = tmp_path / "other"
    write_format(other)
    try:
        h = hubop.hold_state(data)
        assert lock_is_shared(data) and h.format == hubop.DATA_FORMAT == 1 and hubop.DATA_KIND == "hub"
        hubop.hold_state(other)  # a second serve in this process (a test): the first is released
        assert not lock_is_shared(data) and lock_is_shared(other)
    finally:
        hubop.release_state()
    assert not lock_is_shared(other)


def serve_args(state):
    return (state, "127.0.0.1:0", "http://127.0.0.1:9", [])


@pytest.mark.raw_state
def test_serve_refuses_a_state_without_format_json_before_any_database(tmp_path):
    state = tmp_path / "hub"
    HubKeys.init(state)  # keys only: no format.json
    with pytest.raises(DataFormatError) as e:
        SV.serve(*serve_args(state), stop=threading.Event(), out=io.StringIO())
    assert e.value.code == "format.missing"
    assert not (state / "registry.db").exists() and not lock_is_shared(state)


def test_serve_refuses_older_data_and_the_command_says_to_run_orch_update(tmp_path, capsys, monkeypatch):
    state = tmp_path / "hub"
    HubKeys.init(state)  # format 1 on disk
    monkeypatch.setattr(hubop, "DATA_FORMAT", 2)  # this code is newer than the data
    rc = cli.main(["serve", "--state", str(state), "--listen", "127.0.0.1:0", "--publish-url", "http://x:1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "refused" in err and "run `orch update`" in err
    assert not (state / "registry.db").exists()


def test_serve_refuses_while_an_update_is_unfinished(tmp_path, monkeypatch):
    state = tmp_path / "hub"
    HubKeys.init(state)
    for k, v in journal(tmp_path, {"v": 1, "phase": "stopped"}).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(DataFormatError) as e:
        SV.serve(*serve_args(state), stop=threading.Event(), out=io.StringIO())
    assert e.value.code == "update.interrupted"


def test_serve_holds_the_shared_lock_while_it_serves_and_lets_go_when_it_stops(tmp_path):
    state = tmp_path / "hub"
    HubKeys.init(state, relay_url="http://127.0.0.1:9", real=False)
    up = FakeUpstream()
    stop, out, result = threading.Event(), io.StringIO(), []
    t = threading.Thread(
        target=lambda: result.append(
            SV.serve(state, "127.0.0.1:0", up.url, [], web_listen="127.0.0.1:0", stop=stop, out=out)
        ),
        daemon=True,
    )
    t.start()
    try:
        end = time.monotonic() + 15
        while "orch-hub ready" not in out.getvalue() and time.monotonic() < end:
            time.sleep(0.05)
        assert "orch-hub ready" in out.getvalue()
        assert lock_is_shared(state)  # `orch update` (exclusive) cannot take the data while the hub serves
    finally:
        stop.set()
        t.join(20)
        up.close()
    assert result == [0] and not lock_is_shared(state)


def test_serve_does_not_recover_a_swap_while_an_update_runs_or_is_unfinished(tmp_path, monkeypatch):
    from orch_hub.backup.restore import paths

    state = tmp_path / "hub"
    HubKeys.init(state)
    new, old = paths(state)
    new.mkdir()  # a leftover staged dir: recover would remove it
    fd = os.open(state / "format.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, EXCLUSIVE)  # `orch update` runs
    try:
        with pytest.raises(DataFormatError) as e:
            SV.serve(*serve_args(state), stop=threading.Event(), out=io.StringIO())
        assert e.value.code == "update.busy" and new.is_dir()  # refused BEFORE recover_swap touched anything
    finally:
        os.close(fd)
    new.rmdir()
    # a swap that stopped between its renames (the state dir is gone) and an update that stopped: nothing is renamed
    os.rename(state, old)
    for k, v in journal(tmp_path, {"v": 1, "phase": "migrated:/x"}).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(DataFormatError) as e:
        SV.serve(*serve_args(state), stop=threading.Event(), out=io.StringIO())
    assert e.value.code == "update.interrupted" and old.is_dir() and not state.exists()


# --- every admin command opens the state by the format rule -----------------------------------------------------------

WS = "ab" * 16
COMMANDS = [
    ["users", "list"],
    ["groups", "list"],
    ["groups", "add", "ops"],
    ["grants", "list"],
    ["channels", "list"],
    ["access", "check", "--user", "u", "--workspace", WS, "--action", "read"],
    ["spaces", "list"],
    ["keys", "card"],
    ["keys", "rotate-op"],
    ["relay", "register"],
    ["releases", "--publish-url", "http://127.0.0.1:9", "--json"],
]


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("argv", COMMANDS, ids=lambda a: " ".join(a[:2]))
def test_every_state_command_refuses_under_an_exclusive_lock_an_old_format_and_an_unfinished_update(
    tmp_path, capsys, monkeypatch, argv
):
    state = tmp_path / "hub"
    HubKeys.init(state)
    before = tree(state)
    cmd = [argv[0], "--state", str(state), *argv[1:]]
    fd = os.open(state / "format.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, EXCLUSIVE)  # an update runs
    try:
        assert cli.main(cmd) == 1
    finally:
        os.close(fd)
    assert "being updated" in capsys.readouterr().err
    j = journal(tmp_path, {"v": 1, "phase": "installed"})
    monkeypatch.setenv("ORCH_UPDATE_JOURNAL", j["ORCH_UPDATE_JOURNAL"])
    assert cli.main(cmd) == 1
    assert "run `orch update`" in capsys.readouterr().err
    monkeypatch.delenv("ORCH_UPDATE_JOURNAL")
    monkeypatch.setattr(hubop, "DATA_FORMAT", 2)  # the code is newer than the data
    assert cli.main(cmd) == 1
    assert "run `orch update`" in capsys.readouterr().err
    assert {k: v for k, v in tree(state).items() if k != "format.lock"} == {
        k: v for k, v in before.items() if k != "format.lock"
    }  # nothing was written


def test_a_state_command_holds_the_shared_lock_while_it_runs(tmp_path, monkeypatch):
    state = tmp_path / "hub"
    HubKeys.init(state)
    seen = []
    monkeypatch.setattr("orch_hub.access.cli.run", lambda args: seen.append(lock_is_shared(state)) or 0)
    assert cli.main(["users", "--state", str(state), "list"]) == 0
    assert seen == [True] and not lock_is_shared(state)  # and it lets go when it ends


def test_keys_init_opens_a_fresh_dir_but_refuses_a_formatted_one_under_an_update(tmp_path, capsys):
    fresh = tmp_path / "fresh"
    assert cli.main(["keys", "--state", str(fresh), "init"]) == 0  # no dir yet: nothing to lock
    installed = tmp_path / "installed"
    write_format(installed)  # `orch install hub` ran: format.json is there, the keys are not
    assert cli.main(["keys", "--state", str(installed), "init"]) == 0
    assert (installed / "meta.json").exists() and lock_is_shared(installed) is False
    locked = tmp_path / "locked"
    write_format(locked)
    fd = os.open(locked / "format.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, EXCLUSIVE)
    try:
        assert cli.main(["keys", "--state", str(locked), "init"]) == 1
    finally:
        os.close(fd)
    assert not (locked / "meta.json").exists() and "being updated" in capsys.readouterr().err


# --- the journal is readable by the service user; a fork does not keep the lock --------------------------------------------


def test_the_journal_in_a_private_install_dir_is_readable_by_the_user_the_parts_run_as(data, tmp_path):
    # `orch update` writes the journal as the one OS user of the install, and the parts run as that same user, so a journal
    # under install/ (0700) can be read here: a committed one opens, an unfinished one refuses for ITS phase, not for a
    # permission error. (A journal that cannot be read still refuses: fail closed.)
    install = tmp_path / "install"
    install.mkdir(mode=0o700)
    jf = install / "update.json"
    jf.write_text(json.dumps({"v": 1, "phase": "committed"}))
    jf.chmod(0o600)
    assert os.access(jf, os.R_OK) and DF._unfinished({"ORCH_UPDATE_JOURNAL": str(jf)}) is False
    open_data(data, "hub", 1, {"ORCH_UPDATE_JOURNAL": str(jf)}).close()
    if os.geteuid() != 0:  # root reads everything
        jf.chmod(0)
        assert DF._unfinished({"ORCH_UPDATE_JOURNAL": str(jf)}) is True


FORKER = """
import os, sys, time
from {module} import open_data
h = open_data(sys.argv[1], {kind!r}, 1, {{}})
pid = os.fork()
if pid == 0:
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 1)  # let go of the pipes the test reads
    os.dup2(null, 2)
    time.sleep(60)
    os._exit(0)
print(pid, flush=True)
os._exit(0)  # the service dies; the child is still there
"""


def test_a_forked_child_does_not_keep_the_lock_past_the_service(data):
    import signal
    import subprocess
    import sys

    script = FORKER.format(module="orch_hub.dataformat", kind="hub")
    cmd = [sys.executable, "-c", script, str(data)]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
    pid = int(out.stdout.split()[0])
    try:
        os.kill(pid, 0)  # the child lives
        assert not lock_is_shared(data)  # but the lock died with the service
    finally:
        os.kill(pid, signal.SIGKILL)


# --- one file in three packages ----------------------------------------------------------------------------------------


def test_the_three_dataformat_copies_are_byte_identical():
    here = COPIES[0].read_bytes()
    for other in COPIES[1:]:
        assert other.read_bytes() == here, f"{other} differs from {COPIES[0]}: copy the file, do not edit one"
