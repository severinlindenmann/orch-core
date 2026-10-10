"""Independent oracle for the F1 canon/hash vectors (ticket-format §5.3-§5.7, §11): hashlib and json only.

Nothing here imports ``orch``. ``python -m tests.canon.oracle_f1`` rewrites ``tests/vectors/f1/*.json``;
``test_f1_vectors.py`` checks that the committed files equal what this oracle produces and that ``orch.canon``
reproduces them, so the files are literal known answers that two separate implementations agree on.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent.parent / "vectors" / "f1"

W = "0123456789abcdef0123456789abcdef"
UID = "01J9ZK4Q7M3R8T2V6X0B5N1C9D"
UID2 = "01J9ZK4Q7M3R8T2V6X0B5N1C9E"
P_SEV = "p_" + "5e" * 16
P_MARA = "p_" + "3a" * 16
SIG = "A" * 86
LABELS = {
    "gate": "orch/v2/gate|",
    "section": "orch/v2/section|",
    "value": "orch/v2/value|",
    "policy": "orch/v2/policy|",
    "people": "orch/v2/people|",
    "question_id": "orch/v2/question-id|",
    "question": "orch/v2/question|",
    "event": "orch/v2/event|",
    "grant_secret": "orch/v2/grant-secret|",
    "sig_ticket_event": "orch/v2/sig/ticket-event|",
    "sig_ws_event": "orch/v2/sig/ws-event|",
    "sig_host_event": "orch/v2/sig/host-event|",
    "sig_checkpoint": "orch/v2/sig/checkpoint|",
}


def cj(o: Any) -> bytes:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def h(label: str, data: bytes) -> str:
    return "sha256:" + hashlib.sha256(LABELS[label].encode() + data).hexdigest()


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def ctx(label: str, log: str, event: dict[str, Any]) -> bytes:
    return LABELS[label].encode() + cj({"contract": 1, "suite": 2, "workspace_id": W, "log": log, "event": event})


# --- canonical json -----------------------------------------------------------------------------------------------


def canon_vectors() -> dict[str, Any]:
    d16 = "[" * 16 + "]" * 16
    return {
        "depth": [
            {"name": "depth_16", "text": d16, "ok": True, "canonical": d16},
            {"name": "depth_17", "text": "[" * 17 + "]" * 17, "ok": False},
            {
                "name": "depth_16_objects",
                "text": '{"a":' * 16 + "1" + "}" * 16,
                "ok": True,
                "canonical": '{"a":' * 16 + "1" + "}" * 16,
            },
            {"name": "depth_17_objects", "text": '{"a":' * 17 + "1" + "}" * 17, "ok": False},
            {"name": "minus_zero", "text": "[-0]", "ok": True, "canonical": "[0]"},
        ]
    }


# --- text -----------------------------------------------------------------------------------------------------------


def text_vectors() -> dict[str, Any]:
    normalize = [
        ("crlf", "a\r\nb", "a\nb"),
        ("lone_cr", "a\rb", "a\nb"),
        ("cr_cr_lf", "\r\r\n", "\n\n"),
        ("nfd_e_acute", "é", "é"),
        ("angstrom_sign", "Å", "Å"),
        ("hangul", "한", "한"),
        ("combining_order", "ạ́", "ạ́"),
        ("trailing_space_kept", "x  \n", "x  \n"),
        ("no_final_newline_kept", "x", "x"),
        ("tab_kept", "a\tb", "a\tb"),
        ("compat_not_folded", "ﬁ", "ﬁ"),
        ("unicode_16_new_char", "\U00010d40", "\U00010d40"),
    ]
    refused = [
        ("nul", "a\x00b"),
        ("c0_bell", "\x07"),
        ("esc", "\x1b[2J"),
        ("del", "\x7f"),
        ("c1_nel", "\x85"),
        ("c1_last", "\x9f"),
        ("bidi_rlo", "‮"),
        ("bidi_pdf", "‬"),
        ("bidi_lri", "⁦"),
        ("bidi_pdi", "⁩"),
        ("bidi_lrm", "‎"),
        ("bidi_rlm", "‏"),
        ("bidi_alm", "؜"),
        ("unassigned_0378", "͸"),
        ("noncharacter_fffe", "￾"),
        ("noncharacter_ffff", "￿"),
        ("unassigned_plane4", "\U00040000"),
        ("unassigned_tags_block", "\U000e0080"),
        ("unassigned_max", "\U0010ffff"),
        ("lone_surrogate", "a\ud800"),
        ("unassigned_cr_survivor", "\r͸"),
    ]
    check_refused_unnormalised = [("nfd", "é"), ("cr", "a\rb"), ("crlf", "a\r\nb")]
    return {
        "unicode_version": "16.0.0",
        "normalize": [{"name": n, "input": i, "output": o} for n, i, o in normalize],
        "refused": [{"name": n, "input": i} for n, i in refused],
        "refused_not_normalised": [{"name": n, "input": i} for n, i in check_refused_unnormalised],
        "kept_invisible": [
            {"name": "zwsp", "input": "a​b"},
            {"name": "tag", "input": "\U000e0041"},
            {"name": "bom", "input": "﻿x"},
            {"name": "private_use", "input": ""},
        ],
    }


# --- hashes -------------------------------------------------------------------------------------------------------


def policy_obj() -> dict[str, Any]:
    return {"approvers": ["maintainer", "owner"], "count": 1, "not": [], "applies": "all", "independent": False}


def hash_vectors() -> dict[str, Any]:
    policy_in = {
        "approvers": ["owner", "maintainer", "owner"],
        "count": 1,
        "not": [],
        "applies": "all",
        "independent": False,
    }
    code_policy = {
        "approvers": ["owner"],
        "count": 2,
        "not": ["assignees"],
        "applies": ["feature", "bug"],
        "independent": True,
    }
    qid = hashlib.sha256(
        LABELS["question_id"].encode() + cj({"workspace_id": W, "ticket": UID, "question": "Q1"})
    ).hexdigest()[:32]
    opts = [{"key": "csv", "label": "Monthly CSV"}, {"key": "api", "label": "Tariff API", "cost": "+1 day"}]
    return {
        "artifact_digest": [
            {"bytes_hex": "", "hash": digest(b"")},
            {"bytes_hex": b"abc".hex(), "hash": digest(b"abc")},
        ],
        "section_hash": [
            {"text": "", "hash": h("section", b"")},
            {"text": "café\nline  \n", "hash": h("section", "café\nline  \n".encode())},
            {"text": "x y \U0001f600 \x7f".replace("\x7f", ""), "hash": h("section", "x y \U0001f600 ".encode())},
        ],
        "section_hash_refused": ["café", "a\r\nb", "a\x00", "‮"],
        "value_hash": [
            {"value": None, "hash": h("value", b"null")},
            {"value": ["a", 1, {"b": True}], "hash": h("value", cj(["a", 1, {"b": True}]))},
            {"value": "m", "hash": h("value", b'"m"')},
        ],
        "policy_hash": [
            {
                "gate": "requirements",
                "policy": policy_in,
                "hash": h("policy", cj({"gate": "requirements", "policy": policy_obj()})),
            },
            {
                "gate": "code",
                "policy": code_policy,
                "hash": h("policy", cj({"gate": "code", "policy": {**code_policy, "applies": ["bug", "feature"]}})),
            },
        ],
        "people_hash": [
            {
                "people": {"ticket_owner": P_SEV, "assignees": [P_MARA, P_SEV, P_MARA]},
                "hash": h("people", cj({"ticket_owner": P_SEV, "assignees": sorted([P_MARA, P_SEV])})),
            },
            {"people": {"ticket_owner": None}, "hash": h("people", cj({"ticket_owner": None}))},
            {"people": {}, "hash": h("people", b"{}")},
        ],
        "question_id": [{"workspace_id": W, "ticket": UID, "question": "Q1", "qid": qid}],
        "question_hash": [
            {
                "qid": qid,
                "ticket": UID,
                "text": "Which tariff export is the source of truth?",
                "options": opts,
                "hash": h(
                    "question",
                    cj(
                        {
                            "question_id": qid,
                            "ticket": UID,
                            "text": "Which tariff export is the source of truth?",
                            "options": opts,
                        }
                    ),
                ),
            },
            {
                "qid": qid,
                "ticket": UID,
                "text": "Ok?",
                "options": [],
                "hash": h("question", cj({"question_id": qid, "ticket": UID, "text": "Ok?", "options": []})),
            },
        ],
        "grant_secret_hash": [{"secret_hex": "00" * 32, "hash": h("grant_secret", bytes(32))}],
    }


# --- gate hash ----------------------------------------------------------------------------------------------------


def _sec(text: str) -> str:
    return h("section", text.encode())


def gate_inputs() -> dict[str, dict[str, Any]]:
    base = {"workspace_id": W, "uid": UID, "schema": "orch.ticket/2", "hash_v": 1, "addon_packages": {}}
    ac = [
        {"id": "AC1", "text": "`dbt seed` loads all 40 tariff tables"},
        {"id": "AC2", "text": "Model joins the seeds"},
    ]
    links = {"repos": ["acme-energy-dbt"], "branches": {"acme-energy-dbt": "feat/DEMO-0043"}, "prs": [], "external": []}
    shot = digest(b"png bytes")
    apol = h("policy", cj({"gate": "x", "policy": policy_obj()}))
    ppl = h("people", b"{}")
    sha1, sha2 = "b7e1f02c" * 5, "a1" * 20
    req = {
        **base,
        "gate": "requirements",
        "sections": {
            "summary": _sec("Load tariffs.\n"),
            "context": _sec(""),
            "requirements": _sec("- R1 café\n"),
            "out_of_scope": _sec("Nothing.\n"),
        },
        "fields": {"ticket_type": "feature", "size": "m", "acceptance": ac, "links": None, "addons": {}},
        "tasks": [],
        "artifacts": {"mock.png": {"kind": "screenshot", "digest": shot, "ac": "AC1", "task": None}},
        "receipts": {},
        "source_sha": [],
        "prior": {},
        "policy_hash": apol,
        "people_hash": ppl,
    }
    plan = {
        **base,
        "gate": "plan",
        "sections": {"plan": _sec("1. export\n2. seed\n"), "decisions": _sec("")},
        "fields": {
            "ticket_type": "feature",
            "size": "m",
            "acceptance": ac,
            "links": None,
            "addons": {"estimate": {"points": 5}},
        },
        "addon_packages": {"estimate": digest(b"estimate package")},
        "tasks": [
            {"id": "T1", "text": "Export CSVs", "verify": {"cmd": "ls seeds | wc -l"}, "proves": []},
            {"id": "T2", "text": "Seed configs", "verify": None, "proves": ["AC1"]},
        ],
        "artifacts": {},
        "receipts": {},
        "source_sha": [],
        "prior": {
            "requirements": {"gen": 1, "approvals": ["01J9ZP0000000000000000000A", "01J9ZP0000000000000000000B"]}
        },
        "policy_hash": apol,
        "people_hash": ppl,
    }
    verify = {
        **base,
        "gate": "verify",
        "sections": {"verification": _sec("Ran dbt seed.\n")},
        "fields": {"ticket_type": "feature", "size": "m", "acceptance": ac, "links": links, "addons": {}},
        "tasks": [],
        "artifacts": {"run.log": {"kind": "log", "digest": digest(b"log"), "ac": None, "task": "T2"}},
        "receipts": {
            "T2": {"event": "01J9ZQ0000000000000000000C", "repo": "acme-energy-dbt", "commit": sha1, "exit": 0}
        },
        "source_sha": [{"repo": "https://github.com/acme/energy-dbt", "ref": "refs/heads/feat/DEMO-0043", "sha": sha1}],
        "prior": {
            "requirements": {"gen": 1, "approvals": ["01J9ZP0000000000000000000A"]},
            "plan": {"gen": 0, "approvals": ["01J9ZP0000000000000000000D"]},
        },
        "policy_hash": apol,
        "people_hash": ppl,
    }
    code = {
        **base,
        "gate": "code",
        "sections": {},
        "fields": {"ticket_type": "feature", "size": None, "acceptance": ac, "links": links, "addons": {}},
        "tasks": [],
        "artifacts": {},
        "receipts": {},
        "source_sha": [{"repo": "https://github.com/acme/energy-dbt", "ref": "refs/heads/feat/DEMO-0043", "sha": sha2}],
        "prior": {"verify": {"gen": 2, "approvals": ["01J9ZR0000000000000000000E"]}},
        "policy_hash": apol,
        "people_hash": ppl,
    }
    return {"requirements": req, "plan": plan, "verify": verify, "code": code}


def gate_vectors() -> dict[str, Any]:
    gi = gate_inputs()
    return {"gate_hash": [{"gate": g, "G": v, "cj_hex": cj(v).hex(), "hash": h("gate", cj(v))} for g, v in gi.items()]}


# --- chain and signed bytes -----------------------------------------------------------------------------------------


def chain_events() -> list[dict[str, Any]]:
    e1: dict[str, Any] = {
        "v": 2, "id": "01J9ZP0000000000000000000A", "seq": 1, "at": "2026-10-09T09:00:00Z", "type": "ticket.created",
        "actor": {"kind": "host"}, "based_on": None, "prev": None, "hash_v": 1, "ws_seq": 1,
    }  # fmt: skip
    e1["host_sig"] = SIG
    e2: dict[str, Any] = {
        "v": 2, "id": "01J9ZP0000000000000000000B", "seq": 2, "at": "2026-10-09T09:10:11Z", "type": "gate.approved",
        "gate": "requirements", "hash": "sha256:" + "fa" * 32, "policy_hash": "sha256:" + "31" * 32, "gate_gen": 0,
        "actor": {"kind": "person", "id": P_SEV, "device": "d_" + "0d" * 16}, "auth": "passphrase",
        "based_on": digest(b"x"), "hash_v": 1, "ws_seq": 1, "roster_v": 1, "prev": h("event", cj(e1)), "sig": SIG,
    }  # fmt: skip
    e2["based_on"] = e2["prev"]
    e2["host_sig"] = "B" * 86
    e3: dict[str, Any] = {
        "v": 2, "id": "01J9ZP0000000000000000000C", "seq": 3, "at": "2026-10-09T09:11:00Z", "type": "log.added",
        "text": "café   note", "actor": {"kind": "host"}, "based_on": h("event", cj(e2)), "prev": h("event", cj(e2)),
        "hash_v": 1, "ws_seq": 2,
    }  # fmt: skip
    e3["host_sig"] = "C" * 86
    return [e1, e2, e3]


def chain_vectors() -> dict[str, Any]:
    evs = chain_events()
    heads = [h("event", cj(e)) for e in evs]
    host = []
    for e in evs:
        body = {k: v for k, v in e.items() if k != "host_sig"}
        host.append(ctx("sig_host_event", UID, body).hex())
    person_body = {k: v for k, v in evs[1].items() if k not in ("seq", "at", "prev", "ws_seq", "sig", "host_sig")}
    ws_evt = {
        "v": 2,
        "id": "01J9ZP0000000000000000000F",
        "type": "member.added",
        "actor": {"kind": "host"},
        "hash_v": 1,
        "n": 1,
    }
    ws_full = {**ws_evt, "seq": 1, "at": "2026-10-09T09:00:00Z", "prev": None, "ws_seq": 1}
    lines = [(cj(e) + b"\n").hex() for e in evs]
    tampered = evs[1] | {"gate": "plan"}
    return {
        "workspace_id": W,
        "log": UID,
        "events": evs,
        "lines_hex": lines,
        "heads": heads,
        "log_head": heads[-1],
        "host_signing_hex": host,
        "ticket_event_signing_hex": ctx("sig_ticket_event", UID, person_body).hex(),
        "ws_event_signing_hex": ctx("sig_ws_event", "workspace", ws_evt).hex(),
        "ws_event": ws_evt,
        "ws_log_host_signing_hex": ctx("sig_host_event", "workspace", ws_full).hex(),
        "tampered_event_head": h("event", cj(tampered)),
        "non_cj_lines": [
            (json.dumps(evs[0], sort_keys=True) + "\n").encode().hex(),
            json.dumps(evs[0], sort_keys=False, separators=(",", ":")).encode().hex(),
            (cj(evs[0]) + b"\n\n").hex(),
            (cj(evs[0]) + b"\r\n").hex(),
        ],
    }


def all_vectors() -> dict[str, dict[str, Any]]:
    return {
        "labels.json": {"labels": LABELS},
        "canon.json": canon_vectors(),
        "text.json": text_vectors(),
        "hashes.json": hash_vectors(),
        "gate_hash.json": gate_vectors(),
        "chain.json": chain_vectors(),
    }


def render(v: dict[str, Any]) -> str:
    return json.dumps(v, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, v in all_vectors().items():
        (OUT / name).write_text(render(v))


if __name__ == "__main__":
    main()
