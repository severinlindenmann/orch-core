"""P1-B2: the relay opens its data dir only at the right format (``format.json``), holds the shared
format lock while it runs, and the three ``dataformat.py`` copies (hub, relay, publish) are one
file."""

import fcntl
import json
import os
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from format_helpers import write_format

from orch_relay import app as app_mod
from orch_relay import cli
from orch_relay import dataformat as DF
from orch_relay.dataformat import DataFormatError, open_data

PACKAGES = Path(__file__).resolve().parents[2]
COPIES = (
    PACKAGES / "hub" / "src" / "orch_hub" / "dataformat.py",
    PACKAGES / "relay" / "src" / "orch_relay" / "dataformat.py",
    PACKAGES / "publish" / "pubsrv" / "dataformat.py",
)
EXCLUSIVE = fcntl.LOCK_EX | fcntl.LOCK_NB
UPDATE_HINT = "run `orch update`"


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "data"
    write_format(d)
    return d


def code(path, current=1, env=None, kind="relay"):
    with pytest.raises(DataFormatError) as e:
        open_data(path, kind, current, {} if env is None else env)
    return e.value.code


def lock_is_shared(path):
    """True when somebody holds format.lock shared: an exclusive lock (`orch update`) is refused."""
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


def journal(tmp_path, doc):
    p = tmp_path / "update.json"
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return {"ORCH_UPDATE_JOURNAL": str(p)}


# --- the happy path and the lock ---------------------------------------------------------------


def test_opens_at_format_1_and_holds_the_shared_lock_until_closed(data):
    h = open_data(data, "relay", 1, {})
    assert (h.kind, h.format) == ("relay", 1) and lock_is_shared(data)
    assert (data / "format.lock").stat().st_mode & 0o777 == 0o600
    h.close()
    h.close()  # twice is fine
    assert not lock_is_shared(data)
    with open_data(data, "relay", 1, {}) as again:  # shared locks stack
        with open_data(data, "relay", 1, {}):
            assert lock_is_shared(data)
        assert again.format == 1


def test_an_update_in_progress_holds_the_lock_exclusive_and_refuses_the_open(data):
    fd = os.open(data / "format.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, EXCLUSIVE)
    try:
        assert code(data) == "update.busy"
    finally:
        os.close(fd)
    open_data(data, "relay", 1, {}).close()


def test_the_os_environment_is_the_default_for_the_journal(data, tmp_path, monkeypatch):
    for k, v in journal(tmp_path, {"v": 1, "phase": "installed"}).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(DataFormatError) as e:
        open_data(data, "relay", 1)
    assert e.value.code == "update.interrupted"


# --- every refusal -----------------------------------------------------------------------------


def test_not_a_directory_is_format_missing(tmp_path):
    assert code(tmp_path / "nothing") == "format.missing"
    f = tmp_path / "file"
    f.write_text("x")
    assert code(f) == "format.missing"


def test_no_format_json_says_to_install_again(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    with pytest.raises(DataFormatError) as e:
        open_data(d, "relay", 1, {})
    assert e.value.code == "format.missing"
    assert str(e.value) == "this folder has no format.json: run `orch install relay` again"
    assert not lock_is_shared(d)  # a refused open does not keep the lock


DAMAGED = [
    b'{"orch": "hub", "format": 1}',  # another kind
    b'{"orch": "relay", "format": true}',  # a bool is not an int
    b'{"orch": "relay", "format": 1.0}',
    b'{"orch": "relay", "format": "1"}',
    b'{"orch": "relay", "format": 0}',
    b'{"orch": "relay", "format": 1, "extra": 1}',
    b'{"orch": "relay"}',
    b'{"format": 1}',
    b"[1]",
    b"not json",
    b"",
    b'{"orch": "relay", "format": 1}' + b" " * 5000,
]


@pytest.mark.parametrize("raw", DAMAGED)
def test_a_damaged_or_foreign_format_json_is_format_missing(tmp_path, raw):
    d = tmp_path / "d"
    write_format(d, raw=raw)
    assert code(d) == "format.missing"
    assert not lock_is_shared(d)


def test_older_and_newer(data):
    with pytest.raises(DataFormatError) as e:
        open_data(data, "relay", 2, {})  # the code is at 2, the data at 1
    assert e.value.code == "format.older"
    assert str(e.value) == "this data was made by an older orch: run `orch update`"
    write_format(data, fmt=3)
    with pytest.raises(DataFormatError) as e:
        open_data(data, "relay", 2, {})
    assert e.value.code == "format.newer" and "format 3" in str(e.value)
    assert not lock_is_shared(data)


def test_a_symlinked_format_json_or_lock_file_is_refused(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"orch": "relay", "format": 1}))
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
    d = tmp_path / "relay-data"
    side = tmp_path / ".relay-data.restore"
    side.mkdir()
    (side / "record.json").write_text(json.dumps({"aside": str(tmp_path / "relay-data.aside")}))
    with pytest.raises(DataFormatError) as e:  # the folder itself is gone, as mid-restore
        open_data(d, "relay", 1, {})
    assert e.value.code == "update.interrupted"
    assert "your data is in 'relay-data.aside'" in str(e.value)
    write_format(d)
    assert code(d) == "update.interrupted"
    (side / "record.json").write_text("garbage")  # an unreadable record still refuses
    with pytest.raises(DataFormatError) as e:
        open_data(d, "relay", 1, {})
    assert e.value.code == "update.interrupted" and "see the folders beside it" in str(e.value)
    link = tmp_path / "link"
    link.symlink_to(d)  # a symlinked root: the sidecar sits beside the real folder
    assert code(link) == "update.interrupted"
    (side / "record.json").unlink()
    open_data(link, "relay", 1, {}).close()


# --- the update journal (ORCH_UPDATE_JOURNAL) --------------------------------------------------

UNFINISHED = [
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
]


@pytest.mark.parametrize("doc", UNFINISHED)
def test_an_unfinished_or_damaged_journal_refuses(data, tmp_path, doc):
    env = journal(tmp_path, doc)
    with pytest.raises(DataFormatError) as e:
        open_data(data, "relay", 1, env)
    assert e.value.code == "update.interrupted"
    assert str(e.value) == "an update was interrupted: run `orch update` to finish or roll back"
    assert not lock_is_shared(data)


@pytest.mark.parametrize(
    "doc",
    [
        {"v": 1, "phase": "committed"},
        {"v": 1, "phase": "committed", "restoring": False, "update_id": "ab"},
    ],
)
def test_a_committed_journal_opens(data, tmp_path, doc):
    open_data(data, "relay", 1, journal(tmp_path, doc)).close()


def test_no_journal_variable_or_no_such_file_skips_the_check(data, tmp_path):
    open_data(data, "relay", 1, {}).close()
    open_data(data, "relay", 1, {"ORCH_UPDATE_JOURNAL": ""}).close()
    missing = {"ORCH_UPDATE_JOURNAL": str(tmp_path / "missing" / "update.json")}
    open_data(data, "relay", 1, missing).close()


def test_a_journal_that_cannot_be_read_counts_as_unfinished(data, tmp_path):
    env = {"ORCH_UPDATE_JOURNAL": str(tmp_path)}  # a directory
    assert code(data, env=env) == "update.interrupted"


def test_the_journal_is_looked_at_again_under_the_lock(data, monkeypatch):
    seen = iter([False, True])  # clean before the lock, an update started meanwhile
    monkeypatch.setattr(DF, "_unfinished", lambda env: next(seen))
    assert code(data) == "update.interrupted"
    assert not lock_is_shared(data)


# --- the relay wiring --------------------------------------------------------------------------


@pytest.mark.raw_data
def test_create_app_refuses_a_data_dir_without_format_json_before_any_database(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    with pytest.raises(DataFormatError) as e:
        app_mod.create_app(d)
    assert e.value.code == "format.missing"
    assert not (d / "relay.sqlite3").exists()


@pytest.mark.raw_data
def test_create_app_refuses_while_an_update_is_unfinished(data, tmp_path, monkeypatch):
    for k, v in journal(tmp_path, {"v": 1, "phase": "stopped"}).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(DataFormatError) as e:
        app_mod.create_app(data)
    assert e.value.code == "update.interrupted"
    assert not (data / "relay.sqlite3").exists()


@pytest.mark.raw_data
def test_a_relay_at_format_1_runs_and_holds_the_shared_lock_while_it_serves(data):
    app = app_mod.create_app(data)
    assert lock_is_shared(data)  # taken at once: before the lifespan opens a database
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
        assert lock_is_shared(data)  # `orch update` (exclusive) cannot take the data meanwhile
    assert not lock_is_shared(data)  # released at shutdown, after the database is closed
    assert (data / "relay.sqlite3").exists()


@pytest.mark.raw_data
def test_serve_command_refuses_older_data_and_says_to_run_orch_update(data, monkeypatch, capsys):
    monkeypatch.setenv("ORCH_RELAY_DATA", str(data))
    monkeypatch.setattr(app_mod, "DATA_FORMAT", 2)  # this code is newer than the data
    with mock.patch("uvicorn.run") as run:
        assert cli.main(["serve"]) == 2
    assert not run.called
    err = capsys.readouterr().err
    assert "refusing to start" in err and UPDATE_HINT in err
    assert not (data / "relay.sqlite3").exists()


@pytest.mark.raw_data
def test_serve_command_holds_the_handle_for_the_process(data, monkeypatch):
    monkeypatch.setenv("ORCH_RELAY_DATA", str(data))
    monkeypatch.setattr(cli, "_HELD", [])
    with mock.patch("uvicorn.run"):
        assert cli.main(["serve"]) == 0
    assert len(cli._HELD) == 1 and lock_is_shared(data)
    cli._HELD.pop().close()
    assert not lock_is_shared(data)


@pytest.mark.raw_data
def test_create_app_gives_the_lock_up_when_the_construction_fails_after_the_open(data, monkeypatch):
    def boom(app, data_dir):
        raise RuntimeError("construction failed")

    monkeypatch.setattr(app_mod, "install_core", boom)
    with pytest.raises(RuntimeError):
        app_mod.create_app(data)
    assert not lock_is_shared(data)  # closed at once, not whenever the garbage collector gets to it


@pytest.mark.raw_data
def test_serve_command_runs_one_worker_so_no_forked_copy_of_the_service_exists(data, monkeypatch):
    monkeypatch.setenv("ORCH_RELAY_DATA", str(data))
    monkeypatch.setattr(cli, "_HELD", [])
    with mock.patch("uvicorn.run") as run:
        assert cli.main(["serve"]) == 0
    assert (
        run.call_args.kwargs["workers"] == 1
    )  # a pre-fork worker pool would share the lock descriptor
    cli._HELD.pop().close()


# --- the journal is readable by the service user; a fork does not keep the lock ----------------


def test_the_journal_in_a_private_install_dir_is_readable_by_the_user_the_parts_run_as(
    data, tmp_path
):
    # `orch update` writes the journal as the one OS user of the install, and the parts run as that
    # same user, so a journal under install/ (0700) can be read here: a committed one opens, an
    # unfinished one refuses for ITS phase, not for a permission error. (A journal that cannot be
    # read still refuses: fail closed.)
    install = tmp_path / "install"
    install.mkdir(mode=0o700)
    jf = install / "update.json"
    jf.write_text(json.dumps({"v": 1, "phase": "committed"}))
    jf.chmod(0o600)
    assert os.access(jf, os.R_OK) and DF._unfinished({"ORCH_UPDATE_JOURNAL": str(jf)}) is False
    open_data(data, "relay", 1, {"ORCH_UPDATE_JOURNAL": str(jf)}).close()
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

    script = FORKER.format(module="orch_relay.dataformat", kind="relay")
    cmd = [sys.executable, "-c", script, str(data)]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
    pid = int(out.stdout.split()[0])
    try:
        os.kill(pid, 0)  # the child lives
        assert not lock_is_shared(data)  # but the lock died with the service
    finally:
        os.kill(pid, signal.SIGKILL)


# --- one file in three packages ----------------------------------------------------------------


def test_the_three_dataformat_copies_are_byte_identical():
    here = COPIES[1].read_bytes()
    for other in COPIES:
        if other.exists():  # a monorepo checkout
            assert other.read_bytes() == here, f"{other} differs: copy the file, do not edit one"
