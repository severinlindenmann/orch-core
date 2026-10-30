"""R6 T-C (#1594): ``hub.network`` / ``hub.network_set``, the owner's proxy for hub links. Owner at the workspace
only; an agent is refused; the password is never returned or logged; the hub cannot reach these files."""

from __future__ import annotations

import json

import pytest

from orch.host import hubnet
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, ok
from tests.host.human_helpers import err

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


def _state_dir(served):
    return next(iter(served.h.host.services.values())).state_dir


def _set(served, **args):
    return served.client().req("hub.network_set", args, idem=True)


def test_show_set_and_clear(served) -> None:
    c = served.client()
    assert ok(c.req("hub.network", {}))["data"] == {"proxy": None, "user": None, "has_password": False, "broken": False}
    got = ok(_set(served, proxy="https://proxy.office:8443", user="anna", password="s3cret"))["data"]
    assert got == {"proxy": "https://proxy.office:8443", "user": "anna", "has_password": True, "broken": False}
    assert "s3cret" not in json.dumps(ok(c.req("hub.network", {})))
    assert hubnet.load(_state_dir(served)).proxy.password == "s3cret"
    assert ok(_set(served, proxy=None))["data"]["proxy"] is None
    assert hubnet.load(_state_dir(served)).proxy is None


def test_the_password_is_not_in_the_security_log(served) -> None:
    ok(_set(served, proxy="http://proxy.office:3128", user="anna", password="very-private-word"))
    log = next((_state_dir(served) / "host").glob("*.jsonl*"), None) or next((_state_dir(served) / "host").iterdir())
    assert "very-private-word" not in log.read_text()
    assert "hub.network" in log.read_text()


@pytest.mark.parametrize(
    "args",
    [{"proxy": "ftp://p:1"}, {"proxy": "http://p"}, {"proxy": "http://u:pw@p:3128"},
     {"proxy": "http://p:3128", "user": "a:b", "password": "x"}, {"proxy": "http://p:3128", "password": "x"}],
)  # fmt: skip
def test_a_bad_setting_is_invalid_input_and_changes_nothing(served, args) -> None:
    assert err(_set(served, **args)) == "invalid.input"
    assert hubnet.load(_state_dir(served)).proxy is None


def test_an_agent_cannot_read_or_change_the_proxy(served) -> None:
    for op, args in (("hub.network", {}), ("hub.network_set", {"proxy": "http://p.office:3128"})):
        r = served.client().req(op, args, idem=op.endswith("_set"), grant=served.grant, session=SESSION)
        assert err(r) in ("op.not_allowed", "human_only"), (op, r)
    assert hubnet.load(_state_dir(served)).proxy is None
