"""D10: the hub's relay client (protocol.md §10.8, §10.8.1, §10.8.2) against an in-process fake relay that runs the
contract's reference verifier. No network."""

from __future__ import annotations

import pytest
from channels_helpers import Env, prove
from cryptography.hazmat.primitives.asymmetric import ec
from relay_helpers import FakeRelay

from orch_hub.contract import hub as H
from orch_hub.contract import keystore as KS
from orch_hub.contract import phase4 as P4
from orch_hub.contract import relayroutes as RR
from orch_hub.contract import suite as S
from orch_hub.contract.canon import loads_strict, unb64u
from orch_hub.relay import PushNotifier, RelayClient, RelayError

TOKEN = bytes(range(40))
PUSH_PRIV = S.key_from_seed(bytes(range(1, 33)))  # the phone's push key (§6.4), never its channel key
PUSH_PUB = S.public_bytes(PUSH_PRIV)


class World:
    def __init__(self, tmp_path, **kw):
        kw.setdefault("dev", True)
        self.env = Env(tmp_path)
        self.relay = FakeRelay(self.env.clock)
        self.sleeps: list[float] = []
        self.client = RelayClient(
            self.env.keys,
            "http://127.0.0.1:9",
            transport=self.relay,
            now_ms=self.env.clock,
            sleep=self.sleeps.append,
            jitter=lambda: 1.0,
            **kw,
        )

    def phone(self, u):
        """A phone channel paired by an operator code; returns (its private key, channel_id)."""
        priv = ec.generate_private_key(ec.SECP256R1())
        s = KS.ChannelSigner(priv)
        code = self.env.svc.create_pairing_code(u)["code"]
        begun = self.env.svc.begin_phone_registration(u, code, s.public, "iPhone", token=self.env.sign_in(u))
        cert = self.env.svc.complete_registration(begun["challenge_id"], prove(s, begun))
        return priv, cert["o"]["channel_id"]


@pytest.fixture
def w(tmp_path):
    x = World(tmp_path)
    yield x
    x.env.close()


def test_register_sends_the_card_unsigned_and_the_relay_holds_it(w):
    out = w.client.register()
    assert out["created"] is True and out["hub_id"] == w.env.keys.hub_id
    assert w.relay.cards[w.env.keys.hub_id] == w.env.keys.card()
    assert w.client.register()["created"] is False  # a repeat or a rotated card is the same call


def test_signed_requests_are_refused_before_registration(w):
    with pytest.raises(RelayError) as e:
        w.client.delete_push_target("ab" * 16)
    assert (e.value.code, e.value.status) == ("unknown_hub", 401)
    assert len(w.relay.log) == 1  # a 401 is final, not retried


def test_push_target_put_replace_delete(w):
    w.client.register()
    ch = "ab" * 16
    w.client.put_push_target(ch, "sandbox", "io.severin.orch", TOKEN)
    w.client.put_push_target(ch, "sandbox", "io.severin.orch", TOKEN[::-1])  # a new token replaces the held one
    assert w.relay.targets[ch]["token"] == TOKEN[::-1] and len(w.relay.targets) == 1
    w.client.delete_push_target(ch)
    w.client.delete_push_target(ch)  # idempotent
    assert w.relay.targets == {}


def test_push_reaches_the_phone_path_sealed_to_the_push_key_with_a_mailbox_copy(w):
    w.client.register()
    u = w.env.user("ann")
    signer, cid = w.phone(u)
    n = PushNotifier(w.client, w.env.svc)
    n.register_target(u, cid, "sandbox", "io.severin.orch", TOKEN, PUSH_PUB)
    ref = "cd" * 16
    assert n.notify_user(u, "prepare", ref) == {cid: "sent"}
    (sent,) = w.relay.sent
    outer = loads_strict(sent["outer"])
    assert set(outer) == {"v", "hub", "ch", "id", "c"} and outer["hub"] == w.env.keys.hub_id and outer["ch"] == cid
    # what the relay and APNs hold is ciphertext only
    assert b"prepare" not in sent["outer"] and ref.encode() not in sent["outer"]
    # the same c under the same id waits in the channel's mailbox (§10.8.1: the phone's catch-up path)
    assert w.relay.mailbox[(cid, outer["id"])]["c"] == outer["c"]
    # the phone's NSE opens it with its push key (§6.4, §10.8.2)
    args = {"hub_id": outer["hub"], "channel_id": cid, "msg_id": outer["id"], "recipient_priv": PUSH_PRIV}
    got = RR.open_push(outer["c"], **args)
    assert got["type"] == "prepare" and got["ref"] == ref and got["ts_ms"] == w.env.clock()
    # and only with that key, that hub, that channel, that message id; never with the channel's signing key
    for kw in (
        {"recipient_priv": signer},
        {"recipient_priv": ec.generate_private_key(ec.SECP256R1())},
        {"hub_id": "11" * 16},
        {"msg_id": "22" * 16},
    ):
        with pytest.raises(H.Refused):
            RR.open_push(outer["c"], **(args | kw))


def test_no_push_key_no_hint_and_the_signing_key_is_never_one(w):
    w.client.register()
    u = w.env.user("ann")
    signer, cid = w.phone(u)
    n = PushNotifier(w.client, w.env.svc)
    n.register_target(u, cid, "sandbox", "io.severin.orch", TOKEN)  # APNs target only, no push key yet
    assert n.notify_user(u, "prepare", "cd" * 16) == {} and w.relay.sent == [] and w.relay.mailbox == {}
    with pytest.raises(RelayError) as e:
        n.register_target(u, cid, "sandbox", "io.severin.orch", TOKEN, S.public_bytes(signer))
    assert e.value.code == "malformed"
    assert n.notify_user(u, "prepare") == {} and w.relay.sent == []


def test_only_live_phone_channels_of_that_user_get_a_push(w):
    w.client.register()
    u, v = w.env.user("ann"), w.env.user("bob")
    _, c1 = w.phone(u)
    _, c2 = w.phone(u)
    _, other = w.phone(v)
    from channels_helpers import register

    register(w.env, u)  # a browser channel: no APNs target, never pushed
    n = PushNotifier(w.client, w.env.svc)
    for c, user in ((c1, u), (c2, u), (other, v)):
        n.register_target(user, c, "sandbox", "io.severin.orch", TOKEN, PUSH_PUB)
    w.env.svc.revoke(c2)
    res = n.notify_user(u, "needs_you")
    assert res == {c1: "sent"}  # c2 revoked, browser skipped, bob's phone is not ann's
    assert [s["channel_id"] for s in w.relay.sent] == [c1]


def test_register_target_refuses_a_channel_of_another_user(w):
    w.client.register()
    u, v = w.env.user("ann"), w.env.user("bob")
    _, cid = w.phone(u)
    n = PushNotifier(w.client, w.env.svc)
    with pytest.raises(RelayError) as e:
        n.register_target(v, cid, "sandbox", "io.severin.orch", TOKEN, PUSH_PUB)
    assert e.value.code == "unknown_channel" and w.relay.targets == {}


def test_revocation_deletes_target_and_mailbox_at_the_relay(w):
    w.client.register()
    u = w.env.user("ann")
    _, cid = w.phone(u)
    n = PushNotifier(w.client, w.env.svc)
    w.env.svc.on_revoke = n.channel_revoked
    n.register_target(u, cid, "sandbox", "io.severin.orch", TOKEN, PUSH_PUB)
    w.client.mailbox_put(cid, "ee" * 16, "AAAA")
    assert cid in w.relay.targets and w.relay.mailbox
    w.env.svc.revoke(cid)
    assert w.relay.targets == {} and w.relay.mailbox == {} and n.pending_cleanup == set()
    assert n.notify_user(u, "prepare") == {}  # nothing for a revoked channel, and no relay call for it


def test_failed_cleanup_is_remembered_and_repeated(w):
    w.client.register()
    u = w.env.user("ann")
    _, cid = w.phone(u)
    n = PushNotifier(w.client, w.env.svc)
    w.env.svc.on_revoke = n.channel_revoked
    n.register_target(u, cid, "sandbox", "io.severin.orch", TOKEN, PUSH_PUB)
    w.relay.faults = [OSError("down")] * 8
    w.env.svc.revoke(cid)  # the revocation itself stands
    assert cid in w.env.acc.revoked_channels() and n.pending_cleanup == {cid} and cid in w.relay.targets
    w.relay.faults.clear()
    n.flush()
    assert n.pending_cleanup == set() and w.relay.targets == {}


def test_a_failing_hook_cannot_undo_a_revocation(w):
    u = w.env.user("ann")
    _, cid = w.phone(u)

    def boom(_cid):
        raise RuntimeError("relay exploded")

    w.env.svc.on_revoke = boom
    w.env.svc.revoke(cid)
    assert cid in w.env.acc.revoked_channels()


def test_mailbox_put_get_delete_and_conflict(w):
    w.client.register()
    ch, mid = "ab" * 16, "ee" * 16
    w.client.mailbox_put(ch, mid, "AAAA")
    w.client.mailbox_put(ch, mid, "AAAA")  # idempotent by id
    assert w.client.mailbox_get(ch, mid) == "AAAA"
    with pytest.raises(RelayError) as e:
        w.client.mailbox_put(ch, mid, "BBBB")
    assert (e.value.code, e.value.status) == ("message_conflict", 409)
    w.client.mailbox_delete(ch, mid)
    assert w.client.mailbox_get(ch, mid) is None


def test_retry_with_backoff_new_nonce_each_attempt_and_retry_after(w):
    w.client.register()
    ch = "ab" * 16
    w.relay.faults = [(503, {"retry-after": "2"}, b'{"error":"overloaded"}'), OSError("reset"), (500, {}, b"x")]
    w.client.put_push_target(ch, "sandbox", "io.severin.orch", TOKEN)
    assert w.sleeps == [2.0, 1.0, 2.0]  # max(backoff, Retry-After): 0.5*2^n capped
    assert ch in w.relay.targets
    assert len(w.relay.nonce_log) == len(set(w.relay.nonce_log))  # a replayed nonce would be 401 replay


def test_gives_up_after_max_attempts_and_does_not_retry_client_errors(w):
    w.client.register()
    w.relay.faults = [OSError("down")] * 4
    with pytest.raises(RelayError) as e:
        w.client.delete_push_target("ab" * 16)
    assert e.value.code == "unavailable" and len(w.sleeps) == 3
    w.relay.faults = [(400, {}, b'{"error":"malformed"}')]
    before = len(w.relay.log)
    with pytest.raises(RelayError):
        w.client.delete_push_target("ab" * 16)
    assert len(w.relay.log) == before + 1


def test_clock_skew_is_corrected_once(w):
    w.client.register()
    w.relay.clock = lambda: w.env.clock() + 600_000  # the relay's clock is ten minutes ahead
    w.client.put_push_target("ab" * 16, "sandbox", "io.severin.orch", TOKEN)
    assert "ab" * 16 in w.relay.targets


def test_dev_flag_must_match_the_card(tmp_path):
    w = World(tmp_path, dev=False)
    try:
        with pytest.raises(RelayError) as e:
            w.client.register()
        assert e.value.code == "dev_mismatch" and w.relay.log == []
    finally:
        w.env.close()


def test_relay_url_must_be_https_or_loopback(w):
    for bad in ("http://relay.example", "ftp://x", "https://u:p@x", "https://x/path"):
        with pytest.raises(RelayError):
            RelayClient(w.env.keys, bad, dev=True, transport=w.relay)
    with pytest.raises(RelayError) as e:
        RelayClient(w.env.keys, dev=True, transport=w.relay)  # the card has no relay_url
    assert e.value.code == "no_relay"


def test_requests_are_signed_over_the_exact_bytes(w):
    w.client.register()
    w.client.put_push_target("ab" * 16, "sandbox", "io.severin.orch", TOKEN)
    seen = {}

    def spy(method, target, headers, body, timeout):
        seen.update(method=method, target=target, headers=dict(headers), body=body)
        return w.relay(method, target, headers, body, timeout)

    w.client._transport = spy
    w.client.delete_push_target("ab" * 16)
    h = seen["headers"]
    msg = P4.relay_request_bytes(
        h["Orch-Hub"],
        h["Orch-Hub-Op-Cert"],
        int(h["Orch-Hub-Ts"]),
        bytes.fromhex(h["Orch-Hub-Nonce"]),
        seen["method"],
        seen["target"],
        seen["body"],
    )
    op_pub = unb64u(w.env.keys.op_cert()["o"]["hk_op_pub"], 65)
    assert S.verify(op_pub, unb64u(h["Orch-Hub-Signature"], 64), msg)
    assert seen["body"] == b"" and h["Orch-Hub"] == w.env.keys.hub_id
    assert h["Orch-Hub-Op-Cert"] == w.env.keys.op_cert()["o"]["cert_id"]


def test_prepare_notifies_the_users_phones_and_a_failing_hook_cannot_break_it(tmp_path):
    from custodial_helpers import Env as CEnv

    e = CEnv(tmp_path)
    try:
        calls = []
        e.engine._notify = lambda user, kind, ref: calls.append((user, kind, ref))
        p = e.prepare()
        assert calls == [(e.user, "prepare", p.prepared_id)]

        def boom(*_a):
            raise RuntimeError("relay down")

        e.engine._notify = boom
        assert e.prepare().prepared_id  # the prepare stands
    finally:
        e.close()


def test_dev_has_no_default():
    import inspect

    p = inspect.signature(RelayClient).parameters["dev"]
    assert p.default is inspect.Parameter.empty


def test_relay_errors_are_failures_and_unknown_codes_are_not_logged(w, caplog):
    w.client.register()
    u = w.env.user("ann")
    _, cid = w.phone(u)
    n = PushNotifier(w.client, w.env.svc)

    def refuse(*_a, **_k):
        raise RelayError("<script>evil", 400)

    n.register_target(u, cid, "sandbox", "io.severin.orch", TOKEN, PUSH_PUB)
    w.client.send_push = refuse
    with caplog.at_level("WARNING"):
        assert n.notify_user(u, "prepare") == {cid: "failed"}
    assert "bad_response" in caplog.text and "evil" not in caplog.text


def test_register_target_refuses_a_browser_channel(w):
    from channels_helpers import register

    u = w.env.user("ann")
    _, cert = register(w.env, u)
    cid = cert["o"]["channel_id"]
    n = PushNotifier(w.client, w.env.svc)
    with pytest.raises(RelayError) as e:
        n.register_target(u, cid, "sandbox", "io.severin.orch", TOKEN, PUSH_PUB)
    assert e.value.code == "unknown_channel"


def test_restart_pass_cleans_channels_revoked_before_the_restart(w):
    w.client.register()
    u = w.env.user("ann")
    _, cid = w.phone(u)
    n = PushNotifier(w.client, w.env.svc)
    n.register_target(u, cid, "sandbox", "io.severin.orch", TOKEN, PUSH_PUB)
    w.client.mailbox_put(cid, "ee" * 16, "AAAA")
    w.env.svc.revoke(cid)  # no hook: as if the hub died before the cleanup ran
    assert cid in w.relay.targets
    fresh = PushNotifier(w.client, w.env.svc)  # the restarted process: empty pending set
    assert fresh.pending_cleanup == set()
    fresh.restart_pass()
    assert w.relay.targets == {} and w.relay.mailbox == {} and fresh.pending_cleanup == set()
    w.relay.faults = [OSError("down")] * 8
    fresh.restart_pass()  # a dead relay: kept for the next flush
    assert fresh.pending_cleanup == {cid}


def test_notify_async_returns_at_once_and_the_worker_sends(w):
    import threading

    w.client.register()
    u = w.env.user("ann")
    _, cid = w.phone(u)
    n = PushNotifier(w.client, w.env.svc)
    n.register_target(u, cid, "sandbox", "io.severin.orch", TOKEN, PUSH_PUB)
    gate = threading.Event()
    real = w.client.send_push

    def slow(*a, **k):
        gate.wait(5)
        return real(*a, **k)

    w.client.send_push = slow
    n.notify_async(u, "prepare", "cd" * 16)  # returns while the relay "hangs"
    assert w.relay.sent == []
    gate.set()
    n.drain()
    assert len(w.relay.sent) == 1
