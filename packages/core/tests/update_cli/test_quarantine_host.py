"""The quarantined host (orch.update.quarantine): control socket only, reads answer, everything else refuses."""

from __future__ import annotations

import pytest

from orch.host.control_client import new_idem
from orch.update import quarantine
from tests.host.lifecycle_helpers import Lab


@pytest.fixture
def lab(tmp_path):
    lab = Lab(tmp_path)
    yield lab
    lab.close()


def test_quarantine_serves_the_control_socket_only(lab):
    with lab.host() as normal:
        with normal.client() as c:
            c.call("host.list")
        assert (lab.run / "runner" / "host.sock").exists()
        c = None
    quarantine.activate(lab.state, "ab" * 16)
    try:
        with lab.host() as h:
            assert h.host.quarantine and h.host.services == {}
            assert not (lab.run / "runner" / "host.sock").exists()
            with h.client() as c:
                assert c.call("host.list")["ok"] is True  # "the host answers"
                for op, args in (("host.register", {"root": lab.root, "peers": lab.peers()}), ("host.wsk_create", {})):
                    got = c.call(op, args, idem=new_idem())
                    assert got["ok"] is False and got["error"]["code"] == "host.unavailable"
    finally:
        quarantine.clear(lab.state)
    with lab.host() as again:  # the marker is gone: a normal host again
        assert not again.host.quarantine
        assert (lab.run / "runner" / "host.sock").exists()


def test_a_broken_symlink_marker_still_quarantines(tmp_path):
    (tmp_path / "install").mkdir()
    (tmp_path / "install" / "quarantine").symlink_to(tmp_path / "nowhere")
    assert quarantine.active(tmp_path)


def test_host_health_opens_every_registered_root(lab):
    from orch.host.control import _store_dir
    from orch.migrate import init_root
    from orch.migrate.format import FORMAT_FILE

    with lab.host() as normal:
        with normal.client() as c:
            assert lab.register(c)["ok"] is True
    store = _store_dir(lab.root)
    quarantine.activate(lab.state, "ab" * 16)
    try:
        with lab.host() as h, h.client() as c:
            fr = c.call("host.health")
            assert fr.get("ok"), str(fr)
            data = fr["result"]["data"]
            assert str(data) and data["problems"] == [] and len(data["unchecked"]) == 1, str(
                data
            )  # no format.json yet: unchecked, not a problem
            init_root(store, "workspace")
            fr = c.call("host.health")
            assert fr.get("ok"), str(fr)
            data = fr["result"]["data"]
            assert data["checked"] == 1 and data["problems"] == []
            (store / FORMAT_FILE).write_text("{damaged")
            fr = c.call("host.health")
            assert fr.get("ok"), str(fr)
            data = fr["result"]["data"]
            assert data["checked"] == 0 and len(data["problems"]) == 1
    finally:
        quarantine.clear(lab.state)
