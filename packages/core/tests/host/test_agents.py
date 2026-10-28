"""H11c acceptance: agent.preview / agent.start (D-P2-28, §12.4), term.preview / term.open {harness}, and the canary.

The runner runs in this process; harnesses are fake programs in ``tmp_path`` named by ``runner.json``
(``login_shell: false``), so no real ``claude`` or ``codex`` is ever started. The login-shell form is checked on the
preview's argv only (spawning it would run whatever the login shell finds as ``codex``).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from orch.host.control_client import new_idem
from orch.host.launch import LOGIN_EXEC, LOGIN_NOTICE
from orch.identity import new_ulid
from orch.runner import enablement, transcript
from orch.runner.config import parse
from tests.host.dispatch_helpers import SESSION, err, ok
from tests.host.lifecycle_helpers import UID
from tests.host.live_helpers import world
from tests.host.test_runner_grants import SECRET_TAIL, deposit, gid_of, issue
from tests.host.test_runner_socket import Runner, wait_for

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")


@pytest.fixture
def w(tmp_path):
    w = world(tmp_path, runner_identity=lambda cred: cred.pid == os.getpid())
    yield w
    w.h.stop()
    w.lab.close()


def grant_addon(w, name: str = "start-agent", caps=("spawn_agent",)) -> None:
    w.person_event("addon.granted", "workspace", name=name, version="0.1.0", package_sha256="sha256:" + "0" * 64,
                   capabilities=list(caps), fields={}, sections=[], artifact_kinds=[])  # fmt: skip


def fake(tmp_path: Path, name: str) -> Path:
    """A harness stand-in: records its argv and environment, then waits."""
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f'for a in "$@"; do printf \'%s\\n\' "$a"; done > {out}/{name}.argv.tmp\n'
        f"env > {out}/{name}.env.tmp\n"
        f"mv {out}/{name}.argv.tmp {out}/{name}.argv; mv {out}/{name}.env.tmp {out}/{name}.env\n"
        "exec sleep 60\n"
    )
    path.chmod(0o755)
    return path


def write_config(w, doc: dict) -> None:
    path = Path(w.lab.state) / "runner.json"
    path.write_text(json.dumps(doc))
    path.chmod(0o600)


@pytest.fixture
def ready(w, tmp_path, monkeypatch):
    """start-agent granted, dashboard execution on (no terminals), runner.json naming the fakes, a runner, a runner
    grant deposited."""
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("SECRET_TOKEN", "must-not-pass")
    grant_addon(w)
    enablement.set_enabled(Path(w.lab.state), w.wid, True, UID, terminals=False, text="t")
    programs = {"codex": str(fake(tmp_path, "codex")), "claude-code": str(fake(tmp_path, "claude"))}
    doc = {"v": 1, "login_shell": False, "programs": programs}
    write_config(w, doc)
    (tmp_path / "rs").mkdir(mode=0o700)
    r = Runner(w, tmp_path / "rs", config=parse(doc))
    w.runner, w.out = r, tmp_path / "out"
    w.value = issue(w)
    ok(deposit(w.conn(), w.value))
    try:
        yield w
    finally:
        r.close()


def preview(c, **args):
    return ok(c.call("agent.preview", args))["data"]


def start(c, idem: str | None = None, **args):
    rid = f"s{new_ulid()}"
    c.ws.send(rid, "agent.start", args, opts={"idem": idem or new_idem()})
    return c.answer(rid)


def read_out(w, name: str) -> tuple[list[str], dict[str, str]]:
    wait_for(lambda: (w.out / f"{name}.env").exists())
    argv = (w.out / f"{name}.argv").read_text().splitlines()
    env = dict(line.split("=", 1) for line in (w.out / f"{name}.env").read_text().splitlines() if "=" in line)
    return argv, env


# -- preview ---------------------------------------------------------------------------------------------------------


def test_preview_blocks_in_order_and_has_no_side_effects(w, tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    c = w.conn()
    _, key = w.ticket()
    args = {"ticket": key, "harness": "codex", "mode": "work"}
    first = preview(c, **args)["blocked"]
    assert first["code"] == "runner.disabled" and first["detail"] == {"missing": "addon"}
    grant_addon(w)
    assert preview(c, **args)["blocked"]["detail"] == {"missing": "machine"}
    enablement.set_enabled(Path(w.lab.state), w.wid, True, UID, terminals=False, text="t")  # no terminals needed
    assert preview(c, **args)["blocked"]["code"] == "runner.unavailable"
    (tmp_path / "rs").mkdir(mode=0o700)
    r = Runner(w, tmp_path / "rs")
    try:
        got = preview(c, **args)["blocked"]
        assert got["code"] == "launch.no_grant" and got["detail"] == {"reason": "none"}
        value = issue(w)
        ok(deposit(c, value))
        pre = preview(c, **args)
        assert pre["blocked"] is None and pre["ticket"] == key and pre["will_create_worktree"] is False
        # a ticket without a codebase works in its own folder, so the prompt names the instructions by full path
        assert pre["cwd"] == {"kind": "workdir", "ticket": key}
        assert pre["argv"][:5] == ["$SHELL", "-l", "-c", LOGIN_EXEC, "codex"]
        assert pre["argv"][5].startswith(f"Run orch status, then work on ticket {key}. Follow /")
        assert pre["argv"][5].endswith("AGENTS.orch.md.")
        assert pre["login_shell"] is True and pre["program"] is None and LOGIN_NOTICE in pre["warnings"]
        assert {"ORCH_GRANT", "ORCH_SESSION", "ORCH_TICKET", "ORCH_HARNESS", "TERM"} <= set(pre["env"])
        assert pre["cwd_path"].endswith(f"/.orch-work/{key}") and pre["grant"]["scope"] == "workable"
        assert preview(c, **args)["spec"] == pre["spec"]
        assert ok(c.call("run.list", {"state": "all"}))["data"]["runs"] == []  # nothing was started or recorded
        assert err(c.call("agent.preview", args, grant=value)) == "op.not_allowed"  # P only
        assert preview(c, **{**args, "ticket": "NOPE-999"})["blocked"]["code"] == "not_found"
        # an unattended start needs a new or empty folder: the preview says what is there (#1078, MED-1)
        assert pre["workdir"] == {"exists": False, "empty": True}
        folder = Path(pre["cwd_path"])
        folder.mkdir(parents=True, mode=0o700)
        assert preview(c, **args)["workdir"] == {"exists": True, "empty": True}
        (folder / ".claude").mkdir()
        assert preview(c, **args)["workdir"] == {"exists": True, "empty": False}
        # a Claude Code agent in the ticket's work folder is isolated from the folder's own settings and MCP files
        cl = preview(c, **{**args, "harness": "claude-code"})
        assert cl["cwd"]["kind"] == "workdir"
        assert cl["argv"][4:11] == [
            "claude",
            "--session-id",
            "@orch-session-id",
            "--setting-sources",
            "user",
            "--strict-mcp-config",
            "--append-system-prompt-file",
        ]
        assert cl["argv"][11] == "@orch-rules"
    finally:
        r.close()


@pytest.mark.parametrize("model", ["-x", "--yolo", "a b", "x;y"])
def test_a_bad_model_is_launch_invalid_at_preview_and_at_start(ready, model):
    w = ready
    c = w.conn()
    _, key = w.ticket()
    pre = preview(c, ticket=key, harness="codex", mode="work", model=model)
    assert pre["blocked"]["code"] == "launch.invalid" and "model" in pre["blocked"]["message"]
    assert err(start(c, ticket=key, harness="codex", mode="work", model=model, spec=pre["spec"])) == "launch.invalid"


def test_subagent_model_is_claude_code_only(ready):
    w = ready
    c = w.conn()
    _, key = w.ticket()
    assert preview(c, ticket=key, harness="codex", mode="work", subagent_model="haiku")["blocked"]["code"] == (
        "launch.invalid"
    )
    pre = preview(c, ticket=key, harness="claude-code", mode="refine", subagent_model="haiku")
    assert pre["blocked"] is None and "CLAUDE_CODE_SUBAGENT_MODEL" in pre["env"]


def test_a_live_claim_refuses(ready):
    w = ready
    c = w.conn()
    _, key = w.ticket()
    ok(c.call("claim", {"ref": key}, grant=w.grant, session=SESSION, idem=True))
    pre = preview(c, ticket=key, harness="codex", mode="work")
    assert pre["blocked"]["code"] == "launch.claimed"
    res = start(c, ticket=key, harness="codex", mode="work", spec=pre["spec"])
    assert err(res) == "launch.claimed" and res["exit"] == 3


def test_fish_as_shell_is_harness_shell_unsupported(ready, monkeypatch):
    w = ready
    c = w.conn()
    _, key = w.ticket()
    write_config(w, {"v": 1})  # the login-shell form
    monkeypatch.setenv("SHELL", "/usr/local/bin/fish")
    assert preview(c, ticket=key, harness="codex", mode="work")["blocked"]["code"] == "harness.shell_unsupported"
    monkeypatch.setenv("SHELL", "/bin/sh")
    pre = preview(c, ticket=key, harness="codex", mode="work")
    w.runner.link._config = parse({"v": 1})
    w.runner.link.env["SHELL"] = "/usr/local/bin/fish"  # the runner's own $SHELL decides at spawn
    w.runner.link.env["PATH"] = str(w.out)  # and nothing named codex could be found anyway
    assert err(start(c, ticket=key, harness="codex", mode="work", spec=pre["spec"])) == "harness.shell_unsupported"


# -- start -------------------------------------------------------------------------------------------------------------


def test_start_spawns_exactly_the_preview_with_the_grant_only_in_the_agents_env(ready):
    w = ready
    c = w.conn()
    _, key = w.ticket()
    pre = preview(c, ticket=key, harness="codex", mode="work", model="o3")
    assert pre["blocked"] is None and pre["login_shell"] is False and pre["program"] == pre["argv"][0]
    run = ok(start(c, ticket=key, harness="codex", mode="work", model="o3", spec=pre["spec"]))["data"]["run"]
    assert run["kind"] == "agent" and run["as_person"] is False and run["harness"] == "codex"
    assert run["session"].startswith("s_") and run["grant"] == gid_of(w.value) and run["grant_state"] == "ok"
    argv, env = read_out(w, "codex")
    assert [pre["program"], *argv] == pre["argv"]  # the spawned argv is the previewed one
    assert env["ORCH_GRANT"] == w.value and env["ORCH_SESSION"] == run["session"]
    assert env["ORCH_TICKET"] == key and env["ORCH_HARNESS"] == "codex"
    assert env["ORCH_WORKSPACE"] == str(Path(w.lab.root))  # the host names the workspace it started it for
    assert "SECRET_TOKEN" not in env
    allowed = set(pre["env"]) | {"PWD", "SHLVL", "_", "OLDPWD"}  # the shell's own bookkeeping
    assert set(env) <= allowed, set(env) - allowed


def test_an_idem_retry_starts_one_process(ready):
    w = ready
    c = w.conn()
    _, key = w.ticket()
    pre = preview(c, ticket=key, harness="codex", mode="fix")
    idem = new_idem()
    first = ok(start(c, idem, ticket=key, harness="codex", mode="fix", spec=pre["spec"]))["data"]["run"]
    again = ok(start(c, idem, ticket=key, harness="codex", mode="fix", spec=pre["spec"]))["data"]["run"]
    assert first["run"] == again["run"]
    assert [r["run"] for r in w.runner.sup.runs()] == [first["run"]]


def test_a_changed_value_or_grant_between_preview_and_start_is_launch_changed(ready):
    w = ready
    c = w.conn()
    _, key = w.ticket()
    pre = preview(c, ticket=key, harness="codex", mode="work")
    assert err(start(c, ticket=key, harness="codex", mode="refine", spec=pre["spec"])) == "launch.changed"
    assert err(start(c, ticket=key, harness="codex", mode="work", spec="0" * 64)) == "launch.changed"
    ok(deposit(c, issue(w)))  # a newer runner grant: the spec names the grant id
    assert err(start(c, ticket=key, harness="codex", mode="work", spec=pre["spec"])) == "launch.changed"
    assert err(start(c, ticket=key, harness="codex", mode="work", spec=pre["spec"], cols=10)) == "invalid.input"
    assert w.runner.sup.runs() == []


def test_start_is_p_only_and_needs_the_switch(ready):
    w = ready
    c = w.conn()
    _, key = w.ticket()
    pre = preview(c, ticket=key, harness="codex", mode="work")
    assert (
        err(
            c.call(
                "agent.start",
                {"ticket": key, "harness": "codex", "mode": "work", "spec": pre["spec"]},
                grant=w.value,
                idem=True,
            )
        )
        == "op.not_allowed"
    )  # an agent (its ORCH_GRANT) never starts one
    assert err(c.call("agent.start", {"ticket": key, "harness": "codex", "mode": "work", "spec": pre["spec"]})) == (
        "idem.required"
    )
    enablement.set_enabled(Path(w.lab.state), w.wid, False, UID)
    got = start(c, ticket=key, harness="codex", mode="work", spec=pre["spec"])
    assert err(got) == "runner.disabled" and got["error"]["detail"] == {"missing": "machine"}
    assert w.runner.sup.runs() == []


def test_a_revoked_grant_ends_grant_state_but_not_the_run(ready):
    w = ready
    c = w.conn()
    _, key = w.ticket()
    pre = preview(c, ticket=key, harness="codex", mode="work")
    run = ok(start(c, ticket=key, harness="codex", mode="work", spec=pre["spec"]))["data"]["run"]
    w.person_event("grant.revoked", grant=gid_of(w.value))
    wait_for(lambda: ok(c.call("run.list", {}))["data"]["runs"][0]["grant_state"] == "ended")
    assert ok(c.call("run.list", {}))["data"]["runs"][0]["state"] == "running" and run["run"]
    assert preview(c, ticket=key, harness="codex", mode="work")["blocked"]["detail"] == {"reason": "none"}


def test_the_secret_is_in_the_agents_env_and_nowhere_else(ready, tmp_path, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    w = ready
    c = w.conn()
    _, key = w.ticket()
    pre = preview(c, ticket=key, harness="codex", mode="work")
    res = start(c, ticket=key, harness="codex", mode="work", spec=pre["spec"])
    ok(c.call("run.list", {}))
    ok(c.call("whoami", {}))
    _, env = read_out(w, "codex")
    assert env["ORCH_GRANT"].endswith(SECRET_TAIL)
    assert SECRET_TAIL not in repr(c.frames) + repr(res) + caplog.text
    agent_env = (w.out / "codex.env").resolve()
    for path in Path(tmp_path).rglob("*"):
        if path.is_file() and not path.is_symlink() and path.resolve() != agent_env:
            assert SECRET_TAIL.encode() not in path.read_bytes(), path


# -- person-run harnesses --------------------------------------------------------------------------------------------


def test_a_person_run_harness_has_no_grant_no_session_no_claim_precheck(ready):
    w = ready
    grant_addon(w, "terminals", ("pty",))
    enablement.set_terminals(Path(w.lab.state), w.wid, True, UID, text="t")
    c = w.conn()
    _, key = w.ticket()
    ok(c.call("claim", {"ref": key}, grant=w.grant, session=SESSION, idem=True))
    pre = ok(c.call("term.preview", {"harness": "claude-code", "ticket": key, "mode": "context"}))["data"]
    assert pre["blocked"] is None and pre["as_person"] is True
    assert "ORCH_GRANT" not in pre["env"] and "ORCH_SESSION" not in pre["env"] and "ORCH_HARNESS" not in pre["env"]
    assert pre["argv"][-1] == f"Run orch show {key} --section current_state, then wait for my instructions."
    rid = c.send("term.open", {"harness": "claude-code", "ticket": key, "mode": "context", "spec": pre["spec"]},
                 idem=True)  # fmt: skip
    run = ok(c.answer(rid))["data"]["run"]
    assert (
        run["kind"] == "shell" and run["session"] is None and run["grant"] is None and run["harness"] == "claude-code"
    )
    argv, env = read_out(w, "claude")
    assert "ORCH_GRANT" not in env and "ORCH_SESSION" not in env and env["ORCH_TICKET"] == key
    assert "ORCH_WORKSPACE" not in env  # only an agent is told its workspace
    session = argv[argv.index("--session-id") + 1]  # the runner swapped the placeholder for the run's session uuid
    assert session == transcript.session_id(run["run"])
    assert [pre["program"], *argv] == [session if a == "@orch-session-id" else a for a in pre["argv"]]
    inter = ok(c.call("term.preview", {"harness": "codex"}))["data"]
    assert inter["blocked"] is None and inter["argv"] == [str(w.out.parent / "bin" / "codex")]


# -- review round 1 (#466) ---------------------------------------------------------------------------------------------


def test_concurrent_same_idem_starts_record_one_session_and_charge_one_start(ready):
    """Codex P2 #1 / Opus L2: two requests with one idem: one record, the session the process has, one rate charge."""
    w = ready
    c, d = w.conn(), w.conn()
    _, key = w.ticket()
    pre = preview(c, ticket=key, harness="codex", mode="work")
    hub = w.h.host.runners
    charged = []
    real = hub.take_start
    hub.take_start = lambda person: charged.append(person) or real(person)
    idem = new_idem()
    args = {"ticket": key, "harness": "codex", "mode": "work", "spec": pre["spec"]}
    c.ws.send("a1", "agent.start", args, opts={"idem": idem})
    d.ws.send("a2", "agent.start", args, opts={"idem": idem})
    one, two = ok(c.answer("a1"))["data"]["run"], ok(d.answer("a2"))["data"]["run"]
    assert one["run"] == two["run"] and one["session"] == two["session"]
    _, env = read_out(w, "codex")
    listed = ok(c.call("run.list", {}))["data"]["runs"]
    assert [r["run"] for r in listed] == [one["run"]] and listed[0]["session"] == env["ORCH_SESSION"]
    assert len(charged) == 1 and len(w.runner.sup.runs()) == 1


def test_a_same_idem_retry_with_another_spec_is_idem_conflict(ready):
    """Opus L3."""
    w = ready
    c = w.conn()
    _, key = w.ticket()
    pre = preview(c, ticket=key, harness="codex", mode="work")
    idem = new_idem()
    ok(start(c, idem, ticket=key, harness="codex", mode="work", spec=pre["spec"]))
    assert err(start(c, idem, ticket=key, harness="codex", mode="refine", spec="1" * 64)) == "idem.conflict"


def test_a_joiner_whose_first_start_failed_before_its_record_answers_outcome_unknown(ready):
    """Opus info (round 2): never internal."""
    import asyncio

    from orch.host.errors import HostError

    w = ready
    c, d = w.conn(), w.conn()
    _, key = w.ticket()
    pre = preview(c, ticket=key, harness="codex", mode="work")
    hub = w.h.host.runners

    async def failing_intent(*a, **kw):
        await asyncio.sleep(0.5)
        raise HostError("run.spawn_failed", "the intent could not be written")

    hub.intent = failing_intent
    idem = new_idem()
    args = {"ticket": key, "harness": "codex", "mode": "work", "spec": pre["spec"]}
    c.ws.send("j1", "agent.start", args, opts={"idem": idem})
    d.ws.send("j2", "agent.start", args, opts={"idem": idem})
    assert err(c.answer("j1")) == "run.spawn_failed"
    assert err(d.answer("j2")) == "outcome.unknown"


# -- P2-3: spawn policy and human-delegated children -------------------------------------------------------------------


def _main(w, c, key, **policy):
    args = dict(ticket=key, harness="claude-code", mode="work", **({"policy": policy} if policy else {}))
    pre = preview(c, **args)
    assert pre["blocked"] is None, pre["blocked"]
    return pre, ok(start(c, spec=pre["spec"], **args))["data"]["run"]


def test_policy_delegate_false_adds_the_disallow_flag_and_true_names_the_helper_model(ready):
    w = ready
    c = w.conn()
    _, key = w.ticket()
    off = preview(c, ticket=key, harness="claude-code", mode="work", policy={"delegate": False, "max_children": 2})
    assert "--disallowedTools=Agent,Task" in off["argv"] and off["policy"]["max_children"] == 2
    on = preview(c, ticket=key, harness="claude-code", mode="work", policy={"delegate": True, "models": ["haiku"]})
    assert "--disallowedTools=Agent,Task" not in on["argv"] and "CLAUDE_CODE_SUBAGENT_MODEL" in on["env"]
    none = preview(c, ticket=key, harness="claude-code", mode="work")
    assert none["policy"] is None and not any(a.startswith("--disallowedTools") for a in none["argv"])
    assert len({off["spec"], on["spec"], none["spec"]}) == 3  # the policy is in the spec digest
    bad = preview(c, ticket=key, harness="claude-code", mode="work", policy={"delegate": True, "models": ["x;y"]})
    assert bad["blocked"]["code"] == "launch.invalid"


def record(w, run: str) -> dict:
    return json.loads((Path(w.lab.state) / "hosts" / w.wid / "runs" / f"{run}.json").read_text())


def test_policy_is_stored_on_the_run_for_claude_code_and_ignored_for_codex(ready):
    w = ready
    c = w.conn()
    _, key = w.ticket()
    _, run = _main(w, c, key, delegate=False, max_children=4, models=[])
    assert record(w, run["run"])["policy"] == {"delegate": False, "max_children": 4, "models": []}
    pre = preview(c, ticket=key, harness="codex", mode="work", policy={"delegate": False})
    assert pre["policy"] is None and not any(a.startswith("--disallowedTools") for a in pre["argv"])


def delegate(c, key, parent, **kw):
    args = dict(
        ticket=key,
        harness="codex",
        mode="task",
        parent_run=parent,
        label="Review it",
        brief="Check X",
        expected="A report",
        **kw,
    )
    pre = preview(c, **args)
    return pre, args


def test_a_human_child_runs_in_the_parents_folder_with_a_dotted_session_and_the_task_prompt(ready):
    w = ready
    c = w.conn()
    _, key = w.ticket()
    _, parent = _main(w, c, key)
    ok(c.call("claim", {"ref": key}, grant=w.grant, session=parent["session"], idem=True))  # the parent holds it
    pre, args = delegate(c, key, parent["run"])
    assert pre["blocked"] is None, pre["blocked"]  # launch.claimed is skipped for the parent's own claim
    assert pre["session"] == parent["session"] + ".1" and pre["parent_run"] == parent["run"]
    assert pre["cwd"] == parent["cwd"]
    assert any("shared worktree" in x for x in pre["warnings"])
    child = ok(start(c, spec=pre["spec"], **args))["data"]["run"]
    assert child["session"] == parent["session"] + ".1" and child["label"] == "Review it"
    argv, env = read_out(w, "codex")
    text = argv[-1]
    assert f"delegated task of session {parent['session']} on ticket {key}" in text
    assert "Task: Review it." in text and "Brief: Check X" in text and "Expected output: A report" in text
    assert env["ORCH_SESSION"] == child["session"]
    rec = record(w, child["run"])
    assert rec["parent_run"] == parent["run"] and rec["spawned_by"] == "human"
    second, _ = delegate(c, key, parent["run"])
    assert second["session"] == parent["session"] + ".2"  # the next free n


def test_a_child_needs_a_live_parent_of_the_caller_on_the_same_ticket(ready):
    w = ready
    c = w.conn()
    _, key = w.ticket()
    _, other = w.ticket("Another ticket")
    _, parent = _main(w, c, key)
    ghost = "r_" + "0" * 26
    assert delegate(c, key, ghost)[0]["blocked"]["code"] == "not_found"
    assert delegate(c, other, parent["run"])[0]["blocked"]["code"] == "launch.invalid"
    no_parent = preview(c, ticket=key, harness="codex", mode="task")
    assert no_parent["blocked"]["code"] == "launch.invalid"
    with_mode = preview(c, ticket=key, harness="codex", mode="work", parent_run=parent["run"])
    assert with_mode["blocked"]["code"] == "launch.invalid"
    ok(c.call("run.stop", {"run": parent["run"]}))
    assert delegate(c, key, parent["run"])[0]["blocked"]["code"] == "launch.invalid"  # not live any more


def test_stopping_the_parent_stops_its_live_human_children(ready):
    w = ready
    c = w.conn()
    _, key = w.ticket()
    _, parent = _main(w, c, key)
    pre, args = delegate(c, key, parent["run"])
    child = ok(start(c, spec=pre["spec"], **args))["data"]["run"]
    ok(c.call("run.stop", {"run": parent["run"]}))
    wait_for(lambda: record(w, child["run"])["state"] == "exited")
    assert record(w, parent["run"])["state"] == "exited"


def test_a_runner_that_lost_the_grant_behind_the_hosts_back_gives_the_access_step(ready):
    """Run 13: the host believes the grant is current, the runner holds none. The start says so in plain words and
    the host forgets the grant, so the next view is the normal "give agent access" step, never a dead end."""
    w = ready
    c = w.conn()
    _, key = w.ticket()
    pre = preview(c, ticket=key, harness="codex", mode="work")
    assert pre["blocked"] is None
    assert w.runner.grants.drop(gid_of(w.value))  # as a runner that restarted without the host seeing a hello
    got = start(c, ticket=key, harness="codex", mode="work", spec=pre["spec"])
    assert err(got) == "launch.no_grant"
    assert "runner holds" not in str(got) and "Agent access" in str(got)
    assert preview(c, ticket=key, harness="codex", mode="work")["blocked"]["detail"] == {"reason": "none"}
    assert ok(c.call("whoami", {}))["data"]["runner"]["grant"] is None


def test_company_skills_reach_only_the_rules_file_of_a_claude_code_ticket_agent(ready, monkeypatch):
    """M3 H-3 end to end: accepted company skills are in the rules file the runner writes (below the rules), a Codex
    run of the same ticket gets nothing, and the harness receives no skill text in its argv or environment."""
    from orch.host import orgstate
    from orch.hubclient.orgbundles import skill_digest

    text = "Write invoice numbers as INV-2026-0001.\n## Steps\n1. Ask for the number."  # a normal skill with a heading
    monkeypatch.setattr(
        orgstate, "served",
        lambda service: {
            "skills": [{"name": "invoices", "text": text, "digest": skill_digest("invoices", text)}],
            "mcp": [],
        },
    )  # fmt: skip
    w = ready
    c = w.conn()
    _, key = w.ticket()
    pre = preview(c, ticket=key, harness="claude-code", mode="work")
    assert pre["blocked"] is None
    ok(start(c, ticket=key, harness="claude-code", mode="work", spec=pre["spec"]))
    argv, env = read_out(w, "claude")
    path = argv[argv.index("--append-system-prompt-file") + 1]
    body = Path(path).read_text()
    assert "### Company skill: invoices" in body and text in body
    opening = body[body.index("~~~~orch-skill-") :].splitlines()[0]
    bare = opening.split("orch-skill-")[0]  # the fence closes with the bare tilde run
    assert body.count(opening) == 1 and body.count("(end of " + opening.split("~")[-1] + ")") == 1  # its sentinel
    assert body.index(opening) < body.index("## Steps") < body.rindex("\n" + bare + "\n")  # inside its fence
    assert body.index("rules of the orch workspace") < body.index("## Company skills from your company hub")
    assert not any(text in a for a in argv) and not any(text in v for v in env.values())  # not in argv or environment
    pre2 = preview(c, ticket=key, harness="codex", mode="work")
    ok(start(c, ticket=key, harness="codex", mode="work", spec=pre2["spec"]))
    cargv, cenv = read_out(w, "codex")
    assert not any("invoices" in a for a in cargv) and not any(text in v for v in cenv.values())
