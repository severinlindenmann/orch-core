"""Exercise real client parsing over an in-process transport so tests never send credentials."""

from __future__ import annotations

import io
import json
import os
import resource

import pytest

from orch.cli import github as cli
from orch.github import token as t
from orch.github.client import GitHubClient
from orch.host.custody import write_private
from orch.host.registry import registry_path
from tests.github.fake import busy_day, user

SECRET = "github_pat_synthetic_cli_only"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    (tmp_path / "hosts" / "ws").mkdir(parents=True, mode=0o700)
    path = registry_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_private(path, {"workspaces": [{"workspace_id": "ws", "prefix": "T"}]})
    fake = busy_day()
    monkeypatch.setattr(cli, "CLIENT_FACTORY", lambda source: GitHubClient(source, transport=fake))
    calls = []
    monkeypatch.setattr(resource, "setrlimit", lambda *args: calls.append(args))

    def run(command, *args, stdin=None, env=None):
        fake.closed = False
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            [command, "--workspace", "T", "--state", str(tmp_path), *args],
            env={} if env is None else env,
            out=out,
            err=err,
            stdin=io.StringIO(SECRET + "\n") if stdin is None else stdin,
            now=lambda: 123,
        )
        assert SECRET not in out.getvalue() + err.getvalue()
        return code, out.getvalue(), err.getvalue()

    return tmp_path, fake, run, calls


def test_full_flow_leak_sweep(lab, caplog):
    state, fake, run, calls = lab
    assert run("login")[0] == 0
    assert calls == [(resource.RLIMIT_CORE, (0, 0))]
    assert t.load(state, "ws", os.getuid()) == t.Token(SECRET)
    audit = state / "host" / "audit-security.jsonl"
    line = json.loads(audit.read_text())
    assert line == {"at": 123, "event": "github.login", "login": "octocat", "token_expires": None}
    code, out, err = run("whoami", "--repo", "team/alpha", "--repo", "team/missing")
    assert code == 0 and "reachable" in out and "not found or no access" in out and "4900 / 5000" in out
    code, out, err = run("whoami", "--json")
    assert json.loads(out)["login"] == "octocat"
    for name in t.CACHE_FILES:
        (t.github_dir(state, "ws") / name).write_text("old")
    assert run("logout")[0] == 0
    assert not (t.github_dir(state, "ws") / "cache.json").exists()
    assert run("logout")[1] == "No token was stored.\n"
    assert "Not connected" in run("whoami")[2]
    assert SECRET not in caplog.text + repr(fake.seen) + str(t.Token(SECRET)) + repr(t.Token(SECRET))
    for path in state.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes()


def test_terminal_and_env_ignored(lab, monkeypatch):
    state, fake, run, _ = lab
    prompts = []

    class TTY(io.StringIO):
        def isatty(self):
            return True

    class Terminal:
        def _read(self, prompt, echo):
            prompts.append((prompt, echo))
            return SECRET

    monkeypatch.setattr(cli, "TERMINAL", Terminal())
    assert run("login", stdin=TTY(), env={"GH_TOKEN": "bad", "GITHUB_TOKEN": "bad"})[0] == 0
    assert prompts == [(cli.PROMPT, False)]
    assert run("login", stdin=io.StringIO(""), env={"GH_TOKEN": SECRET, "GITHUB_TOKEN": SECRET})[0] == 1


@pytest.mark.parametrize("value", ["ghp_classic_fake", "bad", "github_pat_bad control", "github_pat_" + "a" * 512])
def test_bad_inputs(lab, value):
    state, fake, run, _ = lab
    code, out, err = run("login", stdin=io.StringIO(value))
    assert code == 1 and value not in out + err
    assert not fake.seen
    with pytest.raises(t.NoToken):
        t.load(state, "ws", os.getuid())


@pytest.mark.parametrize("command", ["login", "logout", "whoami"])
def test_grant_and_argv_refused(lab, command):
    _, fake, run, _ = lab
    assert run(command, env={"ORCH_GRANT": ""})[0] == 1
    assert "only stops accidents" in run(command, env={"ORCH_GRANT": "yes"})[2]
    assert run(command, SECRET)[0] == 1
    assert not fake.seen


@pytest.mark.parametrize("failure", ["unauthorized", "network", "unexpected"])
def test_failed_login_stores_nothing(lab, monkeypatch, failure):
    state, fake, run, _ = lab
    if failure == "unauthorized":
        fake.bad_token()
    elif failure == "network":
        fake.fail("/user")
    else:

        def fail(source):
            raise RuntimeError(SECRET)

        monkeypatch.setattr(cli, "CLIENT_FACTORY", fail)
    assert run("login")[0] == 1
    with pytest.raises(t.NoToken):
        t.load(state, "ws", os.getuid())


def test_rotation_and_hostile_reply(lab):
    state, fake, run, _ = lab
    assert run("login")[0] == 0
    for name in t.CACHE_FILES:
        (t.github_dir(state, "ws") / name).write_text("old")
    fake.register("/user", json_body=user(SECRET))
    assert run("login", stdin=io.StringIO(SECRET + "2"))[0] == 0
    assert all(not (t.github_dir(state, "ws") / name).exists() for name in t.CACHE_FILES)
    fake.register("/rate_limit", json_body={"resources": []})
    assert "unknown" in run("whoami")[1]
    assert cli.clean_text("a\n\x1b\u202eb") == "ab"
    assert run("whoami", "--repo", "bad/path/extra")[0] == 1
    assert run("whoami", *[item for _ in range(21) for item in ("--repo", "team/alpha")])[0] == 1


def test_bounded_binary_stdin_and_expiry(lab):
    _, fake, run, _ = lab
    expiry = "2027-01-01 00:00:00 UTC"
    fake.register("/user", json_body=user(), headers={"github-authentication-token-expiration": expiry})

    class Bounded(io.BytesIO):
        def readline(self, size=-1):
            assert size == 512
            return super().readline(size)

    stream = Bounded((SECRET + "\nsecond line\n").encode())
    code, out, _ = run("login", stdin=stream)
    assert code == 0 and expiry in out
    assert stream.read() == b"second line\n"


def test_dispatch(monkeypatch):
    from orch.cli.main import main

    calls = []

    def capture(args, **kwargs):
        calls.append((args, kwargs))
        return 7

    monkeypatch.setattr(cli, "main", capture)
    out, err = io.StringIO(), io.StringIO()
    assert main(["github", "whoami"], env={}, stdout=out, stderr=err) == 7
    assert calls[0][0] == ["whoami"]
    assert calls[0][1]["out"] is out and calls[0][1]["err"] is err
