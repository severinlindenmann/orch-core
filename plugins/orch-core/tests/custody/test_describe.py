"""The prompt shows everything that decides what a signature means, and refuses what it cannot show (fail closed)."""

from __future__ import annotations

import json

import pytest

from orch import canon, crypto
from orch.custody import CustodyError
from orch.custody import passphrase as pp
from orch.custody.describe import ENVELOPE, EVENT_FIELDS, MAX_LINES, describe_payload
from tests.identity.helpers import NOW, WS, Person

TICKET = "01J9ZP0000000000000000000A"
PERSON, DEVICE = "p_" + "1" * 32, "d_" + "2" * 32
H1, H2 = "sha256:" + "ab" * 32, "sha256:" + "cd" * 32
SRC = [
    {"repo": "local:demo", "ref": "refs/heads/main", "sha": "c" * 40},
    {"repo": "https://x/y", "ref": "dev", "sha": "e" * 64},
]
alice, bob = Person(), Person()
CERT, CERT2 = alice.cert(), bob.cert()
REV = {
    "o": {
        "v": 2,
        "suite": 2,
        "kind": "revocation",
        "person_id": "aa" * 16,
        "device_id": "bb" * 16,
        "revoked_ms": NOW,
        "reason": "lost",
    },
    "sig": "x",
}
DELEG = {
    "o": {
        "v": 2,
        "suite": 2,
        "kind": "ws_delegation",
        "workspace_id": WS,
        "wsk_pub": "WSKPUB",
        "owner_person_id": "aa" * 16,
        "client_hosted": True,
        "issued_ms": NOW,
    },
    "sig": "x",
}

# type -> (log, payload). Every payload field is a decisive field the prompt must show.
CASES: dict[str, tuple[str, dict]] = {
    "ticket.closed": (TICKET, {"resolution": "duplicate", "duplicate_of": "DEMO-0007", "text": "same as 7"}),
    "ticket.reopened": (TICKET, {"text": "needs more"}),
    "visibility.changed": (TICKET, {"visibility": "workspace"}),
    "people.changed": (TICKET, {"role": "reviewers", "add": [PERSON], "remove": ["p_" + "9" * 32]}),
    "policy.changed": (TICKET, {"gates": {"code": {"approvers": ["owner"], "not": ["assignees"], "applies": "all"}}}),
    "question.answered": (TICKET, {"question": "Q3", "hash": H1, "option": "yes", "text": "go ahead"}),
    "gate.approved": (TICKET, {"gate": "code", "gate_gen": 4, "hash": H1, "policy_hash": H2, "source_sha": SRC}),
    "gate.changes_requested": (TICKET, {"gate": "plan", "gate_gen": 2, "hash": H1, "policy_hash": H2, "text": "no"}),
    "verdict.given": (
        TICKET,
        {"outcome": "fail", "gate_gen": 5, "hash": H1, "policy_hash": H2, "source_sha": SRC, "text": "broken"},
    ),
    "restore": (
        TICKET,
        {
            "from_seq": 3,
            "head": H1,
            "abandoned": {"seq": 9, "head": H2},
            "abandoned_decisions": ["01J9ZP0000000000000000000B"],
            "reason": "rollback",
        },
    ),
    "invalid.acknowledged": ("workspace", {"invalid_seq": 8, "invalid_head": H1, "reason": "known"}),
    "workspace.created": (
        "workspace",
        {
            "workspace_id": WS,
            "prefix": "DEMO",
            "host_id": "h_" + TICKET,
            "wsk_pub": "WSKPUB",
            "owner": {"person": PERSON, "name": "Owner", "pk_pub": "PKPUB"},
            "delegation": DELEG,
            "device_cert": CERT,
        },
    ),
    "member.added": (
        "workspace",
        {"person": PERSON, "name": "Bob", "role": "maintainer", "pk_pub": "PKPUB", "device_cert": CERT2},
    ),
    "member.removed": ("workspace", {"person": PERSON}),
    "role.changed": ("workspace", {"person": PERSON, "role": "owner"}),
    "device.added": ("workspace", {"device": DEVICE, "cert": CERT2}),
    "device.removed": ("workspace", {"device": DEVICE, "reason": "retired laptop"}),
    "device.revoked": ("workspace", {"device": DEVICE, "reason": "lost", "revocation": REV}),
    "settings.changed": (
        "workspace",
        {"set": {"grant_hours": 12, "repos": {"demo": {"path": "/srv/demo"}, "old": None}}},
    ),
    "grant.issued": (
        "workspace",
        {
            "grant": "gr_" + TICKET,
            "scope": "all",
            "verbs": ["claim.take", "task.done"],
            "issued_at": "2026-10-10T10:00:00Z",
            "hours": 6,
            "expires_at": "2026-10-10T16:00:00Z",
            "secret_hash": H1,
            "label": "ci",
        },
    ),
    "grant.revoked": ("workspace", {"grant": "gr_" + TICKET, "reason": "leaked"}),
    "addon.granted": (
        "workspace",
        {
            "name": "dash",
            "version": "1.2.3",
            "package_sha256": H1,
            "capabilities": ["serve_http", "network"],
            "binds": {
                "fields": {"risk": ["plan"]},
                "sections": [{"id": "dash.notes", "gate": ["plan"], "types": ["feature"]}],
            },
        },
    ),
    "addon.disabled": ("workspace", {"name": "dash"}),
    "addon.purged": ("workspace", {"name": "dash"}),
}


def payload(etype: str, log: str, fields: dict, **env) -> bytes:
    ev = {
        "v": 2,
        "id": "01J9ZP0000000000000000000B",
        "type": etype,
        "hash_v": 1,
        "roster_v": 7,
        "auth": "passphrase",
        "actor": {"kind": "person", "id": PERSON, "device": DEVICE},
        **fields,
        **env,
    }
    label = "sig_ws_event" if log == "workspace" else "sig_ticket_event"
    body = {"contract": 1, "suite": 2, "workspace_id": WS, "log": log, "event": ev}
    return canon.LABELS[label].encode() + canon.cj_checked(body)


def leaves(value, name="", out=None):
    out = [] if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            leaves(v, f"{name}.{k}", out)
    elif isinstance(value, list):
        for v in value:
            leaves(v, name, out)
    else:
        out.append(value)
    return out


def rendered(p: bytes) -> str:
    return pp.render_prompt(
        pp.PassphraseRequest("unlock", "dk", "", crypto.sha256(p).hex()[:32], tuple(describe_payload(p)))
    )


def test_every_person_event_type_has_a_case_and_the_table_is_complete():
    assert set(CASES) == set(EVENT_FIELDS)
    for etype, (_, fields) in CASES.items():
        assert set(fields) <= set(EVENT_FIELDS[etype]), etype
    assert not set(EVENT_FIELDS) & {"task.done", "claim.taken", "artifact.added", "edit.external", "branch.pushed"}


@pytest.mark.parametrize("etype", sorted(CASES))
def test_every_decisive_field_appears_in_the_prompt(etype):
    log, fields = CASES[etype]
    text = rendered(payload(etype, log, fields))
    assert f"type: {etype}" in text and f"workspace: {WS}" in text and f"log: {log}" in text
    assert "auth: passphrase" in text and "roster_v: 7" in text
    assert f"actor.person: {PERSON}" in text and f"actor.device: {DEVICE}" in text
    for field, value in fields.items():
        if field in ("delegation", "device_cert", "cert", "revocation"):
            obj = value["o"]
            for f in (
                "device_id",
                "person_id",
                "scopes_max",
                "expires_ms",
                "revoked_ms",
                "reason",
                "workspace_id",
                "owner_person_id",
                "client_hosted",
                "wsk_pub",
                "dk_sig_pub",
                "dk_kx_pub",
            ):
                if f in obj:
                    for leaf in leaves(obj[f]):
                        assert _shown(text, leaf), (etype, field, f, leaf)
            continue
        assert f"\n{field}" in text or f"\n{field}[" in text or f"\n{field}." in text or f"\n{field}#" in text, (
            etype,
            field,
        )
        for leaf in leaves(value):
            assert _shown(text, leaf), (etype, field, leaf)


def _shown(text: str, leaf) -> bool:
    flat = text.replace("\n", "")
    if leaf is None:
        return "null" in text
    if isinstance(leaf, bool):
        return ("true" if leaf else "false") in text
    return str(leaf) in text or str(leaf) in flat


def test_gate_decisions_show_gate_generation_hash_decision_and_the_whole_source_list():
    text = rendered(payload("gate.approved", TICKET, CASES["gate.approved"][1]))
    assert "gate: code" in text and "gate_gen: 4" in text and f"hash: {H1}" in text and f"policy_hash: {H2}" in text
    for i, e in enumerate(SRC, 1):
        for k in ("repo", "ref", "sha"):
            assert f"source_sha[{i}].{k}: {e[k]}" in text
    v = rendered(payload("verdict.given", TICKET, CASES["verdict.given"][1]))
    assert "outcome: fail" in v and "text: broken" in v
    c = rendered(payload("gate.changes_requested", TICKET, CASES["gate.changes_requested"][1]))
    assert "type: gate.changes_requested" in c


def test_grant_issue_shows_scope_hours_verbs_and_expiry():
    text = rendered(payload("grant.issued", "workspace", CASES["grant.issued"][1]))
    for needle in (
        "scope: all",
        "hours: 6",
        "verbs[1]: claim.take",
        "verbs[2]: task.done",
        "expires_at: 2026-10-10T16:00:00Z",
    ):
        assert needle in text
    agent = rendered(payload("grant.issued", "workspace", {**CASES["grant.issued"][1], "verbs": "agent"}))
    assert "verbs: agent" in agent


def test_unhandled_or_unknown_event_types_refuse():
    for etype in (
        "ticket.updated",
        "task.done",
        "claim.taken",
        "artifact.added",
        "edit.external",
        "branch.pushed",
        "nonsense.event",
        "dash.custom",
        "",
        None,
        5,
        ["gate.approved"],
    ):
        with pytest.raises(CustodyError, match="refusing to prompt"):
            describe_payload(payload(etype, TICKET, {}))
    with pytest.raises(CustodyError):
        describe_payload(
            canon.LABELS["sig_ticket_event"].encode()
            + canon.cj_checked(
                {"contract": 1, "suite": 2, "workspace_id": WS, "log": TICKET, "event": {"actor": {"kind": "person"}}}
            )
        )


def test_fields_the_prompt_does_not_show_refuse():
    log, fields = CASES["gate.approved"]
    with pytest.raises(CustodyError, match="does not show"):
        describe_payload(payload("gate.approved", log, {**fields, "sneaky": "extra"}))
    with pytest.raises(CustodyError, match="does not show"):
        describe_payload(payload("member.added", "workspace", {**CASES["member.added"][1], "admin": True}))
    for k in ENVELOPE - {"type"}:
        assert k in {"v", "id", "actor", "based_on", "hash_v", "auth", "roster_v"}


def test_events_in_the_wrong_log_or_with_the_wrong_label_refuse():
    with pytest.raises(CustodyError):
        describe_payload(payload("member.added", TICKET, CASES["member.added"][1]))
    with pytest.raises(CustodyError):
        describe_payload(payload("gate.approved", "workspace", CASES["gate.approved"][1]))
    ws = payload("gate.approved", TICKET, CASES["gate.approved"][1]).replace(
        canon.LABELS["sig_ticket_event"].encode(), canon.LABELS["sig_ws_event"].encode(), 1
    )
    with pytest.raises(CustodyError):
        describe_payload(ws)


def test_non_person_actors_contracts_and_shapes_refuse():
    p = payload("ticket.reopened", TICKET, {"text": "x"})
    body = json.loads(p[len(canon.LABELS["sig_ticket_event"]) :])
    label = canon.LABELS["sig_ticket_event"].encode()
    for mutate in (
        lambda b: b["event"].update(actor={"kind": "agent"}),
        lambda b: b.update(contract=2),
        lambda b: b.update(suite=1),
        lambda b: b.update(extra=1),
        lambda b: b.pop("log"),
    ):
        b = json.loads(json.dumps(body))
        mutate(b)
        with pytest.raises(CustodyError):
            describe_payload(label + canon.cj_checked(b))
    for junk in (b"", b"junk", label + b"not json", label + b"[]", label + b'{"a":1.5}'):
        with pytest.raises(CustodyError):
            describe_payload(junk)


def test_embedded_objects_with_unexpected_fields_refuse():
    log, fields = CASES["device.added"]
    odd = json.loads(json.dumps(fields))
    odd["cert"]["o"]["admin"] = True
    with pytest.raises(CustodyError, match="plain device_cert"):
        describe_payload(payload("device.added", log, odd))
    odd = json.loads(json.dumps(fields))
    odd["cert"] = "just text"
    with pytest.raises(CustodyError, match="signed object"):
        describe_payload(payload("device.added", log, odd))


def test_pk_objects_are_described_and_other_labels_refuse():
    cert = describe_payload(crypto.L["sig_device_cert"] + canon.cj_checked(CERT["o"]))
    d = dict(cert)
    assert d["signs"] == "device-cert" and d["scopes_max[1]"] == "look" and d["device_id"] == CERT["o"]["device_id"]
    rev = dict(describe_payload(crypto.L["sig_revocation"] + canon.cj_checked(REV["o"])))
    assert rev["reason"] == "lost" and rev["device_id"] == "bb" * 16
    deleg = dict(describe_payload(crypto.L["sig_ws_delegation"] + canon.cj_checked(DELEG["o"])))
    assert deleg["client_hosted"] == "true" and deleg["wsk_pub"] == "WSKPUB"
    with pytest.raises(CustodyError, match="plain device_cert"):
        describe_payload(crypto.L["sig_device_cert"] + canon.cj_checked({**CERT["o"], "admin": 1}))
    for label in ("sig_decision", "sig_bridge", "sig_enroll_request", "sig_relay_auth", "sig_card_wsk", "sig_publish"):
        with pytest.raises(CustodyError, match="not prompted"):
            describe_payload(crypto.L[label] + b"{}")


def test_too_many_lines_refuse_instead_of_truncating():
    many = {"set": {"repos": {f"r{i}": {"path": f"/p/{i}"} for i in range(MAX_LINES)}}}
    with pytest.raises(CustodyError, match="too many"):
        describe_payload(payload("settings.changed", "workspace", many))


def test_backend_refuses_before_prompting_for_unhandled_types(tmp_path):
    from tests.custody.test_backends import pass_backend

    b = pass_backend(tmp_path)
    b.create("dk")
    with pytest.raises(CustodyError, match="refusing to prompt"):
        b.sign("dk", payload("task.done", TICKET, {"task": "T1"}), action="")
    assert not any(r.kind == "unlock" for r in b.seen)
    ok = payload("ticket.reopened", TICKET, {"text": "x"})
    assert crypto.verify(b.public_key("dk"), b.sign("dk", ok, action=""), ok)


# --- canonical bytes, exact actor, field names (re-review P1, P2, names) -----------------------------------------------


def test_non_canonical_signing_bytes_refuse():
    good = payload("ticket.reopened", TICKET, {"text": "x"})
    label = canon.LABELS["sig_ticket_event"].encode()
    body = good[len(label) :]
    obj = json.loads(body)
    reordered = json.dumps(dict(reversed(list(obj.items()))), separators=(",", ":")).encode()
    assert reordered != body  # key order differs from the sorted canonical form
    for bad in (reordered, body.replace(b",", b", ", 1), b" " + body, body + b"\n", body.replace(b'"x"', b'"\\u0078"')):
        with pytest.raises(CustodyError, match="canonical"):
            describe_payload(label + bad)
    describe_payload(good)


def test_a_duplicate_key_in_the_bytes_refuses():
    label = canon.LABELS["sig_ticket_event"].encode()
    body = payload("ticket.reopened", TICKET, {"text": "x"})[len(label) :]
    dup = body.replace(b'"text":"x"', b'"text":"x","text":"y"')
    assert dup != body
    with pytest.raises(CustodyError):
        describe_payload(label + dup)


def test_actor_must_be_exactly_kind_id_device():
    log, fields = CASES["ticket.reopened"]
    base = {"kind": "person", "id": PERSON, "device": DEVICE}
    for bad in ({**base, "session": "s_x"}, {"kind": "person", "id": PERSON}, {"kind": "person", "device": DEVICE}, {}):
        with pytest.raises(CustodyError, match="actor must be exactly"):
            describe_payload(payload("ticket.reopened", log, fields, actor=bad))
    describe_payload(payload("ticket.reopened", log, fields, actor=base))


def test_long_field_names_refuse_instead_of_truncating():
    repos = {"a" * 100 + "1": {"path": "/x"}, "a" * 100 + "2": {"path": "/y"}}
    p = payload("settings.changed", "workspace", {"set": {"repos": repos}})
    with pytest.raises(CustodyError, match="too long"):
        rendered(p)
    ok = payload("settings.changed", "workspace", {"set": {"repos": {"demo": {"path": "/x"}}}})
    assert "set.repos.demo.path: /x" in rendered(ok)
