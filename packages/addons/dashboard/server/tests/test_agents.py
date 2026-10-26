"""M7b Start agent, the server side without a runner: the host link's op list (never `grant.deposit`), the typed routes
and their closed bodies, the projections (host text untrusted, no secret, no session) and the run.sub runner item.
The real host and runner are in test_realhost_agents.py."""

import re
from pathlib import Path

import pytest
from conftest import ORIGIN

from orch_dashboard import hostlink
from orch_dashboard.adapters import runs as project
from orch_dashboard.hostlink import HostLink

SPEC = "ab" * 32
IDEM = "a_01M4Y46B485AXD4CBEJZ15Z1TV"
GRANT = "gr_01M4Y46B485AXD4CBEJZ15Z1TV"
SECRET = "demo-not-a-real-secret-tail"
RUNNER_GRANT = {"id": GRANT, "until": "2026-10-15T20:00:00Z", "scope": "workable"}


def hdr(token):
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "Content-Type": "application/json"}


# -- the host link -------------------------------------------------------------------------------------------------


def test_the_agent_ops_go_under_run_1_and_grant_deposit_is_not_one_of_ours():
    for op in ("agent.preview", "agent.start", "grant.deposit_status", "grant.forget"):
        assert hostlink.OPS[op] == 1 and op in hostlink.RUN_OPS
    assert "grant.deposit" not in hostlink.OPS and "grant.deposit" in hostlink.FORBIDDEN


@pytest.mark.parametrize("kw", [{}, {"write": True, "idem": IDEM}])
def test_the_http_process_cannot_send_grant_deposit(kw):
    link = object.__new__(HostLink)  # no socket at all: the refusal comes before anything could be sent
    with pytest.raises(ValueError, match="grant.deposit"):
        link.call("grant.deposit", {"deposit_id": "0" * 26}, **kw)


def test_no_module_of_this_server_names_grant_deposit_but_the_refusal():
    root = Path(hostlink.__file__).parent
    hits = sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*.py") if re.search(r"\"grant\.deposit\"", p.read_text())
    )
    assert hits == ["hostlink.py"]
    assert hostlink.__file__ and "auth" not in HostLink.call.__code__.co_varnames  # no way to pass `auth.grant`


# -- routes, with the host faked --------------------------------------------------------------------------------------


class Host:
    def __init__(self):
        self.calls = []
        self.answers = {}

    def call(self, op, args=None, write=False, idem=None, **kw):
        self.calls.append((op, args, write, idem))
        answer = self.answers[op]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def host(gateway, monkeypatch):
    fake = Host()
    monkeypatch.setattr(gateway, "call", lambda ws, op, args=None: fake.call(op, args))
    monkeypatch.setattr(gateway, "enabled", lambda ws: fake)
    return fake


def url(stub, path):
    return f"/api/w/{stub.ws}{path}"


PREVIEW = {
    "ticket": "DEMO-1",
    "spec": SPEC,
    "argv": ["/bin/codex", "--secret-flag"],
    "program": "/bin/codex",
    "display": "codex 'work on DEMO-1'",
    "login_shell": False,
    "cwd": {"kind": "worktree", "repo": "billing", "ticket": "DEMO-1"},
    "cwd_path": "/w/billing.worktrees/0a1b2c3d-DEMO-1",
    "will_create_worktree": True,
    "env": ["TERM", "ORCH_GRANT", "ORCH_SESSION"],
    "grant": {**RUNNER_GRANT, "secret": SECRET},
    "blocked": None,
    "warnings": ["shared working copy: other runs and you work in the same directory"],
}


def test_preview_sends_the_typed_args_and_projects_what_the_dialog_shows(client, token, stub, host):
    host.answers["agent.preview"] = PREVIEW
    body = {"ticket": "DEMO-1", "harness": "codex", "mode": "work", "model": "o3", "label": "night"}
    res = client.post(url(stub, "/agents/preview"), json=body, headers=hdr(token))
    assert res.status_code == 200, res.text
    assert host.calls == [("agent.preview", {**body, "worktree": True}, False, None)]
    got = res.json()
    assert got["display"] == PREVIEW["display"] and got["cwd_path"] == PREVIEW["cwd_path"]
    assert got["cwd"] == {"kind": "worktree", "repo": "billing", "ticket": "DEMO-1"}
    assert got["will_create_worktree"] is True and got["ticket"] == "DEMO-1" and got["spec"] == SPEC
    assert got["grant"] == RUNNER_GRANT and got["env"] == PREVIEW["env"]
    assert "argv" not in got and "program" not in got and SECRET not in res.text


def test_preview_withholds_hidden_text_and_keeps_only_closed_blocked_detail():
    data = {
        **PREVIEW,
        "display": "codex ‮'x'",
        "cwd_path": "/w/​x",
        "blocked": {
            "code": "launch.no_grant",
            "message": "no runner grant",
            "detail": {"reason": "expiring", "secret": SECRET},
        },
        "warnings": ["ok", "bad⁦", *[f"w{i}" for i in range(10)]],
    }
    got = project.agent_preview(data)
    assert got["display"] is None and got["display_hidden"] is True
    assert got["cwd_path"] is None and got["cwd_path_hidden"] is True
    assert got["blocked"] == {"code": "launch.no_grant", "message": "no runner grant", "detail": {"reason": "expiring"}}
    assert got["warnings"] == ["ok", *[f"w{i}" for i in range(7)]]
    blocked = project.agent_preview({**data, "blocked": {"code": "runner.disabled", "detail": {"missing": "x"}}})
    assert blocked["blocked"]["detail"] == {} and blocked["blocked"]["message"] is None
    assert project.agent_preview({**data, "grant": {**RUNNER_GRANT, "id": f"{GRANT}.{SECRET}"}})["grant"] is None


def test_preview_passes_the_work_folder_state_and_never_invents_it():
    """The page starts a NEW ticket's agent unattended only in a new or empty work folder: the state reaches it, and a
    host that said nothing stays unknown (no `workdir` key), never "empty"."""
    base = {"cwd": {"kind": "workdir", "ticket": "DEMO-1"}, "cwd_path": "/w/.orch-work/DEMO-1", "ticket": "DEMO-1"}
    assert project.agent_preview({**base, "workdir": {"exists": False, "empty": True}})["workdir"] == {
        "exists": False,
        "empty": True,
    }
    assert project.agent_preview({**base, "workdir": {"exists": True, "empty": False}})["workdir"] == {
        "exists": True,
        "empty": False,
    }
    partials = (
        {"exists": True},
        {"empty": True},
        {},
        {"exists": 1, "empty": 0},
        {"exists": "yes", "empty": True},
        {"exists": False, "empty": None},
    )
    for partial in partials:
        assert "workdir" not in project.agent_preview({**base, "workdir": partial}), partial
    assert "workdir" not in project.agent_preview(base)
    assert "workdir" not in project.agent_preview({**base, "workdir": "empty"})


@pytest.mark.parametrize(
    "body",
    [
        {"ticket": "DEMO-1", "harness": "shell", "mode": "work"},
        {"ticket": "DEMO-1", "harness": "codex", "mode": "context2"},
        {"ticket": "DEMO-1", "harness": "codex", "mode": "work", "model": "-x"},
        {"ticket": "DEMO-1", "harness": "codex", "mode": "work", "model": "a b"},
        {"ticket": "DEMO-1", "harness": "codex", "mode": "work", "label": "x" * 41},
        {"ticket": "DEMO-1", "harness": "codex", "mode": "work", "worktree": "yes"},
        {"ticket": "DEMO-1", "harness": "codex", "mode": "work", "cwd": {"kind": "workspace"}},
        {"ticket": "DEMO-1", "harness": "codex", "mode": "work", "argv": ["sh"]},
        {"ticket": "../x", "harness": "codex", "mode": "work"},
        {"harness": "codex", "mode": "work"},  # the host requires a ticket (section 12.3)
    ],
)
def test_preview_bodies_are_closed(client, token, stub, host, body):
    assert client.post(url(stub, "/agents/preview"), json=body, headers=hdr(token)).status_code == 400
    assert host.calls == []


RUN = {
    "run": "r_01M4Y46B485AXD4CBEJZ15Z1TV",
    "rev": 1,
    "kind": "agent",
    "ticket": "DEMO-1",
    "harness": "codex",
    "mode": "work",
    "model": None,
    "subagent_model": None,
    "label": None,
    "session": "s_01M4Y46B485AXD4CBEJZ15Z1TV",
    "grant": GRANT,
    "grant_state": "ok",
    "as_person": False,
    "state": "starting",
    "started_at": "2026-10-15T10:00:00Z",
    "cwd": {"kind": "workspace"},
    "exit": None,
}


def test_start_sends_the_preview_values_spec_and_idem_and_answers_the_run(client, token, stub, host):
    host.answers["agent.start"] = {"run": RUN}
    body = {"ticket": "DEMO-1", "harness": "claude-code", "mode": "refine", "subagent_model": "haiku",
            "worktree": False, "spec": SPEC, "idem": IDEM, "cols": 100}  # fmt: skip
    res = client.post(url(stub, "/agents/start"), json=body, headers=hdr(token))
    assert res.status_code == 200, res.text
    sent = {"ticket": "DEMO-1", "harness": "claude-code", "mode": "refine", "subagent_model": "haiku",
            "worktree": False, "spec": SPEC, "cols": 100}  # fmt: skip
    assert host.calls == [("agent.start", sent, True, IDEM)]
    run = res.json()["run"]
    assert run["grant_state"] == "ok" and run["kind"] == "agent"
    assert "session" not in run and "grant" not in run and GRANT not in res.text


@pytest.mark.parametrize(
    "extra", [{"spec": "x"}, {"idem": "r_01M4Y46B485AXD4CBEJZ15Z1TV"}, {"idem": None}, {"rows": 1000}]
)
def test_start_needs_a_spec_and_the_dialogs_idem(client, token, stub, host, extra):
    body = {"ticket": "DEMO-1", "harness": "codex", "mode": "work", "spec": SPEC, "idem": IDEM, **extra}
    assert client.post(url(stub, "/agents/start"), json=body, headers=hdr(token)).status_code == 400
    assert host.calls == []


@pytest.mark.parametrize(
    ("code", "http"),
    [("launch.changed", 409), ("run.limit", 409), ("launch.no_grant", 409), ("runner.unavailable", 503)],
)
def test_start_refusals_keep_their_code_and_the_hosts_message_as_text(client, token, stub, host, code, http):
    from orch_dashboard.errors import HostError

    host.answers["agent.start"] = HostError({"code": code, "message": "8 agent runs at most", "retryable": False}, 9)
    body = {"ticket": "DEMO-1", "harness": "codex", "mode": "work", "spec": SPEC, "idem": IDEM}
    res = client.post(url(stub, "/agents/start"), json=body, headers=hdr(token))
    assert res.status_code == http and res.json()["error"]["code"] == code
    assert res.json()["error"]["message"] == "8 agent runs at most"


def test_runner_card_is_whoami_runner_without_anything_else(client, token, stub, host):
    host.answers["whoami"] = {
        "person": "p_x",
        "runner": {"connected": True, "enabled": True, "terminals": False, "gen": "g1",
                   "grant": {**RUNNER_GRANT, "secret": SECRET}},
    }  # fmt: skip
    res = client.get(url(stub, "/runner"), headers=hdr(token))
    assert res.json() == {"runner": {"connected": True, "enabled": True, "terminals": False, "grant": RUNNER_GRANT}}
    host.answers["whoami"] = {"person": "p_x"}
    assert client.get(url(stub, "/runner"), headers=hdr(token)).json() == {"runner": None}


@pytest.mark.parametrize(
    ("answer", "out"),
    [
        ({"deposit": "confirmed"}, {"deposit": "confirmed"}),
        ({"deposit": "rejected", "reason": "grant.expiring"}, {"deposit": "rejected", "reason": "grant.expiring"}),
        ({"deposit": "rejected", "reason": "the host says no"}, {"deposit": "rejected", "reason": "other"}),
        ({"deposit": "none"}, {"deposit": "none"}),
        ({"deposit": "pending", "secret": SECRET}, {"deposit": "unknown"}),
    ],
)
def test_deposit_status(client, token, stub, host, answer, out):
    host.answers["grant.deposit_status"] = answer
    res = client.get(url(stub, "/runner/deposits/" + "0" * 25 + "1"), headers=hdr(token))
    assert res.json() == out
    assert host.calls == [("grant.deposit_status", {"deposit_id": "0" * 25 + "1"}, False, None)]
    assert client.get(url(stub, "/runner/deposits/hr_" + "0" * 26), headers=hdr(token)).status_code == 400


def test_forget_is_a_write_with_the_browsers_idem(client, token, stub, host):
    host.answers["grant.forget"] = {"forgotten": True}
    res = client.post(url(stub, "/runner/forget"), json={"grant": GRANT, "idem": IDEM}, headers=hdr(token))
    assert res.json() == {"forgotten": True}
    assert host.calls == [("grant.forget", {"grant": GRANT}, True, IDEM)]
    bad = client.post(url(stub, "/runner/forget"), json={"grant": f"{GRANT}.x", "idem": IDEM}, headers=hdr(token))
    assert bad.status_code == 400


# -- projections -------------------------------------------------------------------------------------------------------


def test_run_projection_carries_grant_state_and_never_the_session_or_grant_id():
    out = project.run({**RUN, "grant_state": "ended"})
    assert out["grant_state"] == "ended" and "session" not in out and "grant" not in out
    assert project.run({**RUN, "grant_state": "weird"})["grant_state"] is None
    assert project.run({**RUN, "subagent_model": "hai​ku"})["subagent_model"] is None


def test_run_sub_runner_items_are_passed_as_the_grant_only():
    items = project.items(
        [
            {"id": "3.g1", "t": "runner", "grant": {**RUNNER_GRANT, "secret": SECRET}},
            {"id": "3.g2", "t": "runner", "grant": None},
            {"id": "3.g3", "t": "runner"},
            {"id": "4", "t": "other"},
        ]
    )
    assert items == [{"t": "runner", "grant": RUNNER_GRANT}, {"t": "runner", "grant": None}]


def test_runner_card_passes_the_hosts_startable_rule_when_well_formed(client, token, stub, host):
    until = "2026-10-24T10:00:00Z"
    host.answers["whoami"] = {
        "person": "p_x",
        "runner": {"connected": True, "enabled": True, "terminals": False,
                   "grant": {**RUNNER_GRANT, "startable": True, "startable_until": until, "secret": SECRET}},
    }  # fmt: skip
    got = client.get(url(stub, "/runner"), headers=hdr(token)).json()["runner"]["grant"]
    assert got == {**RUNNER_GRANT, "startable": True, "startable_until": until}
    host.answers["whoami"]["runner"]["grant"] = {**RUNNER_GRANT, "startable": False, "startable_until": "soon"}
    got = client.get(url(stub, "/runner"), headers=hdr(token)).json()["runner"]["grant"]
    assert got == {**RUNNER_GRANT, "startable": False, "startable_until": None}
    host.answers["whoami"]["runner"]["grant"] = {**RUNNER_GRANT, "startable": "yes"}  # not a bool: dropped
    assert client.get(url(stub, "/runner"), headers=hdr(token)).json()["runner"]["grant"] == RUNNER_GRANT


# -- P2-3: spawn policy and the Delegate sheet's task body ---------------------------------------------------------


def test_preview_passes_policy_and_a_delegated_task_to_the_host_typed(client, token, stub, host):
    host.answers["agent.preview"] = PREVIEW
    parent = "r_01M4Y46B485AXD4CBEJZ15Z1TV"
    policy = {
        "ticket": "DEMO-1",
        "harness": "claude-code",
        "mode": "work",
        "policy": {"delegate": True, "max_children": 2, "models": ["haiku"]},
    }
    task = {
        "ticket": "DEMO-1",
        "harness": "codex",
        "mode": "task",
        "parent_run": parent,
        "label": "Review it",
        "brief": "Check X",
        "expected": "A report",
    }
    for body in (policy, task):
        assert client.post(url(stub, "/agents/preview"), json=body, headers=hdr(token)).status_code == 200
    assert host.calls == [("agent.preview", {**policy, "worktree": True}, False, None),
                          ("agent.preview", {**task, "worktree": True}, False, None)]  # fmt: skip


@pytest.mark.parametrize(
    "extra",
    [
        {"policy": {"delegate": "yes"}},
        {"policy": {"max_children": 10}},
        {"policy": {"models": ["a", "b"]}},
        {"policy": {"surprise": 1}},
        {"parent_run": "r_nope"},
        {"brief": "x" * 4001},
        {"expected": "x" * 401},
    ],
)
def test_policy_and_task_bodies_are_closed(client, token, stub, host, extra):
    body = {"ticket": "DEMO-1", "harness": "codex", "mode": "task", **extra}
    assert client.post(url(stub, "/agents/preview"), json=body, headers=hdr(token)).status_code == 400
    assert host.calls == []
