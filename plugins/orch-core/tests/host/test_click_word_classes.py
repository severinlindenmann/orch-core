"""Run 8 (owner 1.6): the confirmation class of an add-on grant comes from the SIGNED capability list; settings keys
and the other word ops keep their class. Publish stays terminal-only; anything unclear fails closed to the word."""

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
def test_a_grant_with_a_capability_keeps_the_word(cap):
    assert op_class("addon.grant", [grant(capabilities=[cap])]) == "word"


def test_a_non_builtin_name_without_capabilities_keeps_the_word():
    assert op_class("addon.grant", [grant(name="my-addon", package_sha256=digest("usage"))]) == "word"


def test_a_builtin_name_with_a_wrong_or_missing_digest_keeps_the_word():
    assert op_class("addon.grant", [grant(package_sha256="0" * 64)]) == "word"
    ev = grant()
    del ev["package_sha256"]
    assert op_class("addon.grant", [ev]) == "word"


def test_a_builtin_with_its_own_digest_is_a_click():
    assert op_class("addon.grant", [grant(name="wiki", package_sha256=digest("wiki"))]) == "click"
    # a digest of another built-in does not carry over
    assert op_class("addon.grant", [grant(name="wiki", package_sha256=digest("usage"))]) == "word"


@pytest.mark.parametrize("bad", [None, "network", {"a": 1}])
def test_a_missing_or_malformed_capability_list_fails_closed_to_the_word(bad):
    assert op_class("addon.grant", [grant(capabilities=bad)]) == "word"
    ev = grant()
    del ev["capabilities"]
    assert op_class("addon.grant", [ev]) == "word"


def test_publish_is_terminal_whatever_the_capabilities():
    assert op_class("addon.grant", [grant(name="publish", capabilities=[])]) == "terminal"
    assert op_class("addon.grant", [grant(name="publish", capabilities=["network"])]) == "terminal"
    assert op_class("addon.grant", args={"name": "publish"}) == "terminal"


def test_the_strongest_event_wins():
    assert op_class("addon.grant", [grant(), grant(capabilities=["pty"])]) == "word"


def test_the_early_check_by_args_alone_is_the_word_ceiling():
    assert op_class("addon.grant", args={"name": "usage"}) == "word"


def test_settings_classes():
    sets = lambda **kw: op_class("settings.set", args={"set": kw})  # noqa: E731
    for key in ("repos", "grant_hours", "sign_methods"):
        assert sets(**{key: None}) == "word"
    assert sets(publish=None) == "terminal"


@pytest.mark.parametrize(
    "op", ["member.add", "member.role", "member.remove", "policy.set", "people.set", "addon.purge"]
)
def test_authority_ops_keep_the_word(op):
    assert op_class(op) == "word"
