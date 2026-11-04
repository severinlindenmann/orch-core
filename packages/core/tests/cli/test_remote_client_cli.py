"""R5 T3: ``orch remote open`` starts the remote client of this computer (person only, addon required)."""

from __future__ import annotations

import io

from orch.cli import dashboard, remote


def run(args, env, monkeypatch=None):
    out, err = io.StringIO(), io.StringIO()
    return remote.main(args, env=env, out=out, err=err), out.getvalue(), err.getvalue()


def test_an_agent_environment_is_refused_before_anything_starts(monkeypatch):
    monkeypatch.setattr(
        dashboard, "_module_installed", lambda env=None: (_ for _ in ()).throw(AssertionError("probed"))
    )
    for name in ("ORCH_GRANT", "ORCH_SESSION"):
        code, _, err = run(["open"], {name: "x"})
        assert code == 3 and "human_only" in err


def test_a_missing_addon_gets_the_install_hint(monkeypatch):
    monkeypatch.setattr(dashboard, "_module_installed", lambda env=None: False)
    code, _, err = run(["open"], {})
    assert code == 1 and "not installed" in err


def test_open_replaces_the_process_with_the_client_module(monkeypatch):
    monkeypatch.setattr(dashboard, "_module_installed", lambda env=None: True)
    seen = {}
    monkeypatch.setattr(remote.os, "execve", lambda path, argv, env: seen.update(argv=argv, env=env))
    run(["open"], {"PATH": "/usr/bin"})
    assert seen["argv"][1:] == ["-P", "-m", "orch_dashboard.remote_client"]
    assert seen["env"] == {"PATH": "/usr/bin"}


def test_pythonpath_never_reaches_the_client_or_its_probe(monkeypatch, tmp_path):
    """A fake ``orch_dashboard`` named by PYTHONPATH is not loaded by ``orch remote open``."""
    import subprocess
    import sys

    fake = tmp_path / "evil"
    (fake / "orch_dashboard").mkdir(parents=True)
    marker = tmp_path / "loaded"
    (fake / "orch_dashboard" / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    seen = {}
    monkeypatch.setattr(remote, "_exec", lambda argv, env: seen.update(argv=argv, env=dict(env)))
    # the real probe runs with the sanitized environment
    env = {"PATH": "/usr/bin", "PYTHONPATH": str(fake), "PYTHONSTARTUP": "x.py"}
    run(["open"], env)
    assert not marker.exists(), "the probe imported code from PYTHONPATH"
    if seen:  # (absent only when the addon is not installed here)
        assert "PYTHONPATH" not in seen["env"] and "PYTHONSTARTUP" not in seen["env"]
        subprocess.run([sys.executable, "-P", "-c", "import orch_dashboard"], env=seen["env"], check=False)
        assert not marker.exists()


def test_install_publish_setup_runs_with_p_and_a_clean_path(monkeypatch):
    from orch.install import cli

    seen = {}
    monkeypatch.setattr(cli, "_run", lambda argv, env=None: seen.update(argv=argv, env=env) or (0, ""))
    monkeypatch.setenv("PYTHONPATH", "rel/dir")
    assert cli._publish_setup("http://x") is None
    assert seen["argv"][1:4] == ["-P", "-m", "orch"] and "PYTHONPATH" not in seen["env"]
