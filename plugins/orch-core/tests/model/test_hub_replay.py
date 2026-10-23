"""C3b task 3 (ticket-format §15.1, §15.3, §15.4): the engine's line checks and ``authorize_hub`` order, ``role_of``
and ``signer``, with the injected fake verifier. The vector cases (real signatures): tests/hub/test_c3b_vectors."""

from __future__ import annotations

import pytest

from orch.model import authz, hubledger, identity
from orch.model.codes import Code
from orch.model.state import admit
from orch.model.testing import FakeVerifier
from orch.model.types import Core, Member, WsCore
from tests.model.test_hubledger import HUB, ROOT, U1, U2, card, join, pin, snap
from tests.model.world import World, sig

CHAN = "a" * 32


def hub_actor(user: str = U1, via: str = CHAN) -> dict:
    return {"kind": "person", "id": "u_" + user, "via": via}


def hub_fields(**kw) -> dict:
    f = {
        "auth": "hub", "channel": CHAN, "channel_factor": "se-unlocked", "hub_id": HUB, "snapshot_version": 1,
        "channel_cert": {"o": {}, "sig": sig("cc")}, "uk_cert": {"o": {}, "sig": sig("uc")}, "channel_proof": sig("cp"),
    }  # fmt: skip
    return {**f, **kw}


@pytest.fixture
def w() -> World:
    w = World(validate=False).bootstrap({"mara": "maintainer"})
    join(w, aliases=[{"person_id": w.people["mara"][2:], "user_id": U2}])
    pin(w, card())
    pin(w, snap(1))
    return w


EID = "01J9ZP0000000000000000TST1"


def try_hub(w: World, actor: dict | None = None, eid: str | None = None, **kw):
    extra = {"id": eid} if eid else {}
    f = hub_fields(**kw)
    return w.try_(
        "workspace", "addon.disabled", actor or hub_actor(), roster_v=w.roster_v, name="estimate", **f, **extra
    )


def test_a_valid_hub_line_passes_authorization_and_the_type_rules(w):
    _, r = try_hub(w)
    assert r.code == Code.ADDON_UNKNOWN, r  # past authorization and the signer rules, at the addon rule (PR B task 4)
    assert w.verifier.actor_calls[-1]["mode"] == "full" and w.verifier.actor_calls[-1]["dev"] is False


@pytest.mark.parametrize(
    "kw,detail",
    [
        ({"channel": "b" * 32}, "malformed"),  # via != channel
        ({"channel_factor": "sms"}, "malformed"),
        ({"item_hash": "sha256:" + "a" * 64}, "malformed"),  # full mode refuses an item_hash
        ({"hub_id": "0" * 32}, "not_joined"),
    ],
)
def test_step_1_shape_is_hub_invalid(w, kw, detail):
    _, r = try_hub(w, **kw)
    assert r.code == Code.HUB_INVALID and r.detail == detail
    assert not w.verifier.actor_calls  # nothing is verified before the shape holds


@pytest.mark.parametrize(
    "contract,code",
    [
        ("revoked", Code.HUB_CERT_INVALID),
        ("bad_signature", Code.HUB_CERT_INVALID),
        ("no_card", Code.HUB_CERT_INVALID),
        ("unknown_snapshot", Code.HUB_UNKNOWN_SNAPSHOT),
        ("channel_revoked", Code.HUB_DENIED),
        ("not_custodial", Code.HUB_DENIED),
        ("factor_too_weak", Code.HUB_DENIED),
        ("malformed", Code.HUB_INVALID),
    ],
)
def test_the_contract_code_maps_to_the_model_code_with_the_code_as_detail(w, contract, code):
    w.verifier.actor_codes[EID] = contract
    _, r = try_hub(w, eid=EID)
    assert (r.code, r.detail) == (code, contract)


def test_foreign_user_is_checked_after_the_chain_and_before_the_role():
    other = "9" * 32
    ws = WsCore(created=True)
    ws.hub = {
        "status": "joined", "hub_id": HUB, "mode": "full", "alias_book": {"c" * 32: {"d" * 32: other}},
        "user_hub": {}, "ledger": {HUB: hubledger.new_ledger(ROOT)},
    }  # fmt: skip
    assert identity.hub_actor(ws, HUB, other).code == Code.HUB_FOREIGN_USER
    ws.hub["ledger"][HUB]["card"] = card()
    ws.hub["ledger"][HUB]["snapshot"] = snap(1)
    e = {"type": "addon.disabled", "actor": hub_actor(other), "at": "2026-10-09T09:10:11Z", "roster_v": 0,
         "id": "x", **hub_fields()}  # fmt: skip
    bad = FakeVerifier(actor_codes={"x": "channel_revoked", "y": "bad_signature"})
    assert authz.authorize_hub(Core(ws=ws), "workspace", e, bad).code == Code.HUB_FOREIGN_USER  # beats step 5
    assert authz.authorize_hub(Core(ws=ws), "workspace", {**e, "id": "y"}, bad).code == Code.HUB_CERT_INVALID


def test_the_via_shape_appears_exactly_when_auth_is_hub_chain_broken(w):
    # a device actor labelled hub, and a via actor labelled otherwise: both break the chain (checked on replay)
    dev = {"kind": "person", "id": w.people["mara"], "device": w.dev["mara"]}
    for actor, auth in ((dev, "hub"), (hub_actor(), "passphrase")):
        e = w.build_unappended("workspace", "addon.disabled", actor, name="estimate", **hub_fields(auth=auth))
        res = admit(w.state(), e, log="workspace")
        assert res.code == Code.CHAIN_BROKEN


def test_a_bad_hub_signature_breaks_the_chain_on_replay(w):
    e = w.wev("addon.disabled", hub_actor(), roster_v=w.roster_v, name="estimate", **hub_fields())
    w.verifier.bad_custodial_sigs.add(e["id"])
    st = w.state()
    assert any(c.code == Code.CHAIN_BROKEN.value and c.log == "workspace" for c in st.chain_errors)


def test_the_default_verifier_refuses_a_hub_line():
    class Bare(FakeVerifier):
        verify_hub_actor = None  # type: ignore[assignment]

    ws = WsCore(created=True, workspace_id="w")
    ws.hub = {"status": "joined", "hub_id": HUB, "mode": "full", "alias_book": {}, "ledger": {HUB: {"root": ROOT}}}
    e = {"type": "addon.disabled", "actor": hub_actor(), "at": "2026-10-09T09:10:11Z", "roster_v": 0, **hub_fields()}
    r = authz.authorize_hub(Core(ws=ws), "workspace", e, Bare())
    assert r.code == Code.HUB_INVALID and r.detail == "unavailable"


# --- role_of / signer (§15.4) ---------------------------------------------------------------------------------------
def ws_with(mode: str, aliases: dict, members: dict, roles: dict, joined: bool = True) -> WsCore:
    ws = WsCore(created=True)
    ws.members = {p: Member(p, p[2:6], r, "k") for p, r in members.items()}
    ws.hub = {
        "status": "joined" if joined else "left", "hub_id": HUB, "mode": mode, "alias_book": {HUB: aliases},
        "ledger": {HUB: {"snapshot": {"o": {"roles": roles}}}},
    }  # fmt: skip
    return ws


P = "p_" + "4" * 32


@pytest.mark.parametrize(
    "mode,aliased,member,hub_role,want",
    [
        ("full", False, None, "admin", "owner"),
        ("full", False, None, "edit", "member"),
        ("full", False, None, "read", "viewer"),
        ("full", False, None, None, None),
        ("selective", False, None, "admin", None),  # not granted
        ("full", True, "member", "admin", "member"),  # the lower of the two (C3b-1)
        ("full", True, "owner", "edit", "member"),
        ("full", True, "maintainer", "read", "viewer"),
        ("full", True, None, "admin", None),  # removed from the roster: never the hub role
        ("full", True, "member", None, None),
        ("selective", True, "maintainer", "read", "maintainer"),  # selective: the member's role (L2P4-1)
        ("selective", True, None, "admin", None),
    ],
)
def test_role_of_for_hub_users(mode, aliased, member, hub_role, want):
    ws = ws_with(
        mode, {"4" * 32: U1} if aliased else {}, {P: member} if member else {}, {U1: hub_role} if hub_role else {}
    )
    assert identity.role_of(ws, "u_" + U1) == want


def test_role_of_no_hub_and_members():
    ws = ws_with("full", {}, {P: "member"}, {U1: "admin"}, joined=False)
    assert identity.role_of(ws, "u_" + U1) is None and identity.role_of(ws, P) == "member"
    assert identity.role_of(ws, "p_" + "5" * 32) is None
    assert identity.role_of(WsCore(), "u_" + U1) is None


def test_signer_for_device_and_hub_actors():
    ws = ws_with("full", {"4" * 32: U1}, {P: "member"}, {U1: "admin"})
    dev = {"type": "log.added", "actor": {"kind": "person", "id": P, "device": "d_" + "1" * 32}}
    assert identity.signer(ws, dev) == identity.Signer(P, "member", "d_" + "1" * 32)
    hub = {"type": "log.added", "actor": hub_actor(), "hub_id": HUB, "channel": CHAN}
    assert identity.signer(ws, hub) == identity.Signer(P, "member", f"hub:{HUB}/{CHAN}")  # canonical person, no d_
    other = {**hub, "actor": hub_actor(U2)}
    assert identity.signer(ws, other) == identity.Signer("u_" + U2, None, f"hub:{HUB}/{CHAN}")
