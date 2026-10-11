"""Independent oracle for the F1 canon/hash vectors (ticket-format §5.3-§5.7, §11): hashlib and json only.

Nothing here imports ``orch``. ``python -m tests.canon.oracle_f1`` rewrites ``tests/vectors/f1/*.json``;
``test_f1_vectors.py`` checks that the committed files equal what this oracle produces and that ``orch.canon``
reproduces them, so the files are literal known answers that two separate implementations agree on.
"""

from __future__ import annotations

import copy
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


# --- canonical json -------------------------------------------------------------------------------


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


# --- text -----------------------------------------------------------------------------------------


NEW_IN_16 = [  # primary composites added after Unicode 14.0: (composite, decomposition); NFC differs on old runtimes
    (0x105C9, (0x105D2, 0x307)), (0x105E4, (0x105DA, 0x307)), (0x11383, (0x11382, 0x113C9)),
    (0x11385, (0x11384, 0x113BB)), (0x1138E, (0x1138B, 0x113C2)), (0x11391, (0x11390, 0x113C9)),
    (0x113C5, (0x113C2, 0x113C2)), (0x113C7, (0x113C2, 0x113B8)), (0x113C8, (0x113C2, 0x113C9)),
    (0x16121, (0x1611E, 0x1611E)), (0x16122, (0x1611E, 0x16129)), (0x16123, (0x1611E, 0x1611F)),
    (0x16124, (0x16129, 0x1611F)), (0x16125, (0x1611E, 0x16120)), (0x16126, (0x16121, 0x1611F)),
    (0x16127, (0x16122, 0x1611F)), (0x16128, (0x16121, 0x16120)), (0x16D68, (0x16D67, 0x16D67)),
    (0x16D69, (0x16D63, 0x16D67)), (0x16D6A, (0x16D69, 0x16D67)),
]  # fmt: skip


def text_vectors() -> dict[str, Any]:
    normalize = [
        ("crlf", "a\r\nb", "a\nb"),
        ("lone_cr", "a\rb", "a\nb"),
        ("cr_cr_lf", "\r\r\n", "\n\n"),
        ("nfd_e_acute", "e\u0301", "\u00e9"),
        ("angstrom_sign", "\u212b", "\u00c5"),
        ("hangul", "\u1112\u1161\u11ab", "\ud55c"),
        ("combining_order", "a\u0323\u0301", "\u1ea1\u0301"),
        ("trailing_space_kept", "x  \n", "x  \n"),
        ("no_final_newline_kept", "x", "x"),
        ("tab_kept", "a\tb", "a\tb"),
        ("compat_not_folded", "\ufb01", "\ufb01"),
        ("unicode_16_new_char", "\U00010d40", "\U00010d40"),
    ]
    refused = [
        ("nul", "a\x00b"),
        ("c0_bell", "\x07"),
        ("esc", "\x1b[2J"),
        ("del", "\x7f"),
        ("c1_nel", "\x85"),
        ("c1_last", "\x9f"),
        ("bidi_rlo", "\u202e"),
        ("bidi_pdf", "\u202c"),
        ("bidi_lri", "\u2066"),
        ("bidi_pdi", "\u2069"),
        ("bidi_lrm", "\u200e"),
        ("bidi_rlm", "\u200f"),
        ("bidi_alm", "\u061c"),
        ("unassigned_0378", "\u0378"),
        ("noncharacter_fffe", "\ufffe"),
        ("noncharacter_ffff", "\uffff"),
        ("unassigned_plane4", "\U00040000"),
        ("unassigned_tags_block", "\U000e0080"),
        ("unassigned_max", "\U0010ffff"),
        ("lone_surrogate", "a\ud800"),
        ("unassigned_cr_survivor", "\r\u0378"),
    ]
    for cp, parts in NEW_IN_16:
        normalize.append((f"nfc16_{cp:x}", "".join(map(chr, parts)), chr(cp)))
    check_refused_unnormalised = [("nfd", "e\u0301"), ("cr", "a\rb"), ("crlf", "a\r\nb")] + [
        (f"nfc16_{cp:x}", "".join(map(chr, parts))) for cp, parts in NEW_IN_16
    ]
    return {
        "unicode_version": "16.0.0",
        "normalize": [{"name": n, "input": i, "output": o} for n, i, o in normalize],
        "refused": [{"name": n, "input": i} for n, i in refused],
        "refused_not_normalised": [{"name": n, "input": i} for n, i in check_refused_unnormalised],
        "kept_invisible": [
            {"name": "zwsp", "input": "a\u200bb"},
            {"name": "tag", "input": "\U000e0041"},
            {"name": "bom", "input": "\ufeffx"},
            {"name": "private_use", "input": "\ue000"},
        ],
    }


# --- hashes ---------------------------------------------------------------------------------------


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
            {"text": "caf\u00e9\nline  ", "hash": h("section", "caf\u00e9\nline  ".encode())},
            {"text": 'a\nb\t"q" \\', "hash": h("section", b'a\nb\t"q" \\')},
            {"text": "x\u2028y \U0001f600 ", "hash": h("section", "x\u2028y \U0001f600 ".encode())},
        ],
        "section_hash_refused": ["cafe\u0301", "a\r\nb", "a\x00", "\u202e", "\nabc", "abc\n", "\n", "a" * 65537],
        "value_hash_refused": ["cafe\u0301", {"k": ["a\r\nb"]}, {"x": "\u202e"}, ["\ud800"]],
        "question_hash_refused": [
            {"text": "cafe\u0301", "options": []},
            {"text": "ok", "options": [{"key": "a", "label": "cafe\u0301"}]},
        ],
        "value_hash": [
            {"value": None, "hash": h("value", b"null")},
            {"value": ["a", 1, {"b": True}], "hash": h("value", cj(["a", 1, {"b": True}]))},
            {"value": "m", "hash": h("value", b'"m"')},
            {"value": 'a\nb\t"q" \\ \u2028', "hash": h("value", cj('a\nb\t"q" \\ \u2028'))},
            {"value": {'k\\"': ["\t\n"]}, "hash": h("value", cj({'k\\"': ["\t\n"]}))},
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
        "grant_secret_hash_refused": ["", "00" * 31, "00" * 33],
        "grant_secret_hash": [{"secret_hex": "00" * 32, "hash": h("grant_secret", bytes(32))}],
    }


# --- gate hash ------------------------------------------------------------------------------------
#
# Every inner hash (section, artifact digest, policy, people) is derived from a small real ticket by
# ``oracle_f1_gate.derive_G`` (see its docstring); the cases below are variants of that one ticket.


def gate_inputs() -> dict[str, dict[str, Any]]:
    """The four gate hash inputs ``G`` of the sample ticket, for tests that mutate them."""
    from . import oracle_f1_gate as g

    t = g.sample_ticket()
    return {gate: g.derive_G(t, gate) for gate in g.GATES}


def gate_vectors() -> dict[str, Any]:
    from . import oracle_f1_gate as g

    base = g.sample_ticket()
    nulls = copy.deepcopy(base)  # a receipt of a command that is not tied to a repo (ticket-format 5.4.1)
    nulls["receipts"]["T2"].update(repo=None, commit=None)
    chore = copy.deepcopy(base)  # a chore has no out_of_scope section; a missing summary is H("")
    chore["type"] = "chore"
    chore["size"] = None
    for sid in ("summary", "out_of_scope", "verification"):
        del chore["sections"][sid]
    no_plan = copy.deepcopy(base)  # plan does not apply to a feature here, so it is not in `prior` (5.7)
    no_plan["overrides"] = {}
    no_plan["ws_policies"]["plan"]["applies"] = ["bug"]
    two_repos = copy.deepcopy(base)  # source list sorted by identity, ssh remote with a port mapped by 5.7
    two_repos["links"]["repos"] = ["acme-energy-dbt", "acme-infra"]
    two_repos["links"]["branches"]["acme-infra"] = "feat/DEMO-0043"
    two_repos["remotes"]["acme-infra"] = "ssh://git@git.example.com:2222/Acme/infra.git"
    two_repos["heads"]["acme-infra"] = "a1" * 20
    cases = [
        ("requirements", "requirements", base),
        ("plan", "plan", base),
        ("verify", "verify", base),
        ("code", "code", base),
        ("verify_null_receipt", "verify", nulls),
        ("requirements_chore", "requirements", chore),
        ("verify_plan_not_applicable", "verify", no_plan),
        ("code_two_repos", "code", two_repos),
    ]
    out = []
    for name, gate, t in cases:
        gi = g.derive_G(t, gate)
        pol = g.derive_policies(t)[gate]
        out.append(
            {
                "name": name,
                "gate": gate,
                "ticket": t,
                "effective_policy": pol,
                "people": g.people_for(pol, t),
                "G": gi,
                "cj_hex": cj(gi).hex(),
                "hash": h("gate", cj(gi)),
            }
        )
    return {
        "note": "G is derived from `ticket` by the rules of ticket-format 5.6/5.7: effective policy = workspace "
        "intersect override; the people hash covers the ticket roles the policy names (plus assignees when "
        "independent); prior lists every earlier gate that applies with its first `count` counting approvals sorted; "
        "code is switched on in this workspace.",
        "gate_hash": out,
    }


# --- chain and signed bytes -----------------------------------------------------------------------


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
        "text": "caf\u00e9 \u2028 note", "actor": {"kind": "host"},
        "based_on": h("event", cj(e2)), "prev": h("event", cj(e2)),
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


def repo_identity_vectors() -> dict[str, Any]:
    from . import oracle_f1_gate as g

    ok = [
        "https://github.com/acme/x",
        "https://github.com/Acme/X.y_z~1",
        "https://git.example.com:8443/a/b/c",
        "https://xn--bcher-kva.example/x",
        "https://127.0.0.1/x",
        "https://127.0.0.1:1/x",
        "https://h:65535/x",
        "https://a.b-c.d/x",
        "local:my-repo",
    ]
    refused = [
        "https://github.com./acme/x", "https://github.com:0443/acme/x", "https://github.com:0/acme/x",
        "https://github.com:99999/acme/x", "https://github.com:65536/acme/x", "https://github.com:443/acme/x",
        "https://github.com//acme/x", "https://github.com/acme//x", "https://github.com/./acme/x",
        "https://github.com/acme/../x", "https://github.com/acme/.", "https://github.com/acme/..",
        "https://0x7f.1/x", "https://2130706433/x", "https://127.0.0.01/x", "https://127.0.0/x",
        "https://256.0.0.1/x", "https://a..b/x", "https://.com/x", "https://-x.com/x", "https://x-.com/x",
        "https://GitHub.com/acme/x", "https://github.com/acme/x.GIT", "https://github.com/acme/x.git",
        "https://github.com/acme/x/", "https://github.com", "https://github.com:/x",
        "https://user:token@github.com/acme/x", "https://token@github.com/x", "https://github.com/a%2Fb",
        "https://github.com/a?b", "https://github.com/a#b", "https://github.com/a b", "https://github.com/a\tb",
        "https://[::1]/x", "https://b\u00fccher.example/x", "https://github.com/caf\u00e9",
        "https://" + "a" * 64 + ".com/x",
        "https://" + ".".join(["abcdefghi"] * 26) + "/x",
        "http://github.com/x", "ssh://git@github.com/x", "git@github.com:acme/x", "file:///tmp/x", "/tmp/x",
        "local:", "local:a b", "local:a/b", "",
    ]  # fmt: skip
    raw = [  # (name, raw remote.origin.url, repo name): the canonical identity is derived by 5.7, independently
        ("https_plain", "https://github.com/acme/x", "x"),
        ("https_dot_git", "https://github.com/acme/x.git", "x"),
        ("https_trailing_slash", "https://github.com/acme/x/", "x"),
        ("https_user_token", "https://user:s3cr3t-token@github.com/acme/x.git", "x"),
        ("https_token_only", "https://ghp_abc123@github.com/acme/x", "x"),
        ("https_host_lowercased_path_kept", "https://GitHub.COM/Acme/X", "x"),
        ("https_default_port_dropped", "https://github.com:443/acme/x", "x"),
        ("https_other_port_kept", "https://git.example.com:8443/a/b", "x"),
        ("https_port_22_kept", "https://git.example.com:22/a/b", "x"),
        ("ssh_url", "ssh://git@github.com/acme/x.git", "x"),
        ("ssh_url_no_user", "ssh://github.com/acme/x", "x"),
        ("ssh_url_password", "ssh://git:pw@github.com/acme/x", "x"),
        ("ssh_default_port_dropped", "ssh://git@github.com:22/acme/x", "x"),
        ("ssh_other_port_kept", "ssh://git@git.example.com:2222/a/b.git", "x"),
        ("scp_like", "git@github.com:acme/x.git", "x"),
        ("scp_like_other_user", "deploy@git.example.com:acme/x.git", "x"),
        ("scp_like_no_user", "github.com:acme/x", "x"),
        ("scp_like_upper_host", "git@GitHub.com:Acme/X.git", "x"),
        ("file_url", "file:///srv/git/x.git", "x"),
        ("absolute_path", "/srv/git/x", "x"),
        ("relative_path", "../x", "x"),
        ("no_remote", "", "x"),
        ("http_not_https", "http://github.com/acme/x", "x"),
    ]
    mapping = [{"name": n, "raw": r, "repo_name": nm, "canonical": g.repo_identity(r, nm)} for n, r, nm in raw]
    return {
        "ok": ok,
        "refused": refused,
        "same": [["https://github.com/Acme/X", "https://github.com/acme/x"]],
        "mapping": mapping,
        "mapping_note": "userinfo (user:token@) is removed before anything else and never appears in a result; a "
        "trailing .git is removed only from the last path segment's end; the https default port 443 and the ssh "
        "default port 22 are dropped, every other port is kept; anything that is not https, ssh or scp-like is "
        "local:<repo name>.",
    }


def section_text_vectors() -> dict[str, Any]:
    from . import oracle_f1_gate as g

    f = "`" * 3
    cases: list[tuple[str, str, str]] = [  # (name, ticket type, body.md text)
        ("two_sections", "feature", "## Context\n\nctx\n\n## Requirements\n\nreq\n"),
        ("lf_trimmed_inner_kept", "feature", "\n\n## Context\n\n\n\nline 1  \n\n\tline 3\n\n\n## Plan\n\nx"),
        ("empty_section", "feature", "## Context\n\n## Requirements\n\nr"),
        ("no_final_newline", "feature", "## Context\n\nctx"),
        ("heading_in_backtick_fence", "feature", f"## Context\n\nbefore\n{f}\n## Plan\n{f}\nafter"),
        ("heading_in_tilde_fence", "feature", "## Context\n\n~~~\n## Plan\n~~~\nafter"),
        ("fence_closes_only_when_long_enough", "feature", f"## Context\n\n{f}`\n{f}\n## Plan\n{f}`\nz"),
        ("fence_closing_with_trailing_spaces", "feature", f"## Context\n\n{f}\n## Plan\n{f}   \n## Plan\n\nreal"),
        ("tilde_does_not_close_backtick_fence", "feature", f"## Context\n\n{f}\n~~~\n## Plan\n{f}\n## Plan\n\nreal"),
        ("fence_with_info_string_opens", "feature", f"## Context\n\n{f}python\n## Plan\n{f}"),
        ("fence_closer_with_text_stays_open", "feature", f"## Context\n\n{f}\n{f} not a closer\n## Plan\n{f}\n"),
        ("indented_fence_is_text", "feature", f"## Context\n\n {f}\n## Plan\n\np"),
        ("two_backticks_are_text", "feature", "## Context\n\n``\n## Plan\n\np"),
        ("indented_heading_is_text", "feature", "## Context\n\n ## Plan\n### Plan\n##Plan\n#### x"),
        ("hash_marks_without_space_are_text", "feature", "## Context\n\n##\n###\n##x"),
        ("empty_heading_refused", "feature", "## Context\n\n## \n"),
        ("current_state_section", "chore", "## Requirements\n\nr\n\n## Current state\n\nhandoff"),
        ("findings_of_a_spike", "spike", "## Findings\n\nf"),
        ("unknown_heading", "feature", "## Notes\n\nn"),
        ("heading_case_matters", "feature", "## context\n\nc"),
        ("section_of_another_type", "feature", "## Findings\n\nf"),
        ("dash_section_for_the_type", "chore", "## Out of scope\n\nnothing"),
        ("duplicate_heading", "feature", "## Context\n\na\n\n## Context\n\nb"),
        ("text_before_first_heading", "feature", "intro\n## Context\n\nc"),
        ("blank_lines_before_first_heading", "feature", "\n\n\n## Context\n\nc"),
        ("open_fence_at_the_end", "feature", f"## Context\n\n{f}\ncode"),
        ("open_tilde_fence_at_the_end", "feature", "## Context\n\n~~~~\n~~~\n"),
        ("empty_file", "feature", ""),
    ]
    out = []
    for name, ty, body in cases:
        try:
            secs = g.parse_body(body, ty)
            out.append(
                {
                    "name": name,
                    "type": ty,
                    "body": body,
                    "sections": secs,
                    "hashes": {k: h("section", v.encode()) for k, v in secs.items()},
                }
            )
        except g.BodyRefused as e:
            out.append({"name": name, "type": ty, "body": body, "refused": e.args[0]})
    return {
        "note": "section text = what lies between a heading and the next one, leading and trailing LF removed; "
        "a heading is '## ' at column 0 outside a fence; the hash is of that text only (ticket-format 4, 5.6)",
        "cases": out,
    }


def artifact_refs_vectors() -> dict[str, Any]:
    from . import oracle_f1_gate as g

    f = "`" * 3
    texts = [
        ("one", "see (artifact:after.png)"),
        ("markdown_image", "![alt](artifact:after.png)"),
        ("sorted_unique", "(artifact:b.png) (artifact:a.png) (artifact:b.png)"),
        ("inside_code_fence", f"{f}\n(artifact:fenced.log)\n{f}"),
        ("inside_inline_code", "`(artifact:inline.log)`"),
        ("none", "no refs (artifact) (artifact:) artifact:x"),
        ("must_start_alnum", "(artifact:.hidden) (artifact:-x) (artifact:_y) (artifact:ok)"),
        ("name_chars", "(artifact:A-b_c.d9)"),
        ("name_128_ok", "(artifact:" + "a" * 128 + ")"),
        ("name_129_no_match", "(artifact:" + "a" * 129 + ")"),
        ("spaces_inside_no_match", "(artifact: a.png) (artifact:a b.png) ( artifact:c.png)"),
        ("unicode_name_no_match", "(artifact:caf\u00e9.png)"),
        ("adjacent", "(artifact:a)(artifact:b)"),
        ("case_matters", "(Artifact:a.png) (artifact:A.png)"),
        ("nested_parens", "((artifact:a.png))"),
    ]
    return {
        "regex": r"\(artifact:([A-Za-z0-9][A-Za-z0-9._-]{0,127})\)",
        "cases": [{"name": n, "text": t, "refs": g.refs_of(t)} for n, t in texts],
    }


def all_vectors() -> dict[str, dict[str, Any]]:
    from . import oracle_f1_signed as sg

    return {
        "signatures.json": sg.signatures_vector(),
        "replay.json": sg.replay_scenario(),
        "tamper.json": sg.tamper_vector(),
        "genesis.json": sg.genesis_vector(),
        "devices.json": {"pins": "device.added and device roster rules", "scenarios": sg.devices_scenarios()},
        "revocation.json": {"pins": "device.revoked and recovery", "scenarios": sg.revocation_scenarios()},
        "checkpoint.json": sg.checkpoint_vector(),
        "restore.json": {"pins": "workspace restore and revocations", "scenarios": sg.restore_scenarios()},
        "labels.json": {"labels": LABELS},
        "section_text.json": section_text_vectors(),
        "artifact_refs.json": artifact_refs_vectors(),
        "canon.json": canon_vectors(),
        "text.json": text_vectors(),
        "hashes.json": hash_vectors(),
        "gate_hash.json": gate_vectors(),
        "chain.json": chain_vectors(),
        "repo_identity.json": repo_identity_vectors(),
    }


def render(v: dict[str, Any]) -> str:
    return json.dumps(v, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, v in all_vectors().items():
        (OUT / name).write_text(render(v))


if __name__ == "__main__":
    main()
