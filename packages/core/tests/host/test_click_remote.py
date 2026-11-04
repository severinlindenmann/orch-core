"""R2 T4 (#1590): turning remote access on and confirming a device are clicks, with the backup key sealed to the signer
(the same sealed field as a hub step); the signer's View carries the plain risk line, and without the key nothing is
signed."""

from __future__ import annotations

import pytest

from orch.click import protocol
from tests.host import human_helpers
from tests.host.test_click import Click
from tests.host.test_click_hub import fast_world, keep_person_key, seal_for  # noqa: F401
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


def test_remote_set_with_the_person_key_kept_still_needs_the_backup_key(click, served):
    """R2 review H1: a power-increasing remote change is never one click, even when setup stored the person key."""
    from orch.click.protocol import ClickError

    keep_person_key(click, served)
    p = click.prepare("remote.set", remote_args())
    v = click.view(p["request_id"])
    click.mono.t += 1.5
    assert v["view"]["seal"]["purposes"] == ["hub.backup_key"]
    with pytest.raises(ClickError) as exc:
        click.sign(p["request_id"], v["view_digest"])  # no sealed key
    assert exc.value.code == "invalid.input"
    served.service.store.refresh()
    assert not served.service.store.state.workspace.remote.get("on")
    assert click.signatures == 0
    click.sign(p["request_id"], v["view_digest"], sealed=seal_for(v, served, p["request_id"]))
    served.service.store.refresh()
    assert served.service.store.state.workspace.remote["on"] is True
