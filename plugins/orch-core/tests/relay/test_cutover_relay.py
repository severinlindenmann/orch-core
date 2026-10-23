# ruff: noqa: F811
"""V3 C10 (design-c10 M2.4, M4): the relay half of the switch over a real :class:`RelayConn` with the ``cut_over`` gate
and a simulated relay (login, holder fence, the cut-over routes of hub protocol 10.8.3)."""

from __future__ import annotations

import contextlib

import pytest

from orch import crypto
from orch.host import cutover, directpath, hubops, relayhook
from orch.host.errors import HostError
from tests.host.human_helpers import human, lab, served  # noqa: F401 - fixtures
from tests.host.test_h5_switch import env, pin_snapshot  # noqa: F401 - fixtures
from tests.host.test_hub_ops import dev_workspace, stub  # noqa: F401
from tests.relay.bridgekit import R6, Epochs
from tests.relay.mock import MockRelay, json_reply
from tests.relay.sim import SimRelay, _err


class Relay:
    """SimRelay plus the cut-over rows of orch-relay (cutover.py): workspace PUT/GET/DELETE, fenced, ``unknown_hub``."""

    def __init__(self, env, tmp_path, *, known_hubs=("x",)) -> None:
        self.mock = MockRelay()
        self.sim = SimRelay(self.mock.origin(), owner_pk=b"\x04" + b"\0" * 64)
        wsk_pub, _sign = hubops._wsk_sign(env.service)  # noqa: SLF001
        self.sim.cards[env.wid] = {"o": {"wsk_pub": crypto.b64u(wsk_pub)}}
        self.wid, self.row, self.known, self.calls = env.wid, None, set(known_hubs), []
        self.mock.fallback = self.handle

    def handle(self, s):
        self.calls.append((s.method, s.path))
        if s.path != f"/v2/workspaces/{self.wid}/cutover":
            return self.sim(s)
        if (r := self.sim._auth(s, self.wid)) is not None:  # noqa: SLF001
            return r
        if s.method == "GET":
            return json_reply(
                200, {"state": self.row["state"], "hub_id": self.row["hub_id"]} if self.row else {"state": "direct"}
            )
        if (r := self.sim._fence(s, self.wid)) is not None:  # noqa: SLF001
            return r
        if s.method == "DELETE":
            self.row = None
            return json_reply(200, {"state": "direct"})
        import json

        body = json.loads(s.body)
        if body["hub_id"] not in self.known:
            return _err(403, "unknown_hub")
        self.row = {"state": body["state"], "hub_id": body["hub_id"]}
        return json_reply(200, {"state": body["state"], "hub_id": body["hub_id"]})


@pytest.fixture
def relay(env, tmp_path, monkeypatch):
    r = Relay(env, tmp_path, known_hubs=(env.state.hub_id,))
    link = {"v": 1, "workspace_id": env.wid, "origin": r.mock.origin(), "max_publication_bytes": 4 << 20,
            "linked_at": 1_790_000_000_000, "card_seq": 1}  # fmt: skip
    # a workspace linked to a relay first and joined a hub later: link.json exists (the guard's local-epochs refusal
    # is for a NEW link on a hub workspace)
    env.service.submit(lambda: env.service.store.relay_link(link, guard=contextlib.nullcontext)).result()
    monkeypatch.setattr(relayhook, "SEAMS", relayhook.Seams(r6=lambda s: (Epochs(), R6(Epochs()))))
    yield r
    r.mock.close()


def test_the_cut_over_goes_to_the_relay_through_the_gate_with_the_holder_fence(env, relay):
    pin_snapshot(env)
    assert cutover.run(env.service, force_start=True) == "closed"
    assert relay.row == {"state": "cut_over", "hub_id": env.state.hub_id}
    methods = [m for m, p in relay.calls if p.endswith("/cutover")]
    assert methods == ["GET", "PUT"]  # resume-safe: it reads first
    assert not any("/bridge" in p or "/card" in p or "/members" in p for _m, p in relay.calls)


def test_a_relay_that_does_not_know_the_hub_leaves_it_closing_with_a_reason(env, relay):
    relay.known = set()
    pin_snapshot(env)
    assert cutover.run(env.service, force_start=True) == "closing"
    st = hubops._load_state(env.service)  # noqa: SLF001
    assert st.direct == "closing" and "hub.cutover_unknown_hub" in st.direct_note
    assert directpath.closed(env.service.state_dir, env.wid, env.service.host_uid)  # fail closed meanwhile
    relay.known = {st.hub_id}
    assert cutover.run(env.service) == "closed"  # the next start finishes it
    assert relay.row["state"] == "cut_over"


def test_a_repeat_after_a_crash_reads_cut_over_and_does_not_put_again(env, relay):
    relay.row = {"state": "cut_over", "hub_id": env.state.hub_id}
    pin_snapshot(env)
    assert cutover.run(env.service, force_start=True) == "closed"
    assert [m for m, p in relay.calls if p.endswith("/cutover")] == ["GET"]


def test_a_leave_deletes_a_pending_row_and_never_a_cut_over_row(env, relay):
    relay.row = {"state": "pending", "hub_id": "x"}
    assert cutover.cutover_delete(env.service) is True and relay.row is None
    relay.row = {"state": "cut_over", "hub_id": "x"}
    assert cutover.cutover_delete(env.service) is True and relay.row["state"] == "cut_over"  # I6: stays closed


def _left_and_reopened(env):
    """The owner leaves the hub (the host unbinds), then signs relay.reopened: the state a reopening needs."""
    from tests.host.dispatch_helpers import ok

    env.human.run("hub.leave", {})
    ok(env.served.client().req("hub.unbind", {}, idem=True))
    env.human.run("relay.reopen", {})


def test_reopen_deletes_the_row_opens_the_state_and_clears_the_gate(env, relay):
    pin_snapshot(env)
    cutover.run(env.service, force_start=True)
    _left_and_reopened(env)
    assert relay.row is not None and relay.row["state"] == "cut_over"  # the leave itself did not reopen it (I6)
    out = cutover.reopen(env.service)
    assert out["direct"] == "open" and relay.row is None
    assert not directpath.closed(env.service.state_dir, env.wid, env.service.host_uid)
    st = hubops._load_state(env.service)  # noqa: SLF001
    assert st.direct == "open" and st.cut_epoch is None


def test_reopen_is_refused_when_the_path_is_not_closed(env):
    with pytest.raises(HostError) as e:
        cutover.reopen(env.service)
    assert e.value.code == "relay.not_cut_over"


def test_reopen_is_refused_before_the_leave_or_the_signature(env, relay):
    pin_snapshot(env)
    cutover.run(env.service, force_start=True)
    with pytest.raises(HostError) as e:  # still joined
        cutover.reopen(env.service)
    assert e.value.code == "hub.not_joined" and relay.row is not None
    env.human.run("hub.leave", {})
    with pytest.raises(HostError) as e:  # left, but the owner did not sign relay.reopened
        cutover.reopen(env.service)
    assert e.value.code == "hub.not_joined" and relay.row is not None


def _check(**over):
    import types

    st = dict(direct="closed", hub_id="h", status="unbound", rotate_after=None, outbox={}, publish_owed={})
    model = {"status": "left", "reopened": True, "hub_id": "h"}
    rotation = over.pop("rotation", "none")
    model.update(over.pop("model", {}))
    st.update(over)
    cutover._reopen_check(types.SimpleNamespace(**st), model, rotation, publish_set=True)  # noqa: SLF001


def test_every_precondition_of_a_reopening_is_checked():
    _check()  # all good
    for kw, code in (
        ({"direct": "open"}, "relay.not_cut_over"),
        ({"model": {"status": "joined"}}, "hub.not_joined"),
        ({"model": {"reopened": False}}, "hub.not_joined"),
        ({"model": {"hub_id": "other"}}, "hub.state_mismatch"),
        ({"status": "bound"}, "hub.not_ready"),
        ({"rotate_after": 3}, "hub.rotation_pending"),
        ({"rotation": "pending"}, "hub.rotation_pending"),
        ({"outbox": {"h": {}}}, "hub.not_ready"),
        ({"publish_owed": {"h": {}}}, "hub.not_ready"),
        ({"direct": "closing"}, "hub.not_ready"),
    ):
        with pytest.raises(HostError) as e:
            _check(**kw)
        assert e.value.code == code, kw


# --- the Opus review of #1112 (L2-C10i-1 to -4) ---------------------------------------------------------------------


def test_a_leave_while_closing_unbinds_deletes_nothing_owed_and_the_path_can_be_reopened(env, relay):
    """L2-C10i-1: the real CUTOVER_DELETE seam, through hub.unbind. `closing` (the relay does not know the hub) -> leave
    -> unbind -> `closed`; reopen then works (before: owed forever)."""
    from tests.host.dispatch_helpers import ok

    relay.known = set()
    pin_snapshot(env)
    assert cutover.run(env.service, force_start=True) == "closing"
    env.human.run("hub.leave", {})
    got = ok(env.served.client().req("hub.unbind", {}, idem=True))["data"]
    assert got["cutover"] == "none"  # nothing owed any more
    assert hubops._load_state(env.service).direct == "closed"  # noqa: SLF001
    env.human.run("relay.reopen", {})
    assert cutover.reopen(env.service)["direct"] == "open"


def test_a_relay_that_is_down_never_fails_the_unbind_it_stays_owed(env, relay):
    from tests.host.dispatch_helpers import ok

    relay.known = set()
    pin_snapshot(env)
    cutover.run(env.service, force_start=True)
    relay.mock.close()  # the relay goes away
    env.human.run("hub.leave", {})
    got = ok(env.served.client().req("hub.unbind", {}, idem=True))["data"]
    assert got["cutover"] == "owed" and hubops._load_state(env.service).direct == "closing"  # noqa: SLF001


def test_a_concurrent_unbind_keeps_its_fields_the_cut_over_writes_only_its_own(env, relay, monkeypatch):
    """L2-C10i-2: another writer saves hub.json between the preflight and the seal; its fields survive."""
    pin_snapshot(env)
    real = cutover._stop_relay  # noqa: SLF001

    def stop_then_other_writer(service):
        real(service)
        with hubops._MU:  # noqa: SLF001
            st = hubops._load_state(service)  # noqa: SLF001
            st.publish_owed = {st.hub_id: {"owed": "by a concurrent unbind"}}
            hubops._write_state(service, st)  # noqa: SLF001

    monkeypatch.setattr(cutover, "_stop_relay", stop_then_other_writer)
    assert cutover.run(env.service, force_start=True) == "closed"
    st = hubops._load_state(env.service)  # noqa: SLF001
    assert st.publish_owed == {st.hub_id: {"owed": "by a concurrent unbind"}} and st.direct == "closed"


def test_a_binding_that_changed_aborts_the_cut_over_without_writing(env, relay, monkeypatch):
    pin_snapshot(env)
    real = cutover._stop_relay  # noqa: SLF001

    def unbound_meanwhile(service):
        real(service)
        with hubops._MU:  # noqa: SLF001
            st = hubops._load_state(service)  # noqa: SLF001
            st.status = "unbound"
            hubops._write_state(service, st)  # noqa: SLF001

    monkeypatch.setattr(cutover, "_stop_relay", unbound_meanwhile)
    cutover.run(env.service, force_start=True)
    st = hubops._load_state(env.service)  # noqa: SLF001
    assert st.status == "unbound" and st.direct == "closing" and st.cut_epoch is None  # nothing of ours after the abort


def test_reopen_after_a_crash_between_the_relay_delete_and_the_save_is_idempotent(env, relay):
    """L2-C10i-3: the row is gone already (our DELETE landed, the save did not): GET sees `direct`, no second DELETE."""
    pin_snapshot(env)
    cutover.run(env.service, force_start=True)
    _left_and_reopened(env)
    relay.row = None  # deleted before the crash
    relay.calls.clear()
    assert cutover.reopen(env.service)["direct"] == "open"
    assert [m for m, p in relay.calls if p.endswith("/cutover")] == ["GET"]


def test_reopen_says_when_the_relay_did_not_start(env, relay, monkeypatch):
    """L2-C10i-4: the answer carries the reason, not an `ok`."""
    pin_snapshot(env)
    cutover.run(env.service, force_start=True)
    _left_and_reopened(env)
    monkeypatch.setattr(env.service, "relay_start", lambda: "none")
    env.service.relay_reason = "start_failed: SeamMissing"
    out = cutover.reopen(env.service)
    assert out["direct"] == "open" and out["relay_started"] is False and "start_failed" in out["relay"]
