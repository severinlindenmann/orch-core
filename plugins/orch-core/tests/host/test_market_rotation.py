"""#1191 rotation on a served host: the adversarial cases of the independent Opus review (state/review-1224.md, tests
A to D) plus the submit recheck of the staged publisher key (C1)."""

# ruff: noqa: F811
from __future__ import annotations

from orch.market import constants
from tests.addons import market_ref as ref
from tests.host import human_helpers
from tests.host import market_helpers as M  # noqa: E402
from tests.host.market_helpers import add_market, err, mfiles, ok, person, stage  # noqa: F401
from tests.host.market_helpers import install as do_install
from tests.market import helpers as H

net = M.net
lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


def _new_key():
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from orch.crypto.encoding import b64u

    k = ec.generate_private_key(ec.SECP256R1())
    return k, b64u(k.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))


def _sign_index_with(net, key):
    def doc(**more):
        d = H.index(net.version, addons=net.entries, url=net.url, signed=False, **more)
        ref.sign_index(d, key[0], key[1])
        return d

    net.doc = doc


def test_A_submit_refuses_a_staged_key_that_is_no_longer_pinned(served, human, net, monkeypatch):
    """C1: a fresh install staged with an entry by X under (KEY, X); before submit the release pins (KEY,) only. The
    index (by KEY) still lists the digest, but the staged key is re-checked against the pins of now: refused, and no
    TOFU record is left on a key this release does not pin."""
    x = _new_key()
    monkeypatch.setattr(constants, "OFFICIAL_KEYS", (H.KEY_B64, x[1]))
    net.add("standup", "1.0.0", key=x)
    add_market(served, net)
    st = ok(stage(served))["data"]
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    signed = human.sign(prepared)
    monkeypatch.setattr(constants, "OFFICIAL_KEYS", (H.KEY_B64,))  # X retired between stage and submit
    assert err(human.submit_res(prepared["request"]["id"], signed.events)) == "market.official_unsigned"
    assert mfiles(served).installs()["keys"] == {}


def test_submit_refuses_when_the_index_now_names_another_publisher(served, human, net, monkeypatch):
    """C1: the staged bytes are still listed, but the current index's entry is now published by another (pinned) key:
    what was reviewed is not what the index says now."""
    x = _new_key()
    monkeypatch.setattr(constants, "OFFICIAL_KEYS", (H.KEY_B64, x[1]))
    net.add("standup", "1.0.0")
    add_market(served, net)
    st = ok(stage(served))["data"]
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    signed = human.sign(prepared)
    e = dict(net.entries[0])  # the same id, version, commit and digest, re-keyed to X
    e["publisher"] = {"key": x[1], "sig": "A" * 86}
    e["publisher"]["sig"] = ref.sign_entry(e, x[0], net.url)
    net.entries = [e]
    net.version += 1
    ok(person(served, "market.refresh"))
    assert err(human.submit_res(prepared["request"]["id"], signed.events)) == "market.staged_changed"
    assert mfiles(served).installs()["keys"] == {}


def test_B_up_to_date_host_across_rekey_then_new_pins(served, human, net, monkeypatch):
    """The deviation's motivating case end to end: record stays on CUR through the rekey (up_to_date), then the release
    pins (NEXT, NEWNEXT): the next update must still go through, and the record moves to NEXT."""
    nxt, newnext = _new_key(), _new_key()
    net.add("standup", "1.0.0")
    add_market(served, net)
    do_install(served, human)
    monkeypatch.setattr(constants, "OFFICIAL_KEYS", (H.KEY_B64, nxt[1]))
    # rekey: same entry content re-signed by NEXT, index by NEXT, version + 1
    e = dict(net.entries[0])
    e["publisher"] = {"key": nxt[1], "sig": "A" * 86}
    e["publisher"]["sig"] = ref.sign_entry(e, nxt[0], net.url)
    net.entries = [e]
    _sign_index_with(net, nxt)
    net.version += 1
    ok(person(served, "market.refresh"))
    assert ok(stage(served, update=True))["data"]["up_to_date"] is True
    assert mfiles(served).installs()["keys"] == {f"{H.OFFICIAL}|standup": H.KEY_B64}  # never moved
    # release (NEXT, NEWNEXT)
    monkeypatch.setattr(constants, "OFFICIAL_KEYS", (nxt[1], newnext[1]))
    net.key = nxt[0]
    net.add("standup", "1.1.0", key=nxt)
    net.version += 1
    ok(person(served, "market.refresh"))
    st = ok(stage(served, update=True))["data"]
    assert st["update"]["publisher"] == "first_use"  # install record still says CUR
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    ok(human.submit_res(prepared["request"]["id"], human.sign(prepared).events))
    assert mfiles(served).installs()["keys"] == {f"{H.OFFICIAL}|standup": nxt[1]}


def test_C_replayed_old_index_signed_by_retired_key_is_refused_on_new_release(served, human, net, monkeypatch):
    nxt, newnext = _new_key(), _new_key()
    net.add("standup", "1.0.0")
    add_market(served, net)
    monkeypatch.setattr(constants, "OFFICIAL_KEYS", (nxt[1], newnext[1]))
    net.version += 5  # a newer version number, still signed by the retired key
    markets = ok(person(served, "market.refresh"))["data"]["markets"]
    assert markets == [{"url": H.OFFICIAL, "error": "market.index_unsigned"}]


def test_D_new_index_with_entry_publisher_swapped_back_to_retired_key(served, human, net, monkeypatch):
    """The index signature does not cover entry publishers. A new-release host gets a NEXT-signed index whose entry
    publisher block is the old CUR one (same id/version/digest): must be official_unsigned at stage."""
    nxt, newnext = _new_key(), _new_key()
    net.add("standup", "1.0.0")  # entry by CUR (H.KEY)
    monkeypatch.setattr(constants, "OFFICIAL_KEYS", (nxt[1], newnext[1]))
    _sign_index_with(net, nxt)
    add_market(served, net)
    assert err(stage(served)) == "market.official_unsigned"
