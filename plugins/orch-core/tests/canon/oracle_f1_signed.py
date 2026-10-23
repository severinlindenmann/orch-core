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
RECEIVER_NOW = "2026-10-10T12:00:00Z"  # the receiver's clock in checkpoint.json unless a case gives its own `now`
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

    def add(name: str, mutation: str, log: str, events: list[dict[str, Any]], broken_at: int | None) -> None:
        """One F1 outcome per case (§5.5 "Reading"): the reader checks each log's lines in file order and reports
        ``chain.broken`` at the first line that fails cj, field set, seq, prev, host_sig or the person sig
        (``chain.bad_ws_seq`` for workspace-position/order checks), naming the line's position (the seq it should carry). A break at workspace seq n also stops every ticket
        event with ws_seq >= n."""
        case: dict[str, Any] = {"name": name, "mutation": mutation, "log": log, "events": events,
                                "chain_broken_at": broken_at}  # fmt: skip
        if log == "workspace":
            stopped = {}
            if broken_at is not None:
                for uid, evs in w.logs.items():
                    if uid != "workspace":
                        hit = [e["seq"] for e in evs if e["ws_seq"] >= broken_at]
                        if hit:
                            stopped[uid] = hit[0]
            case["ticket_events_stopped_from"] = stopped
        cases.append(case)

    e1, e2, e3, e4 = ticket
    add("payload_changed_not_resigned", "seq 2: text 'second' -> 'sec0nd', nothing re-signed: host_sig fails", TICKET,
        [e1, setp(e2, text="sec0nd"), e3, e4], 2)  # fmt: skip
    add("payload_changed_host_resigned", "seq 2: text changed and host_sig re-made (an attacker holding the workspace "
        "key); the person signature no longer verifies, which breaks the chain at that line", TICKET,
        [e1, resign_host(setp(e2, text="sec0nd"), TICKET), e3, e4], 2)  # fmt: skip
    add("event_deleted", "seq 2 removed: the line at position 2 carries seq 3", TICKET, [e1, e3, e4], 2)
    add("events_swapped", "seq 3 and 4 swapped: lines are checked in file order and never sorted, so the line at "
        "position 3 (carrying seq 4) breaks the chain", TICKET, [e1, e2, e4, e3], 3)  # fmt: skip
    add("prev_rewritten", "seq 3: prev set to the head of seq 1", TICKET, [e1, e2, setp(e3, prev=head(e1)), e4], 3)
    add("seq_rewritten", "seq 3: seq set to 5", TICKET, [e1, e2, setp(e3, seq=5), e4], 3)
    add("host_sig_swapped", "seq 2 carries the host_sig of seq 3", TICKET,
        [e1, setp(e2, host_sig=e3["host_sig"]), e3, e4], 2)  # fmt: skip
    add("at_goes_back_host_resigned", "seq 4: `at` set before seq 3's and host_sig re-made (an attacker holding the "
        "workspace key; `at` is not signed by the person): (ws_seq, at) never goes back along a log, so the line "
        "breaks the chain", TICKET, [e1, e2, e3, resign_host(setp(e4, at=e1["at"]), TICKET)], 4)  # fmt: skip
    add("ws_seq_goes_back_host_resigned", "seq 4: ws_seq lowered from 3 to 2, at unchanged, host_sig re-made; "
        "seq/prev and person signature remain valid. Replay keeps this single ticket log in file order: "
        "the workspace head is 3 when this line is read, so only the per-log ws_seq decrease fails. "
        "The cross-ticket merged-order guard is append-only; this history is append-unreachable", TICKET,
        [e1, e2, e3, resign_host(setp(e4, ws_seq=e3["ws_seq"] - 1), TICKET)], 4)  # fmt: skip
    cases[-1].update(replay_only=True, replay_expect="chain.bad_ws_seq")
    add("bad_person_sig_and_ws_seq", "seq 4: bad person signature and backwards ws_seq, valid host signature; "
        "step 1 checks the person signature first and reports chain.broken", TICKET,
        [e1, e2, e3, resign_host(setp(e4, sig="A" * 86, ws_seq=e3["ws_seq"] - 1), TICKET)], 4)  # fmt: skip
    cases[-1]["replay_expect"] = "chain.broken"
    add(
        "tail_truncated", "seq 4 removed: the chain is intact, only a checkpoint can see it", TICKET, [e1, e2, e3], None
    )
    add("genesis_payload_changed", "workspace seq 1: prefix DEMO -> EVIL", "workspace",
        [setp(wsl[0], prefix="EVIL"), *wsl[1:]], 1)  # fmt: skip
    add("member_role_raised_not_resigned", "workspace seq 3: lena member -> maintainer", "workspace",
        [wsl[0], wsl[1], setp(wsl[2], role="maintainer"), wsl[3]], 3)  # fmt: skip
    add("member_role_raised_host_resigned", "workspace seq 3: lena member -> maintainer, host_sig re-made; the "
        "person signature no longer verifies, so the chain breaks at 3 and every ticket event with ws_seq >= 3 stops",
        "workspace", [wsl[0], wsl[1], resign_host(setp(wsl[2], role="maintainer"), "workspace"), wsl[3]], 3)  # fmt: skip
    add("workspace_event_deleted", "workspace seq 2 removed", "workspace", [wsl[0], wsl[2], wsl[3]], 2)
    # §5.2's field set is independent of the signature: sign each malformed host event correctly.
    repaired = w.ev(
        TICKET,
        "projection.repaired",
        w.HOST,
        {"path": "ticket.json", "cause": "projection_mismatch"},
        at=ow.stamp(w.clock),
    )
    ticket.pop()  # keep the shared base logs (also used by checkpoint.json) unchanged
    add(
        "host_field_set_valid",
        "a host projection.repaired without person-only fields",
        TICKET,
        [*ticket, repaired],
        None,
    )
    for field, value in (("sig", e4["sig"]), ("auth", "passphrase"), ("roster_v", w.roster_v)):
        add(
            f"host_field_set_{field}",
            f"5.2: {field} appears on person events and on no others; host_sig verifies",
            TICKET,
            [*ticket, resign_host(setp(repaired, **{field: value}), TICKET)],
            repaired["seq"],
        )
    agent_event = w.ev(
        TICKET,
        "log.added",
        {"kind": "agent", "id": "claude-code", "session": "field-set", "unattended": True},
        {"text": "agent note"},
        at=ow.stamp(w.clock),
    )
    ticket.pop()  # keep the shared base logs unchanged
    add("agent_field_set_valid", "an agent event without person-only fields", TICKET, [*ticket, agent_event], None)
    add(
        "agent_field_set_sig",
        "5.2: sig appears on person events and on no others; agent event host_sig verifies",
        TICKET,
        [*ticket, resign_host(setp(agent_event, sig=e4["sig"]), TICKET)],
        agent_event["seq"],
    )
    return {
        "pins": "tampered chain lines (5.5 Reading): the stated mutation of a signed log and the one F1 outcome, the "
        "line position where a reader reports chain.broken (lines checked in file order, a bad host_sig or person sig "
        "breaks the chain at that line); for a workspace-log break at seq n, the first seq of each ticket log whose "
        "events stop counting (ws_seq >= n); full lines of both logs with real signatures",
        "note": "Case keys: replay_only marks an append-unreachable tampered log, checked directly on replay; "
        "replay_expect pins its refusal code in addition to chain_broken_at.",
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
            "certificate_with_decide_only",
            5,
            "scopes_max ['look', 'decide']: the genesis installs members, policies and settings, which need `operate`",
            device_cert=ow.make_cert("sev", "sev1", ["look", "decide"]),
        ),
        _genesis_case(
            "delegation_check_before_host_sig",
            3,
            "checks 3 and 4 both fail: the genesis checks name 3 (host_sig is also a line check, so the event is "
            "refused with chain.broken, refusal order step 1)",
            delegation=ow.make_delegation("sev", workspace_id=OTHER_W),
            host_key="wsk_other",
        ),
        _genesis_case("roster_v_is_not_zero", None, "the genesis has roster_v 0", shape="genesis.shape", roster_v=1),
    ]
    # (append, replay): checks 1, 2, 3 and 5 are genesis.invalid, check 4 is chain.broken, check 6 is sig.invalid at append
    # and chain.broken on replay; on replay a failing host_sig or person sig breaks the chain before any other check
    codes = {
        "host_sig_by_another_key": ("chain.broken", "chain.broken"),
        "host_sig_covers_the_event": ("chain.broken", "chain.broken"),
        "delegation_check_before_host_sig": ("chain.broken", "chain.broken"),
        "signature_by_another_device": ("sig.invalid", "chain.broken"),
        "signature_over_another_workspace": ("sig.invalid", "chain.broken"),
        "owner_check_before_signature_check": ("genesis.invalid", "chain.broken"),
        "actor_device_is_not_the_certificates": ("genesis.invalid", "chain.broken"),
    }
    for c in refused:
        c["append"], c["replay"] = codes.get(c["name"], ("genesis.invalid", "genesis.invalid"))
    return {
        "pins": "the genesis checks of F1 5.11 (1 owner ids, 2 delegation signature and field "
        "set, 3 delegation binds workspace and wsk, 4 host_sig under wsk_pub, 5 device_cert (valid at `at`, with operate) "
        "and actor.device, 6 sig under the certificate's key; 4 and 6 are line checks and are reported first when they fail together with another); genesis = the head of workspace.created. Each refused "
        "case carries `append` (the code at append: checks 1, 2, 3, 5 genesis.invalid, check 4 chain.broken, check 6 "
        "sig.invalid) and `replay` (chain.broken where a line check fails first, else genesis.invalid)",
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


def acc_fold(heads: list[str], start: str = "sha256:" + "0" * 64) -> str:
    import hashlib

    acc = bytes.fromhex(start[7:])
    for h in heads:
        raw = bytes.fromhex(h[7:])
        assert len(acc) == len(raw) == 32
        acc = hashlib.sha256(b"orch/v2/log-acc|" + acc + raw).digest()
    return "sha256:" + acc.hex()


def checkpoint_ref(cp: dict[str, Any]) -> dict[str, Any]:
    import hashlib

    return {
        "n": cp["o"]["n"],
        "hash": "sha256:" + hashlib.sha256(b"orch/v2/checkpoint-hash|" + ow.cj(cp["o"])).hexdigest(),
    }


def checkpoint_vector() -> dict[str, Any]:
    t = tamper_vector()
    tl, wl = t["ticket_events"][TICKET], t["workspace_events"]
    heads_t, heads_w = t["heads"][TICKET], t["heads"]["workspace"]

    def tcp(seq: int, hd: str, at: str = "2026-10-10T10:03:00Z") -> dict[str, Any]:
        return {"v": 2, "suite": 2, "kind": "ticket_checkpoint", "workspace_id": W, "uid": TICKET, "seq": seq,
                "head": hd, "acc": acc_fold([*heads_t[:seq-1], hd]), "at": at}  # fmt: skip

    def wcp(n: int, ws_seq: int, hd: str, tickets: dict[str, Any]) -> dict[str, Any]:
        tickets = {
            uid: {**entry, "acc": acc_fold([*heads_t[: entry["seq"] - 1], entry["head"]])}
            for uid, entry in tickets.items()
        }
        return {
            "v": 2,
            "suite": 2,
            "kind": "workspace_checkpoint",
            "workspace_id": W,
            "genesis": t["genesis"],
            "n": n,
            "at": "2026-10-10T10:03:30Z",
            "workspace_log": {"seq": ws_seq, "head": hd, "acc": acc_fold([*heads_w[: ws_seq - 1], hd])},
            "tickets": tickets,
        }

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
    held = wcp(2, len(wl), heads_w[-1], {TICKET: {"seq": len(tl), "head": heads_t[-1]}})
    grown = copy.deepcopy(held)
    grown["n"] = 3
    next_head = "sha256:" + "66" * 32
    grown["tickets"][TICKET] = {
        "seq": len(tl) + 1,
        "head": next_head,
        "acc": acc_fold([next_head], held["tickets"][TICKET]["acc"]),
    }
    n_offers = []

    def offer(name, o, expect, **kw):
        n_offers.append({"name": name, "checkpoint": _checkpoint(o), "expect": expect, "now": RECEIVER_NOW, **kw})

    offer("lower_n_same_heads", {**held, "n": 1}, "ignored")
    offer("same_n_same_content", held, "ok")
    offer("same_n_other_content", {**held, "at": "2026-10-10T10:04:00Z"}, "chain.diverged")
    offer("higher_n_same_heads", {**held, "n": 3}, "ok")
    offer("higher_n_lower_at", {**held, "n": 3, "at": "2026-10-10T10:00:00Z"}, "chain.diverged")
    offer("verified_accumulator_growth", grown, "ok", proofs={TICKET: [next_head]})
    offer("missing_growth_proof_is_incomplete", grown, "sync.incomplete")
    offer("spliced_growth_proof_is_incomplete", grown, "sync.incomplete", proofs={TICKET: [heads_t[0]]})
    offer("too_many_heads_is_incomplete", grown, "sync.incomplete", proofs={TICKET: [heads_t[0], next_head]})
    wrong = copy.deepcopy(held)
    wrong["n"] = 3
    wrong["tickets"][TICKET]["acc"] = "sha256:" + "ff" * 32
    offer("equal_height_acc_conflict", wrong, "chain.diverged")
    offer("higher_n_missing_ticket", {**held, "n": 3, "tickets": {}}, "chain.diverged")
    offer("lower_n_higher_height", {**grown, "n": 1}, "chain.diverged")
    # The trust root and shape of a WSK-signed offer (§5.10, §5.11): every signature here verifies.
    offer("signed_offer_other_genesis", {**held, "n": 3, "genesis": "sha256:" + "0e" * 32}, "trust.genesis_mismatch")
    offer("signed_offer_other_workspace", {**held, "n": 3, "workspace_id": OTHER_W}, "chain.diverged")
    offer("signed_first_offer_other_workspace", {**held, "workspace_id": OTHER_W}, "chain.diverged", held=None)
    offer("signed_first_offer_n_zero", {**held, "n": 0}, "chain.diverged", held=None)
    offer("signed_first_offer_accepted", held, "ok", held=None)
    offer("signed_offer_impossible_date", {**held, "n": 3, "at": "2026-02-30T10:00:00Z"}, "chain.diverged")
    offer("signed_offer_out_of_range_time", {**held, "n": 3, "at": "2026-99-99T99:99:99Z"}, "chain.diverged")
    offer("signed_offer_ticket_checkpoint_kind", ticket_o, "chain.diverged")
    for name, o in [
        ("signed_offer_n_true", {**held, "n": True}),
        ("signed_offer_n_string", {**held, "n": "1"}),
        ("signed_offer_version_3", {**held, "n": 3, "v": 3}),
        ("signed_offer_extra_key", {**held, "n": 3, "note": "x"}),
        ("signed_offer_missing_tickets", {k: v for k, v in {**held, "n": 3}.items() if k != "tickets"}),
    ]:
        offer(name, o, "chain.diverged")
    # Growth entries that disagree with their proof.
    skip = copy.deepcopy(grown)
    skip["tickets"][TICKET]["seq"] += 1
    offer("growth_seq_skip_is_incomplete", skip, "sync.incomplete", proofs={TICKET: [next_head]})
    wrong_last = copy.deepcopy(grown)
    wrong_last["tickets"][TICKET]["head"] = "sha256:" + "67" * 32
    offer("growth_wrong_last_head_is_incomplete", wrong_last, "sync.incomplete", proofs={TICKET: [next_head]})
    # The receiver ceiling: at most 300 s ahead of the receiver's clock `now` (C2): 299 and 300 accepted, 301 refused.
    offer(
        "offer_at_clock_ceiling_minus_one",
        {**held, "n": 3, "at": "2026-10-10T10:10:00Z"},
        "ok",
        now="2026-10-10T10:05:01Z",
    )
    offer("offer_at_clock_ceiling", {**held, "n": 3, "at": "2026-10-10T10:10:00Z"}, "ok", now="2026-10-10T10:05:00Z")
    offer(
        "offer_beyond_clock_ceiling",
        {**held, "n": 3, "at": "2026-10-10T10:10:00Z"},
        "checkpoint.future",
        now="2026-10-10T10:04:59Z",
    )
    for x in ticket_offers:
        x["acc"] = acc_fold([*heads_t[: x["seq"] - 1], x["head"]])
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
        "restore (see restore.json); receivers judge workspace checkpoints by n and seq (workspace_n_offers: ok, ignored, "
        "chain.diverged). Receiver inputs (a receiver view; every field is given explicitly, {} or [] meaning none): "
        "the top-level workspace_id, genesis and wsk_pub are the receiver's pins; every offer and transition case gives "
        "the receiver clock `now` (one sampled, trusted clock; a WSK-signed `at` more than 300 s after it is the "
        "retryable checkpoint.future); a workspace_n_offers case with a `held` key (null: an uninitialized receiver) "
        "replaces workspace_n_held; every transition case gives `pinned_delegation` (the workspace's card / genesis "
        "delegation, whose `o` the carried, PK-verified one must equal), `known_devices` (device_id -> person_id and "
        "optional pk_pub: a workspace association, which binds carried entries' identity but never drops or suppresses "
        "held evidence), `known_revocations` (chain-learned: held when judging and authenticated in this workspace's "
        "accepted history; signer refusal and per-pair completeness, no identity constraint on carried entries; entries may "
        "carry `pk_pub`), `local_revocations` (out of band: held when judging, not chain-learned; signer refusal only, "
        "no identity constraint on carried entries, never completeness; never filtered) and `learned_later_in_walk` "
        "(learned only from a higher-n bundle; never refuses; `alarm: true` when it must raise the C3 alarm, otherwise "
        "no alarm). Every held revocation is keyed by (device_id, person_id): it refuses the signer only when it names "
        "the signer's device and person. `unpublished_pairs` is the expected sorted [d_, p_] pairs of "
        "revocation.unpublished for an accepted workspace restore ([] otherwise), `unpublished` their sorted, "
        "de-duplicated d_ hints, and `unpublished_evidence` the index in local_revocations of each pair's reported "
        "entry (its strongest, the first on a tie). "
        "Every refusing transition keeps all signatures valid except the one a case names as its defect.",
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
        "transition_cases": transition_vectors(),
        "workspace_n_held": _checkpoint(held),
        "workspace_n_offers": n_offers,
        "divergence": divergences,
    }


_STRENGTH = {"compromised": 2, "lost": 1, "retired": 1}  # compromised > lost = retired (F1 §5.10)


def _strongest_held(held: list[dict[str, Any]], want: list[str]) -> int:
    """The index of the strongest held entry of the ``[d_device, p_person]`` pair, the first on a tie."""
    idx = [
        i
        for i, x in enumerate(held)
        if ["d_" + x["revocation"]["o"]["device_id"], "p_" + x["revocation"]["o"]["person_id"]] == list(want)
    ]
    return max(idx, key=lambda i: (_STRENGTH[held[i]["revocation"]["o"]["reason"]], -i))


def transition_vectors() -> list[dict[str, Any]]:
    """Full signed source-bound transitions and accumulator proofs, from F1 §5.10."""
    w = World("transition", "source and accumulator")
    genesis = w.genesis_event()
    w.member("mara", "maintainer")
    w.member("lena", "member")
    original = list(w.logs["workspace"])
    heads = [head(e) for e in original]

    def cp(n, branch):
        return _checkpoint(
            {
                "v": 2,
                "suite": 2,
                "kind": "workspace_checkpoint",
                "workspace_id": W,
                "genesis": w.genesis,
                "n": n,
                "at": "2026-10-10T10:00:30Z",
                "workspace_log": {"seq": len(branch), "head": branch[-1], "acc": acc_fold(branch)},
                "tickets": {},
            }
        )

    c = cp(3, heads)
    w.logs["workspace"] = original[:1]
    w.roster_v = 1
    payload = {
        "from_seq": 1,
        "head": heads[0],
        "abandoned": {"seq": 3, "head": heads[-1]},
        "abandoned_decisions": [e["id"] for e in original[1:]],
        "source": checkpoint_ref(c),
        "revocations": [],
        "reason": "rollback",
    }
    e = w.ev("workspace", "restore", w.actor("sev"), payload)
    target = cp(4, [heads[0], head(e)])
    target["o"]["at"] = e["at"]
    target = _checkpoint(target["o"])
    transition = {
        "C": c,
        "T": target,
        "log": "workspace",
        "event": e,
        "device_cert": genesis["device_cert"],
        "pk_pub": genesis["owner"]["pk_pub"],
        "delegation": genesis["delegation"],
        "retained_heads": heads[:1],
        "abandoned_heads": heads,
    }
    proofs = {"workspace": [head(e)]}
    cases = []

    def case(name, expect, tr=None, held=c, proof=proofs, offered=target, applied=()):
        cases.append(
            {
                "name": name,
                "held": held,
                "offered": offered,
                "restore": copy.deepcopy(tr or transition),
                "proofs": proof,
                "applied": list(applied),
                "expect": expect,
                "recorded": e["id"] if expect == "ok" else None,
            }
        )

    case("restore_accumulator_fold", "ok")
    case("relay_requires_every_held_revocation", "chain.diverged")
    cases[-1]["known_revocations"] = [{"revocation": ow.make_revocation("lena", "lena1", "lost")}]
    case("relay_refuses_a_known_revoked_owner_device", "chain.diverged")
    cases[-1]["known_revocations"] = [{"revocation": ow.make_revocation("sev", "sev1", "compromised")}]
    case("source_must_equal_held", "chain.diverged", held=cp(2, heads[:2]))
    case("initialized_receiver_refuses_null_source", "chain.diverged", tr={**transition, "C": None})
    case("uninitialized_receiver_refuses_nonnull_source", "chain.diverged", held=None)
    case("malformed_retained_proof_is_incomplete", "sync.incomplete", tr={**transition, "retained_heads": ["bad"]})
    case("malformed_tail_proof_is_incomplete", "sync.incomplete", proof={"workspace": {"0": head(e)}})
    case("transition_target_is_bound", "chain.diverged", tr={**transition, "T": c})
    case("missing_abandoned_proof", "sync.incomplete", tr={**transition, "abandoned_heads": None})
    case("spliced_abandoned_prefix", "sync.incomplete", tr={**transition, "abandoned_heads": list(reversed(heads))})
    case("restore_not_in_target_proof", "sync.incomplete", proof={"workspace": [heads[-1]]})
    case("restore_single_use", "chain.diverged", applied=[e["id"]])
    bad = copy.deepcopy(transition)
    bad["event"]["reason"] = "other"
    case("full_restore_signature_required", "chain.diverged", tr=bad)
    case(
        "descriptor_is_insufficient",
        "chain.diverged",
        tr={"id": e["id"], "log": "workspace", "from_seq": 1, "abandoned": payload["abandoned"]},
    )
    forged = copy.deepcopy(c)
    forged["sig"] = b64u(bytes(64))
    case("source_signature_required", "chain.diverged", tr={**transition, "C": forged})
    # The source may precede the retained prefix: prove the intervening growth, too.
    w.logs["workspace"] = list(original)
    w.roster_v = 3
    forward_c = cp(1, heads[:1])
    forward = w.ev(
        "workspace",
        "restore",
        w.actor("sev"),
        {
            "from_seq": 3,
            "head": heads[-1],
            "abandoned": None,
            "abandoned_decisions": [],
            "source": checkpoint_ref(forward_c),
            "revocations": [],
            "reason": "other",
        },
    )
    forward_t = cp(2, [*heads, head(forward)])
    forward_t = _checkpoint({**forward_t["o"], "at": forward["at"]})
    forward_tr = {
        **transition,
        "C": forward_c,
        "T": forward_t,
        "event": forward,
        "retained_heads": heads,
        "abandoned_heads": heads[:1],
    }
    cases.append(
        {
            "name": "source_precedes_retained_prefix",
            "held": forward_c,
            "offered": forward_t,
            "restore": forward_tr,
            "proofs": {"workspace": [head(forward)]},
            "applied": [],
            "expect": "ok",
            "recorded": forward["id"],
        }
    )
    cases.append(
        {
            **copy.deepcopy(cases[-1]),
            "name": "forward_proof_cannot_replace_committed_prefix",
            "restore": {**forward_tr, "retained_heads": ["sha256:" + "ef" * 32, *heads[1:]]},
            "expect": "sync.incomplete",
            "recorded": None,
        }
    )
    # P3-D24 authenticates delegation devices; it does not replay owner membership.
    w.logs["workspace"] = list(original)
    w.roster_v = 3
    w.steps = w.steps[:3]
    w.ev("workspace", "role.changed", w.actor("sev"), {"person": pid("mara"), "role": "owner"})
    w.ev("workspace", "role.changed", w.actor("sev"), {"person": pid("sev"), "role": "member"})
    branch = [head(ev) for ev in w.logs["workspace"]]
    demoted_c = cp(5, branch)
    demoted = w.ev(
        "workspace",
        "restore",
        w.actor("sev"),
        {
            **payload,
            "from_seq": len(branch),
            "head": branch[-1],
            "abandoned": None,
            "abandoned_decisions": [],
            "source": checkpoint_ref(demoted_c),
        },
        expect="role.denied",
    )
    demoted_t = _checkpoint({**cp(6, [*branch, head(demoted)])["o"], "at": demoted["at"]})
    cases.append(
        {
            "name": "demoted_delegation_owner_receiver_accepts_host_refuses",
            "held": demoted_c,
            "offered": demoted_t,
            "restore": {
                **transition,
                "C": demoted_c,
                "T": demoted_t,
                "event": demoted,
                "retained_heads": branch,
                "abandoned_heads": branch,
            },
            "proofs": {"workspace": [head(demoted)]},
            "applied": [],
            "expect": "ok",
            "recorded": demoted["id"],
            "host_scenario": copy.deepcopy(w.scenario()),
        }
    )
    for held_reason, listed_reason, expect in [
        ("retired", "lost", "ok"),
        ("lost", "retired", "ok"),
        ("compromised", "lost", "chain.diverged"),
    ]:
        w.logs["workspace"] = original[:1]
        w.roster_v = 1
        entry = {
            "revocation": ow.make_revocation("lena", "lena1", listed_reason),
            "device_cert": ow.make_cert("lena", "lena1"),
            "pk_pub": key("pk_lena").pub_b64u,
        }
        rev_event = w.ev("workspace", "restore", w.actor("sev"), {**payload, "revocations": [entry]})
        rev_target = _checkpoint({**cp(4, [heads[0], head(rev_event)])["o"], "at": rev_event["at"]})
        cases.append(
            {
                "name": "receiver_" + held_reason + "_to_" + listed_reason,
                "held": c,
                "offered": rev_target,
                "restore": {**transition, "event": rev_event, "T": rev_target},
                "proofs": {"workspace": [head(rev_event)]},
                "applied": [],
                "expect": expect,
                "recorded": rev_event["id"] if expect == "ok" else None,
                "known_revocations": [{"revocation": ow.make_revocation("lena", "lena1", held_reason)}],
            }
        )
    w.logs["workspace"] = original[:2]
    w.roster_v = 2
    wrong_head = "sha256:" + "ef" * 32
    substituted = w.ev(
        "workspace",
        "restore",
        w.actor("sev"),
        {
            **payload,
            "from_seq": 2,
            "head": wrong_head,
            "prev": wrong_head,
            "based_on": wrong_head,
        },
        expect="restore.bad_head",
    )
    substituted_t = _checkpoint({**cp(4, [*heads[:2], head(substituted)])["o"], "at": substituted["at"]})
    cases.append(
        {
            "name": "rollback_accumulator_must_bind_retained_head",
            "held": c,
            "offered": substituted_t,
            "restore": {**transition, "T": substituted_t, "event": substituted, "retained_heads": heads[:2]},
            "proofs": {"workspace": [head(substituted)]},
            "applied": [],
            "expect": "sync.incomplete",
            "recorded": None,
        }
    )
    # Without C, the forward proof starts at the pinned genesis, not an arbitrary branch.
    w.logs["workspace"] = list(original)
    w.roster_v = 3
    initial = w.ev(
        "workspace",
        "restore",
        w.actor("sev"),
        {
            **payload,
            "from_seq": 3,
            "head": heads[-1],
            "abandoned": None,
            "abandoned_decisions": [],
            "source": None,
        },
    )
    for name, prefix, expect in [
        ("null_source_with_pinned_genesis", heads, "ok"),
        ("null_source_cannot_substitute_genesis", ["sha256:" + "ef" * 32, *heads[1:]], "sync.incomplete"),
    ]:
        initial_t = _checkpoint({**cp(1, [*prefix, head(initial)])["o"], "at": initial["at"]})
        cases.append(
            {
                "name": name,
                "held": None,
                "offered": initial_t,
                "restore": {
                    **transition,
                    "C": None,
                    "T": initial_t,
                    "event": initial,
                    "retained_heads": prefix,
                    "abandoned_heads": [],
                },
                "proofs": {"workspace": [head(initial)]},
                "applied": [],
                "expect": expect,
                "recorded": initial["id"] if expect == "ok" else None,
            }
        )

    # --- receiver checks, one defect per case; every other signature and binding stays valid (§5.10) ----------
    def variant(name, expect, *, over=None, actor=None, ev_kw=None, tr_over=None, t_at=None, t_head=None,
                proof=None, **extra):  # fmt: skip
        w.logs["workspace"] = original[:1]
        w.roster_v = 1
        ev = w.ev("workspace", "restore", actor or w.actor("sev"), {**payload, **(over or {})}, **(ev_kw or {}))
        hd = t_head or head(ev)
        t_o = _checkpoint({**cp(4, [heads[0], hd])["o"], "at": t_at or ev["at"]})
        tr = {**transition, "event": ev, "T": t_o, "retained_heads": heads[:1], "abandoned_heads": heads}
        cases.append(
            {
                "name": name,
                "held": c,
                "offered": t_o,
                "restore": copy.deepcopy({**tr, **(tr_over or {})}),
                "proofs": proof or {"workspace": [hd]},
                "applied": [],
                "expect": expect,
                "recorded": ev["id"] if expect == "ok" else None,
                **extra,
            }
        )
        return ev

    # The delegation is the one pinned for the workspace (the card's / genesis event's), never the signer's own.
    variant(
        "restore_delegation_of_another_person",
        "chain.diverged",
        actor=w.actor("lena"),
        tr_over={
            "device_cert": ow.make_cert("lena", "lena1"),
            "pk_pub": key("pk_lena").pub_b64u,
            "delegation": ow.make_delegation("lena"),
        },
    )
    # P3-D24 owner-device policy: one defect each.
    variant("owner_policy_person_sig_by_another_device", "chain.diverged", ev_kw={"signer": "dk_sev2"})
    variant("owner_policy_host_sig_by_another_key", "chain.diverged", ev_kw={"host_key": "wsk_other"})
    for name, d in [
        ("owner_policy_delegation_signed_by_another_key", ow.make_delegation("sev", signer="pk_mara")),
        ("owner_policy_delegation_for_another_workspace", ow.make_delegation("sev", workspace_id=OTHER_W)),
        ("owner_policy_delegation_for_another_wsk", ow.make_delegation("sev", wsk="wsk_other")),
        ("owner_policy_delegation_extra_field", ow.make_delegation("sev", extra={"note": "x"})),
    ]:
        # the pin equals the carried delegation here, so only the delegation's own check can refuse it
        variant(name, "chain.diverged", tr_over={"delegation": d}, pinned_delegation=d)
    for name, cert in [
        ("owner_policy_cert_not_signed_by_the_pk", ow.make_cert("sev", "sev1", signer="pk_mara")),
        ("owner_policy_cert_without_operate", ow.make_cert("sev", "sev1", ["look", "decide"])),
        (
            "owner_policy_cert_with_drop_scope",
            ow.make_cert("sev", "sev1", ["drop:" + "ab" * 16], expires_ms=1_900_000_000_000),
        ),
        ("owner_policy_cert_expired_at_line_at", ow.make_cert("sev", "sev1", expires_ms=1_791_000_000_000)),
        ("owner_policy_cert_created_after_line_at", ow.make_cert("sev", "sev1", created_ms=1_800_000_000_000)),
    ]:
        variant(name, "chain.diverged", tr_over={"device_cert": cert})
    variant(
        "owner_policy_cert_valid_at_line_at",
        "ok",
        tr_over={"device_cert": ow.make_cert("sev", "sev1", expires_ms=1_800_000_000_000)},
    )
    variant(
        "owner_policy_actor_id_not_the_delegation_owner",
        "chain.diverged",
        actor={"kind": "person", "id": pid("mara"), "device": did("sev1")},
    )
    variant(
        "owner_policy_actor_device_not_the_cert",
        "chain.diverged",
        actor=w.actor("sev", "sev2"),
        ev_kw={"signer": "dk_sev1"},
    )
    # C equals the held checkpoint, but the owner-signed source names something else.
    variant(
        "signed_source_names_another_checkpoint", "chain.diverged", over={"source": checkpoint_ref(cp(2, heads[:2]))}
    )
    variant(
        "signed_source_other_n_same_hash",
        "chain.diverged",
        over={"source": {**checkpoint_ref(c), "n": c["o"]["n"] - 1}},
    )
    # The line is appended as from_seq + 1 onto the retained head (host-only fields, re-host-signed).
    variant("line_seq_not_from_seq_plus_one", "chain.diverged", ev_kw={"tamper": lambda e: e.update(seq=3)})
    variant("line_prev_not_the_retained_head", "chain.diverged", ev_kw={"tamper": lambda e: e.update(prev=heads[1])})
    # `abandoned` names the highest known entry above from_seq, C's included.
    variant("abandoned_null_below_source_entry", "chain.diverged", over={"abandoned": None})
    variant("abandoned_below_source_entry", "chain.diverged", over={"abandoned": {"seq": 2, "head": heads[1]}})
    variant(
        "abandoned_conflicting_head_at_source_height",
        "chain.diverged",
        over={"abandoned": {"seq": 3, "head": "sha256:" + "cd" * 32}},
    )
    variant("abandoned_above_source_entry", "ok", over={"abandoned": {"seq": 5, "head": "sha256:" + "cd" * 32}})
    # T commits to the restore head: a WSK-signed T over another head, with a proof consistent with that T.
    variant("target_over_another_head", "chain.diverged", t_head="sha256:" + "ab" * 32)
    # C.o.at <= line.at <= T.o.at, equality accepted (C1); the clock ceiling now + 300 s (C2).
    variant("line_at_before_source_at", "chain.diverged", ev_kw={"at": "2026-10-10T10:00:20Z"}, t_at=c["o"]["at"])
    variant("line_at_equals_source_at", "ok", ev_kw={"at": c["o"]["at"]})
    variant(
        "line_at_after_target_at", "chain.diverged", ev_kw={"at": "2026-10-10T11:00:00Z"}, t_at="2026-10-10T10:59:59Z"
    )
    variant("target_at_clock_ceiling", "ok", ev_kw={"at": "2026-10-10T11:00:00Z"}, now="2026-10-10T10:55:00Z")
    variant(
        "target_beyond_clock_ceiling",
        "checkpoint.future",
        ev_kw={"at": "2026-10-10T11:00:00Z"},
        now="2026-10-10T10:54:59Z",
    )

    def rev_entry(person, dev, reason, **kw):
        return {
            "revocation": ow.make_revocation(person, dev, reason, **kw),
            "device_cert": ow.make_cert(person, dev),
            "pk_pub": key("pk_" + person).pub_b64u,
        }

    # Evidence for a device the receiver knows must be of that device's known person and key.
    variant(
        "foreign_evidence_for_a_known_revoked_device",
        "chain.diverged",
        over={"revocations": [rev_entry("mara", "lena1", "compromised")]},
        known_revocations=[
            {"revocation": ow.make_revocation("lena", "lena1", "lost"), "pk_pub": key("pk_lena").pub_b64u}
        ],
    )
    variant(
        "foreign_evidence_for_a_known_revoked_device_person_only",
        "chain.diverged",
        over={"revocations": [rev_entry("mara", "lena1", "compromised")]},
        known_revocations=[{"revocation": ow.make_revocation("lena", "lena1", "lost")}],
    )
    # The same binding for a device the receiver knows as active (known_devices), with no revocation of it known.
    lena_device = {
        ow.device_id(key("dk_lena1").pub): {
            "person_id": ow.person_id(key("pk_lena").pub),
            "pk_pub": key("pk_lena").pub_b64u,
        }
    }
    variant(
        "foreign_evidence_for_a_known_active_device",
        "chain.diverged",
        over={"revocations": [rev_entry("mara", "lena1", "compromised")]},
        known_devices=lena_device,
    )
    variant(
        "own_evidence_for_a_known_active_device",
        "ok",
        over={"revocations": [rev_entry("lena", "lena1", "lost")]},
        known_devices=lena_device,
    )
    # A bundle that revokes its own signer, with no local revocation knowledge: refused for every reason.
    for reason in ("lost", "retired", "compromised"):
        variant(
            "bundle_revokes_its_own_signer_" + reason,
            "chain.diverged",
            over={"revocations": [rev_entry("sev", "sev1", reason)]},
        )
    # C3: a known compromised revocation of the signer refuses at any time, also one issued after line.at.
    variant(
        "signer_known_compromised_listed_issued_after_line_at",
        "chain.diverged",
        over={"revocations": [rev_entry("sev", "sev1", "compromised", revoked_ms=1_900_000_000_000)]},
        known_revocations=[
            {"revocation": ow.make_revocation("sev", "sev1", "compromised", revoked_ms=1_900_000_000_000)}
        ],
    )
    # Re-signed restores that break one structural rule (§5.10 step 0); every signature verifies.
    for name, kw in [
        ("envelope_version", {"over": {"v": 3}}),
        ("hash_version", {"over": {"hash_v": 2}}),
        ("actor_extra_field", {"actor": {**w.actor("sev"), "name": "sev"}}),
        ("timestamp_impossible_date", {"ev_kw": {"at": "2026-02-30T10:00:00Z"}, "t_at": "2026-10-10T11:00:00Z"}),
        ("counter_roster_v_negative", {"ev_kw": {"roster_v": -1}}),
        ("custody_token_unknown", {"over": {"auth": "fingerprint"}}),
        ("abandoned_decision_not_an_id", {"over": {"abandoned_decisions": ["not-a-ulid"]}}),
        ("signature_encoding_malformed", {"ev_kw": {"tamper": lambda e: e.update(sig=e["sig"][:-2])}}),
    ]:
        variant("restore_shape_" + name, "chain.diverged", **kw)
    # The card pin compares the verified `o`: the owner's own, validly signed delegation with other content is refused.
    variant(
        "delegation_other_content_than_the_card",
        "chain.diverged",
        tr_over={"delegation": ow.make_delegation("sev", extra={"issued_ms": 1_790_000_000_001})},
    )
    variant("owner_policy_bundle_pk_not_the_delegations", "chain.diverged", tr_over={"pk_pub": key("pk_mara").pub_b64u})
    variant(
        "signed_source_other_n_higher_same_hash",
        "chain.diverged",
        over={"source": {**checkpoint_ref(c), "n": c["o"]["n"] + 1}},
    )
    # Completeness binds a known person's stored key, too.
    variant(
        "evidence_under_another_stored_key",
        "chain.diverged",
        over={"revocations": [rev_entry("lena", "lena1", "compromised")]},
        known_revocations=[
            {"revocation": ow.make_revocation("lena", "lena1", "lost"), "pk_pub": key("pk_mara").pub_b64u}
        ],
    )
    # Known and listed: the known-signer and bundle-signer rules both apply (each alone: ticket_restore_signer_known_*,
    # bundle_revokes_its_own_signer_*).
    cases[:] = [x for x in cases if x["name"] != "relay_refuses_a_known_revoked_owner_device"]
    variant(
        "relay_refuses_a_known_revoked_owner_device",
        "chain.diverged",
        over={"revocations": [rev_entry("sev", "sev1", "compromised")]},
        known_revocations=[{"revocation": ow.make_revocation("sev", "sev1", "compromised")}],
    )
    variant("target_at_clock_ceiling_minus_one", "ok", ev_kw={"at": "2026-10-10T11:00:00Z"}, now="2026-10-10T10:55:01Z")

    # --- chain-learned vs out-of-band revocations (F1 §5.10 "Revoked signers at receivers", relay §19.8) ---------
    # known_revocations: chain-learned (signer refusal and completeness); local_revocations: out of band (signer
    # refusal and evidence identity only). `unpublished`: the d_ ids an accepted workspace restore does not carry at
    # the locally held strength (revocation.unpublished, non-blocking).
    lena = did("lena1")

    def held_rev(person, dev, reason, **kw):
        return [{"revocation": ow.make_revocation(person, dev, reason, **kw)}]

    variant(
        "out_of_band_non_signer_missing_accepted_unpublished",
        "ok",
        local_revocations=held_rev("lena", "lena1", "lost"),
        unpublished_pairs=[[lena, pid("lena")]],
        note="learned out of band (another workspace, the relay's feed, the owner): never demanded as completeness "
        "evidence; the receiver keeps enforcing it and forwards it for publication",
    )
    variant(
        "chain_learned_non_signer_missing_diverged",
        "chain.diverged",
        known_revocations=held_rev("lena", "lena1", "lost"),
        note="the same revocation, authenticated in this workspace's accepted history: completeness demands it",
    )
    for reason in ("lost", "retired", "compromised"):
        variant(
            "out_of_band_revocation_of_the_signer_refused_" + reason,
            "chain.diverged",
            local_revocations=held_rev("sev", "sev1", reason),
            note="C3 applies to every held revocation, whatever its class or reason",
        )
    variant(
        "revocation_in_both_sets_missing_is_chain_learned",
        "chain.diverged",
        known_revocations=held_rev("lena", "lena1", "lost"),
        local_revocations=held_rev("lena", "lena1", "lost"),
    )
    variant(
        "revocation_in_both_sets_carried_accepted",
        "ok",
        over={"revocations": [rev_entry("lena", "lena1", "lost")]},
        known_revocations=held_rev("lena", "lena1", "lost"),
        local_revocations=held_rev("lena", "lena1", "lost"),
    )
    variant(
        "chain_lost_local_compromised_carried_lost_accepted_unpublished",
        "ok",
        over={"revocations": [rev_entry("lena", "lena1", "lost")]},
        known_revocations=held_rev("lena", "lena1", "lost"),
        local_revocations=held_rev("lena", "lena1", "compromised"),
        unpublished_pairs=[[lena, pid("lena")]],
        note="reasons are tracked separately: the chain-learned lost is carried (complete); the out-of-band "
        "compromised upgrade is not, which is the signal, not a refusal",
    )
    variant(
        "chain_lost_local_compromised_carried_compromised_accepted",
        "ok",
        over={"revocations": [rev_entry("lena", "lena1", "compromised")]},
        known_revocations=held_rev("lena", "lena1", "lost"),
        local_revocations=held_rev("lena", "lena1", "compromised"),
    )
    variant(
        "chain_lost_local_compromised_missing_diverged",
        "chain.diverged",
        known_revocations=held_rev("lena", "lena1", "lost"),
        local_revocations=held_rev("lena", "lena1", "compromised"),
    )
    variant(
        "out_of_band_retired_carried_lost_accepted",
        "ok",
        over={"revocations": [rev_entry("lena", "lena1", "lost")]},
        local_revocations=held_rev("lena", "lena1", "retired"),
        note="lost and retired are equally strong: not weaker, so not unpublished",
    )
    variant(
        "out_of_band_compromised_carried_lost_accepted_unpublished",
        "ok",
        over={"revocations": [rev_entry("lena", "lena1", "lost")]},
        local_revocations=held_rev("lena", "lena1", "compromised"),
        unpublished_pairs=[[lena, pid("lena")]],
    )
    # Held revocations impose no identity constraint on carried entries (only known_devices associations do), and a
    # carried entry of another person never stands in for a held pair (F1 §5.10, per (device, person) pair).
    variant(
        "wrong_person_carriage_never_suppresses_unpublished",
        "ok",
        over={"revocations": [rev_entry("mara", "lena1", "compromised")]},
        local_revocations=[
            {"revocation": ow.make_revocation("lena", "lena1", "lost"), "pk_pub": key("pk_lena").pub_b64u}
        ],
        unpublished_pairs=[[lena, pid("lena")]],
        note="mara's carried entry for lena1 is a different pair: lena's held pair is still reported",
    )
    variant(
        "wrong_person_carriage_never_satisfies_completeness",
        "chain.diverged",
        over={"revocations": [rev_entry("mara", "lena1", "compromised")]},
        known_revocations=held_rev("lena", "lena1", "lost"),
    )
    variant(
        "unpublished_suppressed_for_conflicting_known_association",
        "ok",
        local_revocations=held_rev("mara", "lena1", "compromised"),
        known_devices=lena_device,
        note="known_devices associates lena1 with lena: mara's held pair is not reported (publication eligibility); "
        "it stays retained and enforced by the runtime",
    )
    # The signer is matched by device and person: another person's "revocation" of the signer's device id (a
    # certificate any person can make for any public key) is not a revocation of the signing identity (F1 §5.10).
    variant(
        "cross_person_revocation_of_the_signer_device_not_refused",
        "ok",
        local_revocations=held_rev("mara", "sev1", "compromised"),
        note="mara's PK signs a revocation of sev's device id; known_devices is {}, so the receiver knew no binding "
        "for sev1 before this restore and first-come admission of mara's evidence is reachable: judge_offer must "
        "stay safe on it. It does not refuse the owner's restore, and once accepted the restore's certificate binds "
        "sev1 to sev, so the evidence is dropped: not unpublished either",
    )
    variant(
        "same_person_revocation_of_the_signer_device_refused",
        "chain.diverged",
        local_revocations=held_rev("sev", "sev1", "compromised"),
        note="the genuine counterpart of cross_person_revocation_of_the_signer_device_not_refused",
    )
    # `unpublished` is sorted and de-duplicated: inputs in descending device-id order, one device twice.
    pairs = sorted([("lena", "lena1"), ("mara", "mara1")], key=lambda p: did(p[1]), reverse=True)
    multi = [ow.make_revocation(person, dev, "lost") for person, dev in pairs]
    multi.append(ow.make_revocation(*pairs[0], "retired"))
    variant(
        "out_of_band_several_missing_unpublished_sorted_unique",
        "ok",
        local_revocations=[{"revocation": r} for r in multi],
        unpublished_pairs=sorted([[did("lena1"), pid("lena")], [did("mara1"), pid("mara")]]),
        note="local_revocations in descending device-id order, the first device twice (lost and retired)",
    )
    # Hash-only withholding: C commits to a suffix holding device.revoked(lena1); the bundle proves the suffix with
    # its head only, never its body. Proofs establish ancestry, not disclosure: a receiver that never authenticated
    # that occurrence does not demand it (documented acceptance, F1 §5.10).
    w.logs["workspace"] = list(original)
    w.roster_v = 3
    withheld = w.ev(
        "workspace",
        "device.revoked",
        w.actor("sev"),
        {"device": lena, "reason": "lost", "revocation": ow.make_revocation("lena", "lena1", "lost")},
    )
    suffix_c = cp(5, [*heads, head(withheld)])
    w.logs["workspace"] = list(original)
    hidden = w.ev(
        "workspace",
        "restore",
        w.actor("sev"),
        {
            "from_seq": 3,
            "head": heads[-1],
            "abandoned": {"seq": 4, "head": head(withheld)},
            "abandoned_decisions": [withheld["id"]],
            "source": checkpoint_ref(suffix_c),
            "revocations": [],
            "reason": "rollback",
        },
    )
    hidden_t = _checkpoint({**cp(6, [*heads, head(hidden)])["o"], "at": hidden["at"]})
    for name, expect, extra in [
        ("hash_only_withheld_revocation_not_demanded", "ok", {}),
        (
            "hash_only_withheld_revocation_held_out_of_band",
            "ok",
            {"local_revocations": held_rev("lena", "lena1", "lost"), "unpublished_pairs": [[lena, pid("lena")]]},
        ),
        (
            "hash_only_withheld_revocation_chain_learned_diverged",
            "chain.diverged",
            {"known_revocations": held_rev("lena", "lena1", "lost")},
        ),
    ]:
        cases.append(
            {
                "name": name,
                "held": suffix_c,
                "offered": hidden_t,
                "restore": copy.deepcopy(
                    {
                        **transition,
                        "C": suffix_c,
                        "T": hidden_t,
                        "event": hidden,
                        "retained_heads": heads,
                        "abandoned_heads": [*heads, head(withheld)],
                    }
                ),
                "proofs": {"workspace": [head(hidden)]},
                "applied": [],
                "expect": expect,
                "recorded": hidden["id"] if expect == "ok" else None,
                "withheld_event": copy.deepcopy(withheld),
                "note": "the relay delivers only the head of withheld_event (abandoned_heads); the receiver never "
                "inspected its body, so the revocation is not chain-learned (known limit: the policy preserves only "
                "revocations the receiver established in accepted history)",
                **copy.deepcopy(extra),
            }
        )

    # --- the row-151 fail-open (relay PoC): bindings never drop evidence; signer refusal is per (device, person) ----
    # Bob (mara) certifies the id of Alice's (sev's) laptop sev1 under his own PK and revokes it; that arrives first
    # (feed or a failing bundle: local evidence either way); Alice's genuine compromised revocation arrives next. The
    # stolen sev1 key then signs a restore as sev: the genuine pair is held, so it is refused in every order.
    poison = held_rev("mara", "sev1", "lost")
    genuine = held_rev("sev", "sev1", "compromised")
    variant(
        "poisoned_binding_never_drops_a_genuine_signer_revocation",
        "chain.diverged",
        local_revocations=[*poison, *genuine],
        note="relay PoC: the poisoned (sev1, mara) pair arrived first; the genuine (sev1, sev) pair is retained and "
        "refuses the signer pair",
    )
    variant(
        "genuine_signer_revocation_first_then_poison_refused",
        "chain.diverged",
        local_revocations=[*genuine, *poison],
    )
    variant(
        "failed_bundle_poison_then_chain_learned_genuine_refused",
        "chain.diverged",
        local_revocations=poison,
        known_revocations=genuine,
        note="the poison came from a bundle whose judgment failed (local evidence only); the genuine pair is "
        "chain-learned",
    )
    variant(
        "conflicting_known_device_association_never_suppresses_signer_pair_refusal",
        "chain.diverged",
        local_revocations=genuine,
        known_devices={ow.device_id(key("dk_sev1").pub): {"person_id": ow.person_id(key("pk_mara").pub)}},
        note="an association is not proof of key ownership: known_devices naming mara for sev1 never suppresses "
        "the (sev1, sev) refusal",
    )
    variant(
        "conflicting_known_device_association_never_suppresses_alarm",
        "ok",
        learned_later_in_walk=genuine,
        known_devices={ow.device_id(key("dk_sev1").pub): {"person_id": ow.person_id(key("pk_mara").pub)}},
        alarm=True,
    )
    # Another unrevoked owner device (sev2) signs an honest restore carrying Alice's genuine evidence; Bob's arrived
    # first. Held revocations impose no identity constraint on carried entries, so it is not refused (the DoS is gone).
    variant(
        "honest_restore_by_another_owner_device_carries_genuine_evidence_after_poison",
        "ok",
        actor=w.actor("sev", "sev2"),
        tr_over={"device_cert": ow.make_cert("sev", "sev2")},
        over={"revocations": [rev_entry("sev", "sev1", "compromised")]},
        local_revocations=[*poison, *genuine],
        unpublished_pairs=[[did("sev1"), pid("mara")]],
        note="the genuine pair is carried; mara's pair has no known association here, so it is reported (a hint; "
        "the runtime decides eligibility)",
    )
    # The relay review (F4): sev1 known as sev's; mara's revocation of sev1 neither refuses sev1's restore nor alarms.
    sev1_known = {
        ow.device_id(key("dk_sev1").pub): {
            "person_id": ow.person_id(key("pk_sev").pub),
            "pk_pub": key("pk_sev").pub_b64u,
        }
    }
    variant(
        "known_signer_device_other_person_revocation_not_refused",
        "ok",
        local_revocations=held_rev("mara", "sev1", "compromised"),
        known_devices=sev1_known,
    )
    variant(
        "known_signer_device_other_person_compromised_learned_later_no_alarm",
        "ok",
        learned_later_in_walk=held_rev("mara", "sev1", "compromised"),
        known_devices=sev1_known,
    )
    # Evidence identity binds carried entries to authenticated history: known_devices and chain-learned pairs.
    lena_and_mara = sorted(
        [rev_entry("lena", "lena1", "lost"), rev_entry("mara", "lena1", "compromised")],
        key=lambda x: (x["revocation"]["o"]["device_id"], x["revocation"]["o"]["person_id"]),
    )
    variant(
        "carried_other_person_pair_beside_a_chain_learned_pair_accepted",
        "ok",
        over={"revocations": lena_and_mara},
        known_revocations=held_rev("lena", "lena1", "lost"),
        note="carried-entry identity is checked against known_devices only: a chain-learned (lena1, lena) does not "
        "bind lena1, so mara's extra carried pair is harmless (lena's obligation is carried)",
    )
    variant(
        "carried_other_person_entry_allowed_without_authenticated_identity",
        "ok",
        over={"revocations": lena_and_mara},
        local_revocations=held_rev("lena", "lena1", "lost"),
        note="out-of-band evidence imposes no identity: both pairs may be carried; lena's held pair is carried",
    )
    # Two persons' chain obligations for one device id: the list is unique per (device, person), so both can be carried.
    both_lena1 = [*held_rev("lena", "lena1", "lost"), *held_rev("mara", "lena1", "lost")]
    both_entries = sorted(
        [rev_entry("lena", "lena1", "lost"), rev_entry("mara", "lena1", "lost")],
        key=lambda x: (x["revocation"]["o"]["device_id"], x["revocation"]["o"]["person_id"]),
    )
    variant(
        "conflicting_chain_obligations_both_carried_accepted",
        "ok",
        over={"revocations": both_entries},
        known_revocations=both_lena1,
    )
    variant(
        "conflicting_chain_obligations_one_carried_diverged",
        "chain.diverged",
        over={"revocations": [rev_entry("lena", "lena1", "lost")]},
        known_revocations=both_lena1,
    )

    # Restore 5 after restore 4 (restore.json other_person_evidence_for_a_tombstone_is_a_new_pair): restore 4 carried
    # (zed1, zed) for an unknown device, so a receiver chain-learned it; restore 5 adds mara's pair for zed1.
    variant(
        "tombstone_other_person_pair_in_a_later_restore_accepted",
        "ok",
        over={
            "revocations": sorted(
                [rev_entry("zed", "zed1", "lost"), rev_entry("mara", "zed1", "compromised")],
                key=lambda x: (x["revocation"]["o"]["device_id"], x["revocation"]["o"]["person_id"]),
            )
        },
        known_revocations=held_rev("zed", "zed1", "lost"),
    )
    # The verified person key is part of a pair's identity where the receiver holds it.
    variant(
        "same_pair_other_key_completeness_diverged",
        "chain.diverged",
        over={"revocations": [rev_entry("lena", "lena1", "lost")]},
        known_revocations=[
            {"revocation": ow.make_revocation("lena", "lena1", "lost"), "pk_pub": key("pk_mara").pub_b64u}
        ],
    )
    variant(
        "same_pair_other_key_still_unpublished",
        "ok",
        over={"revocations": [rev_entry("lena", "lena1", "lost")]},
        local_revocations=[
            {"revocation": ow.make_revocation("lena", "lena1", "lost"), "pk_pub": key("pk_mara").pub_b64u}
        ],
        unpublished_pairs=[[lena, pid("lena")]],
    )
    # The reported evidence of a pair is its strongest held entry, not the first or the weakest.
    variant(
        "out_of_band_strongest_held_entry_is_reported",
        "ok",
        local_revocations=[*held_rev("lena", "lena1", "lost"), *held_rev("lena", "lena1", "compromised")],
        unpublished_pairs=[[lena, pid("lena")]],
        note="unpublished_evidence points at the compromised entry (index 1)",
    )
    # Self-revocation is by device id: another person's pair for the signer's device in the carried list refuses too.
    variant(
        "bundle_carries_another_persons_pair_for_its_signer_device",
        "chain.diverged",
        over={"revocations": [rev_entry("mara", "sev1", "lost")]},
        note="the host refuses this earlier (device.cert: sev1 is sev's live device); a log-free receiver refuses it "
        "by the self-revocation rule, which matches the signer's device id whatever the person",
    )

    # --- ticket-log restores: the same workspace checkpoint, ws_seq <= T's workspace seq, revocations [] -------
    tw = World("ticket_transition", "ticket-log restore at a receiver")
    tgen = tw.genesis_event()
    tw.member("mara", "maintainer")
    tw.member("lena", "member")
    tw.ticket()
    tw.ev(TICKET, "log.added", tw.actor("sev"), {"text": "one"})
    tw.ev(TICKET, "log.added", tw.actor("sev"), {"text": "two"})
    ws_heads = [head(x) for x in tw.logs["workspace"]]
    t_events = list(tw.logs[TICKET])
    t_heads = [head(x) for x in t_events]

    def tcp_(n, t_branch, at):
        return _checkpoint(
            {
                "v": 2,
                "suite": 2,
                "kind": "workspace_checkpoint",
                "workspace_id": W,
                "genesis": tw.genesis,
                "n": n,
                "at": at,
                "workspace_log": {"seq": len(ws_heads), "head": ws_heads[-1], "acc": acc_fold(ws_heads)},
                "tickets": {TICKET: {"seq": len(t_branch), "head": t_branch[-1], "acc": acc_fold(t_branch)}},
            }
        )

    tc = tcp_(2, t_heads, ow.stamp(tw.clock))
    t_payload = {
        "from_seq": 1,
        "head": t_heads[0],
        "abandoned": {"seq": len(t_heads), "head": t_heads[-1]},
        "abandoned_decisions": [x["id"] for x in t_events[1:]],
        "source": checkpoint_ref(tc),
        "revocations": [],
        "reason": "rollback",
    }

    def ticket_variant(name, expect, *, over=None, ev_kw=None, t_ws=None, proof=None, **extra):
        tw.logs[TICKET] = t_events[:1]
        r = tw.ev(TICKET, "restore", tw.actor("sev"), {**t_payload, **(over or {})}, **(ev_kw or {}))
        t_o = tcp_(3, [t_heads[0], head(r)], r["at"])
        if t_ws is not None:  # T's workspace entry replaced (re-signed by the WSK)
            t_o = _checkpoint({**t_o["o"], "workspace_log": t_ws})
        cases.append(
            {
                "name": name,
                "held": tc,
                "offered": t_o,
                "restore": {
                    "C": tc,
                    "T": t_o,
                    "log": TICKET,
                    "event": r,
                    "device_cert": tgen["device_cert"],
                    "pk_pub": tgen["owner"]["pk_pub"],
                    "delegation": tgen["delegation"],
                    "retained_heads": t_heads[:1],
                    "abandoned_heads": t_heads,
                },
                "proofs": proof if proof is not None else {TICKET: [head(r)], "workspace": []},
                "applied": [],
                "expect": expect,
                "recorded": r["id"] if expect == "ok" else None,
                "pinned_delegation": tgen["delegation"],
                **extra,
            }
        )

    lena_lost = [{"revocation": ow.make_revocation("lena", "lena1", "lost")}]
    # completeness is a workspace-restore rule: a receiver holding a revocation still accepts a ticket restore's []
    ticket_variant("ticket_restore_accepted_with_held_revocations", "ok", known_revocations=lena_lost)
    ticket_variant(
        "ticket_restore_ws_seq_above_target",
        "chain.diverged",
        ev_kw={"tamper": lambda e: e.update(ws_seq=len(ws_heads) + 1)},
    )
    ticket_variant(
        "ticket_restore_ws_seq_not_positive", "chain.diverged", ev_kw={"tamper": lambda e: e.update(ws_seq=0)}
    )
    ticket_variant(
        "ticket_restore_with_revocations",
        "chain.diverged",
        over={"revocations": [rev_entry("lena", "lena1", "lost")]},
        known_revocations=lena_lost,
    )
    # C3 in the receiver's own order: what it holds when it judges refuses; what it learns later only alarms.
    sev_compromised = [{"revocation": ow.make_revocation("sev", "sev1", "compromised")}]
    ticket_variant(
        "walk_compromised_learned_from_higher_n_bundle_alarm_only",
        "ok",
        learned_later_in_walk=sev_compromised,
        alarm=True,
        note="learned_later_in_walk comes from a higher-n bundle judged after this one: it is not known_revocations "
        "here; the receiver commits this transition and raises an alarm",
    )
    ticket_variant(
        "walk_compromised_learned_from_lower_n_bundle_refuses",
        "chain.diverged",
        known_revocations=sev_compromised,
        note="learned from a lower-n bundle of the same walk, so held when this one is judged",
    )
    ticket_variant(
        "walk_retired_known_before_sync_stays_refused_until_repaired",
        "chain.diverged",
        known_revocations=[{"revocation": ow.make_revocation("sev", "sev1", "retired", revoked_ms=1_700_000_000_000)}],
        note="liveness cost: a genuine older transition by a device the receiver already knows retired stays refused "
        "until an owner pairs the receiver again",
    )
    ticket_variant(
        "non_owner_revocation_held_ticket_restore_accepted",
        "ok",
        known_revocations=[
            {"revocation": ow.make_revocation("lena", "lena1", "compromised"), "pk_pub": key("pk_lena").pub_b64u}
        ],
        note="a ticket restore carries [] and cannot deliver a revocation; a non-owner revocation the receiver holds "
        "neither completes nor blocks it",
    )
    for reason, revoked_ms in [
        ("compromised", 1_900_000_000_000),
        ("compromised", 1_700_000_000_000),
        ("retired", 1_700_000_000_000),
    ]:
        when = "after" if revoked_ms > 1_791_626_400_000 else "before"
        ticket_variant(
            f"ticket_restore_signer_known_{reason}_{when}_line_at",
            "chain.diverged",
            known_revocations=[{"revocation": ow.make_revocation("sev", "sev1", reason, revoked_ms=revoked_ms)}],
        )
    ticket_variant(
        "ticket_restore_out_of_band_revocation_carries_no_unpublished",
        "ok",
        local_revocations=lena_lost,
        note="revocation.unpublished is a workspace-restore signal: a ticket restore carries []",
    )
    ticket_variant("ticket_restore_signer_out_of_band_compromised", "chain.diverged", local_revocations=sev_compromised)
    # C3 alarm filter: only a compromised revocation learned later alarms; a lost (or retired) one changes nothing.
    ticket_variant(
        "cross_person_compromised_learned_later_no_alarm",
        "ok",
        learned_later_in_walk=[{"revocation": ow.make_revocation("mara", "sev1", "compromised")}],
        note="mara's compromised revocation of sev's device id, learned later: matched by device and person like the "
        "refusal, so no alarm (and, held earlier, no refusal either)",
    )
    ticket_variant(
        "walk_lost_learned_from_higher_n_bundle_no_alarm",
        "ok",
        learned_later_in_walk=[{"revocation": ow.make_revocation("sev", "sev1", "lost")}],
        note="learned_later_in_walk holds a lost revocation of the signer: no alarm (only compromised alarms)",
    )
    # Check order: the restore's own tail proof is checked before the other logs' growth. T also conflicts on the
    # workspace head (same seq, another head); with the restore tail missing the verdict is sync.incomplete.
    ws_conflict = {
        "seq": len(ws_heads),
        "head": "sha256:" + "ab" * 32,
        "acc": acc_fold([*ws_heads[:-1], "sha256:" + "ab" * 32]),
    }
    ticket_variant(
        "restore_tail_missing_and_other_log_conflict",
        "sync.incomplete",
        t_ws=ws_conflict,
        proof={"workspace": []},
        note="two defects: the restore's tail proof is removed and T's workspace entry conflicts with the held one; "
        "the restore tail is checked first",
    )
    ticket_variant(
        "other_log_conflict_with_restore_tail_present",
        "chain.diverged",
        t_ws=ws_conflict,
        note="the control of restore_tail_missing_and_other_log_conflict: the same T with the tail proof present",
    )
    # H12d (orch-v2-host §0.8.6 item 9.8, 9.12): the convenience cases for the relay's AUTH_KINDS. A restore line
    # signed with auth "convenience" is accepted; an unknown value is refused, never mapped to a known one. Both
    # re-signed by the owner device, every signature valid; appended last so no earlier case changes.
    variant("restore_auth_convenience_accepted", "ok", over={"auth": "convenience"})
    variant("restore_auth_unknown_refused", "chain.diverged", over={"auth": "bogus"})
    for case_ in cases:
        case_.setdefault("pinned_delegation", genesis["delegation"])
        case_.setdefault("now", RECEIVER_NOW)
        for field in ("known_devices",):
            case_.setdefault(field, {})
        for field in ("known_revocations", "local_revocations", "learned_later_in_walk", "unpublished_pairs"):
            case_.setdefault(field, [])
        # the d_ hints of revocation.unpublished: the sorted, de-duplicated devices of the expected pairs
        case_["unpublished"] = sorted({device for device, _person in case_["unpublished_pairs"]})
        # the evidence reported per pair: the index in local_revocations of its strongest entry, the first on a tie
        case_["unpublished_evidence"] = [
            _strongest_held(case_["local_revocations"], want) for want in case_["unpublished_pairs"]
        ]
        tr = case_["restore"]
        if "retained_heads" not in tr:
            continue
        prefix = tr.pop("retained_heads")
        c_o = tr["C"]["o"] if tr.get("C") else None
        c_entry = (c_o["workspace_log"] if tr["log"] == "workspace" else c_o["tickets"].get(tr["log"])) if c_o else None
        valid = all(isinstance(h, str) and len(h) == 71 for h in prefix)
        tr["acc_from"] = acc_fold(prefix) if valid else "bad"
        tr["acc_before"] = acc_fold(prefix[:-1]) if valid else "bad"
        tr["forward_heads"] = prefix[c_entry["seq"] if c_entry else 0 :]
        abandoned = tr.get("abandoned_heads")
        if isinstance(abandoned, list):
            tr["abandoned_heads"] = abandoned[len(prefix) :]
    # A forward proof bound to a swapped acc_from: the retained prefix no longer folds (C source and null source).
    for name, src in [
        ("forward_proof_acc_from_swapped", "source_precedes_retained_prefix"),
        ("null_source_acc_from_swapped", "null_source_with_pinned_genesis"),
    ]:
        x = copy.deepcopy(next(y for y in cases if y["name"] == src))
        x.update(name=name, expect="sync.incomplete", recorded=None)
        x["restore"]["acc_from"] = acc_fold(["sha256:" + "ef" * 32])
        cases.append(x)
    return cases


def restore_scenarios() -> list[dict[str, Any]]:
    """F1 §5.10 B1, §5.3 recovery, §5.11 precedence. Expected outcomes follow the text only."""

    def evidence(person="lena", dev="lena1", reason="lost", **kwargs):
        return {
            "revocation": ow.make_revocation(person, dev, reason, **kwargs),
            "device_cert": ow.make_cert(person, dev),
            "pk_pub": key("pk_" + person).pub_b64u,
        }

    def setup(name):
        w = World(name, "F1 §5.10: owner-signed atomic revocations, retained-prefix authorization")
        w.genesis_event()
        w.member("mara", "maintainer")
        w.member("lena", "member")
        w.ticket()
        return w

    def restore(w, entries, **kw):
        payload = {
            "from_seq": w.ws_len(),
            "head": head(w.logs["workspace"][-1]),
            "abandoned": None,
            "abandoned_decisions": [],
            "reason": "rollback",
            "source": None,
            "revocations": sorted(
                entries, key=lambda x: (x["revocation"]["o"]["device_id"], x["revocation"]["o"]["person_id"])
            ),
        }
        payload.update(kw.pop("payload", {}))
        return w.ev("workspace", "restore", kw.pop("actor", w.actor("sev")), payload, **kw)

    out = []
    w = setup("restore_carries_revocations_at_its_ws_seq")
    restore(w, [evidence()], after={"workspace": {"devices_revoked": [did("lena1")]}})
    w.ev(TICKET, "log.added", w.actor("lena"), {"text": "revoked now"}, expect="device.invalid")
    w.ev(TICKET, "log.added", w.actor("mara"), {"text": "eligible at restore seq"})
    out.append(w.scenario())

    for name, old_reason, entry, expect in [
        ("identical_present_revocation_has_no_effect", "lost", evidence(), "ok"),
        ("identical_compromised_revocation_has_no_effect", "compromised", evidence(reason="compromised"), "ok"),
        ("same_reason_other_bytes_refused", "lost", evidence(revoked_ms=1_790_000_100_001), "device.exists"),
        ("lost_upgrades_to_compromised", "lost", evidence(reason="compromised"), "ok"),
        ("lost_cannot_replace_compromised", "compromised", evidence(), "device.exists"),
        ("retired_cannot_replace_lost", "lost", evidence(reason="retired"), "device.exists"),
    ]:
        w = setup(name)
        r = evidence(reason=old_reason)["revocation"]
        w.ev("workspace", "device.revoked", w.HOST, {"device": did("lena1"), "reason": old_reason, "revocation": r})
        restore(
            w, [entry], expect=expect, after={"tickets": {TICKET: {"gens": [1, 1, 1, 0]}}} if expect == "ok" else None
        )
        out.append(w.scenario())

    # A known device's evidence must be of its person and key. A tombstone is evidence, not a workspace association:
    # another person's evidence for its device id is a separate (device, person) pair, retained next to it (§5.10).
    w = setup("foreign_person_evidence_known_device")
    restore(w, [evidence("mara", "lena1", "compromised")], expect="device.cert")
    out.append(w.scenario())
    w = setup("other_person_evidence_for_a_tombstone_is_a_new_pair")
    restore(w, [evidence("zed", "zed1")])
    restore(w, [evidence("mara", "zed1", "compromised"), evidence("zed", "zed1")])
    out.append(w.scenario())
    # One restore may carry two persons' evidence for one device id, sorted by (device_id, person_id): both retained.
    w = setup("two_persons_evidence_for_one_unknown_device")
    restore(w, [evidence("zed", "zed1"), evidence("mara", "zed1", "compromised")])
    out.append(w.scenario())
    # The strength rule compares a pair with its own earlier evidence only: a tombstone's compromised (zed1, zed) does
    # not make another person's weaker (zed1, mara, lost) a "weaker entry" (device.exists).
    w = setup("tombstone_compromised_then_other_person_lost_is_a_new_pair")
    restore(w, [evidence("zed", "zed1", "compromised")])
    restore(w, [evidence("zed", "zed1", "compromised"), evidence("mara", "zed1", "lost")])
    out.append(w.scenario())

    w = setup("all_evidence_precedes_any_B1_comparison")
    entries = sorted([evidence(), evidence("mara", "mara1")], key=lambda x: x["revocation"]["o"]["device_id"])
    restore(w, [entries[0]])
    entries[0] = copy.deepcopy(entries[0])
    entries[0]["revocation"]["o"]["reason"] = "retired"
    person = "lena" if entries[0]["revocation"]["o"]["device_id"] == did("lena1")[2:] else "mara"
    entries[0] = evidence(person, person + "1", "retired")
    entries[1]["revocation"]["sig"] = b64u(bytes(64))
    restore(w, entries, expect="device.cert")
    out.append(w.scenario())

    w = setup("fresh_host_revocation_and_plain_upgrade")
    for reason, expect in [
        ("lost", "ok"),
        ("lost", "device.exists"),
        ("compromised", "ok"),
        ("retired", "device.exists"),
    ]:
        w.ev(
            "workspace",
            "device.revoked",
            w.HOST,
            {"device": did("lena1"), "reason": reason, "revocation": evidence(reason=reason)["revocation"]},
            expect=expect,
        )
    out.append(w.scenario())

    w = setup("abandoned_only_person_gets_tombstone")
    restore(w, [evidence("zed", "zed1")])
    w.member("zed", "member", expect="device.exists")
    out.append(w.scenario())

    w = setup("plain_revocation_upgrades_an_abandoned_only_tombstone")
    restore(w, [evidence("zed", "zed1")])
    for reason, expect in [("lost", "device.exists"), ("compromised", "ok"), ("retired", "device.exists")]:
        w.ev(
            "workspace",
            "device.revoked",
            w.HOST,
            {"device": did("zed1"), "reason": reason, "revocation": evidence("zed", "zed1", reason)["revocation"]},
            expect=expect,
        )
    w.member("zed", "member", expect="device.exists")
    out.append(w.scenario())

    w = setup("abandoned_only_device_cannot_be_added")
    restore(w, [evidence("lena", "lena2")])
    w.ev(
        "workspace",
        "device.added",
        w.actor("lena"),
        {"device": did("lena2"), "cert": ow.make_cert("lena", "lena2")},
        expect="device.exists",
    )
    out.append(w.scenario())

    w = setup("recovery_checks_tombstones")
    restore(w, [evidence(), evidence("lena", "lena2")])
    w.ev(
        "workspace",
        "device.added",
        w.actor("lena", "lena2"),
        {"device": did("lena2"), "cert": ow.make_cert("lena", "lena2")},
        expect="device.exists",
    )
    out.append(w.scenario())

    w = World("sole_owner_recovery_after_host_revocation", "§5.3: fresh H delivery and PK-vouched recovery")
    w.genesis_event()
    w.ev(
        "workspace",
        "device.revoked",
        w.HOST,
        {"device": did("sev1"), "reason": "lost", "revocation": evidence("sev", "sev1")["revocation"]},
    )
    w.ev(
        "workspace",
        "device.added",
        w.actor("sev", "sev2"),
        {"device": did("sev2"), "cert": ow.make_cert("sev", "sev2")},
    )
    out.append(w.scenario())

    w = setup("expired_and_look_only_evidence_records_revocation")
    entry = evidence("zed", "zed1")
    entry["device_cert"] = ow.make_cert("zed", "zed1", ["look"], expires_ms=1_790_000_001_000)
    restore(w, [entry])
    out.append(w.scenario())

    w = setup("all_entries_validated_before_effects_and_signature_precedence")
    entries = sorted([evidence(), evidence("mara", "mara1")], key=lambda x: x["revocation"]["o"]["device_id"])
    entries[-1]["revocation"]["sig"] = b64u(bytes(64))
    restore(w, entries, expect="device.cert", after=None)
    restore(w, entries, expect="role.denied", actor=w.actor("mara"))
    restore(w, entries, expect="sig.invalid", signer="dk_evil")
    w.ev(TICKET, "log.added", w.actor("lena"), {"text": "invalid last entry had no effects"})
    w.ev(TICKET, "log.added", w.actor("mara"), {"text": "both devices remain valid"})
    out.append(w.scenario())

    w = setup("owner_authorized_before_self_revocation")
    restore(w, [evidence("sev", "sev1")])
    w.ev(TICKET, "log.added", w.actor("sev"), {"text": "after self revocation"}, expect="device.invalid")
    out.append(w.scenario())
    # Replay of an existing line authorizes against the pre-event state, so a self-revocation stays valid on replay for
    # every reason; the host refuses it before the append (host_cases self_revocation_refused_before_append) and
    # receivers refuse the bundle (checkpoint.json bundle_revokes_its_own_signer_*).
    for reason in ("retired", "compromised"):
        w = setup("owner_authorized_before_self_revocation_" + reason)
        restore(w, [evidence("sev", "sev1", reason)])
        w.ev(TICKET, "log.added", w.actor("sev"), {"text": "after self revocation"}, expect="device.invalid")
        out.append(w.scenario())

    w = setup("workspace_restore_blocks_abandoned_decision")
    gone = w.member("zed", "member")
    w.logs["workspace"].pop()
    w.steps.pop()
    w.roster_v -= 1
    restore(w, [], payload={"abandoned_decisions": [gone["id"]]})
    w.raw("workspace", gone, expect="event.duplicate_id")
    out.append(w.scenario())
    from .oracle_f1_flow import Sim

    sim = Sim("restore_raises_then_compromised_effects_once", "F1 §5.10: post-raise voids and grant termination")
    sim.ready()
    approval = sim.decide("approve", "requirements")
    sim.decide("approve", "plan")
    sim.ws_restore(revocations=[evidence("sev", "sev1", "compromised")])
    sim.row(["requirements", "plan", "verify"])
    sim.invalidated("requirements", "restored", [approval["id"]])
    sim.w.ev(TICKET, "log.added", sim.agent, {"text": "compromised grant"}, expect="grant.invalid")
    sim.w.ev(TICKET, "log.added", sim.w.actor("mara"), {"text": "eligible at restore position"})
    out.append(sim.scenario())
    return out


def refusal_order_scenarios() -> list[dict[str, Any]]:
    """§5.11 "Refusal order": events that break two rules at once get the code of the earlier step."""
    w = base_world("refusal_order_type_step", "an event of the other log's type is refused at the type step, after the envelope and the log "
                   "(on replay a bad signature breaks the chain first, so no vector mixes the two)")  # fmt: skip
    mara = w.actor("mara")
    w.ev(TICKET, "settings.changed", mara, {"set": {"grant_hours": 4}}, expect="event.unknown_type",
         note="a workspace event in a ticket log; the signature is fine", schema_refused=True)  # fmt: skip
    w.ev("workspace", "log.added", mara, {"text": "x"}, expect="event.unknown_type",
         note="a ticket event in the workspace log", schema_refused=True)  # fmt: skip
    w.ev("01J9ZK4QXXXXXXXXXXXXXXXXXX", "settings.changed", mara, {"set": {"grant_hours": 4}}, expect="ticket.unknown",
         note="the log step (ticket.unknown) comes before the type step", schema_refused=True)  # fmt: skip
    w.ev(TICKET, "log.added", mara, {"text": "ok"}, note="an honest event is accepted")
    out = [w.scenario()]
    w = base_world(
        "settings_repo_refusals",
        "F1 step 0: malformed repo fields refuse append and break replay at the line; decision 94 aliases refuse settings",
    )
    for field, value in [
        ("remote", "https://HOST/a"),
        ("remote", "https://git@host/a"),
        *[
            ("default_branch", b)
            for b in (
                "bad..branch",
                "-x",
                "HEAD",
                "@",
                "refs/heads/main",
                "a\u202eb",
                "a\u0085b",
                "a\u200db",
                "a\u00a0b",
                "a\u1680b",
                "a\u2000b",
                "a\u2028b",
                "a\u2029b",
                "a\u202fb",
                "a\u205fb",
                "a\u3000b",
                "a\u0378b",
                "a\U0001e6c0b",
                *[
                    "a" + chr(cp) + "b"
                    for cp in (
                        0xFE00,
                        0xFE0F,
                        0xE0100,
                        0xE01EF,
                        0x034F,
                        0x115F,
                        0x1160,
                        0x3164,
                        0xFFA0,
                        0x180B,
                        0x180C,
                        0x180D,
                        0x180E,
                        0x180F,
                        0x17B4,
                        0x17B5,
                        0x2800,
                        0xE000,
                        0xF0000,
                        0x100000,
                    )
                ],
                "\u0301branch",
                "\u0903branch",  # Mc with combining class zero is still a leading mark
                "\u20ddbranch",  # Me
                "\U00010d69branch",  # Unicode 16.0 mark
                "feat/\u0301branch",  # Mn at a later component
                "feat/\u0903branch",  # Mc
                "feat/part/\u20ddbranch",  # Me at the third component
                "feat/\U00010d69branch",  # Unicode 16.0 mark
            )
        ],
    ]:
        w.ev(
            "workspace",
            "settings.changed",
            w.actor("sev"),
            {"set": {"repos": {"a": {"path": "../a", field: value}}}},
            expect="validation.event",
            schema_refused=True,
        )
        w.steps[-1]["replay_expect"] = "chain.broken"
    w.ev(
        "workspace",
        "settings.changed",
        w.actor("sev"),
        {"set": {"repos": {"a": {"path": "../a", "remote": "local:b"}}}},
        expect="settings.invalid",
    )
    w.ev(
        "workspace",
        "settings.changed",
        w.actor("sev"),
        {"set": {"repos": {"a": {"path": "../a", "remote": "https://h/x"}}}},
    )
    w.ev(
        "workspace",
        "settings.changed",
        w.actor("sev"),
        {"set": {"repos": {"b": {"path": "../b", "remote": "https://h/X"}}}},
        expect="settings.invalid",
    )
    w.ev(
        "workspace",
        "settings.changed",
        w.actor("sev"),
        {"set": {"repos": {"a": None, "b": {"path": "../b", "remote": "https://h/X"}}}},
    )
    for repos in (
        {"c": {"path": "../same"}, "d": {"path": "../same"}},
        {"c": {"path": "../same"}, "d": {"path": "../other/../same/./"}},
        {"c": {"path": "../Same"}, "d": {"path": "../same"}},
        {"c": {"path": "../Street"}, "d": {"path": "../STREET"}},
        {"c": {"path": "../K"}, "d": {"path": "../k"}},  # normalized Kelvin sign; repo_paths.json pins NFC
        {"c": {"path": "//same"}, "d": {"path": "/same"}},
        {"c": {"path": "../c"}, "C": {"path": "../d"}},
        {"c": {"path": "../B/./"}},
        {"B": {"path": "../other"}},
    ):
        w.ev(
            "workspace",
            "settings.changed",
            w.actor("sev"),
            {"set": {"repos": repos}},
            expect="validation.event",
            schema_refused=len(repos) == 2,
            note="decision 94: duplicate normalized paths or case-insensitive names, including unchanged repos",
        )
        w.steps[-1]["replay_expect"] = "chain.broken"
    w.ev(
        "workspace",
        "settings.changed",
        w.actor("sev"),
        {"set": {"repos": {"b": None, "B": {"path": "../b"}}}},
        note="removing the old entry allows its name and path to be reused",
    )
    w.ev(
        "workspace",
        "settings.changed",
        w.actor("sev"),
        {
            "set": {
                "repos": {
                    "c": {"path": "../Stra\u00dfe"},
                    "d": {"path": "../STRASSE"},
                    "e": {"path": "../\ua7cb"},
                    "f": {"path": "../\u0264"},
                }
            }
        },
        note="only ASCII case folds: non-ASCII case pairs remain distinct on every runtime",
    )
    out.append(w.scenario())
    return out


def restore_contract_vectors() -> dict[str, Any]:
    w = World("restore_structure", "F1 §5.10 step 0 and atomic line limit")
    w.genesis_event()
    event = w.ev(
        "workspace",
        "restore",
        w.actor("sev"),
        {
            "from_seq": 1,
            "head": w.genesis,
            "abandoned": None,
            "abandoned_decisions": [],
            "reason": "rollback",
            "source": None,
            "revocations": [],
        },
    )
    entry = {
        "revocation": ow.make_revocation("lena", "lena1", "lost"),
        "device_cert": ow.make_cert("lena", "lena1"),
        "pk_pub": key("pk_lena").pub_b64u,
    }
    cases = []

    def add(name, updates, log="workspace", sig=None, mutated=None):
        e = {**copy.deepcopy(event), **updates}
        e["sig"] = sig or b64u(ow.sign("dk_sev1", ow.person_bytes(W, log, e)))
        e["host_sig"] = b64u(ow.sign("wsk", ow.host_bytes(W, log, e)))
        mutated = mutated or sorted(k for k in e if k not in ("sig", "host_sig") and e[k] != event.get(k, object()))
        cases.append(
            {
                "name": name,
                "log": log,
                "event": e,
                "mutated": mutated,
                "expect": "validation.event",
                "replay": "chain.broken",
            }
        )

    add("closed_reason", {"reason": "backup restored"})
    add("note_byte_limit", {"note": "\u00e9" * 141})
    add("note_single_line", {"note": "first\nsecond"})
    add("duplicate_device", {"revocations": [entry, entry]})
    second = {
        "revocation": ow.make_revocation("mara", "mara1", "lost"),
        "device_cert": ow.make_cert("mara", "mara1"),
        "pk_pub": key("pk_mara").pub_b64u,
    }
    add(
        "unsorted_devices",
        {"revocations": sorted([entry, second], key=lambda x: x["revocation"]["o"]["device_id"], reverse=True)},
    )
    # Unique and sorted per (device_id, person_id): two persons' entries for one device are allowed, in person order.
    other_person = {
        "revocation": ow.make_revocation("mara", "lena1", "lost"),
        "device_cert": ow.make_cert("mara", "lena1"),
        "pk_pub": key("pk_mara").pub_b64u,
    }
    add(
        "unsorted_persons_of_one_device",
        {"revocations": sorted([entry, other_person], key=lambda x: x["revocation"]["o"]["person_id"], reverse=True)},
    )
    add("ticket_requires_empty_revocations", {"revocations": [entry], "ws_seq": 1}, TICKET)
    add("malformed_wins_over_signature", {"reason": "bad", "sig": b64u(bytes(64))})
    cases[-1]["event"]["sig"] = b64u(bytes(64))
    # Re-signed (person sig and host_sig both verify over the changed bytes): the structure alone refuses them.
    add("envelope_version", {"v": 3})
    add("hash_version", {"hash_v": 2})
    add("actor_shape_extra_field", {"actor": {**event["actor"], "name": "sev"}})
    add("actor_shape_host_kind", {"actor": {"kind": "host"}})
    add("timestamp_impossible_date", {"at": "2026-02-30T10:00:00Z"})
    add("timestamp_offset_form", {"at": "2026-10-10T10:00:10+00:00"})
    add("counter_seq_not_from_seq_plus_one", {"seq": 3})
    add("counter_roster_v_negative", {"roster_v": -1})
    add("custody_token_unknown", {"auth": "fingerprint"})
    add("abandoned_decision_not_an_id", {"abandoned_decisions": ["not-a-ulid"]})
    add("abandoned_decision_repeated", {"abandoned_decisions": [ow.ulid(900), ow.ulid(900)]})
    add("ticket_ws_seq_not_positive", {"ws_seq": 0}, TICKET)
    add("ticket_ws_seq_missing", {}, TICKET, mutated=["ws_seq"])
    add("signature_encoding_short", {}, sig=event["sig"][:-2])
    add("signature_encoding_padded", {}, sig=event["sig"] + "==")
    add("oversized_restore_is_one_atomic_event", {"abandoned_decisions": [ow.ulid(100_000 + i) for i in range(18100)]})
    assert len(ow.cj(cases[-1]["event"])) + 1 > 524288
    return {
        "schema_cases": cases,
        "host_cases": [
            {"name": "omitting_present_revocation", "missing": "present", "expect": "validation.restore"},
            {"name": "omitting_abandoned_revocation", "missing": "abandoned", "expect": "validation.restore"},
            {
                "name": "crash_after_restore_commit",
                "fault": "after_commit",
                "expect": "revocation_and_transition_retained",
            },
            {
                "name": "crash_preserves_new_restore_evidence",
                "fault": "after_commit",
                "learned": True,
                "expect": "revocation_and_transition_retained",
            },
            {"name": "crash_before_restore_commit", "fault": "before_commit", "expect": "no_accepted_restore"},
            {"name": "exact_restore_retry", "expect": "prior_result"},
            {"name": "stale_source", "expect": "chain.diverged"},
            {
                "name": "self_revocation_refused_before_append",
                "self_revocation": True,
                "expect": "validation.restore",
                "note": "the host refuses a restore listing its own signer before the append; replay of an existing "
                "such line keeps pre-event authorization (scenarios owner_authorized_before_self_revocation*)",
            },
            *(
                {
                    "name": "restore_signer_revoked_in_durable_evidence_" + reason,
                    "signer_revoked": reason,
                    "expect": "device.invalid",
                    "note": "the signer's revocation sits only in the given-up part: the kept chain authorizes the "
                    "restore, the host's durable revocation evidence refuses it before the append, whatever the reason "
                    "(F1 5.10 host rule, C3); a host-side append check, so it is not a model scenario",
                }
                for reason in ("lost", "retired", "compromised")
            ),
        ],
    }


# --- reader_kat_1.json (orch-v2-host §0.8.6 item 9.2, H12d) ---------------------------------------------------------

READER_VIEWS_1 = ("ticket.get.questions.answer",)  # owner decision with orch-f1, #463 round 1
KAT_AUTHS = ("convenience", "passphrase", "bogus")  # corpus order: accepted, accepted, refused with auth null


def kat_digest(results: list[dict[str, Any]]) -> str:
    """F1 §11.5: ``"sha256:" + hex(sha256(cj(results)))``, results in corpus order."""
    import hashlib

    return "sha256:" + hashlib.sha256(ow.cj(results)).hexdigest()


def _kat_expect(auth: str) -> tuple[bool, str | None]:
    return (True, auth) if auth != "bogus" else (False, None)


def _resign(w: World, e: dict[str, Any], auth: str) -> dict[str, Any]:
    """The same event with another ``auth``, person-signed again by its own device and host-signed again: every
    case is independently valid, so ``bogus`` fails only on ``auth``, never on a signature."""
    out = copy.deepcopy(e)
    out["auth"] = auth
    out.pop("sig")
    out.pop("host_sig")
    dev = "dk_" + ow._dev_name(out["actor"]["device"])
    out["sig"] = b64u(ow.sign(dev, ow.person_bytes(w.workspace_id, "workspace", out)))
    out["host_sig"] = b64u(ow.sign(w.wsk, ow.host_bytes(w.workspace_id, "workspace", out)))
    return out


def reader_kat_vector() -> dict[str, Any]:
    w = World("reader_kat_1", "reader KAT, log variant")
    w.genesis_event()
    w.member("mara", "maintainer")
    fixture = copy.deepcopy(w.logs["workspace"])
    base = w.ev("workspace", "settings.changed", w.actor("sev"), {"set": {"grant_hours": 4}}, expect="fixture")
    log_cases, log_results = [], []
    for auth in KAT_AUTHS:
        e = _resign(w, base, auth)
        accepted, got = _kat_expect(auth)
        result = {"head": head(e), "accepted": accepted, "auth": got}
        log_cases.append({"name": auth, "event": e, "expect": result})
        log_results.append(result)
    context = {"workspace_id": W, "genesis": w.genesis, "log": "workspace", "now": ow.stamp(w.clock + 60)}

    sev, dev = pid("sev"), did("sev1")
    at = ow.stamp(ow.T0 + 600)
    mara = pid("mara")
    views = {  # a ticket.get Question (orch-v2-host §0.6), answered; the signer is on its answer
        "ticket.get.questions.answer": {
            "id": "Q1", "state": "answered", "to": [sev], "blocking": True, "answered_by": sev, "answered_seq": 3,
            "text": "Ship the reader handshake first?", "options": [{"key": "a", "label": "yes"}, {"key": "b", "label": "no"}],
            "asked_at": ow.stamp(ow.T0 + 300), "asked_by": {"kind": "person", "id": mara, "device": did("mara1")},
            "asked_seq": 2,
            "answer": {"by": sev, "at": at, "seq": 3, "option": "a", "text": None},
        },
    }  # fmt: skip
    view_cases, view_results = [], []
    for view in READER_VIEWS_1:
        for auth in KAT_AUTHS:
            accepted, got = _kat_expect(auth)
            result = {"view": view, "accepted": accepted, "auth": got}
            obj = copy.deepcopy(views[view])
            obj["answer"]["signer"] = {"device": dev, "auth": auth}
            view_cases.append({"name": f"{view}/{auth}", "view": view, "object": obj, "expect": result})
            view_results.append(result)
    expected = {"1": {"log": kat_digest(log_results), "view": kat_digest(view_results)}}
    return {
        "pins": "the reader known-answer test reader_kat_1 (orch-v2-host §0.8.6 item 9.2; F1 §11.5): per surface, "
        "three cases that differ only in auth (convenience, passphrase, bogus), in this corpus order; a reader runs its "
        "PRODUCTION parser over each case and records a result; the expected results are accepted, accepted, refused "
        "with auth null (an unknown auth is refused, never mapped to a known one); the digest of a surface is "
        "'sha256:' + hex(sha256(cj(results))), results in corpus order; `expected` is what a level-1 declaration "
        "carries in kat['1'].",
        "note": KEY_NOTE,
        "digest": '"sha256:" + hex(sha256(cj(results))), results in corpus order (F1 §11.5)',
        "log": {
            "how": "for each case independently: the fixture lines (seq 1..k) then the case event (seq k+1), each "
            "serialised as cj(event) + LF, read with the production event parser (strict line parse and schema) and "
            "replayed with the production verifier under `context` (workspace_id and genesis pinned, log `workspace`, "
            "the clock `now`); accepted = the case event is read, verified and authorized (no chain error, no invalid "
            "event); result = {head: head(case event), accepted, auth: the event's auth if accepted else null}",
            "context": context,
            "fixture": fixture,
            "cases": log_cases,
            "results": log_results,
            "digest": expected["1"]["log"],
        },
        "view": {
            "how": "for each case: the production view parser reads `object` as an entry of view `view` "
            "(READER_VIEWS_1: a ticket.get Question whose answer carries signer); accepted = it parses and "
            "answer.signer.auth is a known auth value; result = {view, "
            "accepted, auth: answer.signer.auth if accepted else null}",
            "views": list(READER_VIEWS_1),
            "cases": view_cases,
            "results": view_results,
            "digest": expected["1"]["view"],
        },
        "expected": expected,
    }


# --- phone_events.json (orch-v2-host §11A.14, H12e) ---------------------------------------------------------

PHONE_KEYS = ["wsk", "pk_sev", "dk_sev1", "dk_sevp1", "pk_mara", "dk_mara1", "pk_lena", "dk_lena1"]
TICKET3 = "01J9ZK4Q7M3R8T2V6X0B5N1C9F"
PHONE_EXCLUDED = ("seq", "at", "prev", "ws_seq", "host_sig")


def phone_ulid(n: int, epoch_s: int) -> str:
    """A ULID as the phone makes it (14.1): 48-bit ms time from its clock, 80 bits that are random on a phone and
    derived from ``n`` here, so the file is reproducible."""
    import hashlib

    rand = int.from_bytes(hashlib.sha256(b"orch-f1-test-phone-id|" + str(n).encode()).digest()[:10], "big")
    v = (epoch_s * 1000 << 80) | rand
    return "".join(ow.B32[(v >> (5 * (25 - i))) & 31] for i in range(26))


def question_hash_input(uid: str, question: dict[str, Any]) -> dict[str, Any]:
    """14.5 and 5.6.1, independently: qid and the question hash over the uid. ``options`` is ``[]`` for a kind without
    options; ``spec`` is every question key but the routing keys (id, to, blocking) and text/options."""
    import hashlib

    qid = hashlib.sha256(
        b"orch/v2/question-id|" + ow.cj({"question": question["id"], "ticket": uid, "workspace_id": W})
    ).hexdigest()[:32]
    spec = {k: v for k, v in question.items() if k not in ("id", "to", "blocking", "text", "options")}
    body = {
        "options": list(question.get("options", [])),
        "question_id": qid,
        "spec": spec,
        "text": question["text"],
        "ticket": uid,
    }
    return {
        "qid": qid,
        "hash": "sha256:" + hashlib.sha256(b"orch/v2/question|" + ow.cj(body)).hexdigest(),
        "hashed": body,
    }


def _high_s(sig: str) -> str:
    import base64

    raw = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    s = int.from_bytes(raw[32:], "big")
    return b64u(raw[:32] + max(s, ow.N - s).to_bytes(32, "big"))


def _low_s(sig: str) -> str:
    import base64

    raw = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    s = int.from_bytes(raw[32:], "big")
    return b64u(raw[:32] + min(s, ow.N - s).to_bytes(32, "big"))


def phone_events_vector() -> dict[str, Any]:
    import hashlib

    sevp = "sevp1"
    n = [0]

    def pid_(w: World) -> str:  # a fresh phone id with the ULID time of the next `at`
        n[0] += 1
        return phone_ulid(n[0], w.clock + 10)

    def phone(w: World) -> dict[str, Any]:
        return w.actor("sev", sevp)

    def setup(name: str, pins: str) -> World:
        w = base_world(name, pins)
        w.ev("workspace", "device.added", w.actor("sev"), _dev_added("sev", sevp, ["look", "decide"]),
             note="sev1 adds sev's phone sevp1, certified for look and decide only")  # fmt: skip
        return w

    def ask(w: World, qn: str, text: str, options: list[dict[str, str]] | None, *, note: str, **extra: Any):
        q: dict[str, Any] = {"id": qn, "to": "ticket_owner", "text": text, "blocking": True, **extra}
        q["kind"] = "choice" if options is not None else "text"
        if options is not None:
            q["options"] = options
        hi = question_hash_input(TICKET, q)
        w.ev(
            TICKET, "question.asked", w.actor("mara"), {"question": q, "qid": hi["qid"], "hash": hi["hash"]}, note=note
        )
        return {**hi, "raw": q}

    accepted: list[dict[str, Any]] = []

    def pin(name: str, scenario: str, w: World, e: dict[str, Any], desc: str, question: dict[str, Any] | None = None):
        log = w.steps[-1]["log"]
        submitted = {k: v for k, v in e.items() if k not in PHONE_EXCLUDED}
        signing = ow.person_bytes(W, log, e)
        case = {
            "name": name,
            "desc": desc,
            "scenario": scenario,
            "step": len(w.steps) - 1,
            "log": log,
            "submitted": submitted,
            "submitted_cj": ow.cj(submitted).decode("utf-8"),
            "person_signing_hex": signing.hex(),
            "digest": "sha256:" + hashlib.sha256(signing).hexdigest(),
            "sig": e["sig"],
            "event": e,
            "head": head(e),
        }
        if question is not None:
            case["question"] = question
        accepted.append(case)

    refused: list[dict[str, Any]] = []

    def refuse(name: str, scenario: str, w: World, desc: str, also: str | None = None) -> None:
        step = w.steps[-1]
        case = {"name": name, "desc": desc, "scenario": scenario, "step": len(w.steps) - 1, "code": step["expect"],
                "log": step["log"], "submitted": {k: v for k, v in step["event"].items() if k not in PHONE_EXCLUDED}}  # fmt: skip
        if also:
            case["see_also"] = also
        refused.append(case)

    scenarios = []

    # 1. accepted answers and a creation, then the refusals
    s1 = "phone_answers_and_creates"
    w = setup(s1, "a phone (look, decide) answers questions and creates a ticket; refusals break one rule each")
    q1_text = "Welcher Tarif-Export gilt? Gr\u00f6\u00dfe \u2264 5 MB \u2014 Z\u00fcrich \u00e9t\u00e9"
    q1_opts = [{"key": "csv", "label": "Monatliche CSV-Datei (\u00bd Tag)", "cost": "\u00bd Tag, CHF 400"},
               {"key": "api", "label": "Tarif-API \u2713"}]  # fmt: skip
    q1 = ask(w, "Q1", q1_text, q1_opts, note="a non-ASCII question; the first option has a cost", why="Seeds")
    e = w.ev(TICKET, "question.answered", phone(w), {"question": "Q1", "hash": q1["hash"], "option": "csv"},
             auth="secure-enclave-unlocked", eid=pid_(w), note="the phone answers with the costed option")  # fmt: skip
    pin(
        "answer_non_ascii_option_with_cost",
        s1,
        w,
        e,
        "secure-enclave-unlocked; non-ASCII text and labels; an option with cost",
        q1,
    )
    q2_text = "Anything else we should know?"
    q2 = ask(w, "Q2", q2_text, None, note="a text question (no options): the hash uses options []")
    e = w.ev(TICKET, "question.answered", phone(w), {"question": "Q2", "hash": q2["hash"], "text": "Keep\tthe old seeds"},
             auth="secure-enclave-unlocked", eid=pid_(w), note="a text answer with a TAB")  # fmt: skip
    pin(
        "answer_text_to_options_absent_with_tab",
        s1,
        w,
        e,
        "a text answer (with a TAB) to a text question",
        q2,
    )
    q3_opts = [{"key": "yes", "label": "Yes"}, {"key": "no", "label": "No"}]
    q3 = ask(w, "Q3", "Ship it?", q3_opts, note="a plain question")
    e = w.ev(TICKET, "question.answered", phone(w), {"question": "Q3", "hash": q3["hash"], "option": "yes"},
             auth="passphrase", eid=pid_(w), note="a Simulator/dev build: passphrase custody, honestly declared")  # fmt: skip
    pin("answer_passphrase", s1, w, e, "auth passphrase from the phone device (Simulator/dev custody, 14.3)", q3)
    q4 = ask(
        w,
        "Q4",
        "Which day?",
        [{"key": "mon", "label": "Monday"}, {"key": "tue", "label": "Tuesday"}],
        note="another question",
    )
    high = w.ev(TICKET, "question.answered", phone(w), {"question": "Q4", "hash": q4["hash"], "option": "tue"},
                auth="secure-enclave-unlocked", eid=pid_(w), tamper=lambda x: x.update(sig=_high_s(x["sig"])),
                note="a high-s signature (s > n/2): accepted, F1 never normalises s")  # fmt: skip
    pin("answer_high_s", s1, w, high, "the signature's s is in the upper half; it verifies and is accepted", q4)
    e = w.ev(TICKET2, "ticket.created", phone(w),
             {"key": "DEMO-0002", "ticket_type": "feature", "title": "Check the tariff seeds", "owner": pid("sev")},
             auth="secure-enclave-unlocked", eid=pid_(w), note="the phone creates DEMO-0002: based_on null, seq 1")  # fmt: skip
    pin(
        "creation",
        s1,
        w,
        e,
        "ticket.created by the phone: title-only, owner the signer, the exact next key, based_on null",
    )
    q5 = ask(
        w,
        "Q5",
        "Which region?",
        [{"key": "ch", "label": "Switzerland"}, {"key": "de", "label": "Germany"}],
        note="an open question",
    )
    old = question_hash_input(TICKET, {**q5["raw"], "text": "Which country?"})
    w.ev(TICKET, "question.answered", phone(w), {"question": "Q5", "hash": old["hash"], "option": "ch"},
         auth="secure-enclave-unlocked", eid=pid_(w), expect="question.stale", note="the hash of content the phone saw before a re-ask")  # fmt: skip
    refuse("question_stale", s1, w, "the signed hash is not the question's current hash")
    w.ev(TICKET, "question.answered", phone(w), {"question": "Q5", "hash": q5["hash"], "option": "ch"},
         auth="secure-enclave-unlocked", eid=pid_(w), roster_v=w.roster_v - 1, expect="members.stale",
         note="an older roster version")  # fmt: skip
    refuse("members_stale", s1, w, "roster_v is not the current member-list version (C-auto: re-sign with members_v)")
    w.ev(TICKET, "question.answered", phone(w), {"question": "Q5", "hash": q5["hash"], "option": "ch"},
         auth="secure-enclave-unlocked", eid=pid_(w), based_on="sha256:" + "ab" * 32, expect="event.bad_base",
         note="based_on is no head of this log")  # fmt: skip
    refuse("bad_base", s1, w, "based_on is not the head of an earlier event of the log")
    w.ev(TICKET, "question.answered", phone(w), {"question": "Q5", "hash": q5["hash"], "option": "ch"},
         auth="secure-enclave-unlocked", eid=pid_(w), signer="dk_sev1", expect="sig.invalid",
         note="signed by sev1 while actor.device names the phone")  # fmt: skip
    refuse("sig_invalid", s1, w, "the signature does not verify under the phone's dk_sig_pub")
    w.ev("workspace", "settings.changed", phone(w), {"set": {"grant_hours": 4}}, auth="secure-enclave-unlocked",
         eid=pid_(w), expect="device.scope", note="an operate event: the phone's certificate has no operate")  # fmt: skip
    refuse("device_scope", s1, w, "the phone's certificate (look, decide) lacks operate")
    dup = copy.deepcopy(high)
    dup["sig"] = _low_s(high["sig"])
    w.raw(TICKET, dup, expect="event.duplicate_id", note="the high-s answer again with its low-s signature: same id")
    refuse(
        "duplicate_id",
        s1,
        w,
        "the same id twice; the content is identical, only the signature bytes differ (dedup is by content, never by signature)",
    )
    w.ev(TICKET3, "ticket.created", phone(w),
         {"key": "DEMO-0001", "ticket_type": "bug", "title": "Another", "owner": pid("sev")},
         auth="secure-enclave-unlocked", eid=pid_(w), expect="ticket.exists",
         note="a taken key at model level (the host answers ticket.key_not_next before the model, 14.6 L5)")  # fmt: skip
    refuse(
        "taken_key",
        s1,
        w,
        "the model refuses a taken key with ticket.exists; on relay.submit L5 answers ticket.key_not_next first",
    )
    scenarios.append(w.scenario())

    # 2. semantics pins: a routing-only re-ask keeps the answer; A -> B -> A, then a delayed A answer is accepted
    s2 = "phone_answer_semantics"
    w = setup(s2, "everything but routing is bound: a blocking-only re-ask keeps the answer; A -> B -> A clears it, and a "
              "delayed answer to A (signed before B, based_on the head it reviewed) is then accepted")  # fmt: skip
    opts = [{"key": "a", "label": "Alpha"}, {"key": "b", "label": "Beta"}]
    qa = ask(w, "Q1", "Pick one", opts, note="Q1 asked (content A)", why="first reason")
    e = w.ev(TICKET, "question.answered", phone(w), {"question": "Q1", "hash": qa["hash"], "option": "a"},
             auth="secure-enclave-unlocked", eid=pid_(w), note="the phone answers A")  # fmt: skip
    pin("answer_before_routing_reask", s2, w, e, "an answer that survives a blocking-only re-ask", qa)
    q_re = {**qa["raw"], "blocking": False}
    w.ev(TICKET, "question.asked", w.actor("mara"), {"question": q_re, **{k: qa[k] for k in ("qid", "hash")}},
         note="re-ask with only blocking changed (routing): same hash, the answer stands")  # fmt: skip
    w.steps[-1]["after"] = {"tickets": {TICKET: {"answers": {"Q1": e["id"]}}}}
    qa2 = ask(
        w,
        "Q2",
        "Go or wait?",
        [{"key": "go", "label": "Go"}, {"key": "wait", "label": "Wait"}],
        note="Q2 asked (content A)",
    )
    reviewed = head(w.logs[TICKET][-1])
    delayed_id = pid_(w)  # the phone signs now (the reviewed head is based_on); the send is delayed
    qb = ask(w, "Q2", "Go, wait or stop?", [{"key": "go", "label": "Go"}, {"key": "wait", "label": "Wait"},
             {"key": "stop", "label": "Stop"}], note="Q2 re-asked (content B)")  # fmt: skip
    assert qb["hash"] != qa2["hash"]
    ask(
        w,
        "Q2",
        "Go or wait?",
        [{"key": "go", "label": "Go"}, {"key": "wait", "label": "Wait"}],
        note="Q2 re-asked (content A again)",
    )
    e = w.ev(TICKET, "question.answered", phone(w), {"question": "Q2", "hash": qa2["hash"], "option": "go"},
             auth="secure-enclave-unlocked", eid=delayed_id, based_on=reviewed,
             note="the delayed answer to A arrives: based_on the earlier reviewed head; accepted (F1 semantics)")  # fmt: skip
    w.steps[-1]["after"] = {"tickets": {TICKET: {"answers": {"Q1": accepted[-1]["event"]["id"], "Q2": e["id"]}}}}
    pin(
        "delayed_answer_after_a_b_a",
        s2,
        w,
        e,
        "A -> B -> A, then the answer to A signed before B: accepted; based_on is an earlier head",
        qa2,
    )
    scenarios.append(w.scenario())

    # 3. an abandoned phone answer replayed after a restore (cross-reference questions.json)
    s3 = "phone_abandoned_answer_replayed"
    w = setup(s3, "a restore abandons a phone answer (abandoned_decisions); the same signed event appended again is "
              "event.duplicate_id (on relay.submit L3 answers event.abandoned first)")  # fmt: skip
    q = ask(w, "Q1", "Pick one", opts, note="Q1 asked")
    asked = w.logs[TICKET][-1]
    answered = w.ev(TICKET, "question.answered", phone(w), {"question": "Q1", "hash": q["hash"], "option": "b"},
                    auth="secure-enclave-unlocked", eid=pid_(w), note="(the phone answer that the rollback gives up)")  # fmt: skip
    gone = w.logs[TICKET].pop()
    w.steps.pop()
    w.ev(TICKET, "restore", w.actor("sev"),
         {"from_seq": asked["seq"], "head": head(asked), "abandoned": {"seq": answered["seq"], "head": head(gone)},
          "abandoned_decisions": [answered["id"]], "reason": "rollback"}, note="the owner restores to the question")  # fmt: skip
    w.raw(TICKET, answered, expect="event.duplicate_id", note="the abandoned phone answer appended again")
    refuse(
        "abandoned_replayed",
        s3,
        w,
        "an abandoned phone answer replayed: same id, signature and based_on",
        also="questions.json:question_replayed_decision_after_restore",
    )
    scenarios.append({**w.scenario(), "abandoned_answer": gone})

    return {
        "pins": "phone person events over relay.submit (orch-v2-host 11A.14): question.answered and ticket.created "
        "signed by a phone (dk_sevp1, scopes look+decide) under signed-event contract 1; per accepted case the "
        "submitted form (no seq, at, prev, ws_seq, host_sig), the signing bytes, the signed-content digest, sig, the "
        "appended event and its head; refusals; semantics pins (blocking-only re-ask, A -> B -> A)",
        "note": KEY_NOTE,
        "keys": {nm: {"scalar_hex": f"{key(nm).d:064x}", "pub_b64u": key(nm).pub_b64u} for nm in PHONE_KEYS}
        | {"id_of": {"p_sev": pid("sev"), "d_sev1": did("sev1"), "d_sevp1": did(sevp), "p_mara": pid("mara")}},
        "phone": {
            "key": "dk_sevp1",
            "person": pid("sev"),
            "device": did(sevp),
            "scopes_max": ["look", "decide"],
            "added_by": "device.added signed by dk_sev1",
            "column_r_auth": ["secure-enclave-unlocked", "passphrase"],
        },  # fmt: skip
        "workspace_id": W,
        "digest": '"sha256:" + hex(sha256(person_signing_bytes)), where person_signing_bytes = '
        '"orch/v2/sig/ticket-event|" || cj({"contract":1, "event":E, "log":uid, "suite":2, "workspace_id":W}) and E '
        "is the submitted form without sig (11A.14 14.2, 14.4)",
        "accepted": accepted,
        "refused": refused,
        "scenarios": scenarios,
    }
