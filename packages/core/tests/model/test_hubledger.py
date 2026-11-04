"""C3b task 2 (ticket-format §15.2, §15.6): the hub ledger of ``hub.pinned`` lines and the ``hub.joined`` bookkeeping
(``ledger``, ``user_hub``, ``foreign_user``). The verifier is the injected fake: what it refuses is the contract's;
what the ledger refuses on its own (not joined, duplicates, caps, order) is tested here."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orch.crypto import suite
from orch.crypto.encoding import b64u
from orch.hubclient import wire
from orch.model import hubledger
from orch.model.codes import Code
from tests.model.world import World, sig

HUB = "38d8abed52ccfb538da401fd54e64bdc"
ROOT = "BI_UMykYqAqu1C9i1pPLsPGXktAHAqZlKB_EpiicGtikqSoV9hdFn8S3nWlSvAmzbCp1iQ79iiLjD9tWA1p39ik"
_KEY_B = suite.public_bytes(suite.generate_private_key())
HUB_B, ROOT_B = wire.hub_id(_KEY_B), b64u(_KEY_B)
U1, U2 = "a368bd380588e5dabee62a516031e21f", "9124812ba8533a024bee116af55a7da0"
HOST = {"kind": "host"}


def card(n: int = 1, hub: str = HUB) -> dict:
    return {
        "o": {"kind": "hub.card", "hub_id": hub, "n": n},
        "op_cert": {"o": {}, "sig": sig("c")},
        "sig": sig(f"card{n}"),
    }


def snap(version: int, channels: tuple[str, ...] = (), hub: str = HUB) -> dict:
    return {"o": {"kind": "hub.snapshot", "hub_id": hub, "version": version, "revoked_channels": list(channels),
                  "roles": {U1: "admin"}, "ends_ms": {}}, "sig": sig(f"snap{version}")}  # fmt: skip


def revoked(n: int, hub: str = HUB) -> dict:
    return {"o": {"kind": "hub.op_revoked", "hub_id": hub, "cert_id": f"{n:032x}"}, "sig": sig(f"rev{n}")}


@pytest.fixture
def w() -> World:
    return World().bootstrap({"mara": "maintainer"})


def join(w: World, *, hub: str = HUB, root: str = ROOT, seq: int = 1, aliases=(), mode: str = "full"):
    return w.try_("workspace", "hub.joined", "sev", hub_id=hub, hk_root_pub=root, mode=mode, binding_seq=seq,
                  aliases=sorted(aliases, key=lambda a: a["person_id"]))  # fmt: skip


def alias(w: World, name: str, uid: str) -> dict:
    return {"person_id": w.people[name][2:], "user_id": uid}


def pin(w: World, obj: dict, hub: str = HUB):
    return w.try_("workspace", "hub.pinned", HOST, hub_id=hub, object=obj)


def ledger(w: World, hub: str = HUB) -> dict:
    return w.state()._core.ws.hub["ledger"][hub]


def detail(r) -> str:
    assert r.code == Code.HUB_INVALID, r
    return r.detail


def test_a_join_starts_the_ledger_with_the_root_and_it_survives_leave_and_rejoin(w):
    assert not hasattr(join(w)[1], "code")
    assert ledger(w) == hubledger.new_ledger(ROOT)
    assert not hasattr(pin(w, card())[1], "code")
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    assert ledger(w)["card"] == card()
    assert not hasattr(join(w, seq=2)[1], "code")
    assert ledger(w)["card"] == card() and ledger(w)["root"] == ROOT


def test_a_rejoin_with_another_root_is_refused(w):
    join(w)
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    assert detail(join(w, root=ROOT_B, seq=2)[1]) == "root_mismatch"


def test_standalone_and_left_workspaces_refuse_every_pin(w):
    for obj in (card(), snap(1), revoked(1)):
        assert detail(pin(w, obj)[1]) == "not_joined"
    assert "hub" not in w.state()._core.ws.__dict__ or not w.state()._core.ws.hub
    join(w)
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    assert detail(pin(w, card())[1]) == "not_joined"


def test_a_pin_for_another_hub_is_refused(w):
    join(w)
    assert detail(pin(w, card(hub=HUB_B), hub=HUB_B)[1]) == "not_joined"
    assert detail(pin(w, card(hub=HUB_B), hub=HUB)[1]) == "other_hub"


def test_card_snapshot_and_revocation_fill_the_ledger(w):
    join(w)
    for obj in (card(), snap(3, ("c" * 32,)), revoked(7)):
        assert not hasattr(pin(w, obj)[1], "code")
    led = ledger(w)
    assert led["card"] == card() and led["snapshot"] == snap(3, ("c" * 32,))
    assert led["revoked_op_certs"] == [f"{7:032x}"] and led["revoked_channels"] == ["c" * 32]


def test_the_verifier_gets_the_ledger_and_the_lines_at(w):
    join(w)
    e, _ = pin(w, card())
    pin(w, snap(1))
    call = w.verifier.pin_calls[-1]
    assert call["root"] == ROOT and call["card"] == card() and call["snapshot"] is None
    assert call["workspace_id"] == w.workspace_id and call["dev"] is False and call["revoked"] == frozenset()
    from orch.model.types import ts

    assert w.verifier.pin_calls[0]["at_ms"] == ts(e["at"]) * 1000


def test_a_refusal_of_the_verifier_is_hub_invalid_with_its_code_and_leaves_the_ledger(w):
    join(w)
    pin(w, card())
    before = ledger(w)
    w.verifier.pin_codes["hub.snapshot"] = "revoked"
    assert detail(pin(w, snap(2))[1]) == "revoked"
    assert ledger(w) == before


def test_the_same_card_again_and_the_same_revocation_again(w):
    join(w)
    pin(w, card())
    pin(w, revoked(1))
    assert detail(pin(w, card())[1]) == "already_pinned"
    assert detail(pin(w, revoked(1))[1]) == "already_pinned"
    assert not hasattr(pin(w, card(2))[1], "code")  # a rotation is a different card


def test_a_snapshot_needs_a_card_and_the_same_version_is_already_pinned(w):
    join(w)
    assert detail(pin(w, snap(1))[1]) == "no_card"
    pin(w, card())
    assert not hasattr(pin(w, snap(1))[1], "code")
    assert detail(pin(w, snap(1))[1]) == "already_pinned"
    w.verifier.pin_codes["hub.snapshot:1"] = "snapshot_conflict"
    assert detail(pin(w, snap(1, ("a" * 32,)))[1]) == "snapshot_conflict"


def test_a_channel_revocation_is_permanent_in_the_ledger(w):
    join(w)
    pin(w, card())
    pin(w, snap(1, ("a" * 32,)))
    pin(w, snap(2, ("b" * 32,)))
    pin(w, snap(3, ()))
    assert ledger(w)["revoked_channels"] == ["a" * 32, "b" * 32]
    assert hubledger.channel_revoked(ledger(w), "a" * 32) and not hubledger.channel_revoked(ledger(w), "c" * 32)


def test_caps_cards_revocations_and_snapshot_size(w):
    join(w)
    for n in range(hubledger.MAX_CARDS):
        assert not hasattr(pin(w, card(n))[1], "code"), n
    assert detail(pin(w, card(99))[1]) == "too_many"
    for n in range(hubledger.MAX_OP_REVOCATIONS):
        assert not hasattr(pin(w, revoked(n))[1], "code"), n
    assert detail(pin(w, revoked(999))[1]) == "too_many"
    big = snap(1)
    big["o"]["pad"] = "x" * 70_000
    assert detail(pin(w, big)[1]) in ("too_large", "validation")  # the schema string limit may refuse it first


def test_a_verifier_without_the_hub_methods_refuses_everything():
    ws = SimpleNamespace(
        hub={"status": "joined", "hub_id": HUB, "ledger": {HUB: hubledger.new_ledger(ROOT)}}, workspace_id="w"
    )
    e = {"hub_id": HUB, "object": card(), "at": "2026-10-09T09:10:11Z"}
    r = hubledger.apply_pin(ws, e, object(), dev=False)  # type: ignore[arg-type]
    assert r is not None and r.detail == "unavailable"
    assert ws.hub["ledger"][HUB]["card"] is None


def test_the_default_verifier_methods_refuse():
    from orch.identity.verifier import CryptoVerifier
    from orch.model.verifier import RefuseHub

    for v in (CryptoVerifier(), RefuseHub()):
        assert v.verify_custodial_sigs({}, workspace_id="w", log="workspace") is False
        assert v.verify_hub_pin({}) == "unavailable" and v.verify_hub_actor({}) == "unavailable"


def test_user_hub_records_aliases_and_survives_a_leave(w):
    join(w, aliases=[alias(w, "mara", U1)])
    assert w.state()._core.ws.hub["user_hub"] == {U1: HUB}
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    assert w.state()._core.ws.hub["user_hub"] == {U1: HUB}


def test_join_refuses_an_id_aliased_under_another_hub_to_another_person(w):
    join(w, aliases=[alias(w, "mara", U1)])
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    r = join(w, hub=HUB_B, root=ROOT_B, seq=2, aliases=[alias(w, "sev", U1)])[1]
    assert r.code == Code.HUB_INVALID  # the existing union rule or foreign_user, both refuse


def test_join_aliasing_the_same_person_on_both_hubs_is_fine(w):
    join(w, aliases=[alias(w, "mara", U1)])
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    assert not hasattr(join(w, hub=HUB_B, root=ROOT_B, seq=2, aliases=[alias(w, "mara", U1)])[1], "code")
    assert w.state()._core.ws.hub["user_hub"][U1] == HUB  # never re-pointed
    assert set(w.state()._core.ws.hub["ledger"]) == {HUB, HUB_B}


def test_join_refuses_an_id_first_recorded_unaliased_under_another_hub():
    from orch.model import workspace
    from orch.model.types import Member, WsCore

    core = WsCore(created=True)
    core.members = {"p_" + "4" * 32: Member("p_" + "4" * 32, "a", "owner", "k")}
    core.hub = {"status": "left", "hub_id": HUB_B, "binding_seq": 1, "alias_book": {}, "aliases": {},
                "user_hub": {U1: HUB_B}, "ledger": {HUB_B: hubledger.new_ledger(ROOT_B)}}  # fmt: skip
    e = {"type": "hub.joined", "actor": {"kind": "person", "id": "p_" + "4" * 32}, "hub_id": HUB, "hk_root_pub": ROOT,
         "mode": "full", "binding_seq": 2, "aliases": [{"person_id": "4" * 32, "user_id": U1}]}  # fmt: skip
    r = workspace.hub_joined(core, e)
    assert r is not None and r.code == Code.HUB_INVALID and r.detail == "foreign_user"
    assert core.hub["status"] == "left"  # unchanged
    e["aliases"] = []
    assert workspace.hub_joined(core, e) is None and core.hub["user_hub"] == {U1: HUB_B}


def test_every_actor_rule_type_has_a_handler():
    """No type the actor table admits may reach a handler table without an entry (KeyError / unverified accept)."""
    import inspect

    from orch.model import authz, engine

    src = inspect.getsource(engine._ticket_handler) + inspect.getsource(engine._workspace_handler)  # noqa: SLF001
    assert "h[typ]" not in src  # a missing type is a Refusal, never a KeyError
    for typ in authz.ACTOR_RULES:
        in_ws = typ in engine.LOG_TYPES["workspace"]
        in_ticket = typ in engine.LOG_TYPES["ticket"]
        assert in_ws or in_ticket, typ
        quoted = f'"{typ}"'
        assert (
            quoted in src or typ in ("ticket.created", "workspace.created") or typ.startswith(("task.", "artifact."))
        ), typ
