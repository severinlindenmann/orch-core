from pathlib import Path

import pytest

from hello_status.provider import ARGV, GitStatusProvider, parse
from orch.addons.manifest import load_manifest
from orch.addons.runtime import SlotView
from orch.testing import AddonContract, FakeRunner, ProviderContract

ADDON = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).with_name("fixtures")
MANIFEST = load_manifest(ADDON)
PAGE = f"page.{MANIFEST.name}"


def test_parse():
    items = parse("# branch.head main\n# branch.ab +2 -1\n1 .M N... x\n? new.txt\n")
    assert [(i["id"], i["text"], i["role"]) for i in items] == [
        ("branch", "main", "info"), ("changes", "2", "warn"), ("upstream", "ahead 2, behind 1", "neu")]


def test_fetch_with_recorded_git(orch_workspace):
    runner = FakeRunner.from_dir(FIXTURES)
    snap = GitStatusProvider().fetch(orch_workspace.provider_context(MANIFEST, runner=runner), "harness", None)
    assert snap.health == "ok" and snap.items[0]["text"] == "main" and snap.items[1]["text"] == "1"
    assert runner.calls == [tuple(ARGV)]


@pytest.mark.parametrize("recording, message", [
    ({"returncode": 128, "stderr": "fatal: not a git repository"}, "not a git repository"),
    ({"raises": "missing"}, "not installed"),
    ({"raises": "timeout"}, "timed out"),
])
def test_fetch_errors_become_health(orch_workspace, recording, message):
    runner = FakeRunner([{"argv": ARGV, **recording}])
    snap = GitStatusProvider().fetch(orch_workspace.provider_context(MANIFEST, runner=runner), "harness", None)
    assert snap.health == "error" and message in snap.message


def test_page_shows_the_cached_snapshot(orch_workspace):
    addon = orch_workspace.load(ADDON, runner=FakeRunner.from_dir(FIXTURES))
    orch_workspace.cache(MANIFEST.name, GitStatusProvider().fetch(addon.ctx.provider_context(), "harness", None))
    [card] = addon.obj.widgets(PAGE, SlotView(orch_workspace.ws, addon, PAGE))
    assert card.title == "Hello, acme" and card.body[0].rows[0][0] == "Branch"


def test_greeting_setting(orch_workspace):
    orch_workspace.enable(MANIFEST.name, {"greeting": "Hi"})
    addon = orch_workspace.load(ADDON)
    [card] = addon.obj.widgets(PAGE, SlotView(orch_workspace.ws, addon, PAGE))
    assert card.title == "Hi, acme" and "Refresh" in card.body[0].text


class TestProvider(ProviderContract):
    @pytest.fixture
    def provider(self):
        return GitStatusProvider()

    @pytest.fixture
    def provider_ctx(self, orch_workspace):
        return orch_workspace.provider_context(MANIFEST, runner=FakeRunner.from_dir(FIXTURES))


class TestAddon(AddonContract):
    addon_dir = ADDON
    runner = FakeRunner.from_dir(FIXTURES, strict=False)
