"""HUB-SV-02 piece a: the signed action's context, digest and the single-use nonce."""

import os

import pytest

from orch_hub.services import helper_proto as P

U, C = "a" * 32, "b" * 32


def _ctx(text="Restart Relay on vps1.", **kw):
    base = {
        "hub_id": "h" * 8,
        "host_id": "vps1",
        "part": "relay",
        "action": "restart",
        "target_version": None,
        "nonce": "c" * 32,
        "iat": 1000,
        "channel_id": C,
        "user_id": U,
        "text": text,
    }
    return P.context(**{**base, **kw})


def test_digest_binds_every_field():
    base = P.digest(_ctx())
    for k, v in (
        ("hub_id", "x" * 8),
        ("host_id", "vps2"),
        ("part", "publish"),
        ("action", "stop"),
        ("target_version", "1.2.3"),
        ("nonce", "d" * 32),
        ("iat", 1001),
        ("channel_id", "e" * 32),
        ("user_id", "f" * 32),
        ("text", "Restart Relay on vps1. "),
    ):
        assert P.digest(_ctx(**{k: v})) != base, k
    assert P.digest(_ctx()) == base


def test_text_sentences_and_hub_warning():
    t = P.text_for
    assert t("relay", "Relay", "stop", "vps1", None) == "Phone alerts pause until you start it again."
    assert t("relay", "Relay", "start", "vps1", None) == "Phone alerts work again."
    assert t("relay", "Relay", "restart", "vps1", None) == "Restart Relay. Unavailable for a few seconds."
    assert "outside the company until you start it again" in t("publish", "Publish", "stop", "vps1", None)
    assert "console goes offline" in t("hub", "Hub", "restart", "vps1", None)
    assert "console goes offline" in t("hub", "Hub", "stop", "vps1", None)
    assert "1.2.3" in t("publish", "Publish", "update", "vps1", "1.2.3")
    assert t("other", "Other", "start", "vps1", None) == "Start Other."  # an unknown part still names itself


def test_sentence_follows_the_part_key_not_the_label():
    # a hand-edited label must not change which effect is shown for the bound part
    assert P.text_for("relay", "Publish", "stop", "vps1", None) == "Phone alerts pause until you start it again."
    assert "console goes offline" not in P.text_for("relay", "Hub", "stop", "vps1", None)
    assert "console goes offline" in P.text_for("hub", "Relay", "stop", "vps1", None)


def test_text_never_shows_the_host_name():
    for label in ("Relay", "Publish", "Hub", "Other"):
        for action in ("start", "stop", "restart", "update"):
            assert "vps1" not in P.text_for(label.lower(), label, action, "vps1", "1.2.3"), (label, action)


@pytest.fixture
def store(tmp_path):
    os.chmod(tmp_path, 0o700)
    t = [10_000]
    s = P.NonceStore(tmp_path / "n.db", now_ms=lambda: t[0])
    s.t = t
    yield s
    s.close()


def test_nonce_is_single_use_and_replay_is_unknown(store):
    ctx = _ctx()
    store.put(ctx["nonce"], U, ctx, "t")
    got = store.take(ctx["nonce"])
    assert got is not None and got[0] == ctx and got[1] == "t" and got[2] == 10_000 + P.TTL_MS
    assert store.take(ctx["nonce"]) is None  # replay


def test_unknown_and_malformed_nonce(store):
    assert store.take("0" * 32) is None
    assert store.take("../x") is None and store.take(None) is None


def test_expired_rows_are_reported_expired_and_still_consumed(store):
    ctx = _ctx()
    exp = store.put(ctx["nonce"], U, ctx, "t")
    store.t[0] = exp + 1
    got = store.take(ctx["nonce"])
    assert got is not None and got[2] <= store.t[0]
    assert store.take(ctx["nonce"]) is None


def test_a_new_prepare_drops_the_same_persons_older_one_and_cap(store):
    a, b = _ctx(nonce="1" * 32), _ctx(nonce="2" * 32)
    store.put(a["nonce"], U, a, "t")
    store.put(b["nonce"], U, b, "t")
    assert store.take(a["nonce"]) is None and store.take(b["nonce"]) is not None
    for i in range(P.MAX_PENDING):
        n = f"{i:032x}"
        store.put(n, f"{i:032x}", _ctx(nonce=n), "t")
    with pytest.raises(P.HelperError) as e:
        store.put("f" * 32, "9" * 32, _ctx(nonce="f" * 32), "t")
    assert e.value.code == "busy"
