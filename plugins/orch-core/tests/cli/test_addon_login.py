"""V3 G6 (#924) C2: ``orch addon login|logout``: the person's own terminal only, never an agent."""

from __future__ import annotations

import io
import json
import os
import resource

import pytest

from orch.addons import credential as cr
from orch.cli import addon_credential as cli
from orch.custody import NoPrompt
from orch.host.custody import write_private
from orch.host.registry import registry_path

SECRET = "k-synthetic-cli-only-0123"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    (tmp_path / "hosts" / "ws").mkdir(parents=True, mode=0o700)
    path = registry_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_private(path, {"workspaces": [{"workspace_id": "ws", "prefix": "T"}]})
    cores = []
    monkeypatch.setattr(resource, "setrlimit", lambda *a: cores.append(a))
    state = {
        "typed": SECRET,
        "rows": {"demo": {"name": "demo", "state": "active", "credential": "missing"}},
        "asked": 0,
    }

    class Terminal:
        def _read(self, prompt, echo):
            state["asked"] += 1
            assert echo is False and SECRET not in prompt
            if state["typed"] is None:
                raise NoPrompt("no controlling terminal")
            return state["typed"]

    monkeypatch.setattr(cli, "TERMINAL", Terminal())

    def row(env, workspace, name):
        state["asked"] += 100
        return state["rows"].get(name)

    monkeypatch.setattr(cli, "_row", row)

    def run(*args, env=None, typed=SECRET):
        state["typed"] = typed
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            [*args[:2], "--workspace", "T", "--state", str(tmp_path), *args[2:]],
            env={} if env is None else env,
            out=out,
            err=err,
            now=lambda: 123,
        )
        assert SECRET not in out.getvalue() + err.getvalue()
        return code, out.getvalue(), err.getvalue()

    return tmp_path, run, state, cores


def test_login_logout_flow_and_leak_sweep(lab, caplog):
    tmp, run, st, cores = lab
    code, out, _ = run("login", "demo")
    assert code == 0 and "Only demo gets this key" in out
    assert cores == [(resource.RLIMIT_CORE, (0, 0))]
    assert cr.load(tmp, "ws", os.getuid(), "demo").reveal() == SECRET
    audit = tmp / "host" / "audit-security.jsonl"
    assert json.loads(audit.read_text().splitlines()[-1]) == {"at": 123, "event": "addon.login", "addon": "demo"}
    assert run("logout", "demo")[1] == "Key removed.\n"
    assert run("logout", "demo")[1] == "No key was stored.\n"
    assert SECRET not in caplog.text
    for p in tmp.rglob("*"):
        if p.is_file():
            assert SECRET.encode() not in p.read_bytes()


@pytest.mark.parametrize("var", ["ORCH_GRANT", "ORCH_SESSION"])
@pytest.mark.parametrize("cmd", ["login", "logout"])
def test_agent_environment_refused_before_anything_opens(lab, var, cmd):
    tmp, run, st, _ = lab
    code, out, err = run(cmd, "demo", env={var: "x"})
    assert code == 1 and "human_only" in err
    assert st["asked"] == 0  # neither the host nor the terminal was touched
    assert not (tmp / "hosts" / "ws" / "addons").exists()


def test_no_terminal_is_refused_and_nothing_is_stored(lab):
    tmp, run, st, _ = lab
    code, _, err = run("login", "demo", typed=None)
    assert code == 1 and "own terminal" in err
    assert cr.status(tmp, "ws", os.getuid(), "demo") == "missing"


def test_login_reads_nothing_from_stdin_env_or_argv(lab, monkeypatch):
    tmp, run, st, _ = lab
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET + "\n"))
    code, _, err = run("login", "demo", SECRET, typed=None)  # a value in argv is a usage error, stdin is not read
    assert code == 1 and cr.status(tmp, "ws", os.getuid(), "demo") == "missing"
    code, _, err = run("login", "demo", env={"ORCH_ADDON_KEY": SECRET, "KEY": SECRET}, typed=None)
    assert code == 1 and "own terminal" in err


@pytest.mark.parametrize("typed", ["", "has space", "x" * 1025, "tab\there"])
def test_shape_refused(lab, typed):
    tmp, run, st, _ = lab
    assert run("login", "demo", typed=typed)[0] == 1
    assert cr.status(tmp, "ws", os.getuid(), "demo") == "missing"


@pytest.mark.parametrize(
    "rows",
    [
        {},
        {"demo": {"name": "demo", "state": "active"}},
        {"demo": {"name": "demo", "state": "disabled", "credential": "x"}},
    ],
)
def test_login_needs_an_enabled_addon_that_holds_credential(lab, rows):
    tmp, run, st, _ = lab
    st["rows"] = rows
    code, _, err = run("login", "demo")
    assert code == 1 and "credential" in err and st["asked"] == 100  # the terminal was never opened
    assert cr.status(tmp, "ws", os.getuid(), "demo") == "missing"


@pytest.mark.parametrize("name", ["../x", "Demo", "a/b", ""])
def test_bad_names_refused(lab, name):
    assert lab[1]("login", name)[0] == 1
