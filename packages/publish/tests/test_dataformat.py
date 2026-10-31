"""P1-B2: pubsrv opens its data dir only at the right format (``format.json``), holds the shared format
lock while it serves, and the three ``dataformat.py`` copies (hub, relay, publish) are one file."""

import fcntl
import json
import os
from pathlib import Path

import pytest

from pubsrv import cli, db
from pubsrv import dataformat as DF
from pubsrv.app import create_app
from pubsrv.dataformat import DataFormatError, open_data
from pubsrv.serve import create_share_app
from tests.format_helpers import write_format

PACKAGES = Path(__file__).resolve().parents[2]
COPIES = (
    PACKAGES / "hub" / "src" / "orch_hub" / "dataformat.py",
    PACKAGES / "relay" / "src" / "orch_relay" / "dataformat.py",
    PACKAGES / "publish" / "pubsrv" / "dataformat.py",
)
EXCLUSIVE = fcntl.LOCK_EX | fcntl.LOCK_NB
HOST = "127.0.0.1:8103"
KIND = "publish"


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "data"
    write_format(d)
    return d


def code(path, current=1, env=None, kind=KIND):
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
    h = open_data(data, KIND, 1, {})
    assert (h.kind, h.format) == (KIND, 1) and lock_is_shared(data)
    assert (data / "format.lock").stat().st_mode & 0o777 == 0o600
    h.close()
    h.close()  # twice is fine
    assert not lock_is_shared(data)
    with open_data(data, KIND, 1, {}) as again:  # shared locks stack
        with open_data(data, KIND, 1, {}):
            assert lock_is_shared(data)
        assert again.format == 1


def test_an_update_in_progress_holds_the_lock_exclusive_and_refuses_the_open(data):
    fd = os.open(data / "format.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, EXCLUSIVE)
    try:
        assert code(data) == "update.busy"
    finally:
        os.close(fd)
    open_data(data, KIND, 1, {}).close()


def test_the_os_environment_is_the_default_for_the_journal(data, tmp_path, monkeypatch):
    for k, v in journal(tmp_path, {"v": 1, "phase": "installed"}).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(DataFormatError) as e:
        open_data(data, KIND, 1)
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
        open_data(d, KIND, 1, {})
    assert e.value.code == "format.missing"
    assert str(e.value) == "this folder has no format.json: run `orch install publish` again"
    assert not lock_is_shared(d)  # a refused open does not keep the lock


@pytest.mark.parametrize(
    "raw",
    [
        b'{"orch": "hub", "format": 1}',  # another kind
        b'{"orch": "publish", "format": true}',  # a bool is not an int
        b'{"orch": "publish", "format": 1.0}',
        b'{"orch": "publish", "format": "1"}',
        b'{"orch": "publish", "format": 0}',
        b'{"orch": "publish", "format": 1, "extra": 1}',
        b'{"orch": "publish"}',
        b'{"format": 1}',
        b"[1]",
        b"not json",
        b"",
        b'{"orch": "publish", "format": 1}' + b" " * 5000,
    ],
)
def test_a_damaged_or_foreign_format_json_is_format_missing(tmp_path, raw):
    d = tmp_path / "d"
    write_format(d, raw=raw)
    assert code(d) == "format.missing"
    assert not lock_is_shared(d)


def test_older_and_newer(data):
    with pytest.raises(DataFormatError) as e:
        open_data(data, KIND, 2, {})  # the code is at 2, the data at 1
    assert e.value.code == "format.older"
    assert str(e.value) == "this data was made by an older orch: run `orch update`"
    write_format(data, fmt=3)
    with pytest.raises(DataFormatError) as e:
        open_data(data, KIND, 2, {})
    assert e.value.code == "format.newer" and "format 3" in str(e.value)
    assert not lock_is_shared(data)


def test_a_symlinked_format_json_or_lock_file_is_refused(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"orch": KIND, "format": 1}))
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
    d = tmp_path / "pub-data"
    side = tmp_path / ".pub-data.restore"
    side.mkdir()
    (side / "record.json").write_text(json.dumps({"aside": str(tmp_path / "pub-data.aside")}))
    with pytest.raises(DataFormatError) as e:  # the folder itself is gone, as mid-restore
        open_data(d, KIND, 1, {})
    assert e.value.code == "update.interrupted"
    assert "your data is in 'pub-data.aside'" in str(e.value)
    write_format(d)
    assert code(d) == "update.interrupted"
    (side / "record.json").write_text("garbage")  # an unreadable record still refuses
    with pytest.raises(DataFormatError) as e:
        open_data(d, KIND, 1, {})
    assert e.value.code == "update.interrupted" and "see the folders beside it" in str(e.value)
    link = tmp_path / "link"
    link.symlink_to(d)  # a symlinked root: the sidecar sits beside the real folder
    assert code(link) == "update.interrupted"
    (side / "record.json").unlink()
    open_data(link, KIND, 1, {}).close()


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
        open_data(data, KIND, 1, env)
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
    open_data(data, KIND, 1, journal(tmp_path, doc)).close()


def test_no_journal_variable_or_no_such_file_skips_the_check(data, tmp_path):
    open_data(data, KIND, 1, {}).close()
    open_data(data, KIND, 1, {"ORCH_UPDATE_JOURNAL": ""}).close()
    missing = {"ORCH_UPDATE_JOURNAL": str(tmp_path / "missing" / "update.json")}
    open_data(data, KIND, 1, missing).close()


def test_a_journal_that_cannot_be_read_counts_as_unfinished(data, tmp_path):
    env = {"ORCH_UPDATE_JOURNAL": str(tmp_path)}  # a directory
    assert code(data, env=env) == "update.interrupted"


def test_the_journal_is_looked_at_again_under_the_lock(data, monkeypatch):
    seen = iter([False, True])  # clean before the lock, an update started meanwhile
    monkeypatch.setattr(DF, "_unfinished", lambda env: next(seen))
    assert code(data) == "update.interrupted"
    assert not lock_is_shared(data)


# --- the publish wiring ------------------------------------------------------------------------


@pytest.mark.raw_data
def test_create_app_refuses_a_data_dir_without_format_json_before_any_database(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    with pytest.raises(DataFormatError) as e:
        create_app(str(d), HOST)
    assert e.value.code == "format.missing"
    assert not any(p.name.endswith(".sqlite3") or p.suffix == ".db" for p in d.iterdir())


@pytest.mark.raw_data
def test_create_app_refuses_while_an_update_is_unfinished(data, tmp_path, monkeypatch):
    for k, v in journal(tmp_path, {"v": 1, "phase": "stopped"}).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(DataFormatError) as e:
        create_app(str(data), HOST)
    assert e.value.code == "update.interrupted"


@pytest.mark.raw_data
def test_an_app_and_a_share_server_hold_the_shared_lock_while_they_live(data):
    app = create_app(str(data), HOST)
    assert lock_is_shared(data)
    app.state.settings.data_handle.close()
    assert not lock_is_shared(data)
    srv = create_share_app(str(data))
    assert lock_is_shared(data)
    srv.data_handle.close()
    assert not lock_is_shared(data)


@pytest.mark.raw_data
def test_the_admin_command_refuses_a_bare_dir_and_leaves_no_lock(tmp_path, capsys):
    d = tmp_path / "data"
    d.mkdir()
    assert cli.main(["admin", "--data", str(d), "owners", "list"]) == 2
    assert "no format.json" in capsys.readouterr().err
    assert not lock_is_shared(d)


@pytest.mark.raw_data
def test_the_admin_command_runs_beside_a_serving_process(data, capsys):
    app = create_app(str(data), HOST)  # holds the shared lock, as a running server does
    try:
        assert cli.main(["admin", "--data", str(data), "owners", "list"]) == 0
        assert lock_is_shared(data)  # the server's lock is still there
    finally:
        app.state.settings.data_handle.close()


@pytest.mark.raw_data
def test_serving_older_data_says_to_run_orch_update(data, monkeypatch, capsys):
    monkeypatch.setattr(db, "DATA_FORMAT", 2)  # this code is newer than the data
    assert cli.main(["admin", "--data", str(data), "owners", "list"]) == 2
    assert "run `orch update`" in capsys.readouterr().err


# --- one file in three packages ----------------------------------------------------------------


def test_the_three_dataformat_copies_are_byte_identical():
    here = COPIES[2].read_bytes()
    for other in COPIES:
        if other.exists():  # a monorepo checkout
            assert other.read_bytes() == here, f"{other} differs: copy the file, do not edit one"
