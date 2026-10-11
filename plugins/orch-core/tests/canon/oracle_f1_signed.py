"""Independent oracle, part 4: the signed-bytes vectors of ticket-format §5.3, §5.5, §5.10, §5.11 (real P-256
signatures, replay refusals, the genesis checks, devices, revocations, checkpoints, tampered chains).

Built with :mod:`oracle_f1_world`; imports nothing from ``orch``. Each function returns the content of one vector file
and each file states in its ``pins`` / ``note`` fields what it pins.
"""

# ruff: noqa: E501
from __future__ import annotations

import copy
from typing import Any

from . import oracle_f1_world as ow
from .oracle_f1_world import TICKET, TICKET2, World, b64u, did, head, key, pid

W = ow.W
OTHER_W = "f" * 32
KEY_NOTE = (
    "TEST KEYS ONLY. Every private scalar below is derived from the key name as sha256('orch-f1-test-key|' + name) "
    "mod (n-1) + 1 and is published on purpose; the signatures are RFC 6979 (deterministic), so consumers verify the "
    "stored signature and never re-sign."
)
KEYS = ["wsk", "wsk_other", "pk_sev", "pk_mara", "pk_lena", "dk_sev1", "dk_sev2", "dk_sev3", "dk_mara1", "dk_lena1",
        "dk_lena2", "dk_evil"]  # fmt: skip


def keys_vector() -> dict[str, Any]:
    return {n: {"scalar_hex": f"{key(n).d:064x}", "pub_b64u": key(n).pub_b64u} for n in KEYS} | {
        "id_of": {"p_sev": pid("sev"), "p_mara": pid("mara"), "p_lena": pid("lena"), "d_sev1": did("sev1")}
    }


def base_world(name: str, pins: str) -> World:
    """sev (owner, device sev1), mara (maintainer), lena (member), one ticket owned by sev."""
    w = World(name, pins)
    w.genesis_event()
    w.member("mara", "maintainer")
    w.member("lena", "member")
    w.ticket()
    return w


# --- signatures.json ----------------------------------------------------------------------------------------


def signatures_vector() -> dict[str, Any]:
    w = base_world("sigs", "event signatures")
    note = w.ev(TICKET, "log.added", w.actor("mara"), {"text": "caf\u00e9 note"})
    ws_ev = w.ev("workspace", "settings.changed", w.actor("sev"), {"set": {"grant_hours": 4}})
    t_cert = ow.make_cert("mara", "mara1")
    s_cert = ow.make_cert("sev", "sev1")

    def person_case(name: str, desc: str, e: dict[str, Any], cert: dict[str, Any], log: str, ws: str = W) -> dict:
        return {"name": name, "desc": desc, "event": e, "cert": cert, "workspace_id": ws, "log": log}

    def re_sign(**kw: Any) -> dict[str, Any]:
        e = copy.deepcopy(note)
        e.pop("host_sig")
        e.pop("sig")
        e["sig"] = b64u(ow.sign("dk_mara1", ow.person_bytes(W, TICKET, e, **kw)))
        return e

    def flip(e: dict[str, Any]) -> dict[str, Any]:
        e = copy.deepcopy(e)
        e["sig"] = ("B" if e["sig"][0] != "B" else "C") + e["sig"][1:]
        return e

    def mod(e: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return {**copy.deepcopy(e), **kw}

    refusals = [
        person_case("other_ticket", "the same event verified as an event of another ticket", note, t_cert, TICKET2),
        person_case("other_workspace", "the same event under another workspace id", note, t_cert, TICKET, OTHER_W),
        person_case("ticket_event_read_as_workspace_log", "log is 'workspace'", note, t_cert, "workspace"),
        person_case("workspace_event_read_as_ticket_log", "a workspace event as a ticket event", ws_ev, s_cert, TICKET),
        person_case("signed_over_contract_2", "signature made over contract 2", re_sign(contract=2), t_cert, TICKET),
        person_case("signed_over_suite_3", "signature made over suite 3", re_sign(suite=3), t_cert, TICKET),
        person_case("signed_with_ws_event_label", "label swapped", re_sign(label="ws_event"), t_cert, TICKET),
        person_case("signed_with_host_label", "label swapped", re_sign(label="host_event"), t_cert, TICKET),
        person_case("tampered_text_byte", "one payload character changed", mod(note, text="cafe note"), t_cert, TICKET),
        person_case("flipped_signature_bit", "first signature character changed", flip(note), t_cert, TICKET),
        person_case("truncated_signature", "63 bytes", mod(note, sig=note["sig"][:-2]), t_cert, TICKET),
        person_case(
            "signed_by_another_device",
            "dk_lena1 signed, the certificate is dk_mara1's",
            note,
            ow.make_cert("lena", "lena1"),
            TICKET,
        ),  # fmt: skip
        person_case(
            "actor_names_another_device",
            "the actor's device is not the certificate's",
            mod(note, actor={**note["actor"], "device": did("lena1")}),
            t_cert,
            TICKET,
        ),  # fmt: skip
    ]
    covered = [
        ("id", ow.ulid(900)), ("type", "log.removed"), ("actor", {**note["actor"], "id": pid("sev")}),
        ("auth", "webauthn"), ("based_on", "sha256:" + "00" * 32), ("roster_v", 99), ("hash_v", 2),
        ("text", "other"), ("gate_gen", 1),
    ]  # fmt: skip
    for field, value in covered:
        refusals.append(person_case("changed_" + field, f"{field} is covered by the signature", mod(note, **{field: value}), t_cert, TICKET))  # fmt: skip
    uncovered = [("seq", 99), ("at", "2030-01-01T00:00:00Z"), ("prev", None), ("ws_seq", 7), ("host_sig", "Z" * 86)]
    accepted = [
        person_case(
            "changed_" + field,
            f"{field} is not covered by the person's signature",
            mod(note, **{field: value}),
            t_cert,
            TICKET,
        )  # fmt: skip
        for field, value in uncovered
    ]
    host_note = {k: v for k, v in note.items() if k != "host_sig"}

    def host_case(name: str, desc: str, e: dict[str, Any], log: str, ws: str = W, pub: str | None = None) -> dict:
        return {
            "name": name,
            "desc": desc,
            "event": e,
            "log": log,
            "workspace_id": ws,
            "wsk_pub": pub or key("wsk").pub_b64u,
        }

    host_refusals = [
        host_case("other_ticket", "host_sig replayed into another ticket log", note, TICKET2),
        host_case("workspace_log", "ticket event read as a workspace event", note, "workspace"),
        host_case("other_workspace", "another workspace id", note, TICKET, OTHER_W),
        host_case("other_key", "verified under another workspace key", note, TICKET, W, key("wsk_other").pub_b64u),
    ]
    for field, value in (("seq", 99), ("at", "2030-01-01T00:00:00Z"), ("prev", "sha256:" + "00" * 32), ("ws_seq", 7),
                         ("sig", flip(note)["sig"]), ("text", "other"), ("id", ow.ulid(901))):  # fmt: skip
        host_refusals.append(host_case("changed_" + field, f"{field} is covered by host_sig", mod(note, **{field: value}), TICKET))  # fmt: skip
    return {
        "pins": "the signed bytes of F1 5.3/5.5 with real P-256 signatures: person sig over "
        "'orch/v2/sig/ticket-event|' (or ws-event) + cj({contract, suite, workspace_id, log, event}), host_sig over "
        "the host-event label and the full event; what the signatures cover; replay into another ticket, log, "
        "workspace, contract, suite or label is refused",
        "note": KEY_NOTE,
        "workspace_id": W,
        "keys": keys_vector(),
        "ticket_event": {
            "event": note,
            "log": TICKET,
            "cert": t_cert,
            "person_signing_hex": ow.person_bytes(W, TICKET, note).hex(),
            "host_signing_hex": ow.host_bytes(W, TICKET, note).hex(),
            "head": head(note),
        },
        "ws_event": {
            "event": ws_ev,
            "log": "workspace",
            "cert": s_cert,
            "person_signing_hex": ow.person_bytes(W, "workspace", ws_ev).hex(),
            "host_signing_hex": ow.host_bytes(W, "workspace", ws_ev).hex(),
            "head": head(ws_ev),
        },
        "host_unsigned_ticket_event": host_note,
        "person_refused": refusals,
        "person_accepted_despite_change": accepted,
        "host_refused": host_refusals,
    }


# --- signed_log.json: replay refusals and tampered chain lines ----------------------------------------------


def replay_scenario() -> dict[str, Any]:
    w = base_world("replay", "replayed and re-contexted events are refused by the model with the real verifier")
    w.ticket(TICKET2, key_="DEMO-0002")
    ok_note = w.ev(TICKET, "log.added", w.actor("mara"), {"text": "first"}, note="a valid event by mara")
    seen = w.ev(TICKET, "log.added", w.actor("lena"), {"text": "second"})
    # the same signed event appended again (fresh seq/prev/host_sig): the id is repeated
    w.ev(TICKET, "log.added", w.actor("mara"), {"text": "first"}, eid=ok_note["id"], expect="event.duplicate_id",
         note="replay: the same event id and person signature, new seq and host_sig")  # fmt: skip
    # the signed bytes of `seen` carried into the other ticket: person sig covers the log
    w.ev(TICKET2, "log.added", w.actor("lena"), {"text": "second"}, eid=seen["id"], ctx_log=TICKET, expect="sig.invalid",
         note="replay into another ticket: the person signed for the first ticket's log")  # fmt: skip
    w.ev(TICKET, "log.added", w.actor("mara"), {"text": "third"}, ctx_workspace=OTHER_W, expect="sig.invalid",
         note="signed for another workspace id")  # fmt: skip
    w.ev(TICKET, "log.added", w.actor("mara"), {"text": "third"}, ctx_log="workspace", expect="sig.invalid",
         note="signed as a workspace-log event")  # fmt: skip
    w.ev(TICKET, "log.added", w.actor("mara"), {"text": "third"}, ctx_label="ws_event", expect="sig.invalid",
         note="signed under the ws-event label")  # fmt: skip
    w.ev(TICKET, "log.added", w.actor("mara"), {"text": "third"}, ctx_contract=2, expect="sig.invalid",
         note="signed over contract 2")  # fmt: skip
    w.ev(TICKET, "log.added", w.actor("mara"), {"text": "third"}, ctx_suite=3, expect="sig.invalid",
         note="signed over suite 3")  # fmt: skip
    w.ev(TICKET, "log.added", w.actor("mara"), {"text": "third"}, tamper=lambda e: e.update(text="th1rd"),
         expect="sig.invalid", note="a byte of the payload changed after signing")  # fmt: skip
    w.ev(TICKET, "log.added", w.actor("mara"), {"text": "third"}, signer="dk_lena1", expect="sig.invalid",
         note="signed by another member's device key")  # fmt: skip
    w.ev(TICKET, "log.added", w.actor("mara"), {"text": "third"}, signer="dk_evil", expect="sig.invalid",
         note="signed by a key that has no certificate")  # fmt: skip
    w.ev(TICKET, "log.added", w.actor("mara"), {"text": "third"}, roster_v=w.roster_v - 1, expect="members.stale",
         note="correctly signed, but roster_v is one behind the member list")  # fmt: skip
    w.ev(TICKET, "log.added", {**w.actor("mara"), "device": did("lena1")}, {"text": "third"},
         expect="device.unknown", note="the device belongs to another person")  # fmt: skip
    w.genesis_event(expect="genesis.invalid", schema_refused=True, note="a second genesis is never trusted")
    w.ev(TICKET, "log.added", w.actor("mara"), {"text": "third"}, note="and the honest event is accepted")
    return w.scenario()


def tamper_vector() -> dict[str, Any]:
    """The logs of ``replay_scenario`` without the refused steps, then lines and mutated variants."""
    w = base_world("tamper", "chain breaks")
    w.ev(TICKET, "log.added", w.actor("mara"), {"text": "first"})
    w.ev(TICKET, "log.added", w.actor("lena"), {"text": "second"})
    w.ev(TICKET, "log.added", w.actor("mara"), {"text": "third"})
    w.ev("workspace", "settings.changed", w.actor("sev"), {"set": {"grant_hours": 4}})
    ticket, wsl = w.logs[TICKET], w.logs["workspace"]

    def resign_host(e: dict[str, Any], log: str) -> dict[str, Any]:
        e = copy.deepcopy(e)
        e["host_sig"] = b64u(ow.sign("wsk", ow.host_bytes(W, log, e)))
        return e

    def setp(e: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return {**copy.deepcopy(e), **kw}

    cases: list[dict[str, Any]] = []

    def add(name: str, mutation: str, log: str, events: list[dict[str, Any]], canon_at: int | None,
            chain_at: int | None, invalid: list[list[Any]] | None = None) -> None:  # fmt: skip
        cases.append({"name": name, "mutation": mutation, "log": log, "events": events,
                      "check_chain_breaks_at": canon_at, "replay_chain_broken_at": chain_at,
                      "replay_invalid_seqs": invalid or []})  # fmt: skip

    e1, e2, e3, e4 = ticket
    add("payload_changed_not_resigned", "seq 2: text 'second' -> 'sec0nd', nothing re-signed", TICKET,
        [e1, setp(e2, text="sec0nd"), e3, e4], 3, 2)  # fmt: skip
    add("payload_changed_host_resigned", "seq 2: text changed and host_sig re-made (an attacker holding the workspace "
        "key); the person signature no longer verifies", TICKET,
        [e1, resign_host(setp(e2, text="sec0nd"), TICKET), e3, e4], 3, 3, [[2, "sig.invalid"]])  # fmt: skip
    add("event_deleted", "seq 2 removed", TICKET, [e1, e3, e4], 2, 2)
    add("events_swapped", "seq 3 and 4 swapped", TICKET, [e1, e2, e4, e3], 3, None)
    add("prev_rewritten", "seq 3: prev set to the head of seq 1", TICKET,
        [e1, e2, setp(e3, prev=head(e1)), e4], 3, 3)  # fmt: skip
    add("seq_rewritten", "seq 3: seq set to 5", TICKET, [e1, e2, setp(e3, seq=5), e4], 3, 3)
    add("host_sig_swapped", "seq 2 carries the host_sig of seq 3", TICKET,
        [e1, setp(e2, host_sig=e3["host_sig"]), e3, e4], 3, 2)  # fmt: skip
    add("tail_truncated", "seq 4 removed: the chain is intact, only a checkpoint can see it", TICKET,
        [e1, e2, e3], None, None)  # fmt: skip
    add("genesis_payload_changed", "workspace seq 1: prefix DEMO -> EVIL", "workspace",
        [setp(wsl[0], prefix="EVIL"), *wsl[1:]], 2, 1)  # fmt: skip
    add("member_role_raised_not_resigned", "workspace seq 3: lena member -> maintainer", "workspace",
        [wsl[0], wsl[1], setp(wsl[2], role="maintainer"), wsl[3]], 4, 3)  # fmt: skip
    add("member_role_raised_host_resigned", "workspace seq 3: lena member -> maintainer, host_sig re-made", "workspace",
        [wsl[0], wsl[1], resign_host(setp(wsl[2], role="maintainer"), "workspace"), wsl[3]], 4, 4,
        [[3, "sig.invalid"]])  # fmt: skip
    add("workspace_event_deleted", "workspace seq 2 removed", "workspace", [wsl[0], wsl[2], wsl[3]], 2, 2)
    return {
        "pins": "tampered chain lines: the stated mutation of a signed log, the seq where canon.check_chain stops "
        "(prev/seq links only) and where a reader (signatures included) reports chain.broken; "
        "full lines of both logs with real signatures",
        "workspace_id": W,
        "genesis": w.genesis,
        "now": ow.stamp(w.clock + 60),
        "workspace_events": wsl,
        "ticket_events": {TICKET: ticket},
        "workspace_lines_hex": [(ow.cj(e) + b"\n").hex() for e in wsl],
        "ticket_lines_hex": [(ow.cj(e) + b"\n").hex() for e in ticket],
        "heads": {"workspace": [head(e) for e in wsl], TICKET: [head(e) for e in ticket]},
        "cases": cases,
    }


# --- genesis.json -------------------------------------------------------------------------------------------


def _genesis_case(name: str, check: int | None, desc: str, shape: str | None = None, **kw: Any) -> dict[str, Any]:
    w = World("g", "x")
    w.dev["sev"] = "sev1"
    pay: dict[str, Any] = {
        "workspace_id": W,
        "prefix": "DEMO",
        "host_id": "h_01J9ZK00000000000000000H01",
        "wsk_pub": key("wsk").pub_b64u,
        "owner": {"person": pid("sev"), "name": "Sev", "pk_pub": key("pk_sev").pub_b64u},
        "delegation": ow.make_delegation("sev"),
        "device_cert": ow.make_cert("sev", "sev1"),
    }
    ev_kw: dict[str, Any] = {}
    for k, v in kw.items():
        if k in pay or k in ("owner",):
            pay[k] = v
        else:
            ev_kw[k] = v
    actor_dev = ev_kw.pop("actor_device", None)
    e = w.ev("workspace", "workspace.created", w.actor("sev", actor_dev), pay, **ev_kw)
    return {"name": name, "check": check, "shape": shape, "desc": desc, "event": e, "head": head(e)}


def genesis_vector() -> dict[str, Any]:
    good = _genesis_case("valid", None, "all six checks pass")
    sev_owner = lambda **k: {"person": pid("sev"), "name": "Sev", "pk_pub": key("pk_sev").pub_b64u} | k  # noqa: E731
    refused = [
        _genesis_case(
            "owner_pk_is_another_key",
            1,
            "owner.pk_pub is mara's key; person id, delegation and certificate are sev's",
            owner=sev_owner(pk_pub=key("pk_mara").pub_b64u),
        ),
        _genesis_case("owner_person_is_another", 1, "owner.person names mara", owner=sev_owner(person=pid("mara"))),
        _genesis_case(
            "certificate_of_another_person",
            1,
            "device_cert names mara's person id",
            device_cert=ow.make_cert("mara", "sev1", signer="pk_mara"),
        ),
        _genesis_case(
            "delegation_signed_by_another_key",
            2,
            "delegation signature is mara's",
            delegation=ow.make_delegation("sev", signer="pk_mara"),
        ),
        _genesis_case(
            "delegation_with_an_extra_field", 2, "exact field set", delegation=ow.make_delegation("sev", extra={"x": 1})
        ),
        _genesis_case(
            "delegation_for_another_workspace",
            3,
            "delegation.o.workspace_id differs",
            delegation=ow.make_delegation("sev", workspace_id=OTHER_W),
        ),
        _genesis_case(
            "delegation_for_another_wsk",
            3,
            "delegation.o.wsk_pub differs",
            delegation=ow.make_delegation("sev", wsk="wsk_other"),
        ),
        _genesis_case("host_sig_by_another_key", 4, "host_sig under wsk_other", host_key="wsk_other"),
        _genesis_case(
            "host_sig_covers_the_event",
            4,
            "host_id changed after host_sig; the person signature also fails, but check 4 comes first",
            tamper_after_host=lambda e: e.update(host_id="h_" + "0" * 26),
        ),
        _genesis_case(
            "certificate_signed_by_another_key",
            5,
            "device_cert signature is mara's",
            device_cert=ow.make_cert("sev", "sev1", signer="pk_mara"),
        ),
        _genesis_case(
            "actor_device_is_not_the_certificates",
            5,
            "actor.device is sev2, the certificate is sev1's",
            actor_device="sev2",
        ),
        _genesis_case(
            "certificate_without_decide", 5, "scopes_max ['look']", device_cert=ow.make_cert("sev", "sev1", ["look"])
        ),
        _genesis_case("signature_by_another_device", 6, "sig made by dk_mara1", signer="dk_mara1"),
        _genesis_case("signature_over_another_workspace", 6, "sig covers another workspace id", ctx_workspace=OTHER_W),
        _genesis_case(
            "owner_check_before_signature_check",
            1,
            "checks 1 and 6 both fail: 1 is reported",
            owner=sev_owner(pk_pub=key("pk_mara").pub_b64u),
            signer="dk_mara1",
        ),
        _genesis_case(
            "delegation_check_before_host_sig",
            3,
            "checks 3 and 4 both fail: 3 is reported",
            delegation=ow.make_delegation("sev", workspace_id=OTHER_W),
            host_key="wsk_other",
        ),
        _genesis_case("roster_v_is_not_zero", None, "the genesis has roster_v 0", shape="genesis.shape", roster_v=1),
    ]
    return {
        "pins": "the genesis checks of F1 5.11 in their stated order (1 owner ids, 2 delegation signature and field "
        "set, 3 delegation binds workspace and wsk, 4 host_sig under wsk_pub, 5 device_cert and actor.device, 6 sig "
        "under the certificate's key); genesis = the head of workspace.created",
        "note": KEY_NOTE,
        "workspace_id": W,
        "valid": good,
        "genesis": good["head"],
        "refused": refused,
    }


# --- devices.json and revocation.json -----------------------------------------------------------------------


def _dev_added(person: str, dev: str, scopes: list[str] | None = None, **cert_kw: Any) -> dict[str, Any]:
    return {"device": did(dev), "cert": ow.make_cert(person, dev, scopes, **cert_kw)}


def devices_scenarios() -> list[dict[str, Any]]:
    out = []
    w = base_world("device_added", "device.added: who may sign it, which certificates are refused")
    sev = w.actor("sev")
    w.ev("workspace", "device.added", sev, _dev_added("sev", "sev2"), note="an existing device adds the second")
    w.ev("workspace", "device.added", sev, _dev_added("sev", "sev1"), expect="device.exists", note="already known")
    w.ev("workspace", "device.added", sev, _dev_added("sev", "sev3", ["look", "decide"]),
         note="a device without operate is allowed")  # fmt: skip
    w.ev("workspace", "settings.changed", w.actor("sev", "sev3"), {"set": {"grant_hours": 4}}, expect="device.scope",
         note="...but it cannot sign a settings change (needs operate)")  # fmt: skip
    w.ev(TICKET, "log.added", w.actor("sev", "sev3"), {"text": "decide is enough"}, note="a person event needs decide")
    drop = ow.make_cert("sev", "evil", ["drop:" + "ab" * 16], expires_ms=1_800_000_000_000)
    w.ev("workspace", "device.added", sev, {"device": did("evil"), "cert": drop}, expect="device.scope",
         note="a drop: certificate is refused in device.added", schema_refused=True)  # fmt: skip
    w.ev("workspace", "device.added", sev, _dev_added("sev", "evil", ["look"]), expect="device.scope",
         note="no decide scope", schema_refused=True)  # fmt: skip
    w.ev("workspace", "device.added", sev, _dev_added("sev", "evil", expires_ms=1_790_000_001_000),
         expect="device.invalid", note="a certificate that expired before the event")  # fmt: skip
    w.ev("workspace", "device.added", sev, {"device": did("lena2"), "cert": ow.make_cert("sev", "evil")},
         expect="device.cert", note="event.device is not the certificate's device id", schema_refused=True)  # fmt: skip
    w.ev("workspace", "device.added", sev, {"device": did("evil"), "cert": ow.make_cert("sev", "evil", signer="pk_mara")},
         expect="device.cert", note="certificate not signed by the person's key")  # fmt: skip
    w.ev("workspace", "device.added", w.actor("mara"), _dev_added("sev", "evil"), expect="device.cert",
         note="mara's device adds a device certified by sev's key: only the person's own key vouches", schema_refused=True)  # fmt: skip
    w.ev("workspace", "device.added", sev, _dev_added("sev", "evil"), roster_v=w.roster_v - 1, expect="members.stale",
         note="stale roster_v")  # fmt: skip
    w.ev("workspace", "device.added", {**sev, "device": did("evil")}, _dev_added("sev", "evil"),
         expect="device.unknown", note="self-signed by the new device while sev has valid devices")  # fmt: skip
    out.append(w.scenario())

    w = base_world("device_recovery", "recovery: a new device signs its own device.added only when none is left")
    lena = w.actor("lena")
    w.ev("workspace", "device.added", {**lena, "device": did("lena2")}, _dev_added("lena", "lena2"),
         expect="device.unknown", note="self-signed while lena1 is valid")  # fmt: skip
    w.ev("workspace", "device.removed", lena, {"device": did("lena1"), "reason": "lost phone"})
    w.ev(TICKET, "log.added", lena, {"text": "after removal"}, expect="device.invalid",
         note="a removed device signs nothing")  # fmt: skip
    new = {**lena, "device": did("lena2")}
    w.ev("workspace", "device.added", new, {"device": did("lena2"), "cert": ow.make_cert("lena", "lena2", signer="dk_evil")},
         expect="device.cert", note="recovery needs a certificate signed by the person key")  # fmt: skip
    w.ev("workspace", "device.added", new, _dev_added("lena", "lena2", ["look"]), expect="device.scope",
         note="recovery certificate must have decide", schema_refused=True)  # fmt: skip
    w.ev("workspace", "device.added", new, _dev_added("lena", "lena2"), note="recovery: signed by the new device")
    w.dev["lena"] = "lena2"
    w.ev(TICKET, "log.added", w.actor("lena"), {"text": "back"}, note="the new device signs person events")
    out.append(w.scenario())
    return out


def revocation_scenarios() -> list[dict[str, Any]]:
    out = []
    w = base_world("device_revoked", "device.revoked: the embedded revocation is the authority")
    rev = lambda p, d, reason="lost", **k: {  # noqa: E731
        "device": did(d), "reason": reason, "revocation": ow.make_revocation(p, d, reason, **k)}  # fmt: skip
    sev = w.actor("sev")
    steps: list[dict[str, Any]] = []
    w.ev(
        "workspace",
        "device.revoked",
        sev,
        {**rev("mara", "mara1"), "reason": "compromised"},
        expect="device.cert",
        note="payload reason differs from revocation.o.reason",
        schema_refused=True,
    )
    steps.append({"step": len(w.steps) - 1, "identity": "device.reason_mismatch"})
    w.ev("workspace", "device.revoked", sev, rev("mara", "mara1", signer="pk_sev"), expect="device.cert",
         note="revocation signed by the sender's person key, not the device's person's")  # fmt: skip
    steps.append({"step": len(w.steps) - 1, "identity": "bad_signature"})
    bad = {"device": did("mara1"), "reason": "lost", "revocation": ow.make_revocation("lena", "lena1", "lost")}
    w.ev("workspace", "device.revoked", sev, bad, expect="device.cert",
         note="a valid revocation of another person's device does not revoke this one", schema_refused=True)  # fmt: skip
    steps.append({"step": len(w.steps) - 1, "identity": "bad_signature"})
    w.ev("workspace", "device.revoked", sev, rev("mara", "evil"), expect="device.unknown", note="unknown device")
    w.ev("workspace", "device.revoked", sev, rev("mara", "mara1"), note="another member's device appends it, the "
         "authority is mara's embedded revocation")  # fmt: skip
    w.ev(TICKET, "log.added", w.actor("mara"), {"text": "x"}, expect="device.invalid", note="the revoked device")
    w.ev("workspace", "device.added", {**w.actor("mara")}, _dev_added("mara", "mara1"), expect="device.invalid",
         note="the revoked device cannot re-add itself, not even as a recovery")  # fmt: skip
    w.ev("workspace", "device.revoked", w.HOST, rev("lena", "lena1", "retired"),
         note="the host appends a PK-signed revocation (actor H)")  # fmt: skip
    w.ev("workspace", "device.revoked", w.HOST, {"device": did("lena1"), "reason": "lost",
         "revocation": ow.make_revocation("lena", "lena1", "retired")}, expect="device.cert",
         note="the host's payload reason must equal the revocation's", schema_refused=True)  # fmt: skip
    out.append({**w.scenario(), "identity_checks": steps})

    w = base_world("revoked_recovery", "a person whose only device was revoked recovers with a new device")
    w.ev("workspace", "device.revoked", w.actor("sev"), rev("lena", "lena1", "lost"))
    new = {**w.actor("lena"), "device": did("lena2")}
    w.ev("workspace", "device.added", new, _dev_added("lena", "lena2"), note="recovery after a revocation")
    w.dev["lena"] = "lena2"
    w.ev(TICKET, "log.added", w.actor("lena"), {"text": "back"})
    out.append(w.scenario())
    return out


# --- checkpoint.json ----------------------------------------------------------------------------------------


def _checkpoint(o: dict[str, Any], signer: str = "wsk", label: str = "checkpoint") -> dict[str, Any]:
    return {"o": o, "sig": b64u(ow.sign(signer, ow.LABEL[label].encode() + ow.cj(o)))}


def checkpoint_vector() -> dict[str, Any]:
    t = tamper_vector()
    tl, wl = t["ticket_events"][TICKET], t["workspace_events"]
    heads_t, heads_w = t["heads"][TICKET], t["heads"]["workspace"]

    def tcp(seq: int, hd: str, at: str = "2026-10-10T10:03:00Z") -> dict[str, Any]:
        return {"v": 2, "suite": 2, "kind": "ticket_checkpoint", "workspace_id": W, "uid": TICKET, "seq": seq,
                "head": hd, "at": at}  # fmt: skip

    def wcp(n: int, ws_seq: int, hd: str, tickets: dict[str, Any]) -> dict[str, Any]:
        return {"v": 2, "suite": 2, "kind": "workspace_checkpoint", "workspace_id": W, "genesis": t["genesis"], "n": n,
                "at": "2026-10-10T10:03:30Z", "workspace_log": {"seq": ws_seq, "head": hd}, "tickets": tickets}  # fmt: skip

    ticket_o = tcp(len(tl), heads_t[-1])
    ws_o = wcp(1, len(wl), heads_w[-1], {TICKET: {"seq": len(tl), "head": heads_t[-1]}})
    signed_ticket, signed_ws = _checkpoint(ticket_o), _checkpoint(ws_o)
    forged = [
        {"name": "signed_by_another_key", "checkpoint": _checkpoint(ticket_o, "wsk_other")},
        {"name": "head_changed", "checkpoint": {"o": {**ticket_o, "head": heads_t[1]}, "sig": signed_ticket["sig"]}},
        {"name": "seq_changed", "checkpoint": {"o": {**ticket_o, "seq": 3}, "sig": signed_ticket["sig"]}},
        {"name": "signed_under_the_ws_event_label", "checkpoint": _checkpoint(ticket_o, label="ws_event")},
        {"name": "signed_under_the_host_event_label", "checkpoint": _checkpoint(ticket_o, label="host_event")},
        {
            "name": "kind_changed",
            "checkpoint": {"o": {**ticket_o, "kind": "workspace_checkpoint"}, "sig": signed_ticket["sig"]},
        },
        {
            "name": "workspace_checkpoint_head_changed",
            "checkpoint": {"o": {**ws_o, "workspace_log": {"seq": 4, "head": heads_w[1]}}, "sig": signed_ws["sig"]},
        },  # fmt: skip
    ]
    # offers made to a store that already holds the ticket checkpoint at seq 4 / the workspace checkpoint n=1
    ticket_offers = [
        {"name": "higher_seq", "seq": 5, "head": "sha256:" + "11" * 32, "expect": "ok"},
        {"name": "same_seq_same_head", "seq": 4, "head": heads_t[-1], "expect": "ok"},
        {"name": "lower_seq", "seq": 3, "head": heads_t[2], "expect": "chain.diverged"},
        {"name": "same_seq_other_head", "seq": 4, "head": "sha256:" + "22" * 32, "expect": "chain.diverged"},
    ]
    ws_offers = [
        {
            "name": "same_height_other_head",
            "log_seq": len(wl),
            "head": "sha256:" + "33" * 32,
            "expect": "chain.diverged",
        },
        {"name": "lower_height", "log_seq": len(wl) - 1, "head": heads_w[-2], "expect": "chain.diverged"},
        {"name": "same_height_same_head", "log_seq": len(wl), "head": heads_w[-1], "expect": "ok", "n": 2},
    ]
    # logs as they are on disk when a checkpoint is checked (heads by seq, 0-based list)
    divergences = [
        {"name": "log_agrees", "ticket_heads": heads_t, "expect": []},
        {
            "name": "head_at_the_checkpointed_seq_differs",
            "ticket_heads": [*heads_t[:3], "sha256:" + "44" * 32],
            "expect": ["chain.diverged"],
        },
        {"name": "log_rolled_back_behind_the_checkpoint", "ticket_heads": heads_t[:3], "expect": ["chain.diverged"]},
        {"name": "log_grew_past_the_checkpoint", "ticket_heads": [*heads_t, "sha256:" + "55" * 32], "expect": []},
    ]
    return {
        "pins": "protocol 2.4 checkpoint objects of F1 5.10: the exact shape of `o`, the signed bytes "
        "('orch/v2/sig/checkpoint|' + cj(o)), real signatures; forged variants fail; a lower seq or n, or the same "
        "seq with another head, is refused (chain.diverged); a log that disagrees with a checkpoint is diverged; "
        "restore (see restore.json)",
        "note": KEY_NOTE,
        "workspace_id": W,
        "wsk_pub": key("wsk").pub_b64u,
        "genesis": t["genesis"],
        "ticket_checkpoint": {
            "signed": signed_ticket,
            "signing_hex": (ow.LABEL["checkpoint"].encode() + ow.cj(ticket_o)).hex(),
        },
        "workspace_checkpoint": {
            "signed": signed_ws,
            "signing_hex": (ow.LABEL["checkpoint"].encode() + ow.cj(ws_o)).hex(),
        },
        "forged": forged,
        "ticket_log": {"uid": TICKET, "heads": heads_t},
        "workspace_log": {"heads": heads_w},
        "ticket_offers": ticket_offers,
        "workspace_offers": ws_offers,
        "divergence": divergences,
    }


def restore_scenarios() -> list[dict[str, Any]]:
    def prefix(w: World) -> None:
        w.genesis_event()
        w.member("mara", "maintainer")
        w.member("lena", "member")
        w.ev("workspace", "settings.changed", w.actor("sev"), {"set": {"grant_hours": 4}})

    rev = ow.make_revocation("lena", "lena1", "lost")
    old = World("old", "x")
    prefix(old)
    old_revoked = old.ev("workspace", "device.revoked", old.actor("sev"),
                         {"device": did("lena1"), "reason": "lost", "revocation": rev})  # fmt: skip
    out = []
    for name, reappend in (("restore_reappends_the_revocation", True), ("restore_without_the_revocation", False)):
        w = World(name, "a workspace restore after a rollback: the host re-appends every PK-signed revocation it has "
                  "seen as device.revoked with actor H, the embedded revocation verbatim")  # fmt: skip
        prefix(w)
        h4 = head(w.logs["workspace"][-1])
        payload = {"from_seq": 4, "head": h4, "abandoned": {"seq": 5, "head": head(old_revoked)},
                   "abandoned_decisions": [], "reason": "restored a backup"}  # fmt: skip
        w.ev("workspace", "restore", w.actor("mara"), {**payload}, expect="role.denied", note="owners only")
        w.ev("workspace", "restore", w.actor("sev"), {**payload, "from_seq": 3}, expect="restore.bad_head",
             note="from_seq and head must name the last event on disk", schema_refused=True)  # fmt: skip
        w.ev("workspace", "restore", w.actor("sev"), {**payload, "head": head(old_revoked)},
             expect="restore.bad_head", note="head is not the last event's", schema_refused=True)  # fmt: skip
        w.ev("workspace", "restore", w.actor("sev"), payload, note="the owner's restore, seq 5, prev = from_seq's head",
             after={"workspace": {"devices_revoked": []}})  # fmt: skip
        if reappend:
            w.ev("workspace", "device.revoked", w.HOST, {"device": did("lena1"), "reason": "lost", "revocation": rev},
                 note="the host puts the revocation back at once",
                 after={"workspace": {"devices_revoked": [did("lena1")]}})  # fmt: skip
        out.append(w.scenario(reappends=reappend))
    return out
