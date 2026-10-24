"""The confirmation class of an op. #1198 C1: the typed-word class is gone, every former word op is a plain click;
Publish and ``grant`` stay terminal-only (a click only for the runner deposit). Add-on grants are one click (V3-D38)."""

import pytest

from orch.addons import builtin
from orch.addons.package import MANIFEST_FILE, package_digest
from orch.click import protocol
from orch.click import view as views
from orch.click.protocol import CLICK_OPS, CONTRACT, REFUSALS, op_class


def digest(name):
    return package_digest({MANIFEST_FILE: builtin.manifest_bytes(name)})


GRANT = {"type": "addon.granted", "name": "usage", "capabilities": [], "package_sha256": digest("usage")}


def grant(**over):
    return {**GRANT, **over}


def test_a_capability_free_grant_is_a_plain_click():
    assert op_class("addon.grant", [grant()]) == "click"


@pytest.mark.parametrize("cap", ["network", "pty", "spawn_agent", "serve_http", "git_push"])
def test_a_grant_with_a_capability_is_one_click_too(cap):
    """Owner V3-D38 (24 Oct): installing an add-on is always one click; the review lists the capabilities first."""
    assert op_class("addon.grant", [grant(capabilities=[cap])]) == "click"


def test_a_market_add_on_is_one_click():
    assert (
        op_class("addon.grant", [grant(name="my-addon", package_sha256="0" * 64, capabilities=["network"])]) == "click"
    )


def test_publish_is_a_click_whatever_the_capabilities():
    """#1198 C2: the Publish add-on's grant is a click like any other; its review lists the capabilities."""
    assert op_class("addon.grant", [grant(name="publish", capabilities=[])]) == "click"
    assert op_class("addon.grant", [grant(name="publish", capabilities=["network"])]) == "click"
    assert op_class("addon.grant", args={"name": "publish"}) == "click"
    assert op_class("addon.grant", [grant(), grant(name="publish")]) == "click"


def test_settings_classes():
    sets = lambda **kw: op_class("settings.set", args={"set": kw})  # noqa: E731
    assert sets(models={"codex": {"work": "x"}}) == "click"
    assert sets(addons={"demo": {"mode": "fast"}}) == "click"  # V3-D38
    for key in ("repos", "grant_hours", "sign_methods"):  # were the typed word
        assert sets(**{key: None}) == "click"
    assert sets(publish=None) == "click"  # C2: where files go is a click, the View lists both servers
    assert sets(publish={"server": "http://a", "share": "http://b"}) == "click"
    assert op_class("settings.set", args={"set": "not a map"}) == "terminal"  # fails closed


@pytest.mark.parametrize(
    "op", ["member.add", "member.role", "member.remove", "policy.set", "people.set", "addon.purge"]
)
def test_every_former_word_op_is_a_plain_click(op):
    assert op in CLICK_OPS
    assert op_class(op) == "click"


@pytest.mark.parametrize("op", ["grant", "device.rotate", "unknown.op", "hub.join"])
def test_terminal_only_ops_are_still_refused_on_click(op):
    assert op_class(op) == "terminal"


def test_the_word_class_and_refusal_are_gone():
    assert "word" not in REFUSALS
    assert not hasattr(protocol, "WORD_OPS") and not hasattr(protocol, "SETTINGS_WORD_KEYS")
    assert not hasattr(views, "word")
    assert CONTRACT == 3
