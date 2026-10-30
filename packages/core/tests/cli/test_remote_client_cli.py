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
