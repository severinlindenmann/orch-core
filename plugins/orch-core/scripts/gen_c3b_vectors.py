#!/usr/bin/env python3
"""Generate ``tests/hub/vectors/c3b_ledger.json``: the C3b amendment's vectors (ticket-format §15).

Run from ``plugins/orch-core``: ``uv run python scripts/gen_c3b_vectors.py``. Never edit the output by hand;
``tests/hub/test_c3b_vectors.py`` checks it (line bytes, signatures, and every replay case against the reference
ledger in that test).

All keys are the FAKE hub-vector keys (``tests/hub/vectors/vectors_hub.json``, seeds ``FAKE orch v2 test vector:
hub/<name>``). ECDSA signatures are random, so a re-run gives other bytes for the same content; the checks are
semantic, and the committed file is the one the other implementations use.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orch.canon import hashing  # noqa: E402
from orch.canon.jcs import dumps as cj  # noqa: E402
from orch.crypto import suite as S  # noqa: E402
from orch.crypto.encoding import b64u  # noqa: E402
from orch.hubclient import custodial as C  # noqa: E402
from orch.hubclient import wire  # noqa: E402
from tests.hub.vec import V, private_key, pub  # noqa: E402

OUT = ROOT / "tests" / "hub" / "vectors" / "c3b_ledger.json"
SIG_HOST_EVENT = hashing.LABELS["sig_host_event"].encode()

CASE = next(c for c in V["cases"] if c["name"] == "custodial_valid")["args"]
WS, LOG = CASE["workspace_id"], CASE["log"]
HUB, OP1, OP2 = V["ids"]["hub"], V["ids"]["op_cert"], V["ids"]["op_cert_next"]
ALICE, BOB, PHONE = V["ids"]["user_alice"], V["ids"]["user_bob"], V["ids"]["channel_alice_phone"]
PERSON_ALICE = V["ids"]["person_alice"]
T0 = 1_790_000_000_000  # the hub vectors' now_ms


def signed(o: dict[str, Any], label: bytes, key: str) -> dict[str, Any]:
    return {"o": o, "sig": b64u(S.sign(private_key(key), label + cj(o)))}


def resign_obj(obj: dict[str, Any], changes: dict[str, Any], label: bytes, key: str) -> dict[str, Any]:
    o = copy.deepcopy(obj["o"])
    o.update(changes)
    return signed(o, label, key)


def snapshot(version: int, *, roles: dict[str, str], revoked: list[str], op: str = OP1, key: str = "hk_op") -> Any:
    base = V["objects"]["snapshot_a_v3"]["o"]
    o = {**copy.deepcopy(base), "version": version, "roles": roles, "revoked_channels": revoked, "op_cert_id": op}
    o["issued_ms"] = base["issued_ms"] + version * 1000
    return signed(o, wire.SIG_SNAPSHOT, key)


def mint(changes: dict[str, Any] | None = None, *, uk: str = "uk_alice", channel: str = "channel_alice_phone") -> Any:
    """The hub vectors' valid custodial event with ``changes``, both signatures redone (contract 8.2)."""
    e = copy.deepcopy(CASE["event"])
    e.update(changes or {})
    e.pop("sig", None)
    e.pop("channel_proof", None)
    e["channel_proof"] = b64u(S.sign(private_key(channel), C.SIG_CHANNEL_PROOF + C.event_digest(WS, LOG, e)))
    e["sig"] = b64u(S.sign(private_key(uk), C.custodial_label(e["type"]) + C._context(WS, LOG, e)))  # noqa: SLF001
    return e


def host_line(event: dict[str, Any], log: str) -> dict[str, Any]:
    """``host_sig`` (ticket-format 5.5) under the workspace key ``wsk_a``; returns the line vector."""
    ctx = cj({"contract": 1, "suite": 2, "workspace_id": WS, "log": log, "event": event})
    full = {**event, "host_sig": b64u(S.sign(private_key("wsk_a"), SIG_HOST_EVENT + ctx))}
    return {
        "log": log,
        "event": full,
        "host_signed_hex": (SIG_HOST_EVENT + ctx).hex(),
        "line_hex": hashing.event_line(full).hex(),
        "head": hashing.event_head(full),
    }


def main() -> None:
    card1 = V["objects"]["card"]
    # rotation: op cert 2 (op_seq 2, HK_op_next) under the root, and the card under it
    op2 = resign_obj(
        card1["op_cert"],
        {"cert_id": OP2, "op_seq": 2, "hk_op_pub": b64u(pub("hk_op_next")), "issued_ms": T0 + 10_000},
        wire.SIG_OP_CERT,
        "hk_root",
    )
    c2o = {**copy.deepcopy(card1["o"]), "issued_ms": T0 + 10_000}
    card2 = {"o": c2o, "op_cert": op2, "sig": b64u(S.sign(private_key("hk_op_next"), wire.SIG_CARD + cj(c2o)))}
    uk2 = resign_obj(CASE["event"]["uk_cert"], {"op_cert_id": OP2}, wire.SIG_UK_CERT, "hk_op_next")
    ch2 = resign_obj(CASE["event"]["channel_cert"], {"op_cert_id": OP2}, wire.SIG_CHANNEL_CERT, "hk_op_next")
    roles = {ALICE: "admin", BOB: "read"}
    objects = {
        "card_1": card1,
        "card_2_rotated": card2,
        "card_rogue_root": {**card1, "o": {**card1["o"]}},  # replaced below: a card under another root
        "op_revoked_1": V["objects"]["op_revoked"],
        "snap_v2": snapshot(2, roles=roles, revoked=[]),
        "snap_v3": V["objects"]["snapshot_a_v3"],
        "snap_v3_other": snapshot(3, roles={ALICE: "edit", BOB: "read"}, revoked=[]),
        "snap_v4_revokes_phone": snapshot(4, roles=roles, revoked=[PHONE]),
        "snap_v5_drops_revocation": snapshot(5, roles=roles, revoked=[]),
        "snap_v4_drops_alice": snapshot(4, roles={BOB: "read"}, revoked=[]),
        "snap_v4_alice_edit": snapshot(4, roles={ALICE: "edit", BOB: "read"}, revoked=[]),
        "snap_v4_under_op2": snapshot(4, roles=roles, revoked=[], op=OP2, key="hk_op_next"),
    }
    # a card whose op cert and card are signed by the rogue root/op: chain to another root
    rog_op = resign_obj(
        card1["op_cert"],
        {"hub_id": V["ids"]["hub_rogue"], "hk_op_pub": b64u(pub("hk_op_rogue"))},
        wire.SIG_OP_CERT,
        "hk_root_rogue",
    )
    rog_o = {**copy.deepcopy(card1["o"]), "hub_id": V["ids"]["hub_rogue"], "hk_root_pub": b64u(pub("hk_root_rogue"))}
    objects["card_rogue_root"] = {
        "o": rog_o,
        "op_cert": rog_op,
        "sig": b64u(S.sign(private_key("hk_op_rogue"), wire.SIG_CARD + cj(rog_o))),
    }

    e1 = CASE["event"]  # id ...EVT1, snapshot_version 3, gate.approved on TKT1
    events = {
        "e1": e1,
        "e2": mint({"id": "01J9ZP0000000000000000EVT2"}),
        "e1_other_bytes": mint({"hash": "sha256:" + "ab" * 32}),  # same id EVT1, other content
        "e_snap4": mint({"id": "01J9ZP0000000000000000EVT3", "snapshot_version": 4}),
        "e_op2": mint({"id": "01J9ZP0000000000000000EVT4", "uk_cert": uk2, "channel_cert": ch2}),
        # selective: the item hash inside the signed payload (section 15.7); refused by C3's code until C3b
        "e_selective_item": mint({"id": "01J9ZP0000000000000000EVT6", "item_hash": "sha256:" + "5e" * 32}),
    }

    # the log lines: one ledger line and one custodial line, with their host fields and host_sig
    pin_line = host_line(
        {
            "v": 2,
            "id": "01J9ZP0000000000000000PIN1",
            "seq": 12,
            "at": "2026-09-21T14:13:19Z",
            "type": "hub.pinned",
            "actor": {"kind": "host"},
            "based_on": "sha256:" + "11" * 32,
            "prev": "sha256:" + "11" * 32,
            "hash_v": 1,
            "hub_id": HUB,
            "object": objects["snap_v3"],
        },  # fmt: skip
        "workspace",
    )
    cus_line = host_line(
        {**copy.deepcopy(e1), "seq": 5, "at": "2026-09-21T14:13:20Z", "prev": e1["based_on"], "ws_seq": 12},
        LOG,
    )

    full = {"mode": "full", "joined": HUB, "aliases": {}}
    replay = [
        {
            "name": "valid_full_mode",
            "hub": full,
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 1000, "expect": "ok"},
            ],
        },
        {
            "name": "later_revoked_op_cert_still_replays",
            "note": "e1 was valid at its position; the revocation pinned after it refuses only later events",
            "hub": full,
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 1000, "expect": "ok"},
                {"pin": "op_revoked_1", "at_ms": T0 + 2000, "expect": "ok"},
                {"custodial": "e2", "at_ms": T0 + 3000, "expect": "revoked"},
            ],
        },
        {
            "name": "snapshot_never_pinned_is_refused",
            "hub": full,
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e_snap4", "at_ms": T0 + 1000, "expect": "unknown_snapshot"},
            ],
        },
        {
            "name": "no_snapshot_pinned_at_all",
            "hub": full,
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 1000, "expect": "unknown_snapshot"},
            ],
        },
        {
            "name": "no_card_pinned",
            "hub": full,
            "steps": [{"custodial": "e1", "at_ms": T0 + 1000, "expect": "no_card"}],
        },
        {
            "name": "older_event_obeys_newest_pinned_revocations",
            "note": "8.3 step 7: the newest snapshot pinned at the event's position applies, not snapshot_version",
            "hub": full,
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 1000, "expect": "ok"},
                {"pin": "snap_v4_revokes_phone", "at_ms": T0 + 2000, "expect": "ok"},
                {"custodial": "e2", "at_ms": T0 + 3000, "expect": "channel_revoked"},
            ],
        },
        {
            "name": "channel_revocation_is_permanent_in_the_ledger",
            "note": "a later snapshot without the channel does not bring it back (union over pinned snapshots)",
            "hub": full,
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v4_revokes_phone", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v5_drops_revocation", "at_ms": T0 + 1000, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 2000, "expect": "channel_revoked"},
            ],
        },
        {
            "name": "role_removed_later",
            "hub": full,
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 1000, "expect": "ok"},
                {"pin": "snap_v4_drops_alice", "at_ms": T0 + 2000, "expect": "ok"},
                {"custodial": "e2", "at_ms": T0 + 3000, "expect": "forbidden_role"},
            ],
        },
        {
            "name": "op_rotation_old_certs_stop_new_ones_work",
            "hub": full,
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 1000, "expect": "ok"},
                {"pin": "card_2_rotated", "at_ms": T0 + 20_000, "expect": "ok"},
                # the old certs are checked under the NEW op key first (section 2.2 order): bad_signature
                {"custodial": "e2", "at_ms": T0 + 21_000, "expect": "bad_signature"},
                {"pin": "snap_v4_under_op2", "at_ms": T0 + 22_000, "expect": "ok"},
                {"custodial": "e_op2", "at_ms": T0 + 23_000, "expect": "ok"},
            ],
        },
        {
            "name": "certificate_time_is_the_event_at_not_the_reader_clock",
            "note": "the uk cert expires at 1792592000000; e1 at T0+1000 replays ok whatever the reader's clock",
            "hub": full,
            "replay_now_ms": 1_900_000_000_000,
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 1000, "expect": "ok"},
                {"custodial": "e2", "at_ms": 1_792_592_000_000, "expect": "cert_expired"},
            ],
        },
        {
            "name": "ledger_is_monotone_and_bounded",
            "hub": full,
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "card_1", "at_ms": T0, "expect": "already_pinned"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "already_pinned"},
                {"pin": "snap_v2", "at_ms": T0, "expect": "stale_snapshot"},
                {"pin": "snap_v3_other", "at_ms": T0, "expect": "snapshot_conflict"},
                {"pin": "op_revoked_1", "at_ms": T0, "expect": "ok"},
                {"pin": "op_revoked_1", "at_ms": T0, "expect": "already_pinned"},
                {"pin": "snap_v4_alice_edit", "at_ms": T0, "expect": "revoked"},
            ],
        },
        {
            "name": "snapshot_before_any_card",
            "hub": full,
            "steps": [{"pin": "snap_v3", "at_ms": T0, "expect": "no_card"}],
        },
        {
            "name": "card_under_another_root",
            "hub": full,
            "steps": [{"pin": "card_rogue_root", "at_ms": T0, "expect": "other_hub"}],
        },
        {
            "name": "standalone_workspace_refuses_everything_hub",
            "note": "D79: no hub.joined -> no ledger line and no custodial line is admitted, and no hub code runs",
            "hub": {"mode": None, "joined": None, "aliases": {}},
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "not_joined"},
                {"custodial": "e1", "at_ms": T0 + 1000, "expect": "not_joined"},
            ],
        },
        {
            "name": "duplicate_delivery",
            "note": "same id, same signed bytes: idempotent ack, no second line; same id, other bytes: replay",
            "hub": full,
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 1000, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 2000, "expect": "duplicate"},
                {"custodial": "e1_other_bytes", "at_ms": T0 + 3000, "expect": "replay"},
            ],
        },
        {
            "name": "user_id_aliased_under_another_hub",
            "note": "L2P4-C1: hub H may not mint a user id that another hub's alias book names",
            "hub": {
                "mode": "full",
                "joined": HUB,
                "aliases": {V["ids"]["hub_rogue"]: {PERSON_ALICE: ALICE}},
            },
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 1000, "expect": "foreign_user"},
            ],
        },
        {
            "name": "unaliased_user_id_first_recorded_under_another_hub",
            "note": "L2-C3-2: hub A's user was never aliased but owned a ticket; after A is left hub B mints the id",
            "hub": {"mode": "full", "joined": HUB, "aliases": {}, "user_hub": {ALICE: V["ids"]["hub_rogue"]}},
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 1000, "expect": "foreign_user"},
            ],
        },
        {
            "name": "user_id_first_recorded_under_this_hub",
            "hub": {"mode": "full", "joined": HUB, "aliases": {}, "user_hub": {ALICE: HUB}},
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 1000, "expect": "ok", "actor": "u_" + ALICE},
            ],
        },
        {
            "name": "aliased_member_removed_locally_has_no_role",
            "note": "C3b-1: an aliased u_ whose p_ left the roster has no role; never the hub role",
            "hub": {"mode": "full", "joined": HUB, "aliases": {HUB: {PERSON_ALICE: ALICE}}, "members": {}},
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 1000, "expect": "forbidden_role"},
            ],
        },
        {
            "name": "aliased_admin_local_member_is_member",
            "note": "C3b-1: full mode, aliased: the lower of the local role and the mapped hub role (admin -> owner)",
            "hub": {
                "mode": "full",
                "joined": HUB,
                "aliases": {HUB: {PERSON_ALICE: ALICE}},
                "members": {"p_" + PERSON_ALICE: "member"},
            },
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 1000, "expect": "ok", "role": "member"},
            ],
        },
        {
            "name": "channel_revoked_voids_open_decisions_only",
            "note": "C3b-2: the pin that first revokes the channel voids its decisions in open generations only",
            "hub": full,
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 1000, "expect": "ok"},
                {"consume": True, "expect": "ok"},
                {"custodial": "e2", "at_ms": T0 + 2000, "expect": "ok"},
                {
                    "pin": "snap_v4_revokes_phone",
                    "at_ms": T0 + 3000,
                    "expect": "ok",
                    "voids": ["01J9ZP0000000000000000EVT2"],
                },
            ],
        },
        {
            "name": "refused_events_leave_no_pins",
            "note": "C3b-4: pins and event are one commit; 100 refused deliveries that each needed a pin add 0 lines",
            "hub": full,
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok", "ledger_lines": 2},
                {
                    "deliver": "e_snap4",
                    "holds": "snap_v4_drops_alice",
                    "repeat": 100,
                    "at_ms": T0 + 1000,
                    "expect": "forbidden_role",
                    "ledger_lines": 2,
                },
                {
                    "deliver": "e_snap4",
                    "holds": "snap_v4_alice_edit",
                    "at_ms": T0 + 2000,
                    "expect": "ok",
                    "ledger_lines": 3,
                },
            ],
        },
        {
            "name": "join_aliasing_an_id_first_recorded_unaliased_under_another_hub",
            "note": "C3b-5: hub.joined may not alias an id another hub's user used unaliased",
            "hub": {"mode": "full", "joined": HUB, "aliases": {}, "user_hub": {ALICE: V["ids"]["hub_rogue"]}},
            "steps": [{"join": {PERSON_ALICE: ALICE}, "expect": "foreign_user"}],
        },
        {
            "name": "join_aliasing_same_person_on_both_hubs",
            "hub": {
                "mode": "full",
                "joined": HUB,
                "aliases": {V["ids"]["hub_rogue"]: {PERSON_ALICE: ALICE}},
                "user_hub": {ALICE: V["ids"]["hub_rogue"]},
            },
            "steps": [
                {"join": {PERSON_ALICE: ALICE}, "expect": "ok"},
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 1000, "expect": "ok", "actor": "p_" + PERSON_ALICE},
            ],
        },
        {
            "name": "aliased_under_this_hub_resolves_to_the_person",
            "hub": {"mode": "full", "joined": HUB, "aliases": {HUB: {PERSON_ALICE: ALICE}}},
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e1", "at_ms": T0 + 1000, "expect": "ok", "actor": "p_" + PERSON_ALICE},
            ],
        },
        {
            "name": "selective_unaliased_user_is_not_granted",
            "hub": {"mode": "selective", "joined": HUB, "aliases": {}},
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e_selective_item", "at_ms": T0 + 1000, "expect": "not_granted"},
            ],
        },
        {
            "name": "selective_aliased_member_with_live_item",
            "pending": "C3b implementation: item_hash is a section 8.1 field in selective mode",
            "hub": {
                "mode": "selective",
                "joined": HUB,
                "aliases": {HUB: {PERSON_ALICE: ALICE}},
                "member_roles": {"p_" + PERSON_ALICE: "member"},
                "shared": {"sha256:" + "5e" * 32: LOG},
            },
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e_selective_item", "at_ms": T0 + 1000, "expect": "ok", "actor": "p_" + PERSON_ALICE},
            ],
        },
        {
            "name": "selective_item_not_shared",
            "pending": "C3b implementation: item_hash is a section 8.1 field in selective mode",
            "hub": {
                "mode": "selective",
                "joined": HUB,
                "aliases": {HUB: {PERSON_ALICE: ALICE}},
                "member_roles": {"p_" + PERSON_ALICE: "member"},
                "shared": {},
            },
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e_selective_item", "at_ms": T0 + 1000, "expect": "not_granted"},
            ],
        },
        {
            "name": "full_mode_refuses_an_item_hash",
            "hub": full,
            "steps": [
                {"pin": "card_1", "at_ms": T0, "expect": "ok"},
                {"pin": "snap_v3", "at_ms": T0, "expect": "ok"},
                {"custodial": "e_selective_item", "at_ms": T0 + 1000, "expect": "malformed"},
            ],
        },
    ]
    out = {
        "comment": "C3b amendment vectors (ticket-format section 15). FAKE keys from vectors_hub.json. Generated by "
        "scripts/gen_c3b_vectors.py; checked by tests/hub/test_c3b_vectors.py. Never edit by hand.",
        "hub_vectors": (ROOT / "tests" / "hub" / "vectors" / "SOURCE").read_text().strip(),
        "workspace_id": WS,
        "ticket": LOG,
        "hub_id": HUB,
        "wsk": "wsk_a",
        "pinned_root": b64u(pub("hk_root")),
        "dev_mode": True,
        "objects": objects,
        "events": events,
        "lines": {"hub_pinned_snapshot": pin_line, "custodial_gate_approved": cus_line},
        "replay": replay,
    }
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(replay)} replay cases")


if __name__ == "__main__":
    main()
