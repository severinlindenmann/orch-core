import json
import subprocess

import pytest

from addon_fixtures import GOOD
from orch.addons.api import Snapshot
from orch.addons.manifest import parse_manifest
from orch.addons.runner import AddonRunError
from orch.testing import (PAYLOAD, AddonContract, FakeProvider, FakeRunner, ProviderContract, check_provider,
                          fake_workspace, run_addon_contract)
from orch.testing.pytest_plugin import orch_user_dir, orch_workspace  # noqa: F401
from addon_fixtures import PROVIDER, make_addon

M = parse_manifest(GOOD)


def test_fake_runner_matches_records_and_raises(tmp_path):
    (tmp_path / "status.json").write_text(json.dumps({"argv": ["git", "status", "*"], "stdout_json": {"a": 1}}))
    (tmp_path / "more.json").write_text(json.dumps([{"argv": ["gh", "api", "user"], "raises": "timeout"},
                                                    {"argv": ["gh", "pr", "list"], "raises": "missing"}]))
    r = FakeRunner.from_dir(tmp_path)
    assert json.loads(r(["git", "status", "--porcelain"], 5).stdout) == {"a": 1}
    with pytest.raises(AddonRunError, match="timed out"):
        r(["gh", "api", "user"], 5)
    with pytest.raises(AddonRunError, match="not installed"):
        r(["gh", "pr", "list"], 5)
    with pytest.raises(AssertionError, match="no recording"):
        r(["git", "log"], 5)
    assert r.calls[0] == ("git", "status", "--porcelain")
    assert FakeRunner(strict=False)(["x"], 1).returncode == 127


def test_fake_workspace_builds_tickets_and_enables(orch_user_dir, tmp_path):
    from orch.addons.userfiles import workspace_addons
    fw = fake_workspace(tmp_path / "w", tickets=[{"title": "One", "status": "open", "external": ["GH-1"]},
                                                 {"title": "Two", "sections": {"Ask": "Do it"}}])
    assert fw.tickets == ["DEMO-0001", "DEMO-0002"]
    assert fw.ws.config["customer"] == "acme" and (fw.root / "orchestrator" / "config.json").is_file()
    fw.enable("hello-status", {"greeting": "Hi"})
    assert workspace_addons(fw.root) == {"hello-status": {"enabled": True, "config": {"greeting": "Hi"}, "background": False}}
    assert fw.context(M).settings == {"greeting": "Hi"}


def test_check_provider_accepts_fake_providers(orch_workspace):
    ctx = orch_workspace.provider_context(M, runner=FakeRunner())
    for p in FakeProvider.each_health():
        assert check_provider(p, ctx) == [], p.health
    for kind in ("reviews", "issues", "pages", "status"):
        assert check_provider(FakeProvider(kind=kind), ctx) == [], kind


def test_check_provider_flags_direct_subprocess(orch_workspace):
    class Sneaky(FakeProvider):
        def fetch(self, ctx, scope, previous):
            subprocess.run(["git", "status"])
            return super().fetch(ctx, scope, previous)
    problems = check_provider(Sneaky(), orch_workspace.provider_context(M, runner=FakeRunner()))
    assert any("subprocess" in p and "ctx.run" in p for p in problems)


def test_check_provider_flags_wrong_snapshots(orch_workspace):
    class WrongScope(FakeProvider):
        def fetch(self, ctx, scope, previous):
            return super().fetch(ctx, scope, previous).replace(scope="other")
    class BadItems(FakeProvider):
        def fetch(self, ctx, scope, previous):
            return super().fetch(ctx, scope, previous).replace(items=({"title": "no keys"},))
    ctx = orch_workspace.provider_context(M, runner=FakeRunner())
    assert any("scope" in p for p in check_provider(WrongScope(), ctx))
    assert any("missing" in p for p in check_provider(BadItems(kind="reviews"), ctx))
    assert any("id" in p for p in check_provider(FakeProvider(id="Bad Id"), ctx))


def test_run_addon_contract_on_a_good_addon(tmp_path, orch_user_dir):
    folder = make_addon(tmp_path)
    runner = FakeRunner().add(["git", "status", "--porcelain"], stdout=" M a\n")
    assert run_addon_contract(folder, runner=runner) == []


@pytest.mark.parametrize("patch, needle", [
    ("return [Card('Hello', (Text(view.workspace_name),))]", None),
    ("return ['<b>html</b>']", "not a widget"),
    ("return [Tile('x', 1)]", "only allowed in the today.summary slot"),
])
def test_run_addon_contract_finds_bad_widgets(tmp_path, orch_user_dir, patch, needle):
    code = PROVIDER.replace("return [Card(\"Hello\", (Text(view.workspace_name),))]", patch).replace(
        "import Card, Text", "import Card, Text, Tile")
    folder = make_addon(tmp_path, provider=code)
    problems = run_addon_contract(folder, runner=FakeRunner(strict=False))
    if needle is None:
        assert problems == []
    else:
        assert any(needle in p for p in problems), problems


def test_contract_catches_ctx_run_during_render(tmp_path, orch_user_dir):
    code = PROVIDER.replace("return [Card(\"Hello\", (Text(view.workspace_name),))]",
                            "return [Text(str(self_ctx.run(['git', 'status'])))]").replace(
        "self.providers = [P()]", "self.providers = [P()]\n        global self_ctx\n        self_ctx = ctx.provider_context()")
    problems = run_addon_contract(make_addon(tmp_path, provider=code), runner=FakeRunner(strict=False))
    assert any("while a page renders" in p for p in problems), problems


def test_contract_checks_capabilities(tmp_path, orch_user_dir):
    over = {**GOOD, "capabilities": ["provider", "page", "settings", "decisions", "events"]}
    problems = run_addon_contract(make_addon(tmp_path, manifest=over), runner=FakeRunner(strict=False))
    assert any("decisions" in p for p in problems) and any("on_event" in p for p in problems)


def test_contract_reports_v1_hooks(tmp_path, orch_user_dir):
    code = PROVIDER + "\nAddon.dashboard = lambda self, router, panels: None\n"
    problems = run_addon_contract(make_addon(tmp_path, provider=code), runner=FakeRunner(strict=False))
    assert any("MC2-1 hook dashboard" in p for p in problems)


def test_contract_does_not_touch_the_real_user_dir(tmp_path, orch_user_dir):
    run_addon_contract(make_addon(tmp_path), runner=FakeRunner(strict=False))
    assert not (orch_user_dir / "workspaces.json").exists()


class TestFakeProviderContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return FakeProvider(kind="issues")

    @pytest.fixture
    def provider_ctx(self, orch_workspace):
        return orch_workspace.provider_context(M, runner=FakeRunner())


class TestAddonContractClass(AddonContract):
    @pytest.fixture(autouse=True)
    def _addon(self, tmp_path, orch_user_dir):
        type(self).addon_dir = make_addon(tmp_path)
        type(self).runner = FakeRunner(strict=False)


def test_payload_is_html():
    assert "<script>" in PAYLOAD


def test_contract_feeds_page_params_and_renders_search(tmp_path, orch_user_dir):
    """The page slot sees view.params with PAYLOAD (view.params['q'] raises if the contract gave none); a Search
    and a Text echoing it are rendered escaped, so the contract passes."""
    code = PROVIDER.replace("return [Card(\"Hello\", (Text(view.workspace_name),))]",
                            "return [Search('q', view.params['q'], 'Find'), Text(view.params['q'])]").replace(
        "import Card, Text", "import Card, Search, Text")
    assert run_addon_contract(make_addon(tmp_path, provider=code), runner=FakeRunner(strict=False)) == []


@pytest.mark.parametrize("returned, problem", [
    ("None", None),
    ("'Noted'", None),
    ("Intent('move', ref=d_ticket, value='open')", None),
    ("Intent('move', ref='DEMO-9999', value='open')", "not the decision's ticket"),
    ("Intent('import', ref=d_ticket, value='x')", "only actions with"),
    ("Intent('new', value='From the phone')", None),
    ("Intent('new', ref=d_ticket, value='x')", "has no ref yet"),
    ("42", "not an Intent"),
])
def test_contract_checks_what_resolve_returns(tmp_path, orch_user_dir, returned, problem):
    """Ruling R-A1-INTENT: resolve() gets a ProviderContext and returns None, a message or an Intent for its own ticket."""
    code = PROVIDER.replace("from orch.addons.api import Snapshot", "from orch.addons.api import Intent, PendingDecision, Snapshot") + f'''

def _decisions(self, view):
    return [PendingDecision("d1", "Pick", ticket="DEMO-0001")]


def _resolve(self, decision_id, choice, ctx):
    assert type(ctx).__name__ == "ProviderContext"
    d_ticket = "DEMO-0001"
    return {returned}


Addon.decisions = _decisions
Addon.resolve = _resolve
'''
    over = {**GOOD, "capabilities": ["provider", "page", "settings", "decisions"]}
    problems = run_addon_contract(make_addon(tmp_path, manifest=over, provider=code), runner=FakeRunner(strict=False))
    if problem is None:
        assert problems == []
    else:
        assert any(problem in p for p in problems), problems


@pytest.mark.parametrize("probe, posix_only", [
    (lambda: subprocess.Popen(["true"]), False),
    (lambda: __import__("os").system("true"), False),
    (lambda: __import__("os").posix_spawn("/bin/true", ["true"], {}), True),
    (lambda: __import__("os").fork(), True),
    (lambda: __import__("socket").getaddrinfo("example.com", 80), False),
    (lambda: __import__("socket").create_connection(("example.com", 80)), False),
    (lambda: __import__("socket").socket().connect_ex(("127.0.0.1", 9)), False),
    (lambda: __import__("socket").socket(type=__import__("socket").SOCK_DGRAM).sendto(b"x", ("127.0.0.1", 9)), False),
])
def test_no_side_effects_refuses_every_probe(probe, posix_only):
    import os
    from orch.testing import SideEffect, no_side_effects
    if posix_only and not hasattr(os, "fork"):
        pytest.skip("os.fork and os.posix_spawn exist on POSIX only")
    with no_side_effects(), pytest.raises(SideEffect):
        probe()
