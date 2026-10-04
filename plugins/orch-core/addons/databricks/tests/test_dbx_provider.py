from datetime import datetime, timedelta
from pathlib import Path

import pytest

from orch.addons.manifest import load_manifest
from orch.testing import FakeRunner, ProviderContract
from orch_databricks.dbcli import READ_ONLY
from orch_databricks.provider import AuthHold, DatabricksProvider

ADDON = Path(__file__).resolve().parents[1]
MANIFEST = load_manifest(ADDON)
FIXTURES = Path(__file__).with_name("fixtures")
HOST = "https://adb-3333333333333333.13.azuredatabricks.net"
CFG = f"[DEFAULT]\n[__settings__]\nauth_storage = secure\n[acme_test]\nhost = {HOST}\nauth_type = databricks-cli\n"
LIVE = {"envs": {"dev": f"acme_test @ {HOST}"}}
SIM = {"envs": {"int": "simulated:int", "prod": "simulated:prod"}, "demo": True}
ME = "dev@example.com"


@pytest.fixture
def cfg(home):
    (home / ".databrickscfg").write_text(CFG, encoding="utf-8")
    return home


def ctx_for(fw, settings, runner):
    fw.enable("databricks", settings)
    return fw.provider_context(MANIFEST, runner=runner)


def recorded(*extra):
    runner = FakeRunner.from_dir(FIXTURES / "acme_test")
    for argv, kw in extra:
        runner.recordings.insert(0, FakeRunner().add(argv, **kw).recordings[0])
    return runner


def assert_read_only(runner):
    for argv in runner.calls:
        assert argv[0] == "databricks" and tuple(argv[1:3]) in READ_ONLY, argv
        assert argv[-4:] == ("--profile", "acme_test", "-o", "json"), argv


def kinds(snap):
    out = {}
    for item in snap.items:
        out[item["type"]] = out.get(item["type"], 0) + 1
    return out


def test_scopes_come_from_settings_only(cfg, orch_workspace):
    p = DatabricksProvider()
    assert p.scopes(orch_workspace.provider_context(MANIFEST)) == []  # a profile in ~/.databrickscfg is never picked
    both = {"envs": {**LIVE["envs"], **SIM["envs"]}, "demo": True}
    assert p.scopes(ctx_for(orch_workspace, both, FakeRunner())) == ["dev", "int", "prod"]


def test_run_url_is_built_from_the_pinned_host():
    from orch_databricks.items import run_item
    run = {"run_id": 1, "job_id": 2, "run_page_url": "https://evil.example/#job/2/run/1"}
    assert run_item("dev", run, HOST)["url"] == f"{HOST}/#job/2/run/1"
    assert "url" not in run_item("dev", run, None)
    assert "url" not in run_item("dev", {**run, "job_id": "2/../x"}, HOST)


def test_live_fetch_with_recorded_cli(cfg, orch_workspace):
    runner = recorded()
    snap = DatabricksProvider().fetch(ctx_for(orch_workspace, LIVE, runner), "dev", None)
    assert snap.health == "ok" and snap.me == ME and snap.complete
    assert kinds(snap) == {"env": 1, "run": 4, "pipeline": 4, "warehouse": 2, "cluster": 1}
    runs = {i["run_id"]: i for i in snap.items if i["type"] == "run"}
    assert runs["111111111111202"]["state"] == "failed" and runs["111111111111202"]["role"] == "err"
    assert runs["990000000000202"]["state"] == "running" and runs["111111111111201"]["state"] == "succeeded"
    assert runs["990000000000101"]["owners"] == [ME] and runs["990000000000101"]["url"] == f"{HOST}/#job/990000000000001/run/990000000000101"
    pipes = {i["name"]: i for i in snap.items if i["type"] == "pipeline"}
    assert pipes["Daily meter export"]["role"] == "err" and pipes["Daily meter export"]["url"] == f"{HOST}/pipelines/00000000-0000-4000-8000-000000000011"
    [cluster] = [i for i in snap.items if i["type"] == "cluster"]
    assert (cluster["access"], cluster["dbr"]) == ("unknown", "16.4")
    assert all(w["access"] == "can_use" and w["http_path"].startswith("/sql/1.0/warehouses/") for w in snap.items if w["type"] == "warehouse")
    assert_read_only(runner)
    since = int(runner.calls[1][4])
    assert abs(since / 1000 - (datetime.now().timestamp() - 24 * 3600)) < 120


def test_real_env_with_databricks_yml_never_runs_bundle(cfg, orch_workspace):
    """bundle summary executes the repo's scripts and Python, so drift is never read from a real workspace."""
    (orch_workspace.root / "databricks.yml").write_text("bundle:\n  name: acme\n", encoding="utf-8")
    runner = recorded()
    snap = DatabricksProvider().fetch(ctx_for(orch_workspace, {**LIVE, "drift": True}, runner), "dev", None)
    assert snap.health == "ok" and not [i for i in snap.items if i["type"] == "drift"]
    assert not any("bundle" in argv for argv in runner.calls) and runner.calls
    assert_read_only(runner)


def test_no_drift_setting():
    assert MANIFEST.field("drift") is None


def test_expired_login_holds_until_logged_in(cfg, orch_workspace):
    runner = FakeRunner.from_dir(FIXTURES / "expired")
    ctx = ctx_for(orch_workspace, LIVE, runner)
    p = DatabricksProvider()
    first = p.fetch(ctx, "dev", None)
    assert first.health == "auth_required" and first.items == ()
    assert f"databricks auth login --host {HOST} --profile acme_test" in first.message
    assert p.fetch(ctx, "dev", first).health == "auth_required" and len(runner.calls) == 1  # no CLI call while held
    assert AuthHold(ctx.addon.state_dir).clear("dev") is True
    p.fetch(ctx, "dev", first)
    assert len(runner.calls) == 2


def test_token_cache_change_releases_the_hold(cfg, orch_workspace):
    runner = FakeRunner.from_dir(FIXTURES / "expired")
    ctx = ctx_for(orch_workspace, LIVE, runner)
    p = DatabricksProvider()
    p.fetch(ctx, "dev", None)
    (cfg / ".databricks").mkdir()
    (cfg / ".databricks" / "token-cache.json").write_text("{}", encoding="utf-8")
    p.fetch(ctx, "dev", None)
    assert len(runner.calls) == 2


def test_success_clears_an_old_hold(cfg, orch_workspace):
    ctx = ctx_for(orch_workspace, LIVE, recorded())
    hold = AuthHold(ctx.addon.state_dir)
    hold.hold("prod", None)
    hold.hold("dev", 123.0)  # a different mtime: not waiting any more
    assert DatabricksProvider().fetch(ctx, "dev", None).health == "ok"
    assert hold.waiting("prod", None) and not hold.clear("dev")


def test_host_mismatch_never_calls_the_cli(home, orch_workspace):
    (home / ".databrickscfg").write_text(CFG.replace(HOST, "https://adb-999.azuredatabricks.net"), encoding="utf-8")
    runner = FakeRunner()
    snap = DatabricksProvider().fetch(ctx_for(orch_workspace, LIVE, runner), "dev", None)
    assert snap.health == "error" and "now points to https://adb-999.azuredatabricks.net" in snap.message and HOST in snap.message
    assert runner.calls == []


def test_unpinned_profile_is_refused_without_a_call(cfg, orch_workspace):
    runner = FakeRunner()
    snap = DatabricksProvider().fetch(ctx_for(orch_workspace, {"envs": {"dev": "acme_test"}}, runner), "dev", None)
    assert snap.health == "error" and "pin the workspace host" in snap.message and runner.calls == []


@pytest.mark.parametrize("rec, health, needle", [
    ({"raises": "missing"}, "error", "not installed"),
    ({"returncode": 1, "stderr": "Error: dial tcp: lookup adb: no such host"}, "offline", "no such host"),
])
def test_probe_failures(cfg, orch_workspace, rec, health, needle):
    runner = FakeRunner([{"argv": ["databricks", "current-user", "me", "--profile", "acme_test", "-o", "json"], **rec}])
    snap = DatabricksProvider().fetch(ctx_for(orch_workspace, LIVE, runner), "dev", None)
    assert snap.health == health and needle in snap.message and snap.items == ()


def test_one_failing_section_keeps_the_rest(cfg, orch_workspace):
    runner = recorded((["databricks", "warehouses", "list", "--profile", "acme_test", "-o", "json"],
                       {"returncode": 1, "stderr": "Error: PERMISSION_DENIED: no access to warehouses\n"}))
    snap = DatabricksProvider().fetch(ctx_for(orch_workspace, LIVE, runner), "dev", None)
    assert snap.health == "error" and not snap.complete and "warehouses: Error: PERMISSION_DENIED" in snap.message
    assert kinds(snap)["run"] == 4 and kinds(snap)["problem"] == 1 and "warehouse" not in kinds(snap)


def test_local_databricks_connect_version_is_read(cfg, orch_workspace):
    (orch_workspace.root / "uv.lock").write_text('[[package]]\nname = "databricks-connect"\nversion = "16.4.2"\n', encoding="utf-8")
    snap = DatabricksProvider().fetch(ctx_for(orch_workspace, LIVE, recorded()), "dev", None)
    assert [i for i in snap.items if i["type"] == "env"][0]["connect_version"] == "16.4.2"


def test_simulated_envs_need_no_cli_and_are_marked(orch_workspace):
    runner = FakeRunner()
    ctx = ctx_for(orch_workspace, SIM, runner)
    snap = DatabricksProvider().fetch(ctx, "int", None)
    assert snap.health == "ok" and snap.me == "demo.user@example.com" and runner.calls == []
    assert all(i["simulated"] is True and i["env"] == "int" for i in snap.items)
    failed = next(i for i in snap.items if i.get("run_id") == "9001")
    started = datetime.fromisoformat(failed["started_at"])
    assert timedelta(minutes=41) < ctx.now() - started < timedelta(minutes=43) and "started_minutes_ago" not in failed


def test_simulated_needs_the_demo_setting(orch_workspace):
    snap = DatabricksProvider().fetch(ctx_for(orch_workspace, {"envs": {"int": "simulated:int"}}, FakeRunner()), "int", None)
    assert snap.health == "error" and "not marked as a demo" in snap.message and snap.items == ()


def test_unknown_simulated_fixture(orch_workspace):
    snap = DatabricksProvider().fetch(ctx_for(orch_workspace, {"envs": {"qa": "simulated:qa"}, "demo": True}, FakeRunner()), "qa", None)
    assert snap.health == "error" and "available: int, prod" in snap.message


def test_env_removed_from_settings(orch_workspace):
    snap = DatabricksProvider().fetch(ctx_for(orch_workspace, SIM, FakeRunner()), "gone", None)
    assert snap.health == "error" and "no longer in the settings" in snap.message


class TestLiveProvider(ProviderContract):
    @pytest.fixture
    def provider(self):
        return DatabricksProvider()

    @pytest.fixture
    def provider_ctx(self, cfg, orch_workspace):
        return ctx_for(orch_workspace, LIVE, recorded())


class TestSimulatedProvider(ProviderContract):
    @pytest.fixture
    def provider(self):
        return DatabricksProvider()

    @pytest.fixture
    def provider_ctx(self, orch_workspace):
        return ctx_for(orch_workspace, SIM, FakeRunner())
