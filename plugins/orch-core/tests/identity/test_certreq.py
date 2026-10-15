"""H12c builder 8 and the pure half of 10 for R6 (design 11A.15.10): the cert request (host), its checks and opening
(primary), the host's checks of the primary's challenge and rejection, and the primary's challenge and rejection
builders, against the vendored relay corpus: every matching case, accept and refuse, exact counts. Ground truth is
orch-relay ``docs/protocol-v2.md`` §8.4 and the corpus; where the design text differs, the vectors win."""

from __future__ import annotations

import pytest

from orch import canon, crypto
from orch.identity import Refused, certreq, pairing, wscard

from ..relay import corpus

S2 = corpus.S2
KEYS = S2["keys"]
IDS = S2["ids"]
CR = S2["cert_request"]
CB = S2["cert_binding"]
REQ = CR["signed"]
INNER = CR["inner"]
PK = bytes.fromhex(KEYS["person_alice.sig"]["pub"])
WSK = bytes.fromhex(KEYS["wsk_a.sig"]["pub"])
_b = crypto.unb64u


def _key(name: str):
    seed = int.from_bytes(bytes.fromhex(KEYS[name]["seed"]), "big")
    return crypto.private_key_from_scalar(seed % (crypto.P256_N - 1) + 1)


PRIMARY_KX = _key("primary.kx")


def _sign_with(name: str):
    key = _key(name)
    return lambda payload: crypto.sign(key, payload)


def _unseal(object_id: bytes, sealed: bytes) -> bytes:
    """The primary's custody unseal, bound here to the corpus key (production: the ``kx`` custody backend, item 9)."""
    return crypto.open_sealed(PRIMARY_KX, bytes.fromhex(IDS["device_primary"]), "cert-request", object_id, 0, sealed)


# --- 8a. cert_request_o (host, §8.4 step 1) -------------------------------------------------------------------------


def _build_request(**kw) -> dict:
    a = {
        "request_id": REQ["o"]["request_id"],
        "workspace_id": REQ["o"]["workspace_id"],
        "person_id": REQ["o"]["person_id"],
        "primary_device_id": REQ["o"]["primary_device_id"],
        "primary_kx_pub": bytes.fromhex(KEYS["primary.kx"]["pub"]),
        "offer_id": INNER["offer_id"],
        "attempt": INNER["attempt"],
        "dk_sig_pub": _b(INNER["dk_sig_pub"]),
        "dk_kx_pub": _b(INNER["dk_kx_pub"]),
        "label": INNER["label"],
        "scopes_max": INNER["scopes_max"],
        "created_ms": REQ["o"]["created_ms"],
    }
    a.update(kw)
    return certreq.cert_request_o(**a)


ADMITTED = [c for c in CB["relay"] if c["op"] == "request" and "ok" in c["expect"]["result"]]


@pytest.mark.parametrize(
    "signed",
    [REQ, *(c["args"]["signed"] for c in ADMITTED)],
    ids=["cert_request.signed", *(c["name"] for c in ADMITTED)],
)
def test_cert_request_o_rebuilds_the_vector_request(signed):
    """Every field but ``sealed`` byte for byte (the seal draws a fresh ephemeral key); both seals open to ``cj`` of
    the same inner, whose ``label`` is the cleaned label unchanged."""
    o = signed["o"]
    built = _build_request(created_ms=o["created_ms"])
    assert {k: v for k, v in built.items() if k != "sealed"} == {k: v for k, v in o.items() if k != "sealed"}
    rid = bytes.fromhex(o["request_id"])
    assert _unseal(rid, _b(built["sealed"])) == _unseal(rid, _b(REQ["o"]["sealed"])) == canon.dumps(INNER)
    assert pairing.clean_label(INNER["label"]) == INNER["label"]


def test_cert_request_o_seals_the_label_as_cleaned_without_nfc():
    """No NFC anywhere (decided 15 Oct 2026): a decomposed label is sealed as ``clean_label`` returns it."""
    label = "Café phone"  # e + combining acute: NFD, cleaning keeps it
    assert pairing.clean_label(label) == label
    built = _build_request(label=label)
    inner = canon.loads_strict(_unseal(bytes.fromhex(built["request_id"]), _b(built["sealed"])))
    assert inner["label"] == label


@pytest.mark.parametrize(
    "kw",
    [
        {"label": "a\u0000b"},  # not the cleaned label
        {"label": "x" * 81},
        {"label": b"phone"},
        {"scopes_max": ["drop:" + "0" * 32]},
        {"scopes_max": ["decide"]},
        {"scopes_max": []},
        {"request_id": REQ["o"]["request_id"].upper()},
        {"offer_id": INNER["offer_id"][:-2]},
        {"attempt": bytes.fromhex(INNER["attempt"])},
        {"dk_sig_pub": b"\x04" + bytes(64)},
        {"dk_kx_pub": INNER["dk_kx_pub"]},
        {"primary_kx_pub": b"\x04" + bytes(64)},
        {"created_ms": -1},
        {"created_ms": 2**53 - 1},  # expires_ms would leave the safe range
        {"created_ms": True},
    ],
)
def test_cert_request_o_refuses(kw):
    with pytest.raises(Refused) as e:
        _build_request(**kw)
    assert e.value.code == "malformed"


# --- 8b. check_cert_request / open_cert_request (primary, §8.4 step 2) ----------------------------------------------


_PERSON_PK = {IDS["person_alice"]: PK, IDS["person_bob"]: bytes.fromhex(KEYS["person_bob.sig"]["pub"])}


def _card(case: dict) -> wscard.VerifiedCard:
    """The primary verifies the card under its own person's PK (Opus M1): another person's card is other_person."""
    return wscard.verify_card(case["card"], _PERSON_PK[case["person_id"]])


def _me(case: dict) -> dict:
    return {"me_person_id": case["person_id"], "me_device_id": IDS[f"device_{case['me']}"]}


def _primary(case: dict, step: dict, journal: certreq.CertJournal) -> dict:
    """§8.4 step 2 as a primary runs it: :func:`check_cert_request` then :func:`open_cert_request`; the result in the
    reference's ``{"ok": inner, "warning"} | {"refuse": code}`` form."""
    try:
        req = certreq.check_cert_request(
            step["signed"], _card(case), journal=journal, now_ms=step["now_ms"], **_me(case)
        )
        inner = certreq.open_cert_request(req, _unseal)
    except Refused as e:
        return {"refuse": e.code}
    return {"ok": inner.as_dict(), "warning": "client_hosted" if req.client_hosted else None, "req": req}


def _journal_after(journal: certreq.CertJournal, req: certreq.CertRequest) -> certreq.CertJournal:
    """What the primary records once it opened a request (the reference's ``seen`` and ``open``)."""
    return certreq.CertJournal(
        seen=journal.seen | {req.request_id},
        open=(
            *(e for e in journal.open if e[0] != req.workspace_id),
            (req.workspace_id, req.request_id, req.expires_ms),
        ),
        closed=journal.closed,
    )


PRIMARY_CASES = [*CR["cases"], *CB["primary"]]


@pytest.mark.parametrize("case", PRIMARY_CASES, ids=[c["name"] for c in PRIMARY_CASES])
def test_check_and_open_cert_request_vectors(case):
    journal = certreq.CertJournal()
    for step in case["steps"]:
        got = _primary(case, step, journal)
        req = got.pop("req", None)
        assert got == step["expect"]
        if req is not None:
            journal = _journal_after(journal, req)


OPEN_CASES = CR["open_cases"]
_OPEN_CARD = CR["cases"][0]["card"]
_OPEN_VCARD = wscard.verify_card(_OPEN_CARD, PK)
_OPEN_ME = {"me_person_id": CR["cases"][0]["person_id"], "me_device_id": IDS["device_primary"]}


@pytest.mark.parametrize("case", OPEN_CASES, ids=[c["name"] for c in OPEN_CASES])
def test_open_cases(case):
    """The next request after one expired or was rejected or signed (a request is open while unexpired and neither
    signed nor rejected). The ``challenge``/``sign`` steps are the primary's certificate flow (reusing
    ``make_device_cert``, item 10); they close a request in the journal here."""
    journal = certreq.CertJournal()
    reqs = {}
    for step in case["steps"]:
        if step["op"] == "open":
            got = _primary({"card": _OPEN_CARD, "person_id": _OPEN_ME["me_person_id"], "me": "primary"}, step, journal)
            req = got.pop("req", None)
            assert got == step["expect"]
            if req is not None:
                reqs[req.request_id] = req
                journal = _journal_after(journal, req)
        elif step["op"] == "reject" or (step["op"] == "sign" and "ok" in step["expect"]):
            journal = certreq.CertJournal(
                seen=journal.seen, open=journal.open, closed=journal.closed | {step["request_id"]}
            )


def test_cert_request_cases_are_all_found():
    assert len(CR["cases"]) == 10 and len(CB["primary"]) == 7 and len(OPEN_CASES) == 5
    steps = [s["expect"] for c in PRIMARY_CASES for s in c["steps"]]
    assert len(steps) == 19
    assert sorted(e.get("refuse", "ok") for e in steps) == sorted(
        [
            *["ok"] * 5,
            "bad_signature",
            "expired",
            *["malformed"] * 7,
            "not_for_this_device",
            "other_person",
            "replayed",
            "request_open",
            "scope_exceeded",
        ]
    )
    assert sum(s["op"] == "open" for c in OPEN_CASES for s in c["steps"]) == 8


RELAY_REQUESTS = [c for c in CB["relay"] if c["op"] == "request"]


@pytest.mark.parametrize("case", RELAY_REQUESTS, ids=[c["name"] for c in RELAY_REQUESTS])
def test_relay_request_cases_through_the_primary_shape_check(case):
    """The relay's admission shapes (G3b clear binding) are the primary's step 2.7 shapes: the admitted request passes
    :func:`check_cert_request`, each malformed one is refused ``malformed`` there."""
    args = case["args"]
    want = case["expect"]["result"]
    call = lambda: certreq.check_cert_request(  # noqa: E731
        args["signed"], _OPEN_VCARD, journal=certreq.CertJournal(), now_ms=args["now_ms"], **_OPEN_ME
    )
    if "refuse" in want:
        with pytest.raises(Refused) as e:
            call()
        assert e.value.code == want["refuse"]
    else:
        assert call().request_id == want["ok"]


def test_relay_request_cases_are_all_found():
    assert len(RELAY_REQUESTS) == 7 and len(ADMITTED) == 1


# --- 8c. the host's checks of the primary's challenge and rejection (11A.15.6 7c, 7d) --------------------------------

QUEUE = [c for c in CB["relay"] if c["op"] == "queue"]


@pytest.mark.parametrize("case", QUEUE, ids=[c["name"] for c in QUEUE])
def test_check_cert_challenge_and_rejection_vectors(case):
    """The relay's G3b queue checks, which the host repeats before it relays anything (under the pinned ``PK``,
    against its own stored request)."""
    args = case["args"]
    fn = {"challenge": certreq.check_cert_challenge, "rejection": certreq.check_cert_rejection}[args["action"]]
    req_o = case["state"]["request"]["o"]
    want = case["expect"]["result"]
    if "refuse" in want:
        with pytest.raises(Refused) as e:
            fn(args["signed"], bytes.fromhex(args["pk_pub"]), req_o)
        assert e.value.code == want["refuse"]
    else:
        got = fn(args["signed"], bytes.fromhex(args["pk_pub"]), req_o)
        assert got.request_id == req_o["request_id"]


def test_queue_cases_are_all_found():
    assert len(QUEUE) == 23
    assert sum(c["args"]["action"] == "challenge" for c in QUEUE) == 8
    assert sum("ok" in c["expect"]["result"] for c in QUEUE) == 3


def test_the_corpus_challenge_attack_challenge_and_rejection_against_their_requests():
    ch = certreq.check_cert_challenge(CR["challenge"], PK, REQ["o"])
    assert ch.cert_code == CR["cert_code"] and ch.dk_sig_pub == _b(INNER["dk_sig_pub"])
    with pytest.raises(Refused) as e:  # R1: a challenge for another key never matches the honest request
        certreq.check_cert_challenge(CR["attack_challenge"], PK, REQ["o"])
    assert e.value.code == "not_for_this_device"
    obj = S2["rejected_forms"]["objects"]
    rj = certreq.check_cert_rejection(obj["rejection"], PK, obj["request"]["o"])
    assert rj.dk_hash == _b(obj["request"]["o"]["dk_hash"])
    with pytest.raises(Refused) as e:
        certreq.check_cert_rejection(obj["rejection"], WSK, obj["request"]["o"])
    assert e.value.code == "bad_signature"


# --- 10 (pure half). make_cert_challenge / make_cert_rejection (primary) --------------------------------------------


def _inner() -> certreq.CertInner:
    case = CR["cases"][0]
    req = certreq.check_cert_request(
        REQ, _card(case), journal=certreq.CertJournal(), now_ms=REQ["o"]["created_ms"], **_me(case)
    )
    return certreq.open_cert_request(req, _unseal)


def test_make_cert_challenge_rebuilds_the_vector():
    got = certreq.make_cert_challenge(
        PK, _sign_with("person_alice.sig"), REQ["o"], _inner(), bytes.fromhex(CR["primary_nonce"])
    )
    assert got["o"] == CR["challenge"]["o"]
    assert crypto.L["sig_cert_challenge"] + canon.dumps(got["o"]) == bytes.fromhex(CR["signed_bytes_challenge"])
    assert pairing.cert_code(got["o"]) == CR["cert_code"]
    assert certreq.check_cert_challenge(got, PK, REQ["o"]).cert_code == CR["cert_code"]


def test_make_cert_rejection_rebuilds_the_vector():
    obj = S2["rejected_forms"]["objects"]
    got = certreq.make_cert_rejection(PK, _sign_with("person_alice.sig"), obj["request"]["o"])
    assert got["o"] == obj["rejection"]["o"]
    certreq.check_cert_rejection(got, PK, obj["request"]["o"])


@pytest.mark.parametrize(
    "call",
    [
        lambda: certreq.make_cert_challenge(PK, _sign_with("person_alice.sig"), REQ["o"], _inner(), bytes(31)),
        lambda: certreq.make_cert_challenge(PK, _sign_with("wsk_a.sig"), REQ["o"], _inner(), bytes(32)),  # wrong key
        lambda: certreq.make_cert_challenge(PK, lambda p: b"\0" * 63, REQ["o"], _inner(), bytes(32)),
        lambda: certreq.make_cert_challenge(PK, _sign_with("person_alice.sig"), REQ["o"], INNER, bytes(32)),
        lambda: certreq.make_cert_challenge(
            PK, _sign_with("person_alice.sig"), {**REQ["o"], "attempt": "0" * 32}, _inner(), bytes(32)
        ),
        lambda: certreq.make_cert_rejection(PK, _sign_with("wsk_a.sig"), REQ["o"]),
        lambda: certreq.make_cert_rejection(PK, _sign_with("person_alice.sig"), {**REQ["o"], "extra": 1}),
    ],
)
def test_make_refuses(call):
    with pytest.raises(Refused) as e:
        call()
    assert e.value.code in ("malformed", "bad_signature", "not_for_this_device")


def test_open_cert_request_refuses_a_bad_unseal():
    req = certreq.check_cert_request(
        REQ, _card(CR["cases"][0]), journal=certreq.CertJournal(), now_ms=REQ["o"]["created_ms"], **_me(CR["cases"][0])
    )

    def wrong(object_id: bytes, sealed: bytes) -> bytes:
        return crypto.open_sealed(
            PRIMARY_KX, bytes.fromhex(IDS["device_primary"]), "cert-request", object_id, 1, sealed
        )

    with pytest.raises(Refused) as e:
        certreq.open_cert_request(req, wrong)
    assert e.value.code == "malformed"


def test_the_seal_is_fresh_per_call():
    a, b = _build_request(), _build_request()
    assert a["sealed"] != b["sealed"]


def test_check_cert_request_takes_only_a_verified_card():
    """Opus M1: a card dict (verified or not) is refused; the attacker-WSK card never becomes a VerifiedCard."""
    with pytest.raises(Refused) as e:
        certreq.check_cert_request(
            REQ, _OPEN_CARD, journal=certreq.CertJournal(), now_ms=REQ["o"]["created_ms"], **_OPEN_ME
        )
    assert e.value.code == "malformed"
