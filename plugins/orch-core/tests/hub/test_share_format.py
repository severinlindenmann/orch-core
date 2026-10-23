"""V3 C6: the 10.2b sealed format against the contract vectors, the test-side opener and the sync's version counter."""

from __future__ import annotations

import hashlib
import json

import pytest

from orch.crypto import suite as S
from orch.crypto.encoding import unb64u
from orch.hubclient import sharefmt as F
from orch.hubclient import shares as SH
from orch.hubclient.errors import HubRefused

from . import share_open as SO
from .vec import V, private_key, pub

P4 = V["phase4"]["share_item"]
ITEM = P4["item"]
ITEM_O = ITEM["o"]
HUB, WS, ITEM_ID = ITEM_O["hub_id"], ITEM_O["workspace_id"], ITEM_O["item_id"]


def fake_seed(label: str) -> bytes:
    return hashlib.sha256(b"FAKE orch v2 test vector: " + label.encode()).digest()


def _sign_det(msg: bytes) -> bytes:
    """RFC 6979 like the vectors: the contract's signatures are reproducible."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    r, s = decode_dss_signature(private_key("wsk_a").sign(msg, ec.ECDSA(hashes.SHA256(), deterministic_signing=True)))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def test_the_production_builder_reproduces_the_vector_byte_for_byte(monkeypatch):
    signed = F.build_signed_item(
        hub_id=HUB, workspace_id=WS, item_id=ITEM_ID, item_kind=ITEM_O["item_kind"],
        item_version=ITEM_O["item_version"],
        issued_ms=ITEM_O["issued_ms"], chain=ITEM_O["chain"], content=ITEM_O["content"], dev=True,
        sign=_sign_det,
    )  # fmt: skip
    assert signed == ITEM
    assert F.item_hash(signed) == P4["item_hash"] == SO.item_hash(signed)
    eph = S.private_key_from_scalar(int.from_bytes(fake_seed("hub/share_eph/1"), "big") % (S.P256_N - 1) + 1)
    with monkeypatch.context() as m:  # the three fresh values of a put, fixed: IK, the ephemeral key, the salt
        m.setattr(F, "_new_ik", lambda: bytes.fromhex(P4["ik_hex"]))
        m.setattr(S, "generate_private_key", lambda: eph)
        m.setattr(S.secrets, "token_bytes", lambda n: fake_seed("hub/share_salt/1")[:n])
        sealed = F.seal_share_item(signed, hxk_pub=pub("hxk"))
    assert sealed == P4["body"]["sealed"]


def test_aad_matches_the_vector():
    assert SO.aad(HUB, WS, ITEM_ID, 1).hex() == P4["aad_hex"]
    from orch.crypto.labels import L

    assert (
        L["aad_hub_share"] + bytes([2]) + bytes.fromhex(HUB) + bytes.fromhex(WS) + bytes.fromhex(ITEM_ID)
        + (1).to_bytes(4, "big")
    ).hex() == P4["aad_hex"]  # fmt: skip


CONTENTS = {
    "needs_you": {"type": "gate", "title": "Approve the plan", "deadline": None},
    "ticket": {"key": "ORCH-12", "type": "bug", "title": "T", "status": "in_progress", "labels": ["a", "b"],
               "deadline": "2026-11-01"},
    "question": {"key": "ORCH-12", "type": "feature", "id": "Q3", "title": "Which?", "text": "line one\nline two",
                 "deadline": None},
}  # fmt: skip


def _build(kind, version=1, content=None, dev=True, hxk=None, **kw):
    sk = private_key("wsk_a")
    return F.build_signed_item(
        hub_id=HUB, workspace_id=WS, item_id=ITEM_ID, item_kind=kind, item_version=version, issued_ms=1,
        chain={"log": "01J9ZP0000000000000000TKT1", "seq": 2, "head": "sha256:" + "ab" * 32},
        content=content or CONTENTS[kind], dev=dev, sign=lambda m: S.sign(sk, m), **kw,
    )  # fmt: skip


def _open(sealed, **kw):
    args = {"hub_id": HUB, "workspace_id": WS, "hxk_priv": private_key("hxk"), "wsk_pub": pub("wsk_a")}
    return SO.open_share_item({"item_id": ITEM_ID, "sealed": sealed}, **{**args, **kw})


@pytest.mark.parametrize("kind", sorted(CONTENTS))
def test_a_fresh_put_round_trips_through_the_hub_check(kind):
    signed = _build(kind)
    sealed = F.seal_share_item(signed, hxk_pub=pub("hxk"))
    got = _open(sealed)
    assert got["item"] == signed["o"] and got["hash"] == F.item_hash(signed) and got["new"]
    assert F.sealed_item_version(sealed) == 1
    assert F.seal_share_item(signed, hxk_pub=pub("hxk")) != sealed  # IK, ephemeral key and salt are fresh per put
    assert len(unb64u(sealed)) <= F.MAX_SEALED


def test_the_item_cannot_be_opened_for_another_hub_workspace_item_or_key():
    sealed = F.seal_share_item(_build("ticket"), hxk_pub=pub("hxk"))
    for kw in ({"hub_id": "11" * 16}, {"workspace_id": "22" * 16}, {"hxk_priv": private_key("wxk_a")}):
        with pytest.raises(SO.Refused) as e:
            _open(sealed, **kw)
        assert e.value.code == "bad_wrap"
    with pytest.raises(SO.Refused) as e:
        SO.open_share_item({"item_id": "33" * 16, "sealed": sealed}, hub_id=HUB, workspace_id=WS,
                           hxk_priv=private_key("hxk"), wsk_pub=pub("wsk_a"))  # fmt: skip
    assert e.value.code == "bad_wrap"


def test_a_put_from_another_wsk_is_refused_by_the_hub():
    sealed = F.seal_share_item(_build("ticket"), hxk_pub=pub("hxk"))
    with pytest.raises(SO.Refused) as e:
        _open(sealed, wsk_pub=pub("wsk_b"))
    assert e.value.code == "bad_signature"


def test_dev_marker_follows_the_binding():
    sealed = F.seal_share_item(_build("ticket", dev=False), hxk_pub=pub("hxk"))
    with pytest.raises(SO.Refused) as e:
        _open(sealed, dev_mode=True)
    assert e.value.code == "dev_mismatch"


BAD_TEXT = ["a\nb", "a\rb", "a b", "a b", "a\u0085b", "a​b", "a‮b", "a‎b", "a\x1bb", " a", "a ", ""]


@pytest.mark.parametrize("bad", BAD_TEXT)
def test_the_text_rule_is_enforced_before_signing(bad):
    for kind in ("needs_you", "ticket", "question"):
        with pytest.raises(HubRefused) as e:
            _build(kind, content={**CONTENTS[kind], "title": bad})
        assert e.value.code == "malformed"
    if bad != "a\nb":  # LF is the one character multi-line text may carry
        with pytest.raises(HubRefused):
            _build("question", content={**CONTENTS["question"], "text": bad})


def test_lf_is_allowed_only_in_multiline_text_and_sizes_are_in_bytes():
    assert F.text_ok("a\nb", 10, multiline=True) and not F.text_ok("a\nb", 10)
    assert F.text_ok("é" * 100, 200) and not F.text_ok("é" * 101, 200)
    assert F.text_ok("x" * 2000, 2000, multiline=True) and not F.text_ok("x" * 2001, 2000, multiline=True)


@pytest.mark.parametrize(
    "kind,patch",
    [
        ("needs_you", {"type": "approve"}), ("needs_you", {"extra": 1}), ("needs_you", {"deadline": "2026-13-01"}),
        ("ticket", {"status": "weird"}), ("ticket", {"labels": ["b", "a"]}), ("ticket", {"labels": ["A"]}),
        ("ticket", {"key": "orch-1"}), ("ticket", {"type": {"x": 1}}), ("question", {"id": "q1"}),
        ("question", {"options": []}), ("question", {"text": "x" * 2001}),
    ],
)  # fmt: skip
def test_content_is_exact_per_kind(kind, patch):
    with pytest.raises(HubRefused) as e:
        _build(kind, content={**CONTENTS[kind], **patch})
    assert e.value.code == "malformed"


def test_missing_key_and_other_kinds_content_are_refused():
    with pytest.raises(HubRefused):
        _build("ticket", content={k: v for k, v in CONTENTS["ticket"].items() if k != "status"})
    with pytest.raises(HubRefused):
        _build("needs_you", content=CONTENTS["ticket"])
    with pytest.raises(HubRefused):
        _build("item", content=CONTENTS["ticket"])


def test_chain_and_ids_are_checked():
    with pytest.raises(HubRefused):
        F.build_signed_item(
            hub_id=HUB, workspace_id=WS, item_id=ITEM_ID, item_kind="ticket", item_version=1, issued_ms=1,
            chain={"log": "not-a-log", "seq": 1, "head": "sha256:" + "ab" * 32}, content=CONTENTS["ticket"],
            dev=True, sign=lambda m: bytes(64),
        )  # fmt: skip
    with pytest.raises(HubRefused):
        _build("ticket", version=0)


def test_a_sealed_item_over_16_kib_is_refused_before_it_is_sent(monkeypatch):
    monkeypatch.setattr(F, "MAX_SEALED", 300)
    with pytest.raises(HubRefused) as e:
        F.seal_share_item(_build("question"), hxk_pub=pub("hxk"))
    assert e.value.code == "too_large"
    assert F.MAX_SEALED == 300 and SO.MAX_SEALED == 16 * 1024
    monkeypatch.undo()
    assert F.MAX_SEALED == 16 * 1024 and F.MAX_ITEMS == 1000 == SH.MAX_ITEMS


def test_the_largest_legal_item_fits_in_16_kib():
    big = {**CONTENTS["question"], "title": "t" * 200, "text": ("é" * 999 + "\n") * 1}
    big["text"] = "é" * 1000
    sealed = F.seal_share_item(_build("question", content=big), hxk_pub=pub("hxk"))
    assert len(unb64u(sealed)) < 4096


def test_the_hub_caps_are_the_contracts():
    assert SO.MAX_ITEMS == 1000
    signed = _build("ticket")
    sealed = F.seal_share_item(signed, hxk_pub=pub("hxk"))
    with pytest.raises(SO.Refused) as e:
        _open(sealed, live_items=1000)
    assert e.value.code == "too_many_items"


# --- the builder and the version counter ---------------------------------------------------------------------------


class HubSim:
    """Keeps what the hub would keep (version rule, tombstones) and opens every put with the test-side check."""

    def __init__(self):
        self.held: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.lose_reply_on_put = False

    def call(self, op, body):
        self.calls.append((op, body))
        if op == "share_revoke":
            SO.check_share_revoke(body)
            if body["item_id"] in self.held:
                self.held[body["item_id"]]["revoked"] = True
            return {}
        got = SO.open_share_item(
            body, hub_id=HUB, workspace_id=WS, hxk_priv=private_key("hxk"), wsk_pub=pub("wsk_a"),
            held=self.held.get(body["item_id"]), live_items=sum(1 for h in self.held.values() if not h.get("revoked")),
        )  # fmt: skip
        self.held[body["item_id"]] = {"item_version": got["item"]["item_version"], "hash": got["hash"]}
        if self.lose_reply_on_put:
            self.lose_reply_on_put = False
            raise HubRefused("hub_unreachable")
        return {}


def builder(clock=lambda: 1_000):
    sk = private_key("wsk_a")
    return SH.make_builder(
        hub_id=HUB, workspace_id=WS, hxk_pub=pub("hxk"), sign=lambda m: S.sign(sk, m), dev=True, now_ms=clock,
        chain_of=lambda uid: (4, "sha256:" + "cd" * 32) if uid.startswith("01J") else None,
    )  # fmt: skip


def T(
    uid="01J9ZP0000000000000000TKT1",
    title="A title",
    labels=("public",),
    due=None,
    key="ORCH-1",
    questions=(),
    needs=(),
):
    return SH.TicketFact(uid, key, "feature", title, "open", tuple(labels), due, tuple(questions), tuple(needs))


def run(st, tickets, hub, **kw):
    return SH.sync(st, SH.select(st.rules, tickets), link=hub, build=builder(), save=lambda s: None, **kw)


def test_sync_puts_valid_items_the_hub_accepts_and_versions_only_grow():
    st, hub = SH.ShareState(rules=[{"kind": "tag", "tag": "public"}]), HubSim()
    assert run(st, [T()], hub) == {"put": 1, "revoke": 0, "kept": 0}
    (entry,) = st.sent.values()
    assert entry["version"] == 1
    run(st, [T(title="Renamed")], hub)
    assert st.sent["ticket:01J9ZP0000000000000000TKT1"]["version"] == 2
    assert [json.loads(json.dumps(c[0])) for c in hub.calls] == ["share_put", "share_put"]
    assert hub.held[entry["item_id"]]["item_version"] == 2
    assert run(st, [T(title="Renamed")], hub)["kept"] == 1 and len(hub.calls) == 2  # unchanged: nothing sent


def test_a_put_whose_reply_was_lost_is_retried_under_the_same_id_and_a_higher_version():
    st, hub = SH.ShareState(rules=[{"kind": "tag", "tag": "public"}]), HubSim()
    hub.lose_reply_on_put = True
    with pytest.raises(HubRefused):
        run(st, [T()], hub)
    (entry,) = st.sent.values()
    assert entry["digest"] is None and entry["version"] == 1 and hub.held[entry["item_id"]]["item_version"] == 1
    run(st, [T()], hub)  # the hub took the first put; the retry has other bytes, so it must carry a higher version
    assert st.sent["ticket:01J9ZP0000000000000000TKT1"]["version"] == 2
    assert len({c[1]["item_id"] for c in hub.calls}) == 1 and hub.held[entry["item_id"]]["item_version"] == 2


def test_revoke_is_idempotent_and_a_reshared_item_gets_a_fresh_id():
    st, hub = SH.ShareState(rules=[{"kind": "tag", "tag": "public"}]), HubSim()
    run(st, [T()], hub)
    first = next(iter(st.sent.values()))["item_id"]
    run(st, [T(labels=())], hub)  # stops matching: revoked
    assert run(st, [T(labels=())], hub)["revoke"] == 0  # nothing to revoke twice
    assert hub.call("share_revoke", {"item_id": first}) == {} and hub.call("share_revoke", {"item_id": "99" * 16}) == {}
    run(st, [T()], hub)
    again = next(iter(st.sent.values()))
    assert again["item_id"] != first and again["version"] == 1


def test_unshareable_items_are_not_offered_and_a_shared_one_that_turns_bad_is_revoked():
    st, hub = SH.ShareState(rules=[{"kind": "tag", "tag": "public"}]), HubSim()
    run(st, [T()], hub)
    assert st.sent
    assert run(st, [T(key="not a key")], hub)["revoke"] == 1 and not st.sent  # the contract cannot express it
    long_q = SH.QuestionFact("Q1", "x" * 2001, True)
    st2 = SH.ShareState(rules=[{"kind": "questions"}])
    assert SH.select(st2.rules, [T(questions=[long_q])]) == []  # over 2000 bytes: never truncated, never sent


def test_item_payloads_are_the_exact_contract_content():
    qs = [SH.QuestionFact("Q1", "line one\r\nline two", True)]
    needs = [
        SH.NeedFact("question", "Q1", True),
        SH.NeedFact("approve", "plan", True),
        SH.NeedFact("addon", "x.y", True),
    ]
    t = T(labels=("b", "a", "BAD LABEL", "a"), due="2026-11-01", questions=qs, needs=needs)
    items = {
        i.kind: i
        for i in SH.select([{"kind": "needs_you"}, {"kind": "questions"}, {"kind": "type", "type": "feature"}], [t])
    }
    assert items["ticket"].payload["labels"] == ["a", "b"] and items["ticket"].payload["status"] == "open"
    assert items["question"].payload["text"] == "line one\nline two"
    summaries = sorted(i.payload["type"] for i in SH.select([{"kind": "needs_you"}], [t]) if i.kind == "summary")
    assert summaries == ["gate", "question"]  # the needs the contract has no type for are not shared
    for i in items.values():
        F.check_content(SH.ITEM_KIND[i.kind], i.payload)


def test_an_item_without_a_readable_log_is_never_sent():
    st, hub = SH.ShareState(rules=[{"kind": "tag", "tag": "public"}]), HubSim()
    got = run(st, [T(uid="XNOLOG")], hub)  # skipped and reported, not sent (one item never stops the sync)
    assert got["refused"] == {"ticket:XNOLOG": "malformed"} and hub.calls == [] and st.sent == {}


def test_the_state_file_keeps_the_version_and_reads_an_older_one_as_zero():
    st = SH.ShareState()
    st.sent["ticket:a"] = {"item_id": "11" * 16, "digest": "d", "version": 7}
    assert SH.ShareState.from_json(st.to_json()).sent["ticket:a"]["version"] == 7
    old = {**st.to_json(), "sent": {"ticket:a": {"item_id": "11" * 16, "digest": "d"}}}
    assert SH.ShareState.from_json(old).sent["ticket:a"]["version"] == 0
    for bad in (-1, 2**32, "3", True):
        d = {**st.to_json(), "sent": {"ticket:a": {"item_id": "11" * 16, "digest": "d", "version": bad}}}
        with pytest.raises(HubRefused):
            SH.ShareState.from_json(d)


def test_one_workspace_may_share_1000_items_and_not_more():
    items = [T(uid=f"01J9ZP00000000000000{n:06d}"[:26], key=f"K-{n}") for n in range(1001)]
    st = SH.ShareState(rules=[{"kind": "tag", "tag": "public"}])
    with pytest.raises(HubRefused) as e:
        run(st, items, HubSim())
    assert e.value.code == "too_many_items"


def test_the_item_hash_the_hub_computes_is_kept_per_shared_item_and_survives_the_state_file():
    """C6-5: the return check of 10.2b needs each live item's hash; it is the hub's own value of that put."""
    st, hub = SH.ShareState(rules=[{"kind": "tag", "tag": "public"}]), HubSim()
    run(st, [T()], hub)
    (have,) = st.sent.values()
    assert have["item_hash"] == hub.held[have["item_id"]]["hash"]
    assert SH.ShareState.from_json(st.to_json()).sent == st.sent
    run(st, [T(title="Changed")], hub)  # a new version is a new hash, kept in place of the old one
    assert st.sent["ticket:01J9ZP0000000000000000TKT1"]["item_hash"] == hub.held[have["item_id"]]["hash"]
    old = {"rules": [], "sent": {"x": {"item_id": "11" * 16, "digest": "d"}}, "withdrawn": []}
    assert SH.ShareState.from_json(old).sent["x"]["item_hash"] is None  # a state file from before: unknown, not guessed
    bad = {**old, "sent": {"x": {"item_id": "11" * 16, "digest": "d", "item_hash": "nope"}}}
    with pytest.raises(HubRefused):
        SH.ShareState.from_json(bad)
