from pathlib import Path
from unittest.mock import Mock

import pytest

from orch_dashboard.gateway import Gateway, RunDirectoryError, run_dir

MESSAGE = "ORCH_RUN_DIR is no longer read; set ORCH_HOST_RUN_DIR (the host's run directory) instead"


@pytest.mark.parametrize("override", [None, "", "relative-run", "/custom/run"])
def test_core_resolution(tmp_path, monkeypatch, override):
    monkeypatch.chdir(tmp_path)
    env = {} if override is None else {"ORCH_HOST_RUN_DIR": override}
    expected = Path(override).absolute() if override else tmp_path / "state/run"
    assert run_dir(Path("state"), env) == expected
    monkeypatch.setattr("os.environ", env)
    gateway = Gateway(Path("state"))
    assert gateway.run == expected
    gateway.close()


@pytest.mark.parametrize("canonical", [None, "custom"])
@pytest.mark.parametrize("legacy", ["other", ""])
def test_legacy_conflict(tmp_path, canonical, legacy):
    env = {"ORCH_RUN_DIR": legacy}
    if canonical is not None:
        env["ORCH_HOST_RUN_DIR"] = canonical
    with pytest.raises(RunDirectoryError) as exc:
        run_dir(tmp_path, env)
    assert str(exc.value) == MESSAGE


@pytest.mark.parametrize("canonical", [None, "custom"])
def test_same_absolute_directory_is_silent(tmp_path, monkeypatch, capsys, canonical):
    monkeypatch.chdir(tmp_path)
    expected = tmp_path / (canonical or "state/run")
    env = {"ORCH_RUN_DIR": str(expected)}
    if canonical:
        env["ORCH_HOST_RUN_DIR"] = canonical
    assert run_dir(Path("state"), env) == expected
    monkeypatch.setattr("os.environ", env)
    gateway = Gateway(Path("state"))
    assert gateway.run == expected
    gateway.close()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("command", ["serve", "open", "launch"])
def test_startup_refuses_before_files_or_sockets(tmp_path, monkeypatch, capsys, command):
    from orch_dashboard import __main__ as cli

    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_HOST_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("ORCH_RUN_DIR", str(tmp_path / "old"))
    monkeypatch.setattr("sys.argv", ["orch-dashboard", command])
    sock = Mock(side_effect=AssertionError("must not open a socket"))
    monkeypatch.setattr(cli.socket, "socket", sock)
    assert cli.main() == 1
    assert capsys.readouterr() == ("", MESSAGE + "\n")
    sock.assert_not_called()
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("override", [None, "", "custom"])
def test_devhost_resolution_and_advertised_socket(tmp_path, monkeypatch, stub, override):
    from devhost.__main__ import dev_directories

    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / ".orch-dev").touch()
    monkeypatch.chdir(state)
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.delenv("ORCH_RUN_DIR", raising=False)
    monkeypatch.delenv("ORCH_HOST_SOCKET", raising=False)
    monkeypatch.delenv("ORCH_HOST_RUN_DIR", raising=False)
    if override is not None:
        monkeypatch.setenv("ORCH_HOST_RUN_DIR", override)
    expected = state / (override or "run")
    assert dev_directories() == (state.resolve(), expected.resolve())
    stub.state = state
    assert stub.call("host.list", {})["data"]["workspaces"][0]["socket"] == str(expected / stub.ws / "host.sock")
    monkeypatch.setenv("ORCH_RUN_DIR", str(expected))
    assert dev_directories()[1] == expected.resolve()
    monkeypatch.setenv("ORCH_RUN_DIR", str(state / "wrong"))
    with pytest.raises(RunDirectoryError):
        dev_directories()


def test_serve_refuses_while_an_update_is_unfinished_and_starts_on_a_fresh_machine(tmp_path, monkeypatch):
    from orch.migrate import init_root
    from orch.migrate.journal import write_journal
    from orch.ops.errors import OrchError
    from orch_dashboard import __main__ as cli

    state = tmp_path / "state"
    env = {"ORCH_STATE_DIR": str(state)}
    init_root(state, "host")
    write_journal("stopped", env, backups={})
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_HOST_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.delenv("ORCH_RUN_DIR", raising=False)
    monkeypatch.setattr(cli, "refuse_agent", lambda: None)
    monkeypatch.setattr(cli, "run_dir", lambda s: tmp_path / "run")
    with pytest.raises(OrchError) as e:
        cli.serve()
    assert e.value.code == "update.interrupted"
