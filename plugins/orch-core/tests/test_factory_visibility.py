"""AI Factory runner: it never fails silently. A program it cannot use, the user settings or a blocking readiness
check is one reason, said on the terminal and on every view of an armed epic; and the program lookup accepts the
standard Homebrew layout (a user-owned, group-writable folder) through the real resolve_bin."""
import logging
import os

import pytest

from orch.core import factory_runner as fr, factory_sessions as fs, store
from orch.dashboard import factory_runner as dash
from test_factory_runner import Fake, _started, _trusted_programs, fa, fh, fws  # noqa: F401  (fixtures)

pytestmark = pytest.mark.real_programs


def _script(path, body="exit 0\n", mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(mode)
    return str(path)


@pytest.fixture
def real_resolve(monkeypatch):
    """The real lookup: not the stand-in test_factory_runner's autouse fixture puts in."""
    monkeypatch.setattr(fr, "resolve_bin", lambda name: fr.resolve_why(name)[0])
    monkeypatch.setattr(fr, "which", __import__("shutil").which)


def test_a_group_writable_folder_of_yours_is_trusted_and_others_are_not(tmp_path, real_resolve):
    brew = tmp_path / "homebrew" / "bin"
    tmux = _script(brew / "tmux")
    brew.chmod(0o775)  # Apple Silicon Homebrew: user-owned, group admin, 0775
    assert fr.resolve_bin(tmux) == tmux
    brew.chmod(0o777)
    path, why = fr.resolve_why(tmux)
    assert path is None and "writable by everyone" in why
    brew.chmod(0o775)
    (brew / "tmux").chmod(0o775)  # the file itself group-writable: refused
    path, why = fr.resolve_why(tmux)
    assert path is None and "writable by group or others" in why
    (brew / "tmux").chmod(0o755)
    path, why = fr.resolve_why("/tmp")  # not a regular file
    assert path is None


def test_another_owners_folder_is_refused(tmp_path, real_resolve, monkeypatch):
    tmux = _script(tmp_path / "theirs" / "tmux")
    real_stat = os.stat
    me = os.getuid()

    def stat_as_someone_else(p, *a, **k):
        st = real_stat(p, *a, **k)
        if str(p) == str(tmp_path / "theirs"):
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, me + 4242, *tuple(st)[5:10]))
        return st
    monkeypatch.setattr(fr.os, "stat", stat_as_someone_else)
    path, why = fr.resolve_why(tmux)
    assert path is None and "owned by someone other than you or root" in why


def test_available_and_run_once_through_the_real_lookup_with_a_0775_folder(fws, fa, fh, human, tmp_path, monkeypatch,  # noqa: F811
                                                                           real_resolve):
    brew = tmp_path / "brew" / "bin"
    for name in ("tmux", "env", "claude"):
        _script(brew / name, 'echo "12345"\n')
    brew.chmod(0o775)
    monkeypatch.setenv("PATH", f"{brew}:/usr/bin:/bin")
    assert dash.available()  # the live run's regression: 0775 Homebrew made this False
    assert fr.program_blocker() is None
    eid, (cid,), d = _started(fws, fa, fh)
    fake = Fake()
    lines = dash.run_once(fws, fake)
    assert lines, lines
    assert any(x.startswith("started ") for x in lines) and len(fake.started) == 1, lines


def test_no_tmux_is_said_on_the_terminal_the_run_view_and_the_epic_page(fws, fa, fh, human, tmp_path, monkeypatch,  # noqa: F811
                                                                        real_resolve, caplog):
    from test_dark_dashboard import _client
    from orch.dashboard.data import factory as data
    brew = tmp_path / "brew" / "bin"
    for name in ("env", "claude"):
        _script(brew / name)
    monkeypatch.setenv("PATH", f"{brew}:/usr/bin:/bin")
    monkeypatch.setattr(fr, "which", lambda name, path=None: None if name == "tmux" else __import__("shutil").which(
        name, path=path))
    eid, (cid,), d = _started(fws, fa, fh)
    dash._SAID.clear()
    with caplog.at_level(logging.WARNING, logger="orch.factory"):
        assert dash.run_once(fws, Fake()) == []
        dash.run_once(fws, Fake())
    said = [r.getMessage() for r in caplog.records if "starts nothing" in r.getMessage()]
    assert len(said) == 1 and "tmux was not found at a trusted path: tmux is not on the dashboard's PATH" in said[0]
    r = data.run_view(fws, store.load(fws, eid)[1])
    assert r["state"] == "blocked" and r["blocker"].startswith("tmux was not found at a trusted path")
    c = _client(fws)
    assert "tmux was not found at a trusted path" in c.get(f"/factory/{eid}").text
    assert "The runner starts nothing: tmux was not found at a trusted path" in c.get(f"/t/{eid}").text
    assert fs.bindings(fws) == []


def test_the_runner_logs_to_the_terminal_at_info(monkeypatch):
    log = logging.getLogger("orch.factory")
    monkeypatch.setattr(log, "handlers", [])
    monkeypatch.setattr(log, "level", logging.NOTSET)
    monkeypatch.setattr(log, "propagate", True)
    dash.configure_logging()
    assert len(log.handlers) == 1 and isinstance(log.handlers[0], logging.StreamHandler)
    assert log.getEffectiveLevel() == logging.INFO and not log.propagate
    dash.configure_logging()
    assert len(log.handlers) == 1  # once
    assert logging.getLogger("orch.dashboard").level == logging.getLogger("orch.dashboard").level  # others untouched
    log.handlers.clear()
