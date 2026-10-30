import pytest

from orch_hub.contract import hooks as HK
from orch_hub.contract.hub import Refused
from orch_hub.hooks.store import HookStore

NOW = 1_800_000_000_000
H = "ab" * 32
SECRET = "hs_" + "S" * 43
WS_A, WS_B = "a" * 32, "b" * 32


def th(i: int) -> str:
    return f"{i:064x}"


def body(i=1, **kw):
    b = {"token_hash": th(i), "followup": "FU-1", "source": "github", "secret": SECRET, "expires_ms": NOW + 86_400_000}
    b.update(kw)
    return b


def test_put_lookup_and_secret_stays_in_store():
    s = HookStore()
    s.put(WS_A, body(), NOW)
    h = s.lookup(th(1), NOW)
    assert (h.ws, h.source, h.secret) == (WS_A, "github", SECRET)
    assert s.lookup(th(2), NOW) is None


def test_exists_for_any_workspace_and_replaces_only_own():
    s = HookStore()
    s.put(WS_A, body(1), NOW)
    with pytest.raises(Refused) as e:
        s.put(WS_B, body(1), NOW)
    assert e.value.code == "hook_exists"
    s.put(WS_B, body(2, replaces=th(1)), NOW)  # B names A's hook: not touched, no error, no leak
    assert s.lookup(th(1), NOW) is not None
    s.put(WS_A, body(3, replaces=th(1)), NOW)
    assert s.lookup(th(1), NOW) is None and s.lookup(th(3), NOW) is not None


def test_expiry_bounds_and_lazy_purge():
    s = HookStore()
    for bad in (NOW, NOW - 1, NOW + HK.MAX_EXPIRY_MS + 1):
        with pytest.raises(Refused) as e:
            s.put(WS_A, body(expires_ms=bad), NOW)
        assert e.value.code == "bad_expiry"
    s.put(WS_A, body(expires_ms=NOW + 1000), NOW)
    assert s.lookup(th(1), NOW + 999) is not None
    assert s.lookup(th(1), NOW + 1000) is None  # expired, even before the purge ran
    s.put(WS_A, body(1), NOW + 2000)  # the expired row does not block its hash
    assert s.lookup(th(1), NOW + 2000) is not None


def test_cap_per_workspace():
    s = HookStore()
    for i in range(1, HK.MAX_HOOKS_PER_WORKSPACE + 1):
        s.put(WS_A, body(i), NOW)
    with pytest.raises(Refused) as e:
        s.put(WS_A, body(999), NOW)
    assert e.value.code == "too_many_hooks"
    s.put(WS_B, body(1000), NOW)  # another workspace has its own 64


def test_first_ring_only_even_after_ack():
    s = HookStore()
    s.put(WS_A, body(), NOW)
    h = s.lookup(th(1), NOW)
    assert s.ring(h, NOW + 5) is True
    assert s.ring(h, NOW + 6) is False
    r = s.pull(WS_A, 0, NOW + 7)
    assert r["rings"] == [
        {"seq": 1, "token_hash": th(1), "source": "github", "kind": "hook.rang", "received_at": NOW + 5}
    ]
    assert s.ack(WS_A, 1) == 1
    assert s.pull(WS_A, 0, NOW + 8)["rings"] == []
    assert s.ring(h, NOW + 9) is False  # acked rows do not let the hook ring again
    assert s.pull(WS_A, 0, NOW + 10)["head"] == 1


def test_ack_and_pull_are_scoped_to_the_workspace():
    s = HookStore()
    s.put(WS_A, body(1), NOW)
    s.put(WS_B, body(2), NOW)
    s.ring(s.lookup(th(1), NOW), NOW)
    s.ring(s.lookup(th(2), NOW), NOW)
    assert [r["token_hash"] for r in s.pull(WS_A, 0, NOW)["rings"]] == [th(1)]
    s.ack(WS_B, 999)  # B acks a seq range that covers A's ring
    assert [r["token_hash"] for r in s.pull(WS_A, 0, NOW)["rings"]] == [th(1)]
    assert s.pull(WS_B, 0, NOW)["rings"] == []
    s.revoke(WS_B, th(1))  # B cannot revoke A's hook
    assert s.lookup(th(1), NOW) is not None


def test_ring_waits_offline_and_expires_with_the_hook():
    s = HookStore()
    s.put(WS_A, body(expires_ms=NOW + 1000), NOW)
    s.ring(s.lookup(th(1), NOW), NOW)
    assert len(s.pull(WS_A, 0, NOW + 500)["rings"]) == 1
    assert s.pull(WS_A, 0, NOW + 1000)["rings"] == []
    assert s.counts(WS_A) == (0, 0)


def test_revoke_drops_hook_and_pending_ring():
    s = HookStore()
    s.put(WS_A, body(), NOW)
    s.ring(s.lookup(th(1), NOW), NOW)
    s.revoke(WS_A, th(1))
    assert s.counts(WS_A) == (0, 0) and s.lookup(th(1), NOW) is None
    s.revoke(WS_A, th(1))  # again: no error
