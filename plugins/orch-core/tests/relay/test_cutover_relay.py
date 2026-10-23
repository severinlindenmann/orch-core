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


def test_reopen_deletes_the_row_opens_the_state_and_clears_the_gate(env, relay):
    pin_snapshot(env)
    cutover.run(env.service, force_start=True)
    out = cutover.reopen(env.service)
    assert out["direct"] == "open" and relay.row is None
    assert not directpath.closed(env.service.state_dir, env.wid, env.service.host_uid)
    st = hubops._load_state(env.service)  # noqa: SLF001
    assert st.direct == "open" and st.cut_epoch is None


def test_reopen_is_refused_when_the_path_is_not_closed(env):
    with pytest.raises(HostError) as e:
        cutover.reopen(env.service)
    assert e.value.code == "relay.not_cut_over"
