"""Core's snapshot verifier (``orch.hubclient.snapshot``) against the hub vectors and the C1 fake hub (hub contract
section 7): every snapshot/card/op_revoked vector gives its ``expect`` code; the pin is monotonic and survives a
restart; a replayed older snapshot, a conflicting one and one under a revoked op cert are refused."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from orch.crypto.encoding import b64u, unb64u
from orch.hubclient import HubLink, HubRefused, HubState, wire
from orch.hubclient import snapshot as snap

from . import fake as F
from .test_join import NOW, WS, Host, sign_wsk
from .vec import V

CASES = {k: [c for c in V["cases"] if c["check"] == k] for k in ("snapshot", "card", "op_revoked")}


def _held(a: dict[str, Any]) -> dict[str, Any]:
    pinned = unb64u(a["pinned_root"], 65) if a.get("pinned_root") else None
    rev = frozenset(a.get("revoked_op_certs", []))
    kw = {"now_ms": a["now_ms"], "dev_mode": a.get("dev_mode", True), "pinned_root": pinned, "revoked_op_certs": rev}
    prior = wire.verify_card(a["held_card"], **kw) if a.get("held_card") else None
    return wire.verify_card(a["card"], **kw, held=prior)


def _outcome(case: dict[str, Any]) -> dict[str, Any]:
    a = case["args"]
    try:
        if case["check"] == "snapshot":
            later = frozenset(a.get("revoked_op_certs", [])) | frozenset(a.get("revoked_later", []))
            o = snap.verify_snapshot(
                a["signed"], _held(a), workspace_id=a["workspace_id"], dev_mode=a.get("dev_mode", True),
                pinned=a.get("pinned"), revoked_op_certs=later,
            )  # fmt: skip
            return {"ok": {"version": o["version"]}}
        if case["check"] == "card":
            h = _held(a)
            return {"ok": {"hub_id": h["card"]["hub_id"], "op_cert_id": h["op_cert"]["cert_id"]}}
        o = wire.verify_op_revoked(a["signed"], unb64u(a["hk_root_pub"], 65))
        return {"ok": {"cert_id": o["cert_id"]}}
    except HubRefused as exc:
        return {"error": exc.code}


@pytest.mark.parametrize("case", CASES["snapshot"], ids=[c["name"] for c in CASES["snapshot"]])
def test_snapshot_vector(case):
    assert _outcome(case) == case["expect"]


@pytest.mark.parametrize(
    "case", CASES["card"] + CASES["op_revoked"], ids=[c["name"] for c in CASES["card"] + CASES["op_revoked"]]
)
def test_chain_vector(case):
    if case["check"] == "card" and case["args"].get("text_hex"):
        pytest.skip("card_text is the strict-parse vector")
    assert _outcome(case) == case["expect"]


def test_vectors_have_positive_and_negative_snapshots():
    ex = [c["expect"] for c in CASES["snapshot"]]
    assert any("ok" in e for e in ex) and any("error" in e for e in ex)


# --- against the C1 fake hub ---------------------------------------------------------------------------------------

ADMIN, READER = "11" * 16, "22" * 16


class Rig:
    """A host joined to the fake hub (join_selective), a link on it, and the state saved as JSON."""

    def __init__(self, hub: F.FakeHub | None = None) -> None:
        self.hub = hub or F.FakeHub()
        self.host = Host(self.hub)
        self.host.join()
        self.link = HubLink(
            self.host.wire, hub_id=self.hub.hub_id, workspace_id=WS, hk_op_pub=self.hub.hk_op_pub,
            sign=sign_wsk, mode="selective", now_ms=lambda: NOW,
        )  # fmt: skip

    @property
    def state(self) -> HubState:
        return self.host.state

    def refresh(self, **kw: Any) -> bool:
        return snap.refresh(
            state=self.state, link=self.link, workspace_id=WS, dev_mode=True, now_ms=lambda: NOW,
            save=self.host._save, **kw,
        )  # fmt: skip

    def restart(self) -> None:
        """Reload the state from its JSON, as a host restart would."""
        self.host.state = HubState.from_json(json.loads(json.dumps(self.state.to_json())))

    def role(self, uid: str) -> str | None:
        return snap.hub_role(self.state, uid, now_ms=NOW, dev_mode=True)


def test_join_keeps_the_verified_card():
    r = Rig()
    assert r.state.card == V["objects"]["card"] and r.state.snapshot is None
    assert snap.pinned_view(r.state) is None and r.role(ADMIN) is None


def test_refresh_pins_the_snapshot_and_reads_roles():
    r = Rig()
    r.hub.set_snapshot(WS, {ADMIN: "admin", READER: "read"})
    assert r.refresh() is True
    v = snap.pinned_view(r.state)
    assert v == {"version": 2, "roles": {ADMIN: "admin", READER: "read"}}
    assert [r.role(ADMIN), r.role(READER), r.role("33" * 16), r.role("zz")] == ["admin", "read", None, None]
    assert r.refresh() is False  # the same version again is a no-op


def test_the_join_time_snapshot_is_version_1_without_roles():
    r = Rig()
    assert r.refresh() is True
    assert snap.pinned_view(r.state) == {"version": 1, "roles": {}}


def test_pin_is_monotonic_across_restart():
    r = Rig()
    r.hub.set_snapshot(WS, {ADMIN: "admin"})
    r.refresh()
    r.restart()
    assert snap.pinned_view(r.state)["version"] == 2 and r.role(ADMIN) == "admin"
    r.hub.set_snapshot(WS, {ADMIN: "admin", READER: "edit"})
    assert r.refresh() is True
    r.restart()
    assert snap.pinned_view(r.state)["version"] == 3 and r.role(READER) == "edit"


def test_replayed_older_snapshot_is_refused_and_pin_stays():
    r = Rig()
    old = r.hub.set_snapshot(WS, {ADMIN: "admin", READER: "read"})
    r.refresh()
    r.hub.set_snapshot(WS, {ADMIN: "admin"})  # the reader was removed
    r.refresh()
    assert r.role(READER) is None
    r.hub.workspaces[WS].snapshot = old  # the hub (or a man in the middle) replays the old one
    with pytest.raises(HubRefused) as e:
        r.refresh()
    assert e.value.code == "stale_snapshot"
    assert snap.pinned_view(r.state)["version"] == 3 and r.role(READER) is None


def test_same_version_other_bytes_is_a_conflict():
    r = Rig()
    r.hub.set_snapshot(WS, {ADMIN: "admin"})
    r.refresh()
    ws = r.hub.workspaces[WS]
    ws.snapshot = r.hub._snapshot(WS, 2, {ADMIN: "admin", READER: "admin"}, [])  # noqa: SLF001
    with pytest.raises(HubRefused) as e:
        r.refresh()
    assert e.value.code == "snapshot_conflict"
    assert r.role(READER) is None


def test_snapshot_for_another_workspace_or_other_key_is_refused():
    r = Rig()
    ws = r.hub.workspaces[WS]
    ws.snapshot = r.hub._snapshot("bb" * 16, 5, {}, [])  # noqa: SLF001
    with pytest.raises(HubRefused) as e:
        r.refresh()
    assert e.value.code == "other_workspace"
    forged = F.sign_with(F.private_key("hk_op_rogue") if "hk_op_rogue" in V["keys"] else F.private_key("hk_root"),
                         "sig_hub_snapshot", r.hub._snapshot(WS, 9, {ADMIN: "admin"}, [])["o"])  # fmt: skip
    ws.snapshot = forged
    with pytest.raises(HubRefused) as e:
        r.refresh()
    assert e.value.code == "bad_signature"
    assert r.state.snapshot is None


def test_revoked_op_cert_refuses_new_snapshots_and_ends_roles():
    r = Rig()
    r.hub.set_snapshot(WS, {ADMIN: "admin"})
    r.refresh()
    assert r.role(ADMIN) == "admin"
    cert_id = r.state.card["op_cert"]["o"]["cert_id"]
    assert snap.record_revoked_op(r.state, V["objects"]["op_revoked"]) is True
    assert cert_id in r.state.revoked_op_certs
    assert snap.record_revoked_op(r.state, V["objects"]["op_revoked"]) is False
    assert r.role(ADMIN) is None  # the pinned copy no longer counts
    r.hub.set_snapshot(WS, {ADMIN: "admin"})
    with pytest.raises(HubRefused) as e:
        r.refresh()
    assert e.value.code == "revoked"
    r.restart()
    assert cert_id in r.state.revoked_op_certs and r.role(ADMIN) is None


def test_revocation_must_be_root_signed():
    r = Rig()
    bad = json.loads(json.dumps(V["objects"]["op_revoked"]))
    bad["o"]["revoked_ms"] += 1
    with pytest.raises(HubRefused) as e:
        snap.record_revoked_op(r.state, bad)
    assert e.value.code == "bad_signature" and r.state.revoked_op_certs == []


def test_an_expired_op_cert_gives_no_role():
    r = Rig()
    r.hub.set_snapshot(WS, {ADMIN: "admin"})
    r.refresh()
    late = r.state.card["op_cert"]["o"]["expires_ms"] + 1
    assert snap.hub_role(r.state, ADMIN, now_ms=late, dev_mode=True) is None
    assert snap.hub_role(r.state, ADMIN, now_ms=NOW, dev_mode=False) is None  # a real workspace refuses a dev hub


def test_tampered_state_gives_no_role():
    r = Rig()
    r.hub.set_snapshot(WS, {ADMIN: "admin"})
    r.refresh()
    r.state.snapshot["o"]["roles"][READER] = "admin"
    assert r.role(READER) is None and r.role(ADMIN) is None


def test_state_without_card_cannot_refresh():
    r = Rig()
    r.state.card = None
    with pytest.raises(HubRefused) as e:
        r.refresh()
    assert e.value.code == "no_card"


def test_unreachable_hub_keeps_the_pin():
    r = Rig()
    r.hub.set_snapshot(WS, {ADMIN: "admin"})
    r.refresh()
    r.host.wire.down = True
    with pytest.raises(HubRefused) as e:
        r.refresh()
    assert e.value.code == "hub_unreachable" and r.role(ADMIN) == "admin"


def test_state_json_roundtrip_is_strict():
    r = Rig()
    r.hub.set_snapshot(WS, {ADMIN: "admin"})
    r.refresh()
    d = r.state.to_json()
    assert HubState.from_json(json.loads(json.dumps(d))).to_json() == d
    for bad in ({**d, "snapshot": "x"}, {**d, "card": []}, {**d, "revoked_op_certs": ["nothex"]}):
        with pytest.raises(HubRefused):
            HubState.from_json(bad)


# --- op-cert rotation ----------------------------------------------------------------------------------------------


def pin(n: int) -> str:
    return b64u(bytes([n]) * 32)


def rotated_card(*, card_seq: int = 1, spki: str | None = None) -> dict[str, Any]:
    """The vector hub's card after a rotation: op_seq 2, HK_op_next, signed by the vector HK_root / HK_op_next."""
    old = V["objects"]["card"]
    op = {
        **old["op_cert"]["o"],
        "cert_id": "ee" * 16,
        "op_seq": 2,
        "hk_op_pub": V["keys"]["hk_op_next"]["pub"],
    }
    o = {**old["o"], "card_seq": card_seq, "tls_spki_sha256": spki or pin(1)}
    return {
        "o": o,
        "op_cert": F.sign_with(F.private_key("hk_root"), "sig_hub_op_cert", op),
        "sig": F.sign_with(F.private_key("hk_op_next"), "sig_hub_card", o)["sig"],
    }


def rotate(r: Rig, card: dict[str, Any]) -> None:
    r.hub.card = card
    r.hub._hk_op = F.private_key("hk_op_next")  # noqa: SLF001
    r.hub.hk_op_pub = F.pub("hk_op_next")


def test_rotation_with_the_old_op_cert_revoked_adopts_the_new_card_and_replies_verify_under_the_new_key():
    r = Rig()
    r.hub.set_snapshot(WS, {ADMIN: "admin"})
    r.refresh()
    assert snap.record_revoked_op(r.state, V["objects"]["op_revoked"]) is True  # the old op cert is revoked
    new = rotated_card(spki=pin(2))
    rotate(r, new)
    r.hub.set_snapshot(WS, {ADMIN: "admin", READER: "read"})
    assert r.refresh(hub_card=new) is True  # a link still holding the OLD op key must be re-keyed by the refresh
    assert r.state.card == new and r.state.tls_spki == pin(2)
    assert r.state.hk_op_pub == V["keys"]["hk_op_next"]["pub"]
    assert snap.pinned_view(r.state)["version"] == 3
    assert [r.role(ADMIN), r.role(READER)] == ["admin", "read"]
    r.restart()
    assert r.role(READER) == "read"


def test_without_adopting_the_rotated_card_replies_under_the_new_key_are_refused():
    r = Rig()
    rotate(r, rotated_card())
    with pytest.raises(HubRefused):
        r.refresh()


def test_a_revoked_new_card_and_a_stale_one_are_refused():
    r = Rig()
    new = rotated_card()
    snap.record_revoked_op(r.state, {**V["objects"]["op_revoked"]})  # old cert revoked: fine for the new card
    r.state.revoked_op_certs.append("ee" * 16)  # but the new cert itself is revoked
    with pytest.raises(HubRefused) as e:
        snap.adopt_card(r.state, new, now_ms=NOW, dev_mode=True)
    assert e.value.code == "revoked"
    r2 = Rig()
    snap.adopt_card(r2.state, new, now_ms=NOW, dev_mode=True)
    with pytest.raises(HubRefused) as e:
        snap.adopt_card(r2.state, V["objects"]["card"], now_ms=NOW, dev_mode=True)  # op_seq 1 after 2
    assert e.value.code == "stale_op_cert"


# --- the hub marketplace policy (D14, orch-core#927) ----------------------------------------------------------------

COMPANY_POLICY = {
    "markets": [
        {"url": "https://git.acme.example/tools/orch-market", "class": "company", "keys": [V["keys"]["hk_op"]["pub"]]},
        {"url": "https://github.com/severinlindenmann/orch-marketplace", "class": "official", "keys": []},
    ],
    "addons": {"allow": ["wiki"], "block": ["wiki@1.0.0"]},
    "ceiling": ["pty"],
}
COMPANY_POLICY["markets"].sort(key=lambda m: m["url"])


def _policy(r: Rig) -> Any:
    return snap.hub_market_policy(r.state, now_ms=NOW, dev_mode=True)


def test_the_pinned_policy_is_read_only_and_fails_closed():
    r = Rig()
    assert _policy(r) is None  # no snapshot pinned yet
    r.hub.set_snapshot(WS, {ADMIN: "admin"}, market_policy=COMPANY_POLICY)
    r.refresh()
    version, pol = _policy(r)
    assert version == 2 and pol.allow == ("wiki",) and pol.block == ("wiki@1.0.0",) and pol.ceiling == ("pty",)
    assert [m.cls for m in pol.markets] == ["company", "official"] and pol.market(pol.markets[0].url).keys
    with pytest.raises(dataclasses.FrozenInstanceError):
        pol.allow = ()  # type: ignore[misc]
    assert snap.hub_market_policy(None, now_ms=NOW, dev_mode=True) is None
    assert snap.hub_market_policy(r.state, now_ms=NOW, dev_mode=False) is None  # dev marker mismatch
    r.state.status = "unbound"
    assert _policy(r) is None


def test_a_snapshot_without_or_with_a_bad_policy_is_refused_and_the_pin_stays():
    r = Rig()
    r.refresh()
    for bad in (None, {}, {"allow": [], "block": [], "ceiling": []}, {**COMPANY_POLICY, "extra": 1}):
        r.hub.set_snapshot(WS, {ADMIN: "admin"}, market_policy=bad if bad is not None else {"markets": None})
        with pytest.raises(HubRefused) as e:
            r.refresh()
        assert e.value.code == "malformed" and snap.pinned_view(r.state)["version"] == 1
