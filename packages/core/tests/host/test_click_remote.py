"""R2 T4 (#1590): turning remote access on and confirming a device are clicks, with the backup key sealed to the signer
(the same sealed field as a hub step); the signer's View carries the plain risk line, and without the key nothing is
signed."""

from __future__ import annotations

import pytest

from orch.click import protocol
from tests.host import human_helpers
from tests.host.test_click import Click
from tests.host.test_click_hub import fast_world, seal_for  # noqa: F401
from tests.host.test_remote_policy import remote_args

lab, served = human_helpers.lab, human_helpers.served


@pytest.fixture
def click(served):
    c = Click(served)
    yield c
    c.close()


def test_remote_set_by_click_needs_the_backup_key_and_shows_the_risk(click, served):
    p = click.prepare("remote.set", remote_args())
    v = click.view(p["request_id"])
    click.mono.t += 1.5
    view = v["view"]
    assert view["confirm"] == {"kind": "click"} and view["seal"]["purposes"] == ["hub.backup_key"]
    assert view["backup_key_line"] in view["lines"]
    assert view["risk_lines"] == [protocol.REMOTE_RISK, protocol.REMOTE_AGENTS_RISK]
    click.sign(p["request_id"], v["view_digest"], sealed=seal_for(v, served, p["request_id"]))
    assert click.signatures == 1
    served.service.store.refresh()
    state = served.service.store.state.workspace.remote
    assert state["on"] is True and state["level"] == "work"
