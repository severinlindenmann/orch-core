"""A running server reports the digest of the code it LOADED, not of what is on disk now (19 Oct: an in-place
reinstall made an old server answer with the new digest, so the launcher never replaced it)."""

import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

import orch_dashboard.__main__ as launcher


@pytest.fixture(autouse=True)
def fresh_digest():
    """`code_digest` is cached per process. Autouse fixtures are set up first and torn down last, so the cache is
    emptied again only after `monkeypatch` has restored `__file__`: no digest of a patched tree outlives the test."""
    launcher.code_digest.cache_clear()
    yield
    launcher.code_digest.cache_clear()


@pytest.fixture
def server(monkeypatch):
    state = Path(tempfile.mkdtemp(prefix="dsh", dir="/tmp"))  # short: a unix socket path has a small limit
    monkeypatch.setattr(launcher, "state_dir", lambda: state)
    for name in ("ORCH_GRANT", "ORCH_SESSION"):
        monkeypatch.delenv(name, raising=False)
    launcher.code_digest.cache_clear()
    thread = threading.Thread(target=launcher.serve, daemon=True)
    thread.start()
    ctl = state / "dashboard" / "ctl.sock"
    deadline = time.monotonic() + 20
    while not ctl.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ctl.exists()
    try:
        yield ctl
    finally:
        try:
            launcher.control(ctl, "stop")
        except OSError:
            pass
        thread.join(15)
        launcher.code_digest.cache_clear()
        shutil.rmtree(state, ignore_errors=True)


def test_hello_answers_the_digest_from_startup_even_after_the_files_changed(server, tmp_path, monkeypatch):
    started = launcher.control(server, "hello")["release"]
    assert started == launcher.code_digest()
    # a reinstall: the package tree on disk is now something else
    tree = tmp_path / "pkg"
    tree.mkdir()
    (tree / "__main__.py").write_text("new code")
    monkeypatch.setattr(launcher, "__file__", str(tree / "__main__.py"))
    launcher.code_digest.cache_clear()
    assert launcher.code_digest() != started  # what a launcher on the new files computes
    assert launcher.control(server, "hello")["release"] == started  # the server still names the code it runs


def test_open_replaces_a_server_whose_startup_digest_differs(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "state_dir", lambda: tmp_path)
    stopped = []
    monkeypatch.setattr(launcher, "control", lambda _p, command, *_: {"release": "old000000000"})
    monkeypatch.setattr(
        launcher, "_stop_stale", lambda ctl: stopped.append(ctl) or (_ for _ in ()).throw(StopIteration)
    )
    with pytest.raises(StopIteration):
        launcher.open_dashboard("/w/DEMO")
    assert len(stopped) == 1
