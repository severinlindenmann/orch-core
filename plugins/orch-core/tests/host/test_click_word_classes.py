"""Run 8 (owner 1.6): the confirmation class of an add-on grant comes from the SIGNED capability list; settings keys
and the other word ops keep their class. Publish stays terminal-only; add-on grants are one click (V3-D38)."""

import pytest

from orch.addons import builtin
from orch.addons.package import MANIFEST_FILE, package_digest
from orch.click.protocol import op_class


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


def test_publish_is_terminal_whatever_the_capabilities():
    assert op_class("addon.grant", [grant(name="publish", capabilities=[])]) == "terminal"
    assert op_class("addon.grant", [grant(name="publish", capabilities=["network"])]) == "terminal"
    assert op_class("addon.grant", args={"name": "publish"}) == "terminal"


def test_publish_wins_over_any_other_event():
    assert op_class("addon.grant", [grant(), grant(name="publish")]) == "terminal"


def test_the_early_check_by_args_alone_is_a_click_except_publish():
    assert op_class("addon.grant", args={"name": "usage"}) == "click"


def test_settings_classes():
    sets = lambda **kw: op_class("settings.set", args={"set": kw})  # noqa: E731
    assert sets(models={"codex": {"work": "x"}}) == "click"
    assert sets(addons={"demo": {"mode": "fast"}}) == "click"  # V3-D38
    for key in ("repos", "grant_hours", "sign_methods"):
        assert sets(**{key: None}) == "word"
    assert sets(publish=None) == "terminal"


@pytest.mark.parametrize(
    "op", ["member.add", "member.role", "member.remove", "policy.set", "people.set", "addon.purge"]
)
def test_authority_ops_keep_the_word(op):
    assert op_class(op) == "word"
