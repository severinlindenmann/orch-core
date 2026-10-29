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
        self.hold: threading.Event | None = None  # a slow hub: the next page waits here until released
        self.entered = threading.Event()
        self.relink = False
        self.links: list[Any] = []

    def logs(self) -> dict[str, Any]:
        """The held ``{log: {seq, head}}`` (the projections, D15b, are in ``view.proj``)."""
        return {k: v for k, v in self.view.summary().items() if k != "proj"}

    def send(self, message: dict[str, Any]) -> dict[str, Any]:
        m = message["o"]
        if self.hold is not None:
            self.entered.set()
            self.hold.wait(10)
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

    hub.links = []  # every link the sender was handed, in order

    def open_link(service):
        """The workspace's one link: the same object until ``hub.relink`` (a redial)."""
        if hub.links and not hub.relink:
            return hub.links[-1]
        hub.relink = False
        _, sign = hubops._wsk_sign(service)
        link = HubLink(hub, hub_id=HUB_ID, workspace_id=service.workspace_id, hk_op_pub=pub("hk_op"), sign=sign,
                       mode="full", now_ms=lambda: int(service.clock() * 1000))  # fmt: skip
        hub.links.append(_Link(link))
        return hub.links[-1]

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
    assert until(lambda: loghub.logs() == local_summary(served))  # the link connect: everything so far
    first = len(loghub.pages)
    assert first >= 1 and all(p["epoch"] == 1 and p["reset"] is False for p in loghub.pages)
    human.ticket("one")
    human.ticket("two")
    assert until(lambda: loghub.logs() == local_summary(served) and len(loghub.logs()) >= 3)
    assert len(loghub.pages) > first
    sent = [p for p in loghub.pages if p["log"] != "workspace"]
    assert sent and all(p["from_seq"] >= 1 for p in sent)
    status = logsyncops.status(served.service)
    assert status is not None and status["running"] and status["last_error"] is None


def test_an_idle_workspace_sends_nothing_more(human, served, stub, loghub):  # noqa: F811
    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    assert until(lambda: loghub.logs() == local_summary(served))
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
    assert until(lambda: loghub.logs() == local_summary(served))
    human.run("hub.leave", {})
    ok(served.client().req("hub.unbind", {}, idem=True))
    assert logsyncops.status(served.service) is None
    n = len(loghub.calls)
    human.ticket("after the leave")
    time.sleep(0.4)
    assert len(loghub.calls) == n


def test_l2_c14_1_a_leave_stops_a_pass_that_a_slow_hub_keeps_alive(monkeypatch, human, served, stub, loghub):  # noqa: F811
    monkeypatch.setattr(logsyncops, "STOP_WAIT_S", 0.3)
    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    assert until(lambda: loghub.logs() == local_summary(served))
    n = len(loghub.pages)
    loghub.hold = threading.Event()
    human.ticket("before the leave")
    assert loghub.entered.wait(8)  # a pass is on the network, the hub is not answering
    human.ticket("written after the leave started")
    human.run("hub.leave", {})
    ok(served.client().req("hub.unbind", {}, idem=True))  # stops the sender and waits (bounded) before the op is sent
    loghub.hold.set()
    time.sleep(0.6)
    assert len(loghub.pages) <= n + 1  # the page already on the wire, nothing after it
    assert loghub.logs() != local_summary(served)


def test_a_hub_left_in_the_log_stops_sending_even_while_hub_json_lags(human, served, stub, loghub):  # noqa: F811
    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    assert until(lambda: loghub.logs() == local_summary(served))
    human.run("hub.leave", {})  # recorded in the log; hub.unbind has NOT run, hub.json still says bound
    assert hubops._load_state(served.service).status == "bound"  # noqa: SLF001
    assert not logsyncops._model_joined(  # noqa: SLF001
        served.service,
        HUB_ID,
        hubops._load_state(served.service).binding_seq,  # noqa: SLF001
    )
    n = len(loghub.calls)
    human.ticket("after the hub.left")
    time.sleep(0.5)
    assert len(loghub.calls) == n and loghub.logs() != local_summary(served)


@pytest.mark.parametrize(
    ("op", "kind", "stops"),
    [("hub.unbind", None, True), ("hub.connect", "join", True), ("hub.connect", "switch", True),
     ("hub.connect", "seal", False), ("hub.refresh", None, False)],
)  # fmt: skip
def test_every_hub_op_that_can_change_the_binding_stops_the_sender_first(monkeypatch, op, kind, stops):
    calls = []
    monkeypatch.setattr(logsyncops, "stop", lambda service, wait=0.0: calls.append(wait))
    hubops._quiesce_logsync(object(), {"op": op, "kind": kind})  # noqa: SLF001
    assert bool(calls) is stops and (not stops or calls == [logsyncops.STOP_WAIT_S])


def test_the_position_is_kept_in_a_private_file_and_survives_a_restart(human, served, stub, loghub):  # noqa: F811
    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    assert until(lambda: loghub.logs() == local_summary(served))
    path = logsyncops._path(served.service)  # noqa: SLF001
    assert path.is_file() and (path.stat().st_mode & 0o777) == 0o600
    logsyncops.stop(served.service)
    n = len(loghub.pages)
    assert logsyncops.start(served.service)
    time.sleep(0.5)
    probe = loghub.pages[n:]
    assert len(probe) == 1 and probe[0]["count"] == 1 and not probe[0]["reset"]  # only the resume probe, no resend
    human.ticket("later")
    assert until(lambda: loghub.logs() == local_summary(served))
    assert loghub.pages[n + 1]["from_seq"] > 1 or loghub.pages[n + 1]["log"] != "workspace"


def test_a_new_link_starts_with_the_probe_and_a_hub_that_lost_its_view_is_refilled(human, served, stub, loghub):  # noqa: F811
    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    human.ticket("one")
    assert until(lambda: loghub.logs() == local_summary(served) and len(loghub.logs()) >= 2)
    assert len(loghub.links) == 1  # one link for the pass after pass: no link of its own per pass
    n = len(loghub.pages)
    logsyncops.wake(served.service)
    time.sleep(0.4)
    assert len(loghub.pages) == n  # same link, nothing new: no probe
    loghub.view = logview.View()  # the hub restored an old database ...
    loghub.relink = True  # ... and the link was redialed
    logsyncops.wake(served.service)
    assert until(lambda: loghub.logs() == local_summary(served))
    assert len(loghub.links) == 2 and all(not p["reset"] for p in loghub.pages[n:])


def test_the_sender_uses_the_workspaces_shared_link_and_never_closes_or_dials_its_own(
    monkeypatch,
    human,
    served,
    stub,  # noqa: F811
    loghub,
):
    from orch.host import peerops

    monkeypatch.setattr(logsyncops, "OPEN_LINK", None)
    seen: list[bool] = []
    made: list[Any] = []

    def shared_link(service, *, dial=True):
        seen.append(dial)
        if not dial:
            return made[-1] if made else None
        if not made:
            made.append(loghub_link(service))
        return made[-1]

    def loghub_link(service):
        _, sign = hubops._wsk_sign(service)
        return _Link(HubLink(loghub, hub_id=HUB_ID, workspace_id=service.workspace_id, hk_op_pub=pub("hk_op"),
                             sign=sign, mode="full", now_ms=lambda: int(service.clock() * 1000)))  # fmt: skip

    closed: list[bool] = []
    monkeypatch.setattr(peerops, "shared_link", shared_link)
    monkeypatch.setattr(_Link, "close", lambda self: closed.append(True))
    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    assert until(lambda: loghub.logs() == local_summary(served))
    human.ticket("x")
    assert until(lambda: loghub.logs() == local_summary(served) and len(loghub.logs()) >= 2)
    assert True in seen and len(made) == 1 and closed == []


def test_core_1034_a_retryable_failure_does_not_end_the_sender(monkeypatch, human, served, stub, loghub):  # noqa: F811
    from orch.hubclient import HubRefused

    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    assert until(lambda: loghub.logs() == local_summary(served))
    real = logsyncops._pass  # noqa: SLF001
    codes = iter(["epoch_changed", "live_check_failed"])
    recovered = threading.Event()  # set once the hub holds the new log, i.e. the first successful pass has sent it
    release = threading.Event()  # holds that pass open, so the window between "sent" and "error cleared" is observable

    def flaky(service, rt):
        code = next(codes, None)
        if code is not None:
            raise HubRefused(code)
        real(service, rt)
        if recovered.is_set() or loghub.logs() == local_summary(served):
            recovered.set()
            assert release.wait(10)

    monkeypatch.setattr(logsyncops, "_pass", flaky)
    human.ticket("during the re-seal")
    assert recovered.wait(12) and len(loghub.logs()) >= 2
    # The hub has everything but the loop clears ``last_error`` only after the pass returns: this used to be a race
    # (the test asserted None here). Now the window is held open, asserted, and then closed on purpose.
    status = logsyncops.status(served.service)
    assert status is not None and status["running"] and status["last_error"] == "live_check_failed"
    release.set()
    assert until(lambda: (logsyncops.status(served.service) or {}).get("last_error") is None, 12)
    assert logsyncops.status(served.service)["running"]


def test_core_1034_the_live_check_tells_a_moved_epoch_from_a_definite_no(monkeypatch, human, served, stub, loghub):  # noqa: F811
    """The pass's ``live`` closure: a moved epoch or unreadable state is retryable, a left workspace is a no."""
    from orch.hubclient import HubRefused
    from orch.hubclient import logsync as LS

    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    assert until(lambda: loghub.logs() == local_summary(served))
    captured: dict[str, Any] = {}
    real = LS.sync_logs
    monkeypatch.setattr(logsyncops, "OPEN_LINK", lambda service: loghub.links[-1])

    def spy(**kw):
        captured.update(kw)
        raise HubRefused("stopped_for_test")

    import orch.hubclient as H

    monkeypatch.setattr(H, "sync_logs", spy)
    logsyncops.stop(served.service)
    assert logsyncops.start(served.service)
    assert until(lambda: "live" in captured)
    monkeypatch.setattr(H, "sync_logs", real)
    live = captured["live"]
    assert live() is True
    real_load = hubops._load_state  # noqa: SLF001
    monkeypatch.setattr(hubops, "_load_state", lambda svc: (_ for _ in ()).throw(OSError("transient")))
    with pytest.raises(HubRefused) as e:
        live()
    assert e.value.code == "live_check_failed"
    monkeypatch.setattr(hubops, "_load_state", real_load)

    def moved(svc):
        st = real_load(svc)
        st.sealed = {**st.sealed, "epoch": st.sealed["epoch"] + 1}
        return st

    monkeypatch.setattr(hubops, "_load_state", moved)
    with pytest.raises(HubRefused) as e:
        live()
    assert e.value.code == "epoch_changed"
    monkeypatch.setattr(hubops, "_load_state", real_load)
    human.run("hub.leave", {})
    assert live() is False  # a definite no: the sender ends


def test_hub_status_reports_the_sender(human, served, stub, loghub):  # noqa: F811
    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    assert until(lambda: loghub.logs() == local_summary(served))
    data = ok(served.client().req("hub.status", {}, idem=False))["data"]
    assert data["log_sync"] == {"running": True, "last_error": None, "stuck": {}}


# --- D15b: the projection P (orch-core#1043) -------------------------------------------------------------------------


def _add_member(served) -> None:  # noqa: ANN001
    """A workspace-log commit by another writer: it changes ``roster_v``, so every ticket's ``P``."""
    from orch import crypto
    from tests.identity.helpers import Person

    p = Person()
    ws = served.lab.ws
    ws.tick()
    store = ws.other()
    ws.store = store
    try:
        store.append(
            ws.person_event(
                ws.owner, "workspace", "member.added", person=p.ref, name="quinn", role="member",
                pk_pub=crypto.b64u(p.pk_pub), device_cert=p.cert(created=ws.clock[0] * 1000),
            ),
            log="workspace",
        )  # fmt: skip
    finally:
        store.close()
        ws.store = None


def test_d15b_the_hub_holds_the_hosts_own_projection_of_each_ticket(human, served, stub, loghub):  # noqa: F811
    from orch.hubclient import check_proj

    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    uid, _key = human.ticket("one")
    assert until(lambda: uid in loghub.view.proj and loghub.logs() == local_summary(served))
    p = loghub.view.proj[uid]
    check_proj(p)
    store = served.lab.ws.other()
    try:
        src = logsyncops.StoreSource(store)
        assert p == src.proj(uid)  # the same values the host would admit with: nothing moved since
        assert isinstance(p["waiting"], list) and p["waiting"] == sorted(set(p["waiting"]))  # R14b
        assert p["roster_v"] == store.state.workspace.roster_v and p["status"] == store.state.tickets[uid].status
        assert (p["seq"], p["head"]) == (store.log_seq(uid), store.head_at(uid, store.log_seq(uid)))
        assert src.proj("workspace") is None
    finally:
        store.close()


def test_d15b_a_changed_projection_with_no_new_line_is_sent_once_for_a_burst(monkeypatch, human, served, stub, loghub):  # noqa: F811
    monkeypatch.setattr(logsyncops, "PROJ_QUIET_S", 1.0)
    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    uid, _key = human.ticket("one")
    assert until(lambda: uid in loghub.view.proj and loghub.logs() == local_summary(served))
    # members are added by local signing below; C8 makes full mode sign through the hub, so lift that rule here
    before = len(loghub.pages)
    roster = loghub.view.proj[uid]["roster_v"]
    seq = loghub.view.proj[uid]["seq"]
    _add_member(served)
    logsyncops.wake(served.service)
    time.sleep(0.2)
    _add_member(served)  # a second change inside the quiet period: still one refresh
    logsyncops.wake(served.service)
    assert until(lambda: loghub.view.proj[uid]["roster_v"] == roster + 2, timeout=15)
    new = loghub.pages[before:]
    refresh = [x for x in new if x["log"] == uid]
    assert len(refresh) == 1 and (refresh[0]["from_seq"], refresh[0]["count"]) == (seq, 1)  # one line, once
    assert loghub.logs() == local_summary(served)
    n = len(loghub.calls)
    logsyncops.wake(served.service)
    time.sleep(0.5)
    assert len(loghub.calls) == n  # settled
