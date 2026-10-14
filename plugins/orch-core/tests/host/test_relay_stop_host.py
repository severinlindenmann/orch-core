"""H12a: ``host.relay_stop`` and ``host.relay_start`` on the control socket (orch-v2-host §11A.4 "Scope of
exactly-once": the operator keeps the relay worker stopped after a state restore)."""

from __future__ import annotations

import errno

import pytest

from orch.host import relaylink
from orch.host.control_client import new_idem
from tests.host.lifecycle_helpers import UID, Lab


@pytest.fixture
def lab(tmp_path):
    value = Lab(tmp_path)
    try:
        yield value
    except OSError as exc:  # pragma: no cover - a sandbox without AF_UNIX bind
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    finally:
        value.close()


def ok(res):
    assert res["t"] == "res" and res["ok"] is True, res
    return res["result"]


def err(res):
    assert res["t"] == "res" and res["ok"] is False, res
    return res["error"]["code"]


class Worker:
    def __init__(self) -> None:
        self.stopped = 0

    def stop(self) -> None:
        self.stopped += 1


def test_relay_stop_stops_the_live_worker_and_persists_the_stop(lab):
    with lab.host() as h, h.client() as c:
        wid = ok(lab.register(c))["data"]["workspace_id"]
        service = h.host.services[wid]
        worker = Worker()
        service.relay = worker
        res = ok(c.call("host.relay_stop", {"workspace_id": wid}))
        assert res["data"] == {"workspace_id": wid, "stopped": True, "worker": "stopped"}
        assert worker.stopped == 1 and service.relay is None
        assert relaylink.is_stopped(lab.state, wid, UID)
        res = ok(c.call("host.relay_stop", {"workspace_id": wid}))  # idempotent
        assert res["data"]["worker"] == "none"
        starts = []
        service.relay_starter = lambda svc: starts.append(svc.workspace_id)
        res = ok(c.call("host.relay_start", {"workspace_id": wid}))
        assert res["data"]["stopped"] is False
        assert not relaylink.is_stopped(lab.state, wid, UID)
        assert starts == []  # not linked: nothing to start
        relaylink.link_path(lab.state, wid).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        relaylink.link_path(lab.state, wid).write_text("{}")
        ok(c.call("host.relay_start", {"workspace_id": wid}))
        assert starts == [wid]


def test_relay_stop_of_an_unregistered_workspace_is_not_found_and_needs_no_idem(lab):
    with lab.host() as h, h.client() as c:
        assert err(c.call("host.relay_stop", {"workspace_id": "b" * 32})) == "not_found"
        assert err(c.call("host.relay_stop", {"workspace_id": "nope"})) == "invalid.input"
        assert err(c.call("host.relay_stop", {"workspace_id": "b" * 32, "x": 1})) == "invalid.input"
        wid = ok(lab.register(c))["data"]["workspace_id"]
        ok(c.call("host.relay_stop", {"workspace_id": wid}, idem=new_idem()))  # an idem is accepted and ignored
