"""e2e-4: the host's prepared hub.joined / hub.left carries ``owner_proof: null`` (the signer fills it later).
The dashboard's own recomputation must leave it out like core's review does, or a hub step by click is refused with
"The host prepared something other than what was asked for"."""

from orch.host import review

from orch_dashboard.adapters import handoff as server


def _event(typ: str) -> dict:
    return {
        "v": 2, "id": "01M5WCHMTJHT0RM9JVTF5XBJBW", "type": typ, "actor": {"kind": "person", "id": "p_1"},
        "auth": "convenience", "hash_v": 1, "roster_v": 1, "based_on": "sha256:" + "0" * 64,
        "hub_id": "ff" * 16, "binding_seq": 1, "owner_proof": None,
    }  # fmt: skip


def test_owner_proof_types_match_core():
    assert set(server.OWNER_PROOF_TYPES) == set(review.OWNER_PROOF_TYPES)


def test_review_matches_ignores_null_owner_proof():
    for typ in server.OWNER_PROOF_TYPES:
        ev = _event(typ)
        payload = {k: v for k, v in ev.items() if k not in server.ENVELOPE and k != "owner_proof"}
        material = {"review": [{"event_id": ev["id"], "type": typ, "payload": payload}]}
        assert server.review_matches(material, [ev])


def test_fingerprint_equals_core():
    for typ in server.OWNER_PROOF_TYPES:
        ev = _event(typ)
        assert server.fingerprint("sha256:" + "1" * 64, [ev], "hub.join") == review.fingerprint(
            "sha256:" + "1" * 64, [ev], "hub.join"
        )


def test_a_non_null_owner_proof_fails_closed():
    for typ in server.OWNER_PROOF_TYPES:
        ev = {**_event(typ), "owner_proof": "sig"}
        payload = {k: v for k, v in _event(typ).items() if k not in server.ENVELOPE and k != "owner_proof"}
        material = {"review": [{"event_id": ev["id"], "type": typ, "payload": payload}]}
        assert not server.review_matches(material, [ev])
