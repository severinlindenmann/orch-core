"""#542 items 1 and 3: no dashboard under an agent's environment; no launch code left at rest."""

import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

import orch_dashboard.__main__ as launcher
from orch_dashboard.auth import CODE_TTL, Auth

PORT = 43210


# --- item 1 ----------------------------------------------------------------------------------------------------------


def _nothing(*_a, **_k):
    pytest.fail("nothing may be started, minted or served under an agent's environment")


@pytest.mark.parametrize("name", ["ORCH_GRANT", "ORCH_SESSION"])
@pytest.mark.parametrize(
    "argv", [[], ["open"], ["launch"], ["open", "/w/DEMO", "--print-url"], ["serve"], ["serve", "--launcher-lock"]]
)
def test_the_launcher_refuses_under_an_agents_environment(tmp_path, monkeypatch, capsys, name, argv):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv(name, "x")
    for seam in ("control", "_serve", "run_dir", "verify_probe"):
        monkeypatch.setattr(launcher, seam, _nothing)
    monkeypatch.setattr(launcher.subprocess, "Popen", _nothing)
    monkeypatch.setattr(launcher.webbrowser, "open", _nothing)
    monkeypatch.setattr(launcher.sys, "argv", ["orch-dashboard", *argv])
    assert launcher.main() == 3
    seen = capsys.readouterr()
    assert not seen.out
    assert seen.err.startswith("orch-dashboard: human_only: ") and seen.err.count("\n") == 1
    assert not (tmp_path / "dashboard").exists()


@pytest.mark.parametrize("call", [lambda: launcher.open_dashboard(), lambda: launcher.serve()])
def test_the_functions_refuse_too(monkeypatch, call):
    monkeypatch.setenv("ORCH_GRANT", "x")
    monkeypatch.setattr(launcher, "run_dir", _nothing)
    with pytest.raises(launcher.AgentRefused, match="human_only"):
        call()


@pytest.mark.parametrize("argv", [["stop"], ["--stop"], ["serve", "--stop"]])
def test_stop_passes_under_an_agents_environment(tmp_path, monkeypatch, argv):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_GRANT", "x")
    monkeypatch.setenv("ORCH_SESSION", "s")
    sent = []
    monkeypatch.setattr(launcher, "control", lambda path, command: sent.append(command) or {"ok": True})
    monkeypatch.setattr(launcher.sys, "argv", ["orch-dashboard", *argv])
    assert launcher.main() == 0 and sent == ["stop"]


def test_empty_agent_variables_do_not_refuse(monkeypatch):
    launcher.refuse_agent({"ORCH_GRANT": "", "ORCH_SESSION": ""})
    with pytest.raises(launcher.AgentRefused):
        launcher.refuse_agent({"ORCH_SESSION": "s"})


def test_python_dash_m_refuses_as_well(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "ORCH_STATE_DIR": str(tmp_path), "ORCH_GRANT": "x"}
    res = subprocess.run(
        [sys.executable, "-m", "orch_dashboard", "open"], env=env, capture_output=True, text=True, timeout=60
    )
    assert res.returncode == 3 and res.stderr.startswith("orch-dashboard: human_only: ") and not res.stdout
    assert not (tmp_path / "dashboard").exists()


# --- item 3 ----------------------------------------------------------------------------------------------------------


def _launch(tmp_path, monkeypatch, opens=True):
    """One open_dashboard run against an in-process Auth; every argv the launcher hands out is recorded."""
    auth = Auth(directory=tmp_path / "dashboard")
    timers = []

    class Timer:
        def __init__(self, delay, fn):
            self.delay, self.fn = delay, fn
            timers.append(self)

        def start(self):
            pass

    monkeypatch.setattr("orch_dashboard.auth.threading.Timer", Timer)
    issued = auth.launch(PORT)
    argvs = []

    class Process:
        def poll(self):
            return None

    def popen(args, **kwargs):
        argvs.append(list(args))
        assert not any(issued["code"] in str(v) for v in (kwargs.get("env") or {}).values())
        return Process()

    def browser(url):
        argvs.append([url])
        return opens

    monkeypatch.setattr(launcher, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(launcher, "control", lambda *_: issued)
    monkeypatch.setattr(launcher, "verify_probe", lambda _: None)
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher.os, "execv", _nothing, raising=False)
    monkeypatch.setattr(launcher.webbrowser, "open", browser)
    return auth, issued, timers, argvs


def test_the_redirect_file_is_gone_after_redemption_and_a_second_read_fails(tmp_path, monkeypatch):
    auth, issued, timers, argvs = _launch(tmp_path, monkeypatch)
    launcher.open_dashboard("/w/DEMO")
    redirect = Path(unquote(urlsplit(argvs[-1][0]).path))
    page = redirect.read_text()  # the person's browser reads it once
    assert issued["code"] in page and 'content="no-store"' in page and "location.replace" in page
    assert [t.delay for t in timers] == [CODE_TTL] == [30]
    assert auth.exchange(issued["code"])[1] == 200
    assert not redirect.exists()
    with pytest.raises(FileNotFoundError):
        redirect.read_text()
    assert auth.exchange(issued["code"])[1] == 401  # and the code itself is spent
    assert not list((tmp_path / "dashboard").glob("launch-*.html"))


def test_an_unredeemed_redirect_file_lives_only_as_long_as_its_code(tmp_path, monkeypatch):
    auth, issued, timers, argvs = _launch(tmp_path, monkeypatch)
    launcher.open_dashboard()
    redirect = Path(unquote(urlsplit(argvs[-1][0]).path))
    assert redirect.exists()
    timers[-1].fn()  # fires at the code's expiry, CODE_TTL
    assert not redirect.exists()


def test_the_code_is_in_no_argv(tmp_path, monkeypatch):
    _, issued, _, argvs = _launch(tmp_path, monkeypatch)
    launcher.open_dashboard("/w/DEMO/tickets")
    assert argvs and all(issued["code"] not in arg and "launch=" not in arg for argv in argvs for arg in argv)
    assert argvs[-1][0].startswith("file://")


def test_no_browser_leaves_no_file(tmp_path, monkeypatch):
    _, issued, _, argvs = _launch(tmp_path, monkeypatch, opens=False)
    with pytest.raises(RuntimeError, match="no browser"):
        launcher.open_dashboard()
    assert not list((tmp_path / "dashboard").glob("launch-*.html"))


def test_the_startup_sweep_uses_the_code_lifetime(tmp_path):
    import os

    directory = tmp_path / "d"
    directory.mkdir(mode=0o700)
    old, young = directory / "launch-old.html", directory / "launch-young.html"
    for p in (old, young):
        p.write_text("x")
    now = 1_000_000.0
    os.utime(old, (now - CODE_TTL, now - CODE_TTL))
    os.utime(young, (now - CODE_TTL + 1, now - CODE_TTL + 1))
    launcher.sweep_redirects(directory, now=now)
    assert not old.exists() and young.exists()
