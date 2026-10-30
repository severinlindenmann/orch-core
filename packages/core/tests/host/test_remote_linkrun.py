"""R4 follow-up (#1592): the runner of the dedicated stream link (``host/remote/linkrun.py``) against a fake link
factory: it runs only while wanted, dials with the stream ALPN, drains queued closes before it closes the link, redials
on a rebind, never runs twice and ends with the workspace. The real hub is in ``test_remote_hub_e2e.py``."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from orch.host import peerops
from orch.host.remote import gate, linkrun, policy
from orch.hubclient.tlslink.client import STREAM_LINK_ALPN, LinkError
from tests.host import human_helpers
from tests.host.remote_world import World

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

BINDING_A, BINDING_B = ("aa" * 16, 1), ("aa" * 16, 2)


class FakeLink:
    """What the pump and the runner use of a ``LinkClient``; every call is recorded in order."""

    hub_id = "7b" * 16

    def __init__(self, w: World) -> None:
        self.w = w
        self.events: list[tuple[str, Any]] = []
        self.is_open = True
        self.mu = threading.Lock()

    def next_stream(self, wait: float = 0.0):
        time.sleep(min(wait, 0.01))
        with self.mu:
            return self.w.link.frames.pop(0) if self.w.link.frames else None

    def answer_stream_open(self, seq: int, error: str | None = None) -> None:
        self.w.link.answers.append((seq, error))

    def send_stream(self, op: str, body: dict[str, Any]) -> None:
        self.events.append((op, body))
        self.w.link.sent.append((op, body))

    def call(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        self.events.append(("call:" + op, body))
        return {"addresses": [], "name": "x"}

    def close(self) -> None:
        self.is_open = False
        self.events.append(("close", None))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]


class Dialer:
    def __init__(self, w: World) -> None:
        self.w = w
        self.links: list[FakeLink] = []
        self.alpns: list[bytes | None] = []
        self.fail: Exception | None = None

    def __call__(self, service: Any, alpn: bytes | None = None) -> FakeLink:
        self.alpns.append(alpn)
        if self.fail is not None:
            raise self.fail
        link = FakeLink(self.w)
        self.links.append(link)
        return link


def wait_for(cond, timeout: float = 8.0, what: str = "condition") -> None:
    end = time.time() + timeout
    while not cond() and time.time() < end:
        time.sleep(0.02)
    assert cond(), what


@pytest.fixture
def dial(monkeypatch, human, served):
    holder: dict[str, Dialer] = {}

    def make(w: World) -> Dialer:
        holder["d"] = Dialer(w)
        monkeypatch.setattr(peerops, "_open_link", holder["d"])
        return holder["d"]

    yield make
    linkrun.stop(served.service)


@pytest.fixture
def world(human, served, dial):
    w = World(human, served)
    w.dialer = dial(w)
    return w


def runner(w: World) -> linkrun.Runner:
    return linkrun._runner(w.service)  # noqa: SLF001


def now_ms(w: World) -> int:
    return int(w.service.clock() * 1000)


def feed(
    w: World, *, on: bool = True, until: int | None = None, code_until: int = 0, binding=BINDING_A
) -> linkrun.Runner:
    r = runner(w)
    r.set_inputs(on, until, code_until, binding)
    r.ensure_started()
    return r


def test_no_runner_while_off_or_unbound(world):
    r = feed(world, on=False)
    assert r.thread is None and world.dialer.links == []
    r = feed(world, on=True, binding=None)  # on, but not joined to a hub
    assert r.thread is None and world.dialer.links == []


def test_refresh_without_a_hub_binding_starts_nothing(world):
    linkrun.refresh(world.service)  # the world's model has remote on but no hub.joined line
    assert runner(world).thread is None and world.dialer.links == []


def test_starts_when_on_and_dials_the_stream_alpn(world):
    r = feed(world)
    wait_for(lambda: world.dialer.links, what="no link dialled")
    assert world.dialer.alpns == [STREAM_LINK_ALPN]
    assert r.thread is not None and r.thread.is_alive()


def test_one_runner_per_workspace_two_wakes_one_thread(world):
    r = feed(world)
    t = r.thread
    for _ in range(3):
        r.ensure_started()
        linkrun.refresh(world.service)
        assert runner(world) is r
    assert r.thread is t
    assert len([x for x in threading.enumerate() if x.name.startswith("orch-streams-")]) == 1


def test_starts_for_a_code_while_off_and_stops_when_it_lapses(world):
    r = feed(world, on=False, code_until=now_ms(world) + 5_000)
    wait_for(lambda: world.dialer.links, what="no link for an outstanding code")
    world.served.lab.ws.clock[0] += 10  # the code's end passes
    wait_for(lambda: r.thread is None, what="the runner kept running after the code lapsed")
    assert world.dialer.links[0].kinds()[-1] == "close"


def test_stops_when_the_policy_end_date_passes(world):
    r = feed(world, until=now_ms(world) + 5_000)
    wait_for(lambda: world.dialer.links)
    world.served.lab.ws.clock[0] += 10
    wait_for(lambda: r.thread is None and r.link is None)


def test_off_sends_queued_closes_before_closing_the_link(world):
    r = feed(world)
    wait_for(lambda: world.dialer.links)
    sid = "ab" * 16
    world.link.frames.append(("stream_open", 7, {"stream_id": sid, "device_id": world.did, "kind": "session"}))
    wait_for(lambda: world.link.answers, what="the open was not served")
    assert world.link.answers[-1][1] is None and gate.table(world.service).is_open(sid)
    assert world.service.submit(policy.turn_off, world.service, "owner").result(timeout=10)
    wait_for(lambda: r.thread is None and r.link is None, what="the runner kept the link")
    kinds = world.dialer.links[0].kinds()
    assert ("stream_close", {"stream_id": sid, "reason": "remote.off"}) in world.dialer.links[0].events
    assert kinds.index("stream_close") < kinds.index("close")  # the client learns why, THEN the link goes
    assert not gate.table(world.service).is_open(sid)


def test_a_rebind_closes_the_link_and_dials_a_new_one(world):
    r = feed(world, binding=BINDING_A)
    wait_for(lambda: len(world.dialer.links) == 1)
    r.set_inputs(True, None, 0, BINDING_B)
    wait_for(lambda: len(world.dialer.links) == 2, what="no redial after the rebind")
    assert world.dialer.links[0].kinds()[-1] == "close" and world.dialer.links[1].is_open


def test_unbind_ends_the_runner(world):
    r = feed(world)
    wait_for(lambda: world.dialer.links)
    r.set_inputs(True, None, 0, None)
    wait_for(lambda: r.thread is None and r.link is None)


def test_a_dead_link_is_redialled_with_backoff(world, monkeypatch):
    monkeypatch.setattr(linkrun, "BACKOFF_FIRST_S", 0.05)
    r = feed(world)
    wait_for(lambda: world.dialer.links)
    world.dialer.links[0].call = lambda *a: (_ for _ in ()).throw(LinkError("malformed"))  # type: ignore[method-assign]
    world.dialer.links[0].is_open = False
    monkeypatch.setattr(linkrun, "KEEPALIVE_S", 0.01)
    wait_for(lambda: len(world.dialer.links) >= 2, what="no redial")
    assert r.thread is not None


def test_a_failed_link_closes_its_streams_even_when_the_redial_keeps_failing(world, monkeypatch):
    monkeypatch.setattr(linkrun, "BACKOFF_FIRST_S", 0.05)
    r = feed(world)
    wait_for(lambda: world.dialer.links)
    sid = "ab" * 16
    world.link.frames.append(("stream_open", 7, {"stream_id": sid, "device_id": world.did, "kind": "session"}))
    wait_for(lambda: gate.table(world.service).is_open(sid), what="the open was not served")
    world.dialer.fail = LinkError("unreachable")  # no new link will ever come
    world.dialer.links[0].call = lambda *a: (_ for _ in ()).throw(LinkError("malformed"))  # type: ignore[method-assign]
    world.dialer.links[0].is_open = False
    monkeypatch.setattr(linkrun, "KEEPALIVE_S", 0.01)
    wait_for(lambda: r.link is None and len(world.dialer.alpns) >= 3, what="the link did not fail and redial")
    assert gate.table(world.service).open_streams() == []


def test_alpn_not_negotiated_means_no_stream_and_a_retry(world, monkeypatch):
    monkeypatch.setattr(linkrun, "BACKOFF_FIRST_S", 0.05)
    world.dialer.fail = LinkError("unsupported")
    r = feed(world)
    wait_for(lambda: len(world.dialer.alpns) >= 3, what="the runner did not retry")
    assert r.link is None and r.last_error == "LinkError"
    assert world.dialer.links == [] and gate.table(world.service).open_streams() == []


def test_keepalive_is_sent_on_an_idle_link(world, monkeypatch):
    monkeypatch.setattr(linkrun, "KEEPALIVE_S", 0.2)
    feed(world)
    wait_for(lambda: world.dialer.links)
    wait_for(lambda: world.dialer.links[0].kinds().count("call:hub_where") >= 2, what="no keepalive")


def test_workspace_close_stops_the_runner(world):
    r = feed(world)
    wait_for(lambda: world.dialer.links)
    t = r.thread
    linkrun.stop(world.service)
    assert t is not None and not t.is_alive()
    assert world.dialer.links[0].kinds()[-1] == "close"
    assert linkrun.current(world.service) is None
    linkrun.refresh(world.service)  # a late commit must not bring it back
    assert linkrun.current(world.service) is None
