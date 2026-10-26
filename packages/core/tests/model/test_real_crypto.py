"""End to end: a small workspace signed through orch.identity/custody (file-tier host key, passphrase-backend person
and device keys) replays through orch.model with orch.identity.CryptoVerifier."""

import copy

from orch import canon, crypto
from orch.custody import FileBackend, PassphraseBackend
from orch.custody.passphrase import KdfParams
from orch.identity import CryptoVerifier, certs, sign_person_event
from orch.model import Code, admit, replay

WS = "705d40abbb8c1c90354a1acaa94c935c"
UID = "01J9ZK4Q7M3R8T2V6X0B5N1C9D"
NOW = 1_790_000_000_000


def build(tmp_path):
    pw = PassphraseBackend(
        tmp_path / "pw", _passphrase_provider=lambda r: "Zq7!mPx2-vL9#rTb4w", _kdf=KdfParams(n=2**10), _min_n=2**10
    )
    host = FileBackend(tmp_path / "host")
    pk = pw.create("pk", role="person")
    dk = pw.create("dk", role="device")
    kx = crypto.public_bytes(crypto.generate_private_key())
    wsk = host.create("wsk")
    person = "p_" + crypto.person_id(pk).hex()
    device = "d_" + crypto.device_id(dk).hex()
    pk_sign = lambda payload: pw.sign("pk", payload, action="cert")  # noqa: E731
    cert = certs.make_device_cert(
        pk,
        pk_sign,
        dk_sig_pub=dk,
        dk_kx_pub=kx,
        label_sealed=b"lbl",
        created_ms=NOW,
        expires_ms=None,
        scopes_max=["look", "decide", "operate", "type"],
    )
    deleg = certs.make_delegation(pk, pk_sign, workspace_id=WS, wsk_pub=wsk, client_hosted=False, issued_ms=NOW)
    actor = {"kind": "person", "id": person, "device": device}
    log: dict[str, list] = {"workspace": [], UID: []}

    def append(name, event, signed=True):
        events = log[name]
        event.update(
            v=2,
            id="01J9ZP00000000000000000" + f"{sum(map(len, log.values())):03d}"[-3:],
            seq=len(events) + 1,
            prev=canon.event_head(events[-1]) if events else None,
            hash_v=1,
        )
        event["based_on"] = event["prev"]
        if signed:
            event.update(auth="passphrase")
            event["sig"] = sign_person_event(pw, "dk", WS, name, event, action=event["type"])
        event["host_sig"] = crypto.b64u(host.sign("wsk", canon.host_signing_bytes(WS, name, event), action="host"))
        events.append(event)
        return event

    append(
        "workspace",
        {
            "at": "2026-10-10T10:00:00Z",
            "type": "workspace.created",
            "actor": actor,
            "roster_v": 0,
            "workspace_id": WS,
            "prefix": "DEMO",
            "host_id": "h_01J9ZP0000000000000000000A",
            "wsk_pub": crypto.b64u(wsk),
            "owner": {"person": person, "name": "Owner", "pk_pub": crypto.b64u(pk)},
            "delegation": deleg,
            "device_cert": cert,
        },
    )

    gid = "gr_01J9ZP0000000000000000000G"
    append(
        "workspace",
        {
            "at": "2026-10-10T10:00:30Z",
            "type": "grant.issued",
            "actor": actor,
            "roster_v": 1,
            "grant": gid,
            "scope": "all",
            "verbs": "agent",
            "issued_at": "2026-10-10T10:00:30Z",
            "hours": 8,
            "expires_at": "2026-10-10T18:00:30Z",
            "secret_hash": canon.grant_secret_hash(b"s" * 32),
        },
    )
    agent = {
        "kind": "agent",
        "id": "claude-code",
        "session": "s_01J9ZP0000000000000000000S",
        "for": person,
        "grant": gid,
    }
    append(
        UID,
        {
            "at": "2026-10-10T10:01:00Z",
            "type": "ticket.created",
            "actor": agent,
            "ws_seq": 2,
            "key": "DEMO-0001",
            "ticket_type": "feature",
            "title": "Real keys",
            "owner": person,
        },
        signed=False,
    )
    append(
        UID,
        {"at": "2026-10-10T10:02:00Z", "type": "log.added", "actor": agent, "ws_seq": 2, "text": "hi"},
        signed=False,
    )
    append(
        UID,
        {
            "at": "2026-10-10T10:03:00Z",
            "type": "ticket.closed",
            "actor": actor,
            "roster_v": 1,
            "ws_seq": 2,
            "resolution": "other",
        },
    )
    return log, actor


def run(log, **kw):
    return replay(
        log["workspace"],
        {UID: log[UID]},
        verifier=CryptoVerifier(),
        now="2026-10-10T11:00:00Z",
        expected_workspace_id=WS,
        **kw,
    )


def test_a_real_signed_workspace_replays_clean(tmp_path):
    log, _ = build(tmp_path)
    s = run(log, expected_genesis=canon.event_head(log["workspace"][0]))
    assert not s.chain_errors and not s.workspace.invalid
    assert len(s.workspace.members) == 1 and s.tickets[UID].key == "DEMO-0001"
    assert not s.tickets[UID].frozen
    assert s.tickets[UID].status == "closed"  # the person's real signature on the ticket log counted
    nxt = copy.deepcopy(log[UID][-1])
    nxt.update(type="ticket.reopened", id="01J9ZP0000000000000000000Z", seq=4, prev=canon.event_head(log[UID][-1]))
    nxt.pop("resolution")
    nxt["based_on"] = nxt["prev"]
    nxt.pop("host_sig")
    assert admit(s, nxt, log=UID).code == Code.SIG_INVALID  # a reused signature is not this event's


def test_tampering_is_caught_by_the_real_verifier(tmp_path):
    log, _ = build(tmp_path)
    forged = copy.deepcopy(log)
    forged[UID][-2]["text"] = "edited"  # breaks host_sig: the chain stops there
    assert run(forged).chain_errors
    other = copy.deepcopy(log)
    other[UID][-1]["sig"] = log["workspace"][0]["sig"]
    # a person signature of another event: host_sig covers sig, so the host must re-sign; do that with the real key
    assert run(other).chain_errors
    assert run(log, expected_genesis="sha256:" + "0" * 64).chain_errors  # wrong pin
    s = replay(
        log["workspace"],
        {UID: log[UID]},
        verifier=CryptoVerifier(),
        now="2026-10-10T11:00:00Z",
        expected_workspace_id="f" * 32,
    )
    assert not s.workspace.members
