from pathlib import Path

import pytest

from orch.addons.manifest import load_manifest
from orch.testing import FakeRunner, ProviderContract
from orch_wiki.confluence import ConfluencePages

MANIFEST = load_manifest(Path(__file__).resolve().parents[1])


@pytest.fixture
def confluence_ctx(orch_workspace):
    orch_workspace.enable("wiki", {"provider": "confluence"})
    return orch_workspace.provider_context(MANIFEST, runner=FakeRunner())


def test_stub_is_quiet_unless_selected(orch_workspace):
    assert ConfluencePages().scopes(orch_workspace.provider_context(MANIFEST, runner=FakeRunner())) == []


def test_stub_says_it_is_not_available(confluence_ctx):
    snap = ConfluencePages().fetch(confluence_ctx, "default", None)
    assert snap.health == "error" and "not available yet" in snap.message and snap.items == ()


class TestConfluenceContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return ConfluencePages()

    @pytest.fixture
    def provider_ctx(self, confluence_ctx):
        return confluence_ctx
