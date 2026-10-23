"""H12c builder 8 and the pure half of 10 for R6 (design 11A.15.10): the cert request (host), its checks and opening
(primary), the host's checks of the primary's challenge and rejection, and the primary's challenge and rejection
builders, against the vendored relay corpus: every matching case, accept and refuse, exact counts. Ground truth is
orch-relay ``docs/protocol-v2.md`` §8.4 and the corpus; where the design text differs, the vectors win."""

from __future__ import annotations

import pytest

from orch import canon, crypto
from orch.identity import Refused, certreq, pairing, wscard

from ..wsproto import corpus

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


def _req(signed: dict = REQ) -> certreq.CertRequest:
    return certreq.check_cert_request(
        signed, _OPEN_VCARD, journal=certreq.CertJournal(), now_ms=signed["o"]["created_ms"], **_OPEN_ME
    )


def _inner(req: certreq.CertRequest | None = None) -> certreq.CertInner:
    return certreq.open_cert_request(req or _req(), _unseal)


def _other_req(**kw) -> certreq.CertRequest:
    """A second genuine request (another ``request_id`` by default), checked as the primary checks any request."""
    kw.setdefault("request_id", "0" * 32)
    return _req(_pk_sign("wsk_a.sig", "sig_cert_request", _build_request(**kw)))


def test_make_cert_challenge_rebuilds_the_vector():
    req = _req()
    got = certreq.make_cert_challenge(
        PK, _sign_with("person_alice.sig"), req, _inner(req), bytes.fromhex(CR["primary_nonce"])
    )
    assert got["o"] == CR["challenge"]["o"]
    assert crypto.L["sig_cert_challenge"] + canon.dumps(got["o"]) == bytes.fromhex(CR["signed_bytes_challenge"])
    assert pairing.cert_code(got["o"]) == CR["cert_code"]
    assert certreq.check_cert_challenge(got, PK, REQ["o"]).cert_code == CR["cert_code"]


def test_make_cert_rejection_rebuilds_the_vector():
    obj = S2["rejected_forms"]["objects"]
    got = certreq.make_cert_rejection(PK, _sign_with("person_alice.sig"), _req(obj["request"]))
    assert got["o"] == obj["rejection"]["o"]
    certreq.check_cert_rejection(got, PK, obj["request"]["o"])


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (
            lambda: certreq.make_cert_challenge(PK, _sign_with("person_alice.sig"), _req(), _inner(), bytes(31)),
            "malformed",
        ),
        (
            lambda: certreq.make_cert_challenge(PK, _sign_with("wsk_a.sig"), _req(), _inner(), bytes(32)),
            "bad_signature",
        ),
        (lambda: certreq.make_cert_challenge(PK, lambda p: b"\0" * 63, _req(), _inner(), bytes(32)), "malformed"),
        (  # #501: a hand-built inner (here the corpus dict) is not a minted CertInner
            lambda: certreq.make_cert_challenge(PK, _sign_with("person_alice.sig"), _req(), INNER, bytes(32)),
            "malformed",
        ),
        (  # #501: a raw request object is not a minted CertRequest
            lambda: certreq.make_cert_challenge(PK, _sign_with("person_alice.sig"), REQ["o"], _inner(), bytes(32)),
            "malformed",
        ),
        (  # #501: an inner opened from another request
            lambda: certreq.make_cert_challenge(PK, _sign_with("person_alice.sig"), _other_req(), _inner(), bytes(32)),
            "not_for_this_device",
        ),
        (lambda: certreq.make_cert_rejection(PK, _sign_with("wsk_a.sig"), _req()), "bad_signature"),
        (
            lambda: certreq.make_cert_rejection(PK, _sign_with("person_alice.sig"), {**REQ["o"], "extra": 1}),
            "malformed",
        ),
        (lambda: certreq.make_cert_rejection(PK, _sign_with("person_alice.sig"), REQ["o"]), "malformed"),
    ],
)
def test_make_refuses(call, code):
    with pytest.raises(Refused) as e:
        call()
    assert e.value.code == code


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


# --- review round 1 of #495 ------------------------------------------------------------------------------------------


def _pk_sign(name: str, label: str, o: dict) -> dict:
    return {"o": o, "sig": crypto.b64u(crypto.sign(_key(name), crypto.L[label] + canon.dumps(o)))}


def test_challenge_with_another_expiry_is_refused():
    """Opus L3 (mutant M14): the challenge's expiry must be the request's."""
    o = {**CR["challenge"]["o"], "expires_ms": REQ["o"]["expires_ms"] + 1}
    with pytest.raises(Refused) as e:
        certreq.check_cert_challenge(_pk_sign("person_alice.sig", "sig_cert_challenge", o), PK, REQ["o"])
    assert e.value.code == "malformed"


def test_challenge_signed_by_another_pk_naming_the_owner_is_refused():
    """Opus L3 (mutant M1): person_id is the request's, but the signer (passed as pk_pub) is another person."""
    bob = bytes.fromhex(KEYS["person_bob.sig"]["pub"])
    signed = _pk_sign("person_bob.sig", "sig_cert_challenge", CR["challenge"]["o"])
    with pytest.raises(Refused) as e:
        certreq.check_cert_challenge(signed, bob, REQ["o"])
    assert e.value.code == "malformed"


def test_rejection_signed_by_another_pk_naming_the_owner_is_refused():
    """Opus L3 (mutant M7)."""
    obj = S2["rejected_forms"]["objects"]
    bob = bytes.fromhex(KEYS["person_bob.sig"]["pub"])
    signed = _pk_sign("person_bob.sig", "sig_cert_rejection", obj["rejection"]["o"])
    with pytest.raises(Refused) as e:
        certreq.check_cert_rejection(signed, bob, obj["request"]["o"])
    assert e.value.code == "other_person"


def test_request_living_longer_than_ten_minutes_is_expired():
    """Opus L3 (mutant M8): expires - created <= 10 min."""
    o = {**REQ["o"], "expires_ms": REQ["o"]["created_ms"] + certreq.CERT_PENDING_MS + 1}
    with pytest.raises(Refused) as e:
        certreq.check_cert_request(
            _pk_sign("wsk_a.sig", "sig_cert_request", o),
            _OPEN_VCARD,
            journal=certreq.CertJournal(),
            now_ms=REQ["o"]["created_ms"],
            **_OPEN_ME,
        )
    assert e.value.code == "expired"


def test_a_refused_request_id_is_recorded_and_cannot_come_back():
    """Opus L2: a request refused at step 2.7/2.8 names its id (``RequestRefused.request_id``); the primary records it
    as seen, so the same id with other content is ``replayed``."""
    case = next(c for c in CR["cases"] if c["name"] == "inner_with_a_host_nonce_is_malformed")
    step = case["steps"][0]
    journal = certreq.CertJournal()
    req = certreq.check_cert_request(step["signed"], _card(case), journal=journal, now_ms=step["now_ms"], **_me(case))
    with pytest.raises(Refused):
        certreq.open_cert_request(req, _unseal)
    journal = journal.with_seen(req.request_id)
    with pytest.raises(Refused) as e:
        certreq.check_cert_request(REQ, _card(case), journal=journal, now_ms=step["now_ms"], **_me(case))
    assert e.value.code == "replayed"
    shape = next(c for c in CB["primary"] if c["name"] == "request_without_the_clear_binding_malformed")
    with pytest.raises(certreq.RequestRefused) as e:
        certreq.check_cert_request(
            shape["steps"][0]["signed"],
            _card(shape),
            journal=certreq.CertJournal(),
            now_ms=shape["steps"][0]["now_ms"],
            **_me(shape),
        )
    assert (e.value.code, e.value.request_id) == ("malformed", REQ["o"]["request_id"])


def test_journal_helpers_record_open_and_close():
    req = certreq.check_cert_request(
        REQ, _OPEN_VCARD, journal=certreq.CertJournal(), now_ms=REQ["o"]["created_ms"], **_OPEN_ME
    )
    j = certreq.CertJournal().with_open(req)
    assert req.request_id in j.seen and j.open_request(req.workspace_id, req.created_ms) == req.request_id
    assert j.with_closed(req.request_id).open_request(req.workspace_id, req.created_ms) is None


def _inner_request(label: str) -> tuple[certreq.CertRequest, dict]:
    o = dict(_build_request(label=pairing.clean_label(label)))
    inner = {**INNER, "label": label}
    rid = bytes.fromhex(o["request_id"])
    o["sealed"] = crypto.b64u(
        crypto.seal(
            bytes.fromhex(KEYS["primary.kx"]["pub"]),
            bytes.fromhex(IDS["device_primary"]),
            "cert-request",
            rid,
            0,
            canon.dumps(inner),
        )
    )
    signed = _pk_sign("wsk_a.sig", "sig_cert_request", o)
    req = certreq.check_cert_request(
        signed, _OPEN_VCARD, journal=certreq.CertJournal(), now_ms=o["created_ms"], **_OPEN_ME
    )
    return req, inner


def test_open_cert_request_gives_a_display_label_and_bounds_the_label():
    """Opus L5: the raw sealed label beside its ``clean_label`` display form; above 80 code points is refused."""
    req, _ = _inner_request("Evil\u202ephone\u0000")
    got = certreq.open_cert_request(req, _unseal)
    assert got.label == "Evil\u202ephone\u0000" and got.display_label == "Evilphone"
    req, _ = _inner_request("x" * 80)
    assert certreq.open_cert_request(req, _unseal).display_label == "x" * 80
    req, _ = _inner_request("x" * 81)
    with pytest.raises(Refused) as e:
        certreq.open_cert_request(req, _unseal)
    assert e.value.code == "malformed"


def test_make_cert_challenge_checks_the_binding_before_signing():
    """Opus L6: a mismatched inner or request costs no person-key operation."""
    calls = []

    def sign(payload: bytes) -> bytes:
        calls.append(payload)
        return _sign_with("person_alice.sig")(payload)

    with pytest.raises(Refused) as e:
        certreq.make_cert_challenge(PK, sign, _other_req(), _inner(), bytes(32))
    assert e.value.code == "not_for_this_device" and calls == []
    bob = bytes.fromhex(KEYS["person_bob.sig"]["pub"])
    with pytest.raises(Refused):
        certreq.make_cert_challenge(bob, sign, _req(), _inner(), bytes(32))  # PK is not the request's person
    assert calls == []
    with pytest.raises(Refused):
        certreq.make_cert_rejection(bob, sign, _req())
    assert calls == []


# --- review round 2 of #495 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"offer_id": ["x"]},
        {"offer_id": "zz" * 16},
        {"attempt": {"a": 1}},
        {"device_id": 7},
        {"dk_hash": crypto.b64u(bytes(31))},
        {"dk_hash": None},
        {"sealed": "not b64u!"},
        {"sealed": ["x"]},
    ],
    ids=repr,
)
def test_a_malformed_clear_field_names_the_request_and_burns_its_id(change):
    """Codex P2: a correctly signed and addressed request with a valid request_id but a malformed clear field is a
    RequestRefused naming the id; once recorded, the corrected request under that id is replayed."""
    signed = _pk_sign("wsk_a.sig", "sig_cert_request", {**REQ["o"], **change})
    journal = certreq.CertJournal()
    with pytest.raises(certreq.RequestRefused) as e:
        certreq.check_cert_request(signed, _OPEN_VCARD, journal=journal, now_ms=REQ["o"]["created_ms"], **_OPEN_ME)
    assert (e.value.code, e.value.request_id) == ("malformed", REQ["o"]["request_id"])
    journal = journal.with_seen(e.value.request_id)
    with pytest.raises(Refused) as e:
        certreq.check_cert_request(REQ, _OPEN_VCARD, journal=journal, now_ms=REQ["o"]["created_ms"], **_OPEN_ME)
    assert e.value.code == "replayed"


def test_expired_and_request_open_burn_the_id():
    """D1-3: ``expired`` and ``request_open`` are RequestRefused too, so the host must retry with a new request_id."""
    with pytest.raises(certreq.RequestRefused) as e:
        certreq.check_cert_request(
            REQ, _OPEN_VCARD, journal=certreq.CertJournal(), now_ms=REQ["o"]["expires_ms"], **_OPEN_ME
        )
    assert e.value.code == "expired"
    blocked = certreq.CertJournal().with_open(_other_req())
    with pytest.raises(certreq.RequestRefused) as e:
        certreq.check_cert_request(REQ, _OPEN_VCARD, journal=blocked, now_ms=REQ["o"]["created_ms"], **_OPEN_ME)
    assert e.value.code == "request_open"


def test_with_open_replaces_the_workspaces_earlier_entry():
    """D1-3: the mutant that keeps the old entry (``rest = self.open``) is killed."""
    req = certreq.check_cert_request(
        REQ, _OPEN_VCARD, journal=certreq.CertJournal(), now_ms=REQ["o"]["created_ms"], **_OPEN_ME
    )
    j = certreq.CertJournal().with_open(_other_req(request_id="1" * 32)).with_open(req)
    assert j.open == ((req.workspace_id, req.request_id, req.expires_ms),)
    assert j.seen == frozenset({"1" * 32, req.request_id})
