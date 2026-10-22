"""C3b task 5: a custodial event the hub delivers lands in the log through the host (ticket-format 15.5).

The full path on a served workspace (real store, real model, real signatures from the FAKE hub vector keys): a full
join, a hub snapshot, then ``hubdeliver.deliver`` with an event the hub signed for THIS workspace and ticket. Covers
the ledger pins riding with the event, dedup before verification, the reply codes, and that a refused event pins
nothing. The pure parts (reply table, pin planning, rate limit) are in tests/hub/test_event_deliver.py."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from orch.crypto import suite as S
from orch.crypto.encoding import b64u
from orch.host import hubdeliver, hubops
from orch.hubclient import custodial as C
from tests.host import human_helpers
from tests.host.dispatch_helpers import ok
from tests.host.test_hub_ops import (  # noqa: F401 - fixtures
    HUB_ID,
    connect_args,
    dev_workspace,
    join_args,
    stub,
)
from tests.hub.vec import V, private_key

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

CASE = next(c for c in V["cases"] if c["name"] == "custodial_valid")["args"]
ALICE = V["ids"]["user_alice"]
HUB_FIELDS = (*C.HUB_FIELDS, "sig")


def hub_snapshot(wid: str, version: int = 3, roles: dict[str, str] | None = None) -> dict[str, Any]:
    """The vector snapshot for this workspace (signed by the vector HK_op), at ``version``."""
    o = copy.deepcopy(V["objects"]["snapshot_a_v3"]["o"])
    o.update(workspace_id=wid, version=version, roles=roles or {ALICE: "admin"}, revoked_channels=[])
    from orch.hubclient import wire

    return {"o": o, "sig": b64u(S.sign(private_key("hk_op"), wire.SIG_SNAPSHOT + wire.cj(o)))}


def mint(wid: str, log: str, typ: str, **payload: Any) -> dict[str, Any]:
    """A custodial event of the vector user for this workspace and log, both signatures made fresh (contract 8.2)."""
    src = CASE["event"]
    e = {k: copy.deepcopy(src[k]) for k in ("actor", "auth", "channel", "channel_cert", "channel_factor", "hub_id",
                                             "snapshot_version", "uk_cert")}  # fmt: skip
    e.update(v=2, hash_v=1, type=typ, **payload)
    e["channel_proof"] = b64u(
        S.sign(private_key("channel_alice_phone"), C.SIG_CHANNEL_PROOF + C.event_digest(wid, log, e))
    )
    e["sig"] = b64u(S.sign(private_key("uk_alice"), C.custodial_label(typ) + C._context(wid, log, e)))  # noqa: SLF001
    return e


class Env:
    def __init__(self, served: Any, human: Any, version: int = 3) -> None:
        self.served = served
        human.run("hub.join", join_args(mode="full"))
        ok(served.client().req("hub.connect", connect_args(), idem=True))
        self.service = served.service
        self.service.submit(self._dev, self.service.store).result()
        self.wid = served.wid
        self.state = hubops._load_state(self.service)  # noqa: SLF001
        self.state.snapshot = hub_snapshot(self.wid, version)
        self.uid, _ = served.ticket("hub ticket")
        served.lab.ws.clock[0] = 1_790_000_000.5  # inside the vector certificates' validity (contract now_ms)
        self.n = 0

    def store(self) -> Any:
        store = self.served.lab.ws.other()
        self._dev(store)
        return store

    @staticmethod
    def _dev(store: Any) -> None:
        """The lab is a dev workspace (the dev_workspace fixture): its replay verifies the vectors' dev hub material."""
        store.hub_dev = True
        store._needs_reload = True  # noqa: SLF001
        store.refresh()

    def lines(self, log: str) -> list[dict[str, Any]]:
        store = self.store()
        try:
            return [e for e, _ in store.events_after(log, 0, 1 << 20)]
        finally:
            store.close()

    def head(self, log: str) -> str | None:
        store = self.store()
        try:
            return store.log_head(log)
        finally:
            store.close()

    def roster_v(self) -> int:
        store = self.store()
        try:
            return store.state.workspace.roster_v
        finally:
            store.close()

    def event(self, typ: str = "log.added", **over: Any) -> dict[str, Any]:
        self.n += 1
        base = {
            "id": f"01J9ZP000000000000000{self.n:05d}",
            "based_on": self.head(self.uid),
            "roster_v": self.roster_v(),
        }
        payload = {"text": "from the hub"} if typ == "log.added" else {}
        return mint(self.wid, self.uid, typ, **{**base, **payload, **over})

    def deliver(self, event: dict[str, Any], log: str | None = None) -> str | None:
        return self.service.submit(hubdeliver.deliver, self.service, self.state, log or self.uid, event).result()


def deliver_with(env: Env, event: dict[str, Any], fetch: Any) -> str | None:
    return env.service.submit(lambda: hubdeliver.deliver(env.service, env.state, env.uid, event, fetch=fetch)).result()


@pytest.fixture
def env(served, human, stub):  # noqa: F811
    return Env(served, human)


def test_a_verified_event_lands_with_the_ledger_pins_in_front_of_it(env):
    ws_before = len(env.lines("workspace"))
    e = env.event()
    assert env.deliver(e) is None
    ws = env.lines("workspace")[ws_before:]
    assert [(x["type"], x["object"]["o"]["kind"]) for x in ws] == [
        ("hub.pinned", "hub.card"),
        ("hub.pinned", "hub.snapshot"),
    ]  # the pins the event needs, in the order of 15.2, then the event
    line = env.lines(env.uid)[-1]
    assert line["id"] == e["id"] and line["auth"] == "hub" and line["actor"]["id"] == "u_" + ALICE
    assert {k: v for k, v in line.items() if k not in C.HOST_FIELDS} == e  # exactly the delivered event + host fields
    # the ticket line is ordered after its pins (ws_seq), and the model replays the whole log to the same view
    assert line["ws_seq"] == env.lines("workspace")[-1]["seq"]
    store = env.store()
    try:
        assert not store.chain_errors()
        assert [i.code for i in store.state.invalid] == [] if hasattr(store.state, "invalid") else True
    finally:
        store.close()


def test_a_resend_is_acknowledged_and_changes_nothing_other_bytes_are_a_replay(env):
    e = env.event()
    assert env.deliver(e) is None
    before = (len(env.lines(env.uid)), len(env.lines("workspace")))
    env.served.lab.ws.clock[0] += 10_000_000  # the certificates have long expired: dedup runs BEFORE verification
    assert env.deliver(copy.deepcopy(e)) is None
    other = copy.deepcopy(e)
    other["text"] = "another text, same id"
    assert env.deliver(other) == "replay"
    assert (len(env.lines(env.uid)), len(env.lines("workspace"))) == before


def test_a_refused_event_pins_nothing(env):
    ws_before = env.lines("workspace")
    stale = env.event(roster_v=env.roster_v() + 5)
    assert env.deliver(stale) == "event_changed"  # members.stale -> the person may sign again
    assert env.lines("workspace") == ws_before  # not even the snapshot pin the event would have needed
    assert env.lines(env.uid)[-1]["type"] != "log.added"
    # after the refusal a good event still lands and brings the pin
    assert env.deliver(env.event()) is None
    assert len(env.lines("workspace")) == len(ws_before) + 2  # the card and the snapshot


@pytest.mark.parametrize(
    ("make", "code"),
    [
        (lambda env: env.event(based_on="sha256:" + "0" * 64), "event_changed"),  # event.bad_base
        (lambda env: env.event("claim.released", session="s_1", reason="released"), "forbidden_role"),  # no claim
        (lambda env: {**env.event(), "channel_factor": "passkey"}, "factor_mismatch"),  # not what the cert says
    ],
)
def test_reply_codes(env, make, code):
    assert env.deliver(make(env)) == code


def test_an_unknown_snapshot_is_fetched_once_and_a_failed_fetch_is_retryable(env):
    env.state.snapshot = hub_snapshot(env.wid, version=2)  # the event names snapshot 3
    e = env.event()
    out = deliver_with(env, e, lambda st: False)
    assert out == "unknown_snapshot" and out in C.RETRYABLE
    fetched = []

    def fetch(st):
        fetched.append(1)
        st.snapshot = hub_snapshot(env.wid, version=3)
        return True

    assert deliver_with(env, e, fetch) is None
    assert fetched == [1]


def test_a_user_the_snapshot_gives_no_role_is_denied_and_nothing_lands(env):
    env.state.snapshot = hub_snapshot(env.wid, roles={"b" * 32: "admin"})
    before = len(env.lines(env.uid))
    assert env.deliver(env.event()) == "forbidden_role"
    assert len(env.lines(env.uid)) == before


def test_the_factor_floor_of_the_workspace_policy_reaches_the_verifier(env, monkeypatch):
    seen = []
    real = C.verify_hub_event

    def spy(state, event, **kw):
        seen.append(kw["min_factor"])
        return real(state, event, **kw)

    monkeypatch.setattr(C, "verify_hub_event", spy)
    assert env.deliver(env.event()) is None
    assert seen == ["password+totp"]  # policies.factor_floor(...) for a log.added: the weakest, from the function


def test_an_admitted_event_records_its_user_under_the_hub(env):
    """The user id is recorded in the model state by the admitted line itself (no injected state), Opus P1."""
    assert env.deliver(env.event()) is None
    store = env.store()
    try:
        assert store.state.workspace.hub["user_hub"][ALICE] == HUB_ID
    finally:
        store.close()


def test_selective_inputs_come_from_the_store_and_the_share_state(env):
    def read():
        land = hubdeliver.StoreLanding(env.service, None, env.state)
        heads = land.log_heads(env.uid)
        return len(heads), heads[len(heads) - 1], land.shared_items(env.uid)

    n, last, (live, superseded) = env.service.submit(read).result()
    assert n == len(env.lines(env.uid)) and last == env.head(env.uid)
    assert live == {} and superseded == frozenset()  # nothing shared yet: no decision is granted (fail closed)
