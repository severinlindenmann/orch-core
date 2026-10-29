"""M3 host H-4 T5: the company MCP proxy wired into agent launches (Opus host conditions 3, 4, 5, 7).

A claude-code ticket work folder agent whose workspace has an accepted, served MCP server gets ``org_mcp`` in its spawn
payload: the proxy port, a per-run capability and the server names. Every other launch gets nothing; a finished or
failed run loses its capability; nothing the agent sees (config file, argv, env) names the upstream or a secret."""

from __future__ import annotations

import asyncio
import http.client
import json
from types import SimpleNamespace

import pytest

from orch.host import agents, orgmcp, orgstate, runners
from orch.host.launch import launch_argv
from orch.runner import mcpconfig
from orch.runner.config import parse
from tests.test_mcp_proxy import FakeUp

LOGIN = parse({"v": 1})
UPSTREAM_URL = "https://crm.internal.example/secret-path/mcp"
WS = "w_demo"


def record(name="crm", auth="none", digest="sha256:" + "a" * 64):
    return {"name": name, "url": UPSTREAM_URL, "auth": auth, "secret": None, "description": "", "digest": digest}


class Hub(SimpleNamespace):
    """The parts of RunnerHub that a launch touches."""

    def __init__(self, fail_dispatch=None):
        super().__init__(
            intents=[],
            payloads=[],
            failed=[],
            fail_dispatch=fail_dispatch,
            grants=SimpleNamespace(watch=lambda: None),
        )

    async def intent(self, wid, fields, payload, **kw):
        self.intents.append(fields["run"])

    def _audit(self, *a, **kw):
        pass

    async def fail(self, run, code):
        self.failed.append((run, code))

    async def dispatch(self, run, payload, audit=False):
        self.payloads.append(payload)
        if self.fail_dispatch is not None:
            raise self.fail_dispatch
        return {"run": run}


@pytest.fixture
def world(monkeypatch):
    state = SimpleNamespace(
        records=[record()], hub_id="h1", seq=3, asked=0, secret_calls=[], iso=False, policy={}, ups=[]
    )
    state.service = SimpleNamespace(workspace_id=WS)
    state.call = SimpleNamespace(service=state.service)

    def served(svc):
        state.asked += 1
        return {"skills": [], "mcp": list(state.records)}

    monkeypatch.setattr(orgstate, "served", served)
    monkeypatch.setattr(orgstate, "_state", lambda svc: SimpleNamespace(hub_id=state.hub_id, binding_seq=state.seq))
    monkeypatch.setattr(orgstate, "isolated", lambda svc: state.iso)
    monkeypatch.setattr(orgstate, "tool_policy", lambda svc, server: state.policy)
    monkeypatch.setattr(orgmcp, "org_secret", lambda svc, name: state.secret_calls.append(name))
    return state


def claude_argv():
    argv, _, _ = launch_argv("claude-code", "work", None, "ORC-7", LOGIN, workdir=True)
    assert argv.count("@orch-mcp") == 1
    return argv


def data(run="r_1", argv=None, **launch):
    base = {
        "ticket": "ORC-7", "grant": "g1", "argv": argv if argv is not None else claude_argv(), "env_names": [],
        "env_fixed": {"ORCH_WORKSPACE": "/ws"}, "cwd": {"kind": "workdir"}, "login_shell": False, "create": None,
        "mkdir": False,
    }  # fmt: skip
    return {
        "run": run, "person": "p1", "harness": "claude-code", "mode": "work", "model": None, "subagent_model": None,
        "label": None, "cwd": {"kind": "workdir"}, "cols": 80, "rows": 24, "idem": "i" + run, "spec": "s",
        "launch": {**base, **launch},
    }  # fmt: skip


def launch(world, hub, **kw):
    return agents._launch(world.call, hub, data(**kw))  # noqa: SLF001


def start(world, hub, **kw):
    return asyncio.run(launch(world, hub, **kw))


async def post(port, cap, name="crm", method="tools/list"):
    def go():
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method})
        c.request("POST", f"/m/{name}", body, {"Host": f"127.0.0.1:{port}", "Authorization": f"Bearer {cap}"})
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    return await asyncio.to_thread(go)


def fake_hub(world, **kw):
    hub = Hub(**kw)
    orgmcp.proxy_for(hub, _upstream=lambda rec, bearer: world.ups.append(FakeUp()) or world.ups[-1])
    return hub


def test_a_served_server_gives_the_claude_code_agent_an_org_mcp_payload(world):
    async def go():
        hub = fake_hub(world)
        await launch(world, hub)
        org = hub.payloads[0]["org_mcp"]
        assert set(org) == {"port", "capability", "servers"} and org["servers"] == ["crm"]
        assert mcpconfig.problem(org) is None
        assert await post(org["port"], org["capability"]) == 200  # usable against the proxy
        assert await post(org["port"], org["capability"], name="other") == 403  # not in the run's server set
        await hub.org_mcp.close()

    asyncio.run(go())


def test_the_capability_is_bound_to_workspace_binding_seq_run_and_servers(world):
    async def go():
        hub = fake_hub(world)
        await launch(world, hub)
        (cap,) = hub.org_mcp._caps.values()  # noqa: SLF001
        assert (cap.ws, cap.hub_id, cap.binding_seq, cap.run, list(cap.servers)) == (WS, "h1", 3, "r_1", ["crm"])
        await hub.org_mcp.close()

    asyncio.run(go())


def test_nothing_served_means_no_org_mcp_and_no_proxy(world):
    world.records = []
    hub = Hub()
    start(world, hub)
    assert "org_mcp" not in hub.payloads[0]
    assert getattr(hub, "org_mcp", None) is None or hub.org_mcp.port is None  # no listener was started


@pytest.mark.parametrize(
    ("harness", "mode", "workdir"),
    [("codex", "work", True), ("claude-code", "work", False), ("shell", None, False), ("codex", "work", False)],
)
def test_codex_worktree_repo_root_and_shell_launches_get_nothing(world, harness, mode, workdir):
    argv, _, _ = launch_argv(harness, mode, None, None if harness == "shell" else "ORC-7", LOGIN, workdir=workdir)
    assert "@orch-mcp" not in argv
    hub = Hub()
    start(world, hub, argv=argv)
    assert "org_mcp" not in hub.payloads[0]
    assert getattr(hub, "org_mcp", None) is None or hub.org_mcp.port is None  # not even a listener


def test_a_misplaced_or_doubled_placeholder_gets_nothing(world):
    for argv in (
        ["claude", "@orch-mcp"],
        ["claude", "--mcp-config", "@orch-mcp", "@orch-mcp"],
        ["@orch-mcp"],
        ["claude", "--x", "@orch-mcp"],
    ):
        assert asyncio.run(orgmcp.launch_mcp(world.call, SimpleNamespace(), "r", argv)) is None
    assert world.asked == 0


def test_any_doubt_reading_the_served_set_is_no_org_mcp_not_a_failed_launch(world, monkeypatch):
    def boom(svc):
        raise OSError("custody.unsafe_mode")

    monkeypatch.setattr(orgstate, "served", boom)
    hub = Hub()
    start(world, hub)
    assert "org_mcp" not in hub.payloads[0] and hub.intents == ["r_1"]


def test_an_unknown_binding_is_no_org_mcp(world, monkeypatch):
    def nobody(svc):
        raise RuntimeError("hub.not_joined")

    monkeypatch.setattr(orgstate, "_state", nobody)
    hub = Hub()
    start(world, hub)
    assert "org_mcp" not in hub.payloads[0]


def test_names_the_agent_config_would_refuse_are_dropped_with_a_log_line(world, caplog):
    world.records = [record("orch-x"), record("Bad Name"), record("crm")]

    async def go():
        hub = fake_hub(world)
        with caplog.at_level("WARNING"):
            await launch(world, hub)
        assert hub.payloads[0]["org_mcp"]["servers"] == ["crm"]
        await hub.org_mcp.close()

    asyncio.run(go())
    assert "orch-x" in caplog.text


def test_only_unusable_names_is_no_org_mcp(world):
    world.records = [record("orch-x")]
    hub = Hub()
    start(world, hub)
    assert "org_mcp" not in hub.payloads[0]


def test_a_run_that_ends_loses_its_capability_and_the_other_runs_keeps_its_own(world):
    async def go():
        hub = fake_hub(world)
        await launch(world, hub, run="r_a")
        await launch(world, hub, run="r_b")
        ca, cb = (p["org_mcp"] for p in hub.payloads)
        assert ca["capability"] != cb["capability"] and ca["port"] == cb["port"]
        assert await post(ca["port"], ca["capability"]) == 200 and await post(cb["port"], cb["capability"]) == 200
        # the real RunnerHub._save of a run's end: a state that is not LIVE revokes
        hub._save_lock = asyncio.Lock()
        hub.store = SimpleNamespace(records={}, rev=0, save=lambda r: None, remove=lambda r: None)
        hub._evict = lambda: []
        hub.publish = lambda r: None
        await runners.RunnerHub._save(hub, {"run": "r_a", "state": "exited"})  # noqa: SLF001
        assert await post(ca["port"], ca["capability"]) == 401
        assert await post(cb["port"], cb["capability"]) == 200
        await runners.RunnerHub._save(hub, {"run": "r_b", "state": "running"})  # noqa: SLF001
        assert await post(cb["port"], cb["capability"]) == 200  # a live save revokes nothing
        await runners.RunnerHub._save(hub, {"run": "r_b", "state": "spawn_failed"})  # noqa: SLF001
        assert await post(cb["port"], cb["capability"]) == 401
        await hub.org_mcp.close()

    asyncio.run(go())


@pytest.mark.parametrize("state", ["lost", "spawn_failed", "exited", "stopped"])
def test_every_not_live_state_revokes(world, state):
    async def go():
        hub = fake_hub(world)
        await launch(world, hub)
        org = hub.payloads[0]["org_mcp"]
        hub._save_lock = asyncio.Lock()
        hub.store = SimpleNamespace(records={}, rev=0, save=lambda r: None, remove=lambda r: None)
        hub._evict = lambda: []
        hub.publish = lambda r: None
        await runners.RunnerHub._save(hub, {"run": "r_1", "state": state})  # noqa: SLF001
        assert await post(org["port"], org["capability"]) == 401
        await hub.org_mcp.close()

    asyncio.run(go())


def test_a_start_that_fails_to_spawn_revokes_the_capability(world):
    async def go():
        hub = fake_hub(world, fail_dispatch=RuntimeError("runner gone"))
        with pytest.raises(RuntimeError):
            await launch(world, hub)
        org = hub.payloads[0]["org_mcp"]
        assert await post(org["port"], org["capability"]) == 401
        await hub.org_mcp.close()

    asyncio.run(go())


def test_a_work_folder_failure_revokes_the_capability(world, monkeypatch):
    from orch.host.errors import HostError

    def boom(call, key, expect):
        raise HostError("launch.cwd_changed", "x")

    monkeypatch.setattr(agents, "make_workdir", boom)

    async def go():
        hub = fake_hub(world)
        with pytest.raises(HostError):
            await launch(world, hub, mkdir=True)
        assert hub.failed == [("r_1", "launch.cwd_changed")]
        assert hub.org_mcp._caps == {}  # noqa: SLF001
        await hub.org_mcp.close()

    asyncio.run(go())


def test_the_agent_sees_neither_the_upstream_url_nor_a_secret(world, tmp_path):
    world.records = [{**record(auth="hub_secret"), "secret": "crm-token"}]
    world.iso = True

    async def go():
        hub = fake_hub(world)
        await launch(world, hub)
        await hub.org_mcp.close()
        return hub.payloads[0]

    payload = asyncio.run(go())
    env = {"ORCH_WORKSPACE": "/ws", "PATH": "/usr/bin"}
    argv = mcpconfig.prepare(tmp_path, "r_1", list(payload["argv"]), env, payload["org_mcp"])
    config = next(tmp_path.rglob("r_1.json")).read_text()
    cap = payload["org_mcp"]["capability"]
    rest = {k: v for k, v in payload.items() if k != "org_mcp"}
    for name, blob in (
        ("config", config), ("argv", json.dumps(argv)), ("env", json.dumps(env)), ("payload", json.dumps(rest)),
    ):  # fmt: skip
        for needle in (UPSTREAM_URL, "crm.internal.example", "secret-path", "crm-token"):
            assert needle not in blob, (name, needle)
    assert cap in config and cap not in json.dumps(argv) and cap not in json.dumps(env)  # the file alone carries it
    assert json.loads(config)["mcpServers"]["crm"]["url"].startswith("http://127.0.0.1:")


def test_tier_a_never_asks_for_a_secret(world):
    """Condition 3: a hub_secret server is not served in tier A, and if a capability for one existed anyway the proxy
    refuses before it asks the secret lookup."""
    world.records = [record(auth="hub_secret")]
    world.iso = False

    async def go():
        hub = fake_hub(world)
        hub.org_mcp_services[WS] = world.service
        cap = hub.org_mcp.mint(
            WS, 3, "r_x", world.records, hub_id="h1"
        )  # forced: bypasses the served filter on purpose
        port = await hub.org_mcp.start()
        status = await post(port, cap)
        await hub.org_mcp.close()
        return status

    assert asyncio.run(go()) != 200
    assert world.secret_calls == [] and world.ups == []


def test_the_secret_lookup_is_a_stub_and_unknown_workspaces_serve_nothing(world):
    assert orgmcp.org_secret(world.service, "crm-token") is None
    hub = Hub()
    proxy = orgmcp.proxy_for(hub)
    assert proxy._served("nobody") == [] and proxy._tool_policy("nobody", "crm") == {}  # noqa: SLF001
    assert proxy._isolated("nobody") is False and proxy._secret("nobody", "x") is None  # noqa: SLF001
    assert orgmcp.proxy_for(hub) is proxy  # one proxy per hub


def test_revoking_without_a_proxy_is_a_no_op():
    hub = Hub()
    orgmcp.revoke_run(hub, "r")
    orgmcp.revoke_workspace(hub, "w")
    orgmcp.revoke_workspace(SimpleNamespace(runners=hub), "w")
    orgmcp.shutdown(hub)


def test_host_stop_closes_the_listener_and_drops_every_capability(world):
    async def go():
        hub = fake_hub(world)
        await launch(world, hub)
        org = hub.payloads[0]["org_mcp"]
        hub._watching, hub._checks, hub.subs = None, {}, {}
        runners.RunnerHub.end_all(hub, "host.restarting")
        assert hub.org_mcp is None
        await asyncio.sleep(0.2)
        with pytest.raises(OSError):
            await post(org["port"], org["capability"])

    asyncio.run(go())


def test_a_revoke_landing_while_the_launch_reads_served_leaves_no_capability(world, monkeypatch):
    """The TOCTOU: served is read, an unbind/rebind revokes the workspace, then the mint happens. The epoch read first
    makes the mint refuse, so the agent starts without company MCP and nothing usable exists."""
    hub = fake_hub(world)
    real = orgstate.served

    def served_then_revoked(svc):
        out = real(svc)
        orgmcp.revoke_workspace(hub, WS)
        return out

    monkeypatch.setattr(orgstate, "served", served_then_revoked)

    async def go():
        await launch(world, hub)
        assert "org_mcp" not in hub.payloads[0]
        assert hub.org_mcp._caps == {}  # noqa: SLF001
        await hub.org_mcp.close()

    asyncio.run(go())


def test_the_binding_callback_follows_the_hub_state(world, monkeypatch):
    hub = fake_hub(world)
    proxy = hub.org_mcp
    hub.org_mcp_services[WS] = world.service
    assert proxy._binding(WS) == ("h1", 3) and proxy._binding("nobody") is None  # noqa: SLF001
    monkeypatch.setattr(orgstate, "_state", lambda svc: (_ for _ in ()).throw(RuntimeError("hub.not_joined")))
    assert proxy._binding(WS) is None  # noqa: SLF001


def test_the_host_redactor_and_request_check_know_the_capability_shape():
    from orch.host.auth import Redactor, secret_in_request
    from orch.host.mcpshared import new_capability

    cap = new_capability()
    grant = "gr_01J9ZP0000000000000000000H." + "z" * 43
    assert cap not in Redactor(grant).text(f"x {cap} y") and "[redacted]" in Redactor(grant).text(f"x {cap} y")
    assert secret_in_request({"args": {"a": [cap]}, "auth": {}})
