"""H12c builders 1-4 for R6 pairing (design 11A.15.10), against the vendored relay corpus: every matching case, accept
and refuse. Ground truth is orch-relay ``docs/protocol-v2.md`` §4, §7.2, §8.3, §8.4 and the corpus; where the design
text differs, the vectors win (the PR lists each difference)."""

from __future__ import annotations

import pytest

from orch import crypto
from orch.identity import Refused, pairing
from orch.relay import wire

from ..relay import corpus

S2 = corpus.S2

# --- 1. wk_commit (protocol §7.2, corpus ``wk_commit``) -------------------------------------------------------------

WK_COMMIT = S2["wk_commit"]


@pytest.mark.parametrize("case", WK_COMMIT, ids=[c["name"] for c in WK_COMMIT])
def test_wk_commit_vectors(case):
    got = pairing.wk_commit(case["workspace_id"], case["epoch"], bytes.fromhex(case["wk"]))
    assert got == case["wk_commit"]


def test_wk_commit_covers_every_corpus_case():
    assert {c["name"] for c in WK_COMMIT} == {"epoch_1", "epoch_2", "other_workspace_same_key", "epoch_u32_max"}


_WS = WK_COMMIT[0]["workspace_id"]
_WK = bytes.fromhex(WK_COMMIT[0]["wk"])


@pytest.mark.parametrize(
    ("ws", "epoch", "wk"),
    [
        (_WS, 0, _WK),  # epoch 0 is the pairing epoch, never a key epoch
        (_WS, 2**32, _WK),  # above u32
        (_WS, -1, _WK),
        (_WS, True, _WK),  # a bool is not an epoch
        (_WS, 1.0, _WK),
        (_WS, "1", _WK),
        (_WS, 1, _WK[:31]),
        (_WS, 1, _WK + b"\0"),
        (_WS, 1, bytearray(_WK)),  # exactly bytes
        (_WS, 1, _WK.hex()),
        (_WS.upper(), 1, _WK),  # ids are lower-case hex
        (_WS[:-2], 1, _WK),
        (bytes.fromhex(_WS), 1, _WK),  # the text form only
    ],
)
def test_wk_commit_refuses_out_of_range_inputs(ws, epoch, wk):
    with pytest.raises(Refused) as e:
        pairing.wk_commit(ws, epoch, wk)
    assert e.value.code == "malformed"


# --- helpers: open the corpus's epoch-0 envelopes (K_pair) -----------------------------------------------------------


def _open_request(case: dict, step: dict) -> tuple[object, dict] | None:
    """The header and meta of an epoch-0 host case, or None when it cannot be opened (unknown offer, bad tag)."""
    try:
        h, body, _sig = wire.parse_envelope(bytes.fromhex(step["envelope"]), direction=1)
    except wire.WireError:
        return None
    offer = case["state"]["offers"].get(h.stream.hex())
    if h.epoch != 0 or offer is None:
        return None
    try:
        pt = wire.open_body(wire.k_pair(bytes.fromhex(offer["secret"]), h.workspace, h.stream), h.pack(), body)
    except wire.WireError:
        return None
    meta, _data = wire.parse_plaintext(pt)
    return h, meta


def _open_answer(ctx: dict, step: dict) -> dict | None:
    """The meta of a device-side epoch-0 answer chunk, or None when its tag does not open under the link's secret."""
    h, body, _sig = wire.parse_envelope(bytes.fromhex(step["envelope"]), direction=2)
    try:
        pt = wire.open_body(wire.k_pair(bytes.fromhex(ctx["secret"]), h.workspace, h.stream), h.pack(), body)
    except wire.WireError:
        return None
    return wire.parse_plaintext(pt)[0]


def _steps(case: dict) -> list[dict]:
    return [case] if "envelope" in case else case["steps"]


HOST_CASES = S2["bridge"]["host_cases"]
ANSWER_CASES = [
    *S2["bridge"]["pair_answers"],
    *S2["cert_binding"]["challenges"],
    *S2["approved_anchor"]["answers"],
]


def _answers() -> list[tuple[str, dict, dict, dict | None]]:
    """``(name, ctx, expect, opened meta)`` for every device-side epoch-0 answer step in the corpus."""
    out = []
    for c in ANSWER_CASES:
        for i, s in enumerate(_steps(c)):
            out.append((f"{c['name']}[{i}]", c["ctx"], s["expect"], _open_answer(c["ctx"], s)))
    return out


ANSWERS = _answers()
_b = crypto.unb64u


# --- 2a. sas (protocol §4, corpus ``bridge.sas`` and every case that shows the pairing code) ------------------------

SAS = S2["bridge"]["sas"]


def test_sas_vector():
    got = pairing.sas(
        SAS["workspace"],
        SAS["offer"],
        bytes.fromhex(SAS["dk_sig_pub"]),
        bytes.fromhex(SAS["dk_kx_pub"]),
        bytes.fromhex(SAS["wsk_pub"]),
        bytes.fromhex(SAS["sas_nonce"]),
    )
    assert got == SAS["code"]


SAS_HOST = [(c["name"], c, s) for c in HOST_CASES for s in _steps(c) if "sas" in s["expect"]]


@pytest.mark.parametrize(("name", "case", "step"), SAS_HOST, ids=[n for n, _, _ in SAS_HOST])
def test_sas_of_every_host_case_that_shows_the_code(name, case, step):
    """The host's side: the code over the request's keys, the offer's nonce and the own WSK."""
    h, meta = _open_request(case, step)
    st = case["state"]
    got = pairing.sas(
        h.workspace.hex(),
        h.stream.hex(),
        _b(meta["dk_sig_pub"]),
        _b(meta["dk_kx_pub"]),
        _b(st["wsk_pub"]),
        _b(st["offers"][h.stream.hex()]["sas_nonce"]),
    )
    assert got == step["expect"]["sas"]


SAS_DEVICE = [a for a in ANSWERS if "sas" in a[2]]


@pytest.mark.parametrize(("name", "ctx", "expect", "meta"), SAS_DEVICE, ids=[a[0] for a in SAS_DEVICE])
def test_sas_of_every_answer_that_shows_the_code(name, ctx, expect, meta):
    """The device's side: the code over the signed ``pending`` answer's five fields."""
    assert meta["state"] == "pending"
    got = pairing.sas(
        ctx["workspace"],
        ctx["offer"],
        _b(meta["dk_sig_pub"]),
        _b(meta["dk_kx_pub"]),
        _b(meta["wsk_pub"]),
        _b(meta["sas_nonce"]),
    )
    assert got == expect["sas"]


def test_sas_cases_are_all_found():
    assert len(SAS_HOST) >= 3 and len(SAS_DEVICE) >= 1


def _sas_args() -> list:
    return [
        SAS["workspace"],
        SAS["offer"],
        bytes.fromhex(SAS["dk_sig_pub"]),
        bytes.fromhex(SAS["dk_kx_pub"]),
        bytes.fromhex(SAS["wsk_pub"]),
        bytes.fromhex(SAS["sas_nonce"]),
    ]


_OFF_CURVE = b"\x04" + bytes(64)


@pytest.mark.parametrize(
    ("i", "bad"),
    [
        (0, SAS["workspace"].upper()),
        (0, bytes.fromhex(SAS["workspace"])),
        (1, SAS["offer"][:-1]),
        (2, _OFF_CURVE),
        (2, bytes.fromhex(SAS["dk_sig_pub"])[:-1]),
        (3, _OFF_CURVE),
        (3, SAS["dk_kx_pub"]),
        (4, _OFF_CURVE),
        (5, bytes.fromhex(SAS["sas_nonce"])[:31]),
        (5, bytes.fromhex(SAS["sas_nonce"]) + b"\0"),
        (5, SAS["sas_nonce"]),
    ],
)
def test_sas_refuses_bad_inputs(i, bad):
    args = _sas_args()
    args[i] = bad
    with pytest.raises(Refused) as e:
        pairing.sas(*args)
    assert e.value.code == "malformed"


# --- 2b. cert_code (protocol §4, §8.4; corpus ``cert_request.cert_code`` and every challenge shown) -----------------

CHALLENGE = S2["cert_request"]["challenge"]["o"]


def test_cert_code_vector():
    assert pairing.cert_code(CHALLENGE) == S2["cert_request"]["cert_code"]


CHALLENGE_HOST = [
    (c["name"], s["expect"]["answer"]["challenge"])
    for c in HOST_CASES
    for s in _steps(c)
    if (s["expect"].get("answer") or {}).get("challenge")
]


@pytest.mark.parametrize(("name", "challenge"), CHALLENGE_HOST, ids=[n for n, _ in CHALLENGE_HOST])
def test_cert_code_of_every_challenge_a_host_relays(name, challenge):
    assert pairing.cert_code(challenge["o"]) == S2["cert_request"]["cert_code"]


CHALLENGE_DEVICE = [a for a in ANSWERS if a[3] is not None and a[3].get("challenge")]


@pytest.mark.parametrize(("name", "ctx", "expect", "meta"), CHALLENGE_DEVICE, ids=[a[0] for a in CHALLENGE_DEVICE])
def test_cert_code_of_every_challenge_a_device_receives(name, ctx, expect, meta):
    """A shown code equals the vector's; the old shape (no ``offer_id``/``attempt``) is refused; every other challenge
    (dropped for a reason that is not its shape) still has a code."""
    o = meta["challenge"]["o"]
    if expect.get("why") == "challenge_signature" and set(o) != pairing.CHALLENGE_FIELDS:
        with pytest.raises(Refused):
            pairing.cert_code(o)
        return
    code = pairing.cert_code(o)
    if "cert_code" in expect:
        assert code == expect["cert_code"]
    assert len(code) == 6 and set(code) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def test_cert_code_cases_are_all_found():
    shown = [a for a in CHALLENGE_DEVICE if "cert_code" in a[2]]
    assert len(CHALLENGE_HOST) >= 1 and len(shown) >= 3
    assert any(a[0].startswith("challenge_in_the_old_shape_dropped") for a in CHALLENGE_DEVICE)


def _mut(**kw) -> dict:
    o = dict(CHALLENGE)
    for k, v in kw.items():
        if v is _DEL:
            del o[k]
        else:
            o[k] = v
    return o


_DEL = object()


@pytest.mark.parametrize(
    "o",
    [
        _mut(offer_id=_DEL),
        _mut(attempt=_DEL),
        _mut(extra=1),
        _mut(kind="cert_request"),
        _mut(v=1),
        _mut(suite=1),
        _mut(request_id=CHALLENGE["request_id"].upper()),
        _mut(person_id=CHALLENGE["person_id"][:-2]),
        _mut(dk_sig_pub=crypto.b64u(_OFF_CURVE)),
        _mut(dk_kx_pub=CHALLENGE["dk_kx_pub"] + "A"),
        _mut(primary_nonce=crypto.b64u(bytes(31))),
        _mut(expires_ms=True),
        _mut(expires_ms=1.5),
        _mut(expires_ms=-1),
        _mut(expires_ms=2**53),
        [CHALLENGE],
        {"o": CHALLENGE, "sig": "x"},  # the object, not the signed wrapper
    ],
)
def test_cert_code_refuses_a_challenge_outside_the_exact_shape(o):
    with pytest.raises(Refused) as e:
        pairing.cert_code(o)
    assert e.value.code == "malformed"


# --- 3. pair links (protocol §8.3 step 1, D45/D46; corpus ``bridge.links``) ------------------------------------------

LINKS = S2["bridge"]["links"]
_VALID = next(c for c in LINKS if c["name"] == "valid")
_P = _VALID["parsed"]
_ST = next(c for c in HOST_CASES if c["name"] == "pair_first_time")["state"]
ORIGIN = "https://relay.example.org"
ORIGIN_ENC = "https%3A%2F%2Frelay.example.org"


def _fields(parsed: dict) -> dict:
    return {
        "workspace_id": parsed["workspace"],
        "offer_id": parsed["offer"],
        "secret": bytes.fromhex(parsed["secret"]),
        "wsk_pin": bytes.fromhex(parsed["wsk_pin"]),
        "pk_pin": bytes.fromhex(parsed["pk_pin"]),
    }


def _as_dict(f) -> dict:
    return {k: getattr(f, k) for k in ("workspace_id", "offer_id", "secret", "wsk_pin", "pk_pin")}


def _build(origin: str = ORIGIN) -> tuple[str, str]:
    return pairing.pair_link(
        origin, _P["workspace"], _P["offer"], bytes.fromhex(_P["secret"]), _b(_ST["wsk_pub"]), _b(_ST["owner_pk_pub"])
    )


def test_pair_link_builds_the_vector_fragment_in_both_forms():
    """The pins come from the keys: the host passes its WSK and the owner's PK, never pins."""
    assert crypto.wsk_pin(_b(_ST["wsk_pub"])).hex() == _P["wsk_pin"]
    assert crypto.pk_pin(_b(_ST["owner_pk_pub"])).hex() == _P["pk_pin"]
    https, native = _build()
    assert https == f"{ORIGIN}/pair#{_VALID['fragment']}"
    assert native == f"orch://pair?relay={ORIGIN_ENC}#{_VALID['fragment']}"


@pytest.mark.parametrize("case", LINKS, ids=[c["name"] for c in LINKS])
def test_parse_pair_fragment_vectors(case):
    """The relay's fragment rule (an optional leading ``#``), accept and refuse."""
    if case["parsed"] is None:
        with pytest.raises(Refused) as e:
            pairing.parse_pair_fragment(case["fragment"])
        assert e.value.code == "malformed"
    else:
        assert _as_dict(pairing.parse_pair_fragment(case["fragment"])) == _fields(case["parsed"])


@pytest.mark.parametrize("case", LINKS, ids=[c["name"] for c in LINKS])
@pytest.mark.parametrize("form", ["https", "native"])
def test_parse_pair_link_vectors_in_both_forms(case, form):
    """Every corpus fragment inside each whole link. The link carries exactly one ``#``, so the ``with_hash`` fragment
    is accepted once its own ``#`` is the link's and refused when doubled."""
    frag = case["fragment"].removeprefix("#")
    head = f"{ORIGIN}/pair" if form == "https" else f"orch://pair?relay={ORIGIN_ENC}"
    text = f"{head}#{frag}"
    if case["parsed"] is None:
        with pytest.raises(Refused):
            pairing.parse_pair_link(text)
    else:
        link = pairing.parse_pair_link(text)
        assert link.origin == ORIGIN and link.native is (form == "native")
        assert _as_dict(link.fields) == _fields(case["parsed"])
    if case["fragment"].startswith("#"):
        with pytest.raises(Refused):
            pairing.parse_pair_link(f"{head}#{case['fragment']}")


@pytest.mark.parametrize(
    "origin",
    ["https://relay.example.org", "https://relay.example.org:8443", "http://127.0.0.1:8787", "http://localhost"],
)
def test_pair_link_round_trips(origin):
    https, native = _build(origin)
    for text, is_native in ((https, False), (native, True)):
        link = pairing.parse_pair_link(text)
        assert (link.origin, link.native) == (origin, is_native)
        assert _as_dict(link.fields) == _fields(_P)
    assert pairing.render_pair_link(pairing.parse_pair_link(native)) == native


def test_pair_link_repr_never_shows_the_secret():
    link = pairing.parse_pair_link(_build()[1])
    assert _P["secret"] not in repr(link) and _b(_VALID["fragment"].split(".")[3]).hex() not in repr(link.fields)
    assert _VALID["fragment"].split(".")[3] not in repr(link)


_F = _VALID["fragment"]


@pytest.mark.parametrize(
    "text",
    [
        f"{ORIGIN}/app/pair#{_F}",  # an alias, not the canonical spelling
        f"{ORIGIN}/pair/#{_F}",
        f"{ORIGIN}//pair#{_F}",
        f"{ORIGIN}/pair?x=1#{_F}",
        f"{ORIGIN}/pair",
        f"{ORIGIN}/pair#",
        f"https://user@relay.example.org/pair#{_F}",
        f"https://RELAY.example.org/pair#{_F}",
        f"HTTPS://relay.example.org/pair#{_F}",
        f"http://relay.example.org/pair#{_F}",  # http only for loopback
        f"https://relay.example.org:0/pair#{_F}",
        f"https://relay.example.org:08443/pair#{_F}",
        f"https://relay.example.org:65536/pair#{_F}",
        f" {ORIGIN}/pair#{_F}",
        f"{ORIGIN}/pair#{_F}\n",
        f"{ORIGIN}/pair#{_F}#",
        f"{ORIGIN}/pair#{_F}.",
        f"{ORIGIN}/pair#{_F.replace('v2.', 'V2.')}",
        f"orch://pair#{_F}",  # no relay origin
        f"orch://pair?relay={ORIGIN}#{_F}",  # not percent-encoded
        f"orch://pair?relay=https%3a%2f%2frelay.example.org#{_F}",  # lower-case escapes are not canonical
        f"orch://pair?relay=https%3A%2F%2Frelay%2Eexample.org#{_F}",  # an unreserved character escaped
        f"orch://pair?relay={ORIGIN_ENC}&relay={ORIGIN_ENC}#{_F}",
        f"orch://pair?relay={ORIGIN_ENC}&x=1#{_F}",
        f"orch://pair?x=1&relay={ORIGIN_ENC}#{_F}",
        f"orch://pair?relay=http%3A%2F%2Fevil.example.org#{_F}",
        f"orch://pair?relay={ORIGIN_ENC}%2Fpath#{_F}",
        f"orch://pair?relay=https%3A%2F%2Fuser%40relay.example.org#{_F}",
        f"orch://pair?relay=%ZZ#{_F}",
        f"orch://pair?relay=#{_F}",
        f"orch://pair/?relay={ORIGIN_ENC}#{_F}",
        f"ORCH://pair?relay={ORIGIN_ENC}#{_F}",
        f"orch://Pair?relay={ORIGIN_ENC}#{_F}",
        f"orch://pair?relay={ORIGIN_ENC}#{_F}x",
        f"{ORIGIN}/pair#{_F}" + "A" * 2048,
        "",
        "é" + f"{ORIGIN}/pair#{_F}",
    ],
)
def test_parse_pair_link_refuses_every_non_canonical_spelling(text):
    with pytest.raises(Refused) as e:
        pairing.parse_pair_link(text)
    assert e.value.code == "malformed"


@pytest.mark.parametrize("bad", [None, b"https://x/pair#v2", 1])
def test_parse_pair_link_refuses_non_text(bad):
    with pytest.raises(Refused):
        pairing.parse_pair_link(bad)
    with pytest.raises(Refused):
        pairing.parse_pair_fragment(bad)


@pytest.mark.parametrize(
    ("i", "bad"),
    [
        (0, "https://relay.example.org/"),
        (0, "http://relay.example.org"),
        (0, "https://relay.example.org/pair"),
        (0, "https://Relay.example.org"),
        (1, _P["workspace"].upper()),
        (2, bytes.fromhex(_P["offer"])),
        (3, bytes.fromhex(_P["secret"])[:31]),
        (3, _P["secret"]),
        (4, _OFF_CURVE),
        (4, bytes.fromhex(_P["wsk_pin"])),  # a pin is not a key: the builder takes the key
        (5, _OFF_CURVE),
    ],
)
def test_pair_link_refuses_bad_inputs(i, bad):
    args = [
        ORIGIN,
        _P["workspace"],
        _P["offer"],
        bytes.fromhex(_P["secret"]),
        _b(_ST["wsk_pub"]),
        _b(_ST["owner_pk_pub"]),
    ]
    args[i] = bad
    with pytest.raises(Refused) as e:
        pairing.pair_link(*args)
    assert e.value.code == "malformed"
