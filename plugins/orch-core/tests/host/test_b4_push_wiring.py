"""B4-H host wiring on a real registered workspace: the ``push`` seam factory, its outbox on the worker's state
directory, the commit wake, and the ``push`` block of ``host.status.relay``. (The planner, the sealing and the send
classes are tested in tests/relay/test_push.py; the host to relay path against the real relay is the T2 test.)"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orch.host import relayhook, relaypush
from orch.host.errors import HostError
from orch.relay.push import PushThread
from tests.host.test_b1_wiring import FakeWorker, _linked, lab  # noqa: F401 - the fixture and the helpers


def test_the_seam_is_optional_and_installed_by_default(monkeypatch):
    assert "push" not in relayhook.Seams().missing()  # a worker is complete without a push thread
    monkeypatch.setattr(relayhook, "SEAMS", relayhook.Seams())
    assert relayhook.install_seams().push is relaypush.push


def test_the_factory_builds_a_thread_over_the_workers_state_and_an_outbox(lab):  # noqa: F811
    with lab.host() as h, h.client() as c:
        _, service = _linked(lab, h, c)
        worker = FakeWorker(service)
        thread = None
        try:
            thread = relaypush.push(service, worker, keys="K", signer="S", view="V")
            assert isinstance(thread, PushThread) and thread.store is service.store
            assert (thread.keys, thread.signer, thread.view) == ("K", "S", "V")
            assert thread.ob.start_ms > 0 and "outbound.sqlite" in worker.state.names()
            assert thread.id8 == service.workspace_id[:8] and worker.alarm.codes() == []
            thread._on_error(RuntimeError("x"))  # noqa: SLF001 - a fence or session error ends the worker
            assert worker.stops == 1 and worker.alarm.codes() == ["relay_push_stopped"]
            assert thread.push_status() == {"state": "on", "intents": {}}
        finally:
            if thread is not None:
                thread.close()
            worker.close()


def test_a_copied_state_directory_starts_a_new_fence_and_alarms(lab):  # noqa: F811
    with lab.host() as h, h.client() as c:
        _, service = _linked(lab, h, c)
        worker = FakeWorker(service)
        try:
            first = relaypush.push(service, worker, None, None, None)
            first.ob._db.execute("UPDATE meta SET v='f' WHERE k='instance_id'")  # noqa: SLF001
            first.close()
            second = relaypush.push(service, worker, None, None, None)
            assert second.ob.restored and worker.alarm.codes() == ["push_outbox_restarted"]
            second.close()
        finally:
            worker.close()


def test_the_factory_refuses_without_a_store_or_a_connection(lab):  # noqa: F811
    with lab.host() as h, h.client() as c:
        _, service = _linked(lab, h, c)
        worker = FakeWorker(service)
        try:
            worker.conn = None
            with pytest.raises(HostError):
                relaypush.push(service, worker, None, None, None)
        finally:
            worker.close()


def test_a_commit_wakes_the_push_thread_and_the_status_shows_its_state(lab):  # noqa: F811
    with lab.host() as h, h.client() as c:
        _, service = _linked(lab, h, c)
        worker = FakeWorker(service)
        thread = relaypush.push(service, worker, None, None, None)
        try:
            service.relay = SimpleNamespace(publisher=None, unpublished=None, companions=[thread])
            assert thread._woken is False  # noqa: SLF001
            service.store.on_commit("workspace", {"type": "ticket.created"})
            assert thread._woken is True and thread._wake.is_set()  # noqa: SLF001
            service.relay = SimpleNamespace(
                publisher=None, unpublished=None, companions=[thread], status=lambda: {"alive": True, "serving": True},
                stopping=False,
            )  # fmt: skip
            out = relayhook._status(service)  # noqa: SLF001
            assert out["push"] == {"state": "on", "intents": {}}
        finally:
            service.relay = None
            thread.close()
            worker.close()
