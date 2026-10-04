from datetime import datetime, timezone
from pathlib import Path

import pytest

from orch.addons.api import Snapshot
from orch.addons.manifest import load_manifest
from orch.addons.runtime import SlotView
from orch.addons.widgets import Action, Badge, Callout, Card, Copy, Link, Table, Text, Tile, widget_problems
from orch.errors import ValidationError
from orch.testing import AddonContract, FakeRunner
from orch_databricks.items import cluster_access

ADDON = Path(__file__).resolve().parents[1]
MANIFEST = load_manifest(ADDON)
FIXTURES = Path(__file__).with_name("fixtures")
HOST = "https://adb-3333333333333333.13.azuredatabricks.net"
CFG = f"[acme_test]\nhost = {HOST}\n"
ENVS = {"dev": f"acme_test @ {HOST}", "int": "simulated:int", "prod": "simulated:prod"}
RUNS = "Failed and running job runs"
MINE_FAILED = "[dev demo_user] DEMO-0001 nightly load"
COLLEAGUE_FAILED = "Export summary 2026-08-14 14:43:48"


@pytest.fixture
def cfg(home):
    (home / ".databrickscfg").write_text(CFG, encoding="utf-8")


@pytest.fixture
def dbx(cfg, orch_workspace):
    """Load the addon in the fake workspace and fetch the given envs into the cache."""
    def build(settings=None, runner=None, fetch=("dev", "int", "prod")):
        orch_workspace.enable("databricks", {"envs": ENVS, "demo": True, **(settings or {})})
        addon = orch_workspace.load(ADDON, runner=runner or FakeRunner.from_dir(FIXTURES / "acme_test"))
        provider = addon.obj.providers[0]
        for scope in fetch:
            orch_workspace.cache("databricks", provider.fetch(addon.ctx.provider_context(), scope, None))
        return addon
    return build


def render(fw, addon, slot):
    widgets = addon.obj.widgets(slot, SlotView(fw.ws, addon, slot))
    for w in widgets:
        assert widget_problems(w, slot=slot, manifest=MANIFEST) == [], w
    return widgets


def cards(widgets):
    return {w.title: w for w in widgets if isinstance(w, Card)}


def table(card):
    return next(b for b in card.body if isinstance(b, Table))


def texts(widget) -> str:
    """Every visible string in a widget tree, for contains-checks."""
    if widget is None:
        return ""
    if isinstance(widget, (str, int, float)):
        return str(widget)
    if isinstance(widget, (list, tuple)):
        return " ".join(texts(w) for w in widget)
    parts = [str(getattr(widget, f, "")) for f in ("title", "text", "label", "url", "target")]
    parts += [texts(getattr(widget, f, ())) for f in ("body", "rows", "columns")]
    return " ".join(parts)


def env_cards(widgets):
    return {c.title: c for c in cards(widgets)["Environments"].body}


def test_page_layout_and_simulated_banner(dbx, orch_workspace):
    widgets = render(orch_workspace, dbx(), "page.databricks")
    assert [w.title for w in widgets if isinstance(w, Card)] == [
        RUNS, "Pipelines", "Environments", "Deploy drift", "Compute for local development"]
    assert widgets[0] == Callout("neu", "int, prod are simulated (demo)", "Recorded data from the addon's fixtures, not a real workspace.")
    assert any(isinstance(w, Text) and "you created or run" in w.text for w in widgets)


def test_mine_is_the_default_and_hides_colleagues(dbx, orch_workspace):
    rows = table(cards(render(orch_workspace, dbx(), "page.databricks"))[RUNS]).rows
    assert [r[1] for r in rows] == ["acme-ingest-daily", MINE_FAILED, "acme-dbt-build", "[dev demo_user] dbt build"]


def test_scope_all_and_prefix(dbx, orch_workspace):
    rows = table(cards(render(orch_workspace, dbx({"scope": "all"}), "page.databricks"))[RUNS]).rows
    assert COLLEAGUE_FAILED in [r[1] for r in rows]
    addon = dbx({"scope": "prefix", "prefixes": "[dev demo_user]"})
    rows = table(cards(render(orch_workspace, addon, "page.databricks"))[RUNS]).rows
    # Simulated envs are exempt from the ownership filter: fixture data belongs to no one, and the demo must
    # show the int failure even though acme-ingest-daily does not start with the prefix.
    assert [r[1] for r in rows] == ["acme-ingest-daily", MINE_FAILED, "acme-dbt-build", "[dev demo_user] dbt build"]
    assert rows[0][0] == Badge("neu", "int · simulated")
    assert "acme-meter-pipeline" in texts(cards(render(orch_workspace, addon, "page.databricks"))["Pipelines"])
    assert render(orch_workspace, addon, "today.summary")[0].value == 2


def test_simulated_envs_ignore_the_owner(dbx, orch_workspace):
    """Under mine, a simulated run is shown even when its owner is not the signed-in user."""
    addon = dbx({"envs": {"int": "simulated:int"}}, fetch=("int",))
    snap = next(iter(SlotView(orch_workspace.ws, addon, "page.databricks").snapshots("databricks")))
    orch_workspace.cache("databricks", snap.replace(
        me="someone.else@example.com", items=tuple({**i, "me": "someone.else@example.com"} for i in snap.items)))
    rows = table(cards(render(orch_workspace, addon, "page.databricks"))[RUNS]).rows
    assert [r[1] for r in rows] == ["acme-ingest-daily", "acme-dbt-build"]


def test_prefix_without_a_prefix_warns(dbx, orch_workspace):
    widgets = render(orch_workspace, dbx({"scope": "prefix"}), "page.databricks")
    assert any(isinstance(w, Callout) and w.title == "No name prefix saved" for w in widgets)


def test_ticket_link_or_create_ticket(dbx, orch_workspace):
    rows = table(cards(render(orch_workspace, dbx({"scope": "all"}), "page.databricks"))[RUNS]).rows
    by_name = {r[1]: r for r in rows}
    assert by_name[MINE_FAILED][4] == Link("DEMO-0001", "/t/DEMO-0001")
    assert by_name[COLLEAGUE_FAILED][4] == Action("create_ticket", "Create ticket", "dev|111111111111111|111111111111202")
    assert by_name["acme-ingest-daily"][0] == Badge("neu", "int · simulated")
    assert by_name[MINE_FAILED][5] == Link("Open", f"{HOST}/#job/990000000000001/run/990000000000101")


def test_create_ticket_only_for_failed_runs(dbx, orch_workspace):
    rows = table(cards(render(orch_workspace, dbx({"scope": "all"}), "page.databricks"))[RUNS]).rows
    by_name = {r[1]: r for r in rows}
    assert by_name["acme-dbt-build"][4] is None and by_name["[dev demo_user] dbt build"][4] is None
    assert isinstance(by_name["acme-ingest-daily"][4], Action)


def kept_after(orch_workspace, addon, scope, **change):
    """What the scheduler stores when a fetch fails with no items: the previous items, complete False, me None."""
    good = next(s for s in SlotView(orch_workspace.ws, addon, "page.databricks").snapshots("databricks") if s.scope == scope)
    orch_workspace.cache("databricks", good.replace(me=None, complete=False, **change))
    return good


@pytest.mark.parametrize("cfg_text, needle", [
    (CFG.replace(HOST, "https://adb-999.azuredatabricks.net"), "now points to https://adb-999.azuredatabricks.net"),
    ("[other]\nhost = https://adb-1.azuredatabricks.net\n", "not in ~/.databrickscfg"),
])
def test_last_known_after_a_host_change_or_missing_profile(dbx, orch_workspace, home, cfg_text, needle):
    addon = dbx({"scope": "all"}, fetch=("dev",))
    (home / ".databrickscfg").write_text(cfg_text, encoding="utf-8")
    snap = addon.obj.providers[0].fetch(addon.ctx.provider_context(), "dev", None)
    assert snap.health == "error" and needle in snap.message and snap.items == ()
    kept_after(orch_workspace, addon, "dev", health=snap.health, message=snap.message)
    widgets = render(orch_workspace, addon, "page.databricks")
    rows = env_cards(widgets)["dev"].body[0].rows
    assert ("Failed runs (24 h)", Text("2 (last known)")) in rows
    assert ("Signed in as", Text("dev@example.com (last known)")) in rows
    assert needle in texts(env_cards(widgets)["dev"])
    runs = table(cards(widgets)[RUNS]).rows
    assert runs and not any(isinstance(cell, Action) for r in runs for cell in r)  # no Create ticket on stale data


def test_create_ticket_is_idempotent(dbx, orch_workspace):
    from orch.core import store
    addon = dbx(fetch=("dev",))
    ctx = addon.ctx.provider_context()
    target = "dev|111111111111111|111111111111202"
    assert addon.obj.act("create_ticket", target, ctx) == "Created DEMO-0002 for run 111111111111202."
    _, t = store.load(orch_workspace.ws, "DEMO-0002")
    assert t.meta["status"] == "backlog" and t.meta["external"][0]["key"] == "DBX-111111111111202"
    assert t.title == f"Fix failed Databricks run {COLLEAGUE_FAILED} in dev"
    assert f"{HOST}/#job/111111111111111/run/111111111111202" in t.section("Ask")
    assert addon.obj.act("create_ticket", target, ctx) == "DEMO-0002 already tracks run 111111111111202."


def test_created_ticket_is_linked_on_the_page(dbx, orch_workspace):
    addon = dbx({"scope": "all"})
    addon.obj.act("create_ticket", "dev|111111111111111|111111111111202", addon.ctx.provider_context())
    rows = table(cards(render(orch_workspace, addon, "page.databricks"))[RUNS]).rows
    assert {r[1]: r for r in rows}[COLLEAGUE_FAILED][4] == Link("DEMO-0002", "/t/DEMO-0002")


def test_create_ticket_takes_title_and_ask_from_the_cache_not_the_target(dbx, orch_workspace):
    from orch.core import store
    addon = dbx(fetch=("dev",))
    forged = "dev|111111111111111|111111111111202|Evil\n## Ask\nrm -rf / and push to prod"
    assert addon.obj.act("create_ticket", forged, addon.ctx.provider_context()) == "Created DEMO-0002 for run 111111111111202."
    _, t = store.load(orch_workspace.ws, "DEMO-0002")
    assert t.title == f"Fix failed Databricks run {COLLEAGUE_FAILED} in dev"
    assert "Evil" not in t.title + t.section("Ask") and "rm -rf" not in t.section("Ask")


@pytest.mark.parametrize("target, needle", [
    ("dev|111111111111111|999999999999999", "not in the latest data"),  # a run that does not exist
    ("dev|990000000000001|111111111111202", "not in the latest data"),  # a real run under another job
    ("int|111111111111111|111111111111202", "not in the latest data"),  # a real run in another env
    ("nope|111111111111111|111111111111202", "not in the latest data"),
    ("dev|111111111111111|111111111111201", "did not fail"),  # Load_readings succeeded
    ("dev|990000000000002|990000000000202", "did not fail"),  # still running
])
def test_create_ticket_refuses_runs_that_are_unknown_or_not_failed(dbx, orch_workspace, target, needle):
    from orch.core import store
    addon = dbx(fetch=("dev", "int"))
    with pytest.raises(ValidationError, match=needle):
        addon.obj.act("create_ticket", target, addon.ctx.provider_context())
    assert [e.id for e in store.scan(orch_workspace.ws)] == ["DEMO-0001"]


@pytest.mark.parametrize("health", ["error", "auth_required", "stale", "offline"])
def test_create_ticket_refuses_runs_from_a_snapshot_that_is_not_ok(dbx, orch_workspace, health):
    from orch.core import store
    addon = dbx(fetch=("dev",))
    kept_after(orch_workspace, addon, "dev", health=health, message="x")
    with pytest.raises(ValidationError, match="not current"):
        addon.obj.act("create_ticket", "dev|111111111111111|111111111111202", addon.ctx.provider_context())
    assert [e.id for e in store.scan(orch_workspace.ws)] == ["DEMO-0001"]


def test_created_ticket_links_the_cached_host(dbx, orch_workspace):
    """The run lives in the workspace it was fetched from, even if the settings now pin another host."""
    from orch.core import store
    addon = dbx(fetch=("dev",))
    other = "https://adb-999.azuredatabricks.net"
    orch_workspace.enable("databricks", {"envs": {**ENVS, "dev": f"acme_test @ {other}"}, "demo": True})
    addon.obj.act("create_ticket", "dev|111111111111111|111111111111202", addon.ctx.provider_context())
    _, t = store.load(orch_workspace.ws, "DEMO-0002")
    assert f"{HOST}/#job/111111111111111/run/111111111111202" in t.section("Ask") and other not in t.section("Ask")


def test_created_ticket_has_no_link_without_a_safe_cached_host(dbx, orch_workspace):
    from orch.core import store
    addon = dbx(fetch=("dev",))
    good = next(s for s in SlotView(orch_workspace.ws, addon, "page.databricks").snapshots("databricks") if s.scope == "dev")
    items = tuple({**i, "host": "javascript:alert(1)"} if i.get("type") == "env" else i for i in good.items)
    orch_workspace.cache("databricks", good.replace(items=items))
    addon.obj.act("create_ticket", "dev|111111111111111|111111111111202", addon.ctx.provider_context())
    _, t = store.load(orch_workspace.ws, "DEMO-0002")
    assert "Run:" not in t.section("Ask") and "javascript" not in t.section("Ask")


def test_create_ticket_needs_a_fetched_env(dbx, orch_workspace):
    addon = dbx(fetch=())
    with pytest.raises(ValidationError, match="not in the latest data"):
        addon.obj.act("create_ticket", "dev|111111111111111|111111111111202", addon.ctx.provider_context())


@pytest.mark.parametrize("action, target", [
    ("create_ticket", "dev|x|1|n"), ("create_ticket", ""), ("logged_in", "int"), ("logged_in", "nope"), ("deploy", "dev")])
def test_bad_actions_are_refused(dbx, orch_workspace, action, target):
    addon = dbx(fetch=())
    with pytest.raises(ValidationError):
        addon.obj.act(action, target, addon.ctx.provider_context())


def test_logged_in_clears_the_hold(dbx, orch_workspace):
    from orch_databricks.provider import AuthHold
    addon = dbx(runner=FakeRunner.from_dir(FIXTURES / "expired"), fetch=("dev",))
    hold = AuthHold(addon.ctx.state_dir)
    assert hold.waiting("dev", None)
    assert addon.obj.act("logged_in", "dev", addon.ctx.provider_context()) == "Checking the login for dev at the next refresh."
    assert not hold.waiting("dev", None)


def test_last_known_counts_when_login_expired(dbx, orch_workspace):
    addon = dbx(fetch=("dev",))
    good = next(s for s in SlotView(orch_workspace.ws, addon, "page.databricks").snapshots("databricks") if s.scope == "dev")
    # What the scheduler stores after an expired login: previous items, health auth_required, complete False.
    orch_workspace.cache("databricks", good.replace(health="auth_required", message="login needed", me=None, complete=False))
    widgets = render(orch_workspace, addon, "page.databricks")
    dev = env_cards(widgets)["dev"]
    rows = dev.body[0].rows
    assert ("Failed runs (24 h)", Text("1 (last known)")) in rows and ("Auth", Badge("warn", "login needed")) in rows
    assert ("Signed in as", Text("dev@example.com (last known)")) in rows
    assert Copy("Copy re-login", f"databricks auth login --host {HOST} --profile acme_test") in dev.body
    assert Action("logged_in", "I logged in", "dev") in dev.body and dev.role == "warn"
    assert MINE_FAILED in texts(cards(widgets)[RUNS])  # the kept rows stay listed


def test_unknown_is_never_zero(dbx, orch_workspace):
    widgets = render(orch_workspace, dbx({"envs": {"dev": ENVS["dev"]}}, fetch=()), "page.databricks")
    runs = cards(widgets)[RUNS]
    assert table(runs).empty == "Unknown: no environment has data yet."
    assert "Unknown for dev (not fetched yet)." in texts(runs)
    assert ("Failed runs (24 h)", "unknown") in env_cards(widgets)["dev"].body[0].rows


def test_one_env_failing_keeps_the_others(dbx, orch_workspace, home):
    (home / ".databrickscfg").write_text(CFG.replace(HOST, "https://adb-999.azuredatabricks.net"), encoding="utf-8")
    widgets = render(orch_workspace, dbx(), "page.databricks")
    envs = env_cards(widgets)
    assert envs["dev"].role == "err" and "now points to https://adb-999.azuredatabricks.net" in texts(envs["dev"])
    assert envs["int"].role == "ok" and "acme-ingest-daily" in texts(cards(widgets)[RUNS])


def test_simulated_without_demo_shows_why(dbx, orch_workspace):
    envs = env_cards(render(orch_workspace, dbx({"demo": False}, fetch=("int",)), "page.databricks"))
    assert envs["int"].role == "err" and "not marked as a demo" in texts(envs["int"])


def test_compute_never_defaults_to_an_env_that_is_not_signed_in(dbx, orch_workspace):
    addon = dbx(runner=FakeRunner.from_dir(FIXTURES / "expired"))
    body = cards(render(orch_workspace, addon, "page.databricks"))["Compute for local development"].body
    assert [getattr(b, "title", None) for b in body[:2]] == ["int · simulated", "prod · simulated"]
    assert body[2] == Text("dev: sign in to see compute (login needed).")


def test_compute_ranking_access_and_copy(dbx, orch_workspace):
    body = cards(render(orch_workspace, dbx(fetch=("int",)), "page.databricks"))["Compute for local development"].body
    int_card = next(b for b in body if getattr(b, "title", "") == "int · simulated")
    assert int_card.body[0] == Text("Recommended: acme-int-dev (DBR 16.4, matches your databricks-connect 16.4.2)")
    rows = int_card.body[1].rows
    assert [r[0] for r in rows] == ["acme-int-dev", "acme-int-shared", "acme-int-sql"]
    assert rows[0][3] == Badge("ok", "You can use") and rows[0][6] == Copy("Copy cluster ID", "0101-000000-intdev")
    assert rows[1][3] == Badge("neu", "Unknown: shared cluster; its permissions are not read")
    assert rows[2][6] == Copy("Copy HTTP path", "/sql/1.0/warehouses/w-int")


def test_cluster_access_states():
    me = "me@example.com"
    assert cluster_access({"data_security_mode": "SINGLE_USER", "single_user_name": "ME@example.com"}, me) == ("can_use", "dedicated to you")
    assert cluster_access({"data_security_mode": "DATA_SECURITY_MODE_DEDICATED", "single_user_name": "x@example.com"}, me) == (
        "no_access", "dedicated to another user")
    assert cluster_access({"data_security_mode": "USER_ISOLATION", "creator_user_name": me}, me) == ("can_use", "you created it")
    assert cluster_access({"data_security_mode": "USER_ISOLATION", "creator_user_name": "x@example.com"}, me)[0] == "unknown"


def test_drift_table(dbx, orch_workspace):
    rows = table(cards(render(orch_workspace, dbx(), "page.databricks"))["Deploy drift"]).rows
    assert rows[0][1:] == ("Deploy drift is not checked for real workspaces yet", "unknown", "unknown", "unknown")
    assert rows[1][1:5] == (Badge("warn", "int is 3 commits ahead of prod"), "4 of 4", "a1b2c3d", "5 h ago")
    assert rows[2][3:5] == ("9f8e7d6", "3 d ago")


def test_cached_drift_of_a_real_env_is_not_shown(dbx, orch_workspace):
    """A snapshot from before drift was dropped for real envs may still hold a bundle-summary drift item."""
    addon = dbx({"envs": {"dev": ENVS["dev"]}}, fetch=())
    old = {"id": "drift:dev", "type": "drift", "label": "dev", "role": "warn", "text": "2 of 3 resources not deployed",
           "deployed": 1, "total": 3}
    orch_workspace.cache("databricks", Snapshot("databricks", "dev", datetime.now(timezone.utc), items=(old,)))
    [row] = table(cards(render(orch_workspace, addon, "page.databricks"))["Deploy drift"]).rows
    assert row[1:] == ("Deploy drift is not checked for real workspaces yet", "unknown", "unknown", "unknown")


def test_today_tile_and_from_addons(dbx, orch_workspace):
    addon = dbx()
    assert render(orch_workspace, addon, "today.summary") == [Tile("Databricks failures", 2, "err", href="/addons/databricks/")]
    [card] = render(orch_workspace, addon, "today.from_addons")
    assert card.title == "Databricks: 2 failed runs" and card.role == "err" and len(table(card).rows) == 2


def test_today_ignores_colleagues_by_default(dbx, orch_workspace):
    addon = dbx({"envs": {"dev": ENVS["dev"]}})
    assert render(orch_workspace, addon, "today.summary")[0].value == 1
    [card] = render(orch_workspace, addon, "today.from_addons")
    assert [r[1] for r in table(card).rows] == [MINE_FAILED] and card.title == "Databricks: 1 failed run"
    assert render(orch_workspace, dbx({"envs": {"dev": ENVS["dev"]}, "scope": "all"}), "today.summary")[0].value == 2


def test_nothing_on_today_without_failures(dbx, orch_workspace):
    addon = dbx({"envs": {"prod": "simulated:prod"}}, fetch=("prod",))
    assert render(orch_workspace, addon, "today.summary") == [] and render(orch_workspace, addon, "today.from_addons") == []


def test_no_envs_lists_profiles_and_picks_none(cfg, orch_workspace):
    addon = orch_workspace.load(ADDON, runner=FakeRunner())
    [callout] = render(orch_workspace, addon, "page.databricks")
    assert callout.title == "No environments mapped yet" and f"acme_test ({HOST})" in callout.text
    assert addon.obj.providers[0].scopes(addon.ctx.provider_context()) == []


def test_render_survives_odd_cached_items(dbx, orch_workspace):
    addon = dbx({"scope": "all"}, fetch=())
    odd = ({"id": "x", "label": "x", "role": "you", "text": "x"},
           {"id": "r", "type": "run", "label": "r", "role": "purple", "text": "t", "state": "failed", "name": "n" * 5000,
            "url": "javascript:alert(1)", "owners": "not a list", "started_at": "yesterday", "run_id": "1", "job_id": "2"},
           {"id": "c", "type": "cluster", "label": "c", "role": "ok", "text": "running", "auto_stop": True, "access": "maybe"},
           {"id": "d", "type": "drift", "label": "d", "role": "ok", "text": "t", "deployed": "4", "total": None},
           {"id": "e", "type": "env", "label": "e", "role": "ok", "text": "t", "me": 42})
    orch_workspace.cache("databricks", Snapshot("databricks", "dev", datetime.now(timezone.utc), items=odd))
    for slot in ("page.databricks", "today.summary", "today.from_addons"):
        render(orch_workspace, addon, slot)  # asserts every widget is valid
    rows = table(cards(render(orch_workspace, addon, "page.databricks"))[RUNS]).rows
    assert rows[0][1] == "n" * 200 and rows[0][2] == Badge("neu", "t") and rows[0][5] is None and rows[0][3] == "unknown"


class TestAddon(AddonContract):
    addon_dir = ADDON
    runner = FakeRunner(strict=False)
