"""The signed ``settings.icon``: set, clear and replay; the closed lists and the id-derived default."""

import pytest

from orch.model import Code
from orch.wsicons import COLOUR_LABELS, ICON_LABELS, default_icon, fnv1a
from tests.model.world import World, refused


@pytest.fixture
def w():
    return World().bootstrap({"tom": "member"})


def test_set_clear_and_replay(w):
    assert "icon" not in w.state().workspace.settings
    w.wev("settings.changed", "sev", set={"icon": {"icon": "rocket", "colour": "pink"}})
    assert dict(w.state().workspace.settings["icon"]) == {"icon": "rocket", "colour": "pink"}
    w.wev("settings.changed", "sev", set={"icon": {"icon": "star", "colour": "teal"}})
    assert dict(w.state().workspace.settings["icon"]) == {"icon": "star", "colour": "teal"}
    w.wev("settings.changed", "sev", set={"icon": None})
    assert "icon" not in w.state().workspace.settings  # a fresh state() replays the whole log
    assert not w.state().workspace.invalid


@pytest.mark.parametrize(
    "bad",
    [{"icon": "nope", "colour": "pink"}, {"icon": "rocket", "colour": "#fff"}, {"icon": "rocket"}, "rocket"],
)
def test_a_value_outside_the_closed_lists_is_refused(w, bad):
    from orch.model import admit

    e = w.build_unappended("workspace", "settings.changed", "sev", set={"icon": bad})
    got = admit(w.state(), e, log="workspace")
    assert getattr(got, "code", None) is not None


def test_only_an_owner_sets_the_icon(w):
    assert (
        refused(w, "workspace", "settings.changed", "tom", set={"icon": {"icon": "star", "colour": "teal"}})
        == Code.ROLE_DENIED
    )
    assert refused(w, "workspace", "settings.changed", "sev", set={"icon": {"icon": "star", "colour": "teal"}}) is None


def test_the_default_comes_from_the_id_with_pinned_vectors():
    assert fnv1a("") == 0x811C9DC5
    assert fnv1a("a") == 0xE40C292C
    assert default_icon("6f1c0d2e-8b4a-4e1f-9c3d-2a7b5e9f0c11") == default_icon(
        " 6F1C0D2E-8B4A-4E1F-9C3D-2A7B5E9F0C11 "
    )
    icon, colour = default_icon("6f1c0d2e-8b4a-4e1f-9c3d-2a7b5e9f0c11")
    assert icon in ICON_LABELS and colour in COLOUR_LABELS
    # vectors the dashboard test repeats (addons/dashboard/src/app/workspaceIcon.test.tsx)
    assert default_icon("w1") == VECTORS["w1"] and default_icon("ws-demo-1") == VECTORS["ws-demo-1"]


VECTORS = {"w1": ("flame", "orange"), "ws-demo-1": ("globe", "orange")}
