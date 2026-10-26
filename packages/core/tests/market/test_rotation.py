"""#1191: official index key rotation (design ``state/design-1191-rotation.md``). An official entry may move from the
remembered key to another key THIS release pins; every other class keeps strict TOFU; a retired key is refused once
a release no longer pins it."""

from __future__ import annotations

import copy
import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from orch.addons.manifest import load_manifest
from orch.crypto.encoding import b64u
from orch.market import MarketError, constants, index
from orch.market import install as mi
from tests.addons import market_ref as ref
from tests.market import helpers as H


def _key():
    k = ec.generate_private_key(ec.SECP256R1())
    return k, b64u(k.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))


CUR, NEXT, NEWNEXT, STRANGER = _key(), _key(), _key(), _key()
URL = H.OFFICIAL
SLOT = f"{URL}|standup"


def entry(key, url=URL):
    e = copy.deepcopy(H.ENTRY)
    e["source"] = {"git": "https://github.com/acme/standup", "subdir": "pkg"}
    e["publisher"] = {"key": key[1], "sig": "A" * 86}
    e["publisher"]["sig"] = ref.sign_entry(e, key[0], url)
    return e


def ctx(cls="official", keys=(CUR[1], NEXT[1]), known=CUR[1], url=URL):
    return mi.Ctx(
        url=url, cls=cls, keys=tuple(keys), core_version="2.0.0", git_hosts=frozenset(constants.DEFAULT_GIT_HOSTS),
        installed={}, known_keys={} if known is None else {f"{url}|standup": known}, seen={},
    )  # fmt: skip


def check(c, e):
    try:
        return mi.check_entry(c, {"addons": [e]}, "standup")
    except MarketError as err:
        return err.code


def test_an_official_entry_may_move_to_the_other_pinned_key_and_counts_as_a_new_key():
    facts = check(ctx(), entry(NEXT))
    assert facts.tofu == "new" and facts.publisher_key == NEXT[1]
    assert check(ctx(known=NEXT[1]), entry(NEXT)).tofu == "known"


def test_a_record_left_on_a_retired_key_does_not_block_the_next_pinned_key():
    # the release after the rotation pins (next, new-next); the host's record still holds the retired current key
    facts = check(ctx(keys=(NEXT[1], NEWNEXT[1]), known=CUR[1]), entry(NEXT))
    assert facts.tofu == "new" and facts.publisher_key == NEXT[1]


def test_rotation_to_a_key_this_release_does_not_pin_is_refused():
    assert check(ctx(), entry(STRANGER)) == "market.official_unsigned"
    assert not mi.key_change_ok("official", (CUR[1], NEXT[1]), CUR[1], STRANGER[1])


def test_downgrade_to_a_retired_key_after_a_release_pins_it_out_is_refused():
    # the host moved to next under (cur, next); the new release pins (next, new-next): the old current key is gone
    assert check(ctx(keys=(NEXT[1], NEWNEXT[1]), known=NEXT[1]), entry(CUR)) == "market.official_unsigned"
    assert check(ctx(keys=(NEXT[1], NEWNEXT[1]), known=None), entry(CUR)) == "market.official_unsigned"
    assert not mi.key_change_ok("official", (NEXT[1], NEWNEXT[1]), NEXT[1], CUR[1])
    # and the index it came with: signed by the retired key, refused before anything is read from it
    doc = H.index(9, addons=[entry(CUR)], signed=False)
    ref.sign_index(doc, CUR[0], CUR[1])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(constants, "OFFICIAL_KEYS", (NEXT[1], NEWNEXT[1]))
        with pytest.raises(MarketError) as e:
            index.evaluate(H.raw(doc), URL, official=True, now=1_000)
        assert e.value.code == "market.index_unsigned"
        mp.setattr(constants, "OFFICIAL_KEYS", (CUR[1], NEXT[1]))
        assert index.evaluate(H.raw(doc), URL, official=True, now=1_000).verified  # the same bytes, the old release


@pytest.mark.parametrize("cls", ["other", "dev"])
def test_a_non_official_publisher_key_change_is_still_refused(cls):
    other = "https://github.com/acme/market"
    keys = () if cls == "other" else (CUR[1], NEXT[1])  # dev: the sandbox's pinned keys, still TOFU
    c = ctx(cls=cls, keys=keys, known=CUR[1], url=other)
    assert check(c, entry(NEXT, url=other)) == "market.publisher_key_changed"
    assert check(c, entry(CUR, url=other)).tofu == "known"
    for k in (CUR, NEXT):
        assert not mi.key_change_ok(cls, (CUR[1], NEXT[1]), STRANGER[1], k[1])


def test_dropping_the_key_is_never_a_rotation():
    e = entry(CUR)
    del e["publisher"]
    assert check(ctx(), e) == "market.official_unsigned"
    assert not mi.key_change_ok("official", (CUR[1], NEXT[1]), CUR[1], None)
    assert not mi.key_change_ok("other", (), CUR[1], None)


def test_no_pinned_key_fails_closed():
    assert check(ctx(keys=()), entry(NEXT)) == "market.publisher_key_changed"
    assert not mi.key_change_ok("official", (), CUR[1], NEXT[1])
    assert not mi.key_change_ok("company", (CUR[1], NEXT[1]), CUR[1], NEXT[1])  # a hub pin is not a rotation
    assert not mi.key_change_ok("", (CUR[1], NEXT[1]), CUR[1], NEXT[1])


def test_the_update_diff_shows_a_rotation_as_a_first_use_and_widened():
    m = load_manifest(
        json.dumps(
            {
                "schema": "orch.addon/3", "name": "x", "version": "1.0.0", "title": "X", "min_core": "2.0.0",
                "api": ">=1.1,<2", "entry": {"cmd": ["{python}", "{pkg}/main.py"]}, "capabilities": ["network"],
            }
        ).encode()
    )  # fmt: skip
    prev = {"version": "1.0.0", "package_sha256": "sha256:" + "a" * 64, "commit": "c" * 40, "publisher_key": CUR[1]}
    got = mi.diff(m, m, prev, mi.Facts({}, "new", NEXT[1], prev))
    assert got["publisher"] == "first_use" and got["widened"] is True and mi.ROTATED in got["changes"]
    same = mi.diff(m, m, prev, mi.Facts({}, "known", CUR[1], prev))
    assert same["publisher"] == "same" and same["widened"] is False and mi.ROTATED not in same["changes"]
