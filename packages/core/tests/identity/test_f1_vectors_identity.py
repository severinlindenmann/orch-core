"""F1 signature vectors against the real verifier (ticket-format §5.3, §5.5, §5.11): the stored P-256 signatures verify
under ``CryptoVerifier`` / ``orch.crypto``, every replayed or tampered variant is refused, and the genesis checks fire
in their stated order. The vector files are written by the independent oracle (``tests/canon/oracle_f1_signed.py``)."""

import json
from pathlib import Path

import pytest

from orch import canon, crypto
from orch.identity import CryptoVerifier, members
from orch.identity.errors import Refused
from orch.model import SigContext

DIR = Path(__file__).parent.parent / "vectors" / "f1"
V = CryptoVerifier()


def load(name):
    return json.loads((DIR / name).read_text(encoding="utf-8"))


S = load("signatures.json")
W = S["workspace_id"]


def _ctx(case, log=None, ws=None):
    return SigContext(ws or case.get("workspace_id", W), log or case["log"], case["cert"])


# --- the keys are the published test keys -------------------------------------------------------------------


def test_published_keys_are_what_the_names_say():
    import hashlib

    for name, k in S["keys"].items():
        if name == "id_of":
            continue
        d = (
            int.from_bytes(hashlib.sha256(("orch-f1-test-key|" + name).encode()).digest(), "big") % (crypto.P256_N - 1)
            + 1
        )
        assert k["scalar_hex"] == f"{d:064x}"
        assert crypto.b64u(crypto.public_bytes(crypto.private_key_from_scalar(d))) == k["pub_b64u"]
    pk = crypto.unb64u(S["keys"]["pk_sev"]["pub_b64u"], crypto.PUB_LEN)
    assert S["keys"]["id_of"]["p_sev"] == "p_" + crypto.person_id(pk).hex()


def test_the_oracles_deterministic_ecdsa_is_rfc6979():
    """The oracle signs without orch; ``cryptography`` (when it has deterministic signing) gives the same bytes."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    from tests.canon import oracle_f1_world as ow

    try:
        ecdsa = ec.ECDSA(hashes.SHA256(), deterministic_signing=True)
    except TypeError:
        pytest.skip("this cryptography has no deterministic ECDSA")
    for name in ("wsk", "pk_sev", "dk_mara1"):
        priv = ec.derive_private_key(ow.key(name).d, ec.SECP256R1())
        r, s = decode_dss_signature(priv.sign(b"orch f1 rfc6979", ecdsa))
        assert ow.sign(name, b"orch f1 rfc6979") == r.to_bytes(32, "big") + s.to_bytes(32, "big")


# --- stored signatures verify -------------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["ticket_event", "ws_event"])
def test_stored_event_signatures_verify(which):
    c = S[which]
    e = c["event"]
    assert V.verify_person(e, _ctx(c))
    msg = canon.person_signing_bytes(W, c["log"], e)
    assert msg.hex() == c["person_signing_hex"]
    pub = crypto.unb64u(c["cert"]["o"]["dk_sig_pub"], crypto.PUB_LEN)
    assert crypto.verify(pub, crypto.unb64u(e["sig"], crypto.SIG_LEN), msg)
    assert canon.host_signing_bytes(W, c["log"], e).hex() == c["host_signing_hex"]
    wsk = crypto.unb64u(S["keys"]["wsk"]["pub_b64u"], crypto.PUB_LEN)
    assert V.verify_host(e, log=c["log"], wsk_pub=wsk, workspace_id=W)
    assert canon.event_head(e) == c["head"]


def test_the_host_signs_the_person_signature_too():
    """host_sig covers `sig`: the unsigned form of the ticket event does not verify."""
    c = S["ticket_event"]
    wsk = crypto.unb64u(S["keys"]["wsk"]["pub_b64u"], crypto.PUB_LEN)
    assert not V.verify_host(S["host_unsigned_ticket_event"], log=c["log"], wsk_pub=wsk, workspace_id=W)


# --- replay and tampering are refused -----------------------------------------------------------------------


@pytest.mark.parametrize("c", S["person_refused"], ids=lambda c: c["name"])
def test_person_signature_refusals(c):
    assert not V.verify_person(c["event"], _ctx(c))


@pytest.mark.parametrize("c", S["person_accepted_despite_change"], ids=lambda c: c["name"])
def test_person_signature_ignores_the_host_fields(c):
    assert V.verify_person(c["event"], _ctx(c))


@pytest.mark.parametrize("c", S["host_refused"], ids=lambda c: c["name"])
def test_host_signature_refusals(c):
    wsk = crypto.unb64u(c["wsk_pub"], crypto.PUB_LEN)
    assert not V.verify_host(c["event"], log=c["log"], wsk_pub=wsk, workspace_id=c["workspace_id"])


# --- the genesis checks, in order ---------------------------------------------------------------------------

G = load("genesis.json")
CHECK_CODE = {
    1: "genesis.owner_mismatch",
    2: "genesis.bad_delegation",
    3: "genesis.delegation_mismatch",
    4: "genesis.host_sig",
    5: "genesis.bad_device_cert",
    6: "genesis.bad_sig",
}


def test_a_valid_genesis_passes_all_six_checks_and_names_the_trust_root():
    e = G["valid"]["event"]
    r = members.check_genesis(e)
    assert r["owner_pk_pub"] == crypto.unb64u(e["owner"]["pk_pub"], crypto.PUB_LEN)
    assert canon.event_head(e) == G["genesis"] == G["valid"]["head"]
    assert V.verify_embedded(e, pk_pub=None)
    assert V.verify_host(e, log="workspace", wsk_pub=None, workspace_id=W)
    assert V.verify_person(e, SigContext(W, "workspace", e["device_cert"]))


@pytest.mark.parametrize("c", G["refused"], ids=lambda c: c["name"])
def test_genesis_refusals_name_the_first_failing_check(c):
    want = CHECK_CODE[c["check"]] if c["check"] else c["shape"]
    with pytest.raises(Refused) as ei:
        members.check_genesis(c["event"])
    assert ei.value.code == want
    # a verifier (the model's view) refuses it as well: either the embedded objects, host_sig or sig fail
    e = c["event"]
    ok = (
        V.verify_embedded(e, pk_pub=None)
        and V.verify_host(e, log="workspace", wsk_pub=None, workspace_id=W)
        and V.verify_person(e, SigContext(W, "workspace", e["device_cert"]))
    )
    assert not ok


# --- revocation: the identity-level codes of the model scenarios --------------------------------------------


def test_revocation_identity_codes():
    rv = load("revocation.json")["scenarios"][0]
    cases = [s for s in rv["identity_checks"]]
    assert [c["identity"] for c in cases] == ["device.reason_mismatch", "bad_signature", "bad_signature"]
    certs = {}
    for step in rv["steps"]:
        e = step["event"]
        if e["type"] in ("member.added",):
            certs["d_" + e["device_cert"]["o"]["device_id"]] = (e["pk_pub"], e["device_cert"]["o"])
    for c in cases:
        e = rv["steps"][c["step"]]["event"]
        pk_b64, cert_o = certs[e["device"]]
        with pytest.raises(Refused) as ei:
            members.check_device_revoked(e, crypto.unb64u(pk_b64, crypto.PUB_LEN), cert_o)
        assert ei.value.code == c["identity"]
