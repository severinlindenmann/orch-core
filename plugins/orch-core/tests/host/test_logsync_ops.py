"""C14: the host's ``log_sync`` sender thread (``orch.host.logsyncops``) through a served workspace: a full join starts
it, committed events follow (debounced), selective mode and a leave stop it, and the hub reads the pages it was sent."""

from __future__ import annotations

import copy
import threading
import time
from typing import Any

import pytest

from orch.host import hubops, logsyncops
from orch.hubclient import HubLink
from tests.host import human_helpers
from tests.host.dispatch_helpers import ok
from tests.host.test_hub_ops import (  # noqa: F401 - fixtures
    CARD,
    HUB_ID,
    StubHub,
    connect_args,
    dev_workspace,
    join_args,
    keystore,
    stub,
)
from tests.hub import logview
from tests.hub.fake import sign_with
from tests.hub.vec import private_key, pub

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


class LogHub:
    """Answers ``log_sync`` the way the hub does (the test-side port of its rules) and records every page."""

    def __init__(self, wsk_pub: bytes, wk: dict[int, bytes]) -> None:
        self.view, self.wsk_pub, self.wk = logview.View(), wsk_pub, wk
        self.pages: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self._mu = threading.Lock()

    def send(self, message: dict[str, Any]) -> dict[str, Any]:
        m = message["o"]
        with self._mu:
            self.calls.append(m["op"])
            body: dict[str, Any]
            try:
                body = logview.apply_page(self.view, m["body"], workspace_id=m["workspace_id"], wsk_pub=self.wsk_pub,
                                          wk=self.wk)  # fmt: skip
                self.pages.append(copy.deepcopy(m["body"]))
            except Exception as exc:  # noqa: BLE001
                body = {"error": getattr(exc, "code", "malformed")}
        o = {
            "v": 2, "suite": 2, "kind": "hub.link", "hv": 0, "hub_id": m["hub_id"], "workspace_id": m["workspace_id"],
            "dir": "h2w", "seq": 1, "ts_ms": m["ts_ms"], "op": m["op"], "re": m["seq"], "body": body,
        }  # fmt: skip
        return sign_with(private_key("hk_op"), "sig_hub_route", o)


class _Link:
    def __init__(self, link: HubLink) -> None:
        self.call = link.call

    def close(self) -> None:
        return None


@pytest.fixture
def loghub(monkeypatch, served, keystore):  # noqa: F811
    store = served.lab.ws.other()
    try:
        wsk = store.genesis_event()["wsk_pub"]
    finally:
        store.close()
    from orch.crypto.encoding import unb64u

    hub = LogHub(unb64u(wsk, 65), {1: keystore.wk(1)})

    def open_link(service):
        _, sign = hubops._wsk_sign(service)
        link = HubLink(hub, hub_id=HUB_ID, workspace_id=service.workspace_id, hk_op_pub=pub("hk_op"), sign=sign,
                       mode="full", now_ms=lambda: int(service.clock() * 1000))  # fmt: skip
        return _Link(link)

    monkeypatch.setattr(logsyncops, "OPEN_LINK", open_link)
    monkeypatch.setattr(logsyncops, "DEBOUNCE_S", 0.05)
    return hub


def local_summary(served) -> dict[str, Any]:
    store = served.lab.ws.other()
    try:
        out = {}
        for log in ["workspace", *sorted(k for k in store.log_marks() if k != "workspace")]:
            seq = store.log_seq(log)
            if seq:
                out[log] = {"seq": seq, "head": store.head_at(log, seq)}
        return out
    finally:
        store.close()


def until(fn, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


def test_a_full_connect_starts_the_sender_and_the_hub_view_follows_every_commit(human, served, stub, loghub):  # noqa: F811
    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    assert until(lambda: loghub.view.summary() == local_summary(served))  # the link connect: everything so far
    first = len(loghub.pages)
    assert first >= 1 and all(p["epoch"] == 1 and p["reset"] is False for p in loghub.pages)
    human.ticket("one")
    human.ticket("two")
    assert until(lambda: loghub.view.summary() == local_summary(served) and len(loghub.view.summary()) >= 3)
    assert len(loghub.pages) > first
    sent = [p for p in loghub.pages if p["log"] != "workspace"]
    assert sent and all(p["from_seq"] >= 1 for p in sent)
    status = logsyncops.status(served.service)
    assert status is not None and status["running"] and status["last_error"] is None


def test_an_idle_workspace_sends_nothing_more(human, served, stub, loghub):  # noqa: F811
    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    assert until(lambda: loghub.view.summary() == local_summary(served))
    n = len(loghub.calls)
    served.service  # noqa: B018
    logsyncops.wake(served.service)
    time.sleep(0.4)
    assert len(loghub.calls) == n  # nothing pending: no link is even opened


def test_selective_mode_never_starts_the_sender(human, served, stub, loghub):  # noqa: F811
    human.run("hub.join", join_args())
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    human.ticket("x")
    time.sleep(0.4)
    assert logsyncops.status(served.service) is None and loghub.calls == []
    assert "log_sync" not in stub.ops()


def test_a_full_join_not_connected_yet_does_not_send(human, served, stub, loghub):  # noqa: F811
    human.run("hub.join", join_args(mode="full"))  # on the log, but no hub.json: the join is not complete
    assert not logsyncops.start(served.service) and loghub.calls == []


def test_a_leave_and_unbind_end_the_sender(human, served, stub, loghub):  # noqa: F811
    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    assert until(lambda: loghub.view.summary() == local_summary(served))
    human.run("hub.leave", {})
    ok(served.client().req("hub.unbind", {}, idem=True))
    assert logsyncops.status(served.service) is None
    n = len(loghub.calls)
    human.ticket("after the leave")
    time.sleep(0.4)
    assert len(loghub.calls) == n


def test_the_position_is_kept_in_a_private_file_and_survives_a_restart(human, served, stub, loghub):  # noqa: F811
    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    assert until(lambda: loghub.view.summary() == local_summary(served))
    path = logsyncops._path(served.service)  # noqa: SLF001
    assert path.is_file() and (path.stat().st_mode & 0o777) == 0o600
    logsyncops.stop(served.service)
    n = len(loghub.pages)
    assert logsyncops.start(served.service)
    time.sleep(0.5)
    assert len(loghub.pages) == n  # the saved position: nothing is resent
    human.ticket("later")
    assert until(lambda: loghub.view.summary() == local_summary(served))
    assert loghub.pages[n]["from_seq"] > 1 or loghub.pages[n]["log"] != "workspace"
