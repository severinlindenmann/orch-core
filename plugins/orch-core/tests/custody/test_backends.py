"""Custody backends (D64-D66, ticket-format §5.3 / §12 O2): passphrase key files, the file tier, unavailable ones."""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from orch import canon, crypto
from orch.custody import (
    AUTH_VALUES,
    PLANNED,
    BackendUnavailable,
    CustodyError,
    FileBackend,
    KdfParams,
    KeyExists,
    KeyNotFound,
    NoPrompt,
    PassphraseBackend,
    PassphraseRequest,
    WrongPassphrase,
    get_backend,
    tty_passphrase_provider,
)
from orch.custody import passphrase as pp

FAST = KdfParams(n=2**10)
WS = "705d40abbb8c1c90354a1acaa94c935c"
TICKET = "01J9ZP0000000000000000000A"
PHRASE = "correct horse battery"


def person_payload(extra="x"):
    ev = {
        "v": 2,
        "id": "01J9ZP0000000000000000000B",
        "type": "ticket.updated",
        "actor": {"kind": "person"},
        "hash_v": 1,
        "n": extra,
    }
    return canon.person_signing_bytes(WS, TICKET, ev)


def host_payload():
    ev = {
        "v": 2,
        "id": "01J9ZP0000000000000000000B",
        "type": "x",
        "seq": 1,
        "at": "2026-10-10T10:00:00Z",
        "prev": None,
        "hash_v": 1,
    }
    return canon.host_signing_bytes(WS, "workspace", ev)


def pass_backend(tmp_path, phrase=PHRASE, **kw):
    seen: list[PassphraseRequest] = []

    def provider(req):
        seen.append(req)
        return phrase

    b = PassphraseBackend(tmp_path / "keys", passphrase_provider=provider, kdf=FAST, min_n=2**10, **kw)
    b.seen = seen  # type: ignore[attr-defined]
    return b


# --- passphrase backend ---------------------------------------------------------------------------------------------


def test_create_sign_verify_round_trip(tmp_path):
    b = pass_backend(tmp_path)
    pub = b.create("dk-sig")
    assert len(pub) == 65 and b.public_key("dk-sig") == pub
    payload = person_payload()
    sig = b.sign("dk-sig", payload, action="approve requirements")
    assert crypto.verify(pub, sig, payload)
    assert b.seen[-1].kind == "unlock" and b.seen[-1].action == "approve requirements"
    assert b.seen[-1].digest == crypto.sha256(payload).hex()[:32]


def test_backend_metadata():
    assert PassphraseBackend.auth == "passphrase" and PassphraseBackend.auth in AUTH_VALUES
    assert PassphraseBackend.person_capable is True
    assert FileBackend.auth is None and FileBackend.person_capable is False


def test_key_file_is_0600_and_holds_no_plaintext_scalar(tmp_path):
    b = pass_backend(tmp_path)
    b.create("pk")
    path = tmp_path / "keys" / "pk.key.json"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    doc = json.loads(path.read_text())
    assert doc["kdf"]["name"] == "scrypt" and doc["kdf"]["n"] == 2**10
    assert set(doc) == {"v", "key_id", "pub", "kdf", "nonce", "ct"}


def test_import_secret_gives_that_key(tmp_path):
    b = pass_backend(tmp_path)
    key = crypto.generate_private_key()
    pub = b.create("pk", secret=crypto.private_scalar(key))
    assert pub == crypto.public_bytes(key)
    payload = person_payload()
    assert crypto.verify(pub, b.sign("pk", payload, action="sign"), payload)


def test_wrong_passphrase_is_refused_and_signs_nothing(tmp_path):
    b = pass_backend(tmp_path)
    b.create("dk")
    bad = PassphraseBackend(tmp_path / "keys", passphrase_provider=lambda r: "not the phrase", kdf=FAST, min_n=2**10)
    with pytest.raises(WrongPassphrase):
        bad.sign("dk", person_payload(), action="a")
    empty = PassphraseBackend(tmp_path / "keys", passphrase_provider=lambda r: "", kdf=FAST, min_n=2**10)
    with pytest.raises(WrongPassphrase):
        empty.sign("dk", person_payload(), action="a")


def test_short_passphrase_is_refused_at_create(tmp_path):
    b = pass_backend(tmp_path, phrase="short")
    with pytest.raises(CustodyError):
        b.create("dk")
    assert not b.exists("dk")


def test_create_refuses_existing_key_and_overwrites_nothing(tmp_path):
    b = pass_backend(tmp_path)
    pub = b.create("dk")
    with pytest.raises(KeyExists):
        b.create("dk")
    assert b.public_key("dk") == pub


def test_never_caches_two_signatures_run_the_kdf_twice(tmp_path, monkeypatch):
    b = pass_backend(tmp_path)
    b.create("dk")
    calls = []
    real = pp._derive
    monkeypatch.setattr(pp, "_derive", lambda *a: (calls.append(1), real(*a))[1])
    b.sign("dk", person_payload("1"), action="a")
    b.sign("dk", person_payload("2"), action="a")
    assert len(calls) == 2
    assert [r.kind for r in b.seen if r.kind == "unlock"] == ["unlock", "unlock"], "asked each time"


def test_no_key_material_is_held_between_signatures(tmp_path):
    b = pass_backend(tmp_path)
    b.create("dk")
    b.sign("dk", person_payload(), action="a")
    held = list(vars(b).values())
    for v in held:
        assert not isinstance(v, (bytes, bytearray)), v
        assert "EllipticCurvePrivateKey" not in type(v).__name__
    assert set(vars(b)) <= {"_dir", "_provider", "_kdf", "_min_n", "seen"}


def test_scalar_buffer_is_zeroised(tmp_path, monkeypatch):
    b = pass_backend(tmp_path)
    b.create("dk")
    buffers = []
    real = pp._zeroise

    def spy(buf):
        buffers.append(buf)
        real(buf)

    monkeypatch.setattr(pp, "_zeroise", spy)
    b.sign("dk", person_payload(), action="a")
    assert buffers and all(not any(x) for x in buffers)


def _no_tty(monkeypatch):
    def boom(*a, **k):
        raise OSError("no tty")

    monkeypatch.setattr(pp.os, "open", boom)


def test_default_provider_refuses_without_a_terminal(tmp_path, monkeypatch):
    b = PassphraseBackend(tmp_path / "k", kdf=FAST, min_n=2**10)
    _no_tty(monkeypatch)
    with pytest.raises(NoPrompt):
        b.create("dk")
    with pytest.raises(NoPrompt):
        tty_passphrase_provider(PassphraseRequest("unlock", "dk", "approve", "abcd"))


def test_default_provider_never_touches_stdio(monkeypatch, capsys):
    """Stdin/stdout/stderr may belong to an agent: even with all three TTYs the provider uses /dev/tty only."""
    _no_tty(monkeypatch)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        monkeypatch.setattr(stream, "isatty", lambda: True, raising=False)
    with pytest.raises(NoPrompt):
        tty_passphrase_provider(PassphraseRequest("unlock", "dk", "approve", "abcd"))
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_tty_provider_writes_the_prompt_to_the_tty_and_reads_from_it(monkeypatch, capsys):
    written = []
    monkeypatch.setattr(pp, "_open_tty", lambda: (10, 11, lambda: written.append("closed")))
    monkeypatch.setattr(pp, "_read_secret", lambda r, w, prompt: (written.append(prompt), "phrase-ok-1")[1])
    req = PassphraseRequest("unlock", "dk", "approve gate", "ab" * 16)
    assert tty_passphrase_provider(req) == "phrase-ok-1"
    assert "action: approve gate" in written[0] and "sha256: " + "ab" * 16 in written[0]
    assert written[-1] == "closed"


def test_prompt_escapes_hostile_values_and_cannot_be_forged():
    evil_action = "approve\x1b[2J\x1b]0;pwned\x07\rSign: approve gate\nsha256: " + "0" * 32 + "\u202etxet\u200b"
    req = PassphraseRequest("unlock", "dk\x1b[31m", evil_action, "ab" * 16)
    text = pp.render_prompt(req)
    lines = text.split("\n")
    assert "\x1b" not in text and "\r" not in text and "\x07" not in text
    assert "\u202e" not in text and "\u200b" not in text
    # exactly one line per fixed label, and the real hash is the last field
    assert [ln.split(":")[0] for ln in lines if ln.startswith(("key", "action", "sha256"))] == [
        "key",
        "action",
        "sha256",
    ]
    assert sum(ln.startswith("sha256: ") for ln in lines) == 1
    assert lines[-2] == "sha256: " + "ab" * 16
    action_line = next(ln for ln in lines if ln.startswith("action: "))
    assert "U+001B" in action_line and "U+0007" in action_line
    short = pp.render_prompt(PassphraseRequest("unlock", "dk", "a\u202eb\u200bc\rd", "ab" * 16))
    assert "U+202E" in short and "U+200B" in short and "\r" not in short and "\u202e" not in short
    assert "Sign: approve gate" in action_line  # present only as inert text on the action line
    assert not any(ln.startswith("Sign:") for ln in lines)


def test_prompt_values_are_length_limited():
    text = pp.render_prompt(PassphraseRequest("unlock", "dk", "x" * 5000, "ab" * 16))
    assert len(text) < 400
    assert pp.shown("x" * 5000).endswith("\u2026") and len(pp.shown("x" * 5000)) == 80


def test_non_hex_digest_is_escaped_too():
    text = pp.render_prompt(PassphraseRequest("unlock", "dk", "a", "\x1b[0m" + "f" * 100))
    assert "\x1b" not in text


def test_backend_passes_the_signing_hash_not_caller_text(tmp_path):
    b = pass_backend(tmp_path)
    b.create("dk")
    payload = person_payload()
    b.sign("dk", payload, action="approve\nsha256: 0000\x1b[2J")
    req = b.seen[-1]
    assert req.digest == crypto.sha256(payload).hex()[:32]
    assert "\n" not in req.action and "\x1b" not in req.action


def test_passphrase_key_refuses_host_labels_and_unlabelled_bytes(tmp_path):
    b = pass_backend(tmp_path)
    b.create("dk")
    for bad in (host_payload(), b"anything", b"", crypto.L["sig_card_wsk"] + b"{}", crypto.L["h_person_id"] + b"x"):
        with pytest.raises(CustodyError):
            b.sign("dk", bad, action="a")
    assert not any(r.kind == "unlock" for r in b.seen), "refused before prompting"


def test_tampered_key_file_is_refused(tmp_path):
    b = pass_backend(tmp_path)
    b.create("dk")
    path = tmp_path / "keys" / "dk.key.json"
    good = json.loads(path.read_text())
    other = pass_backend(tmp_path / "other")
    other_pub = other.create("dk")
    # swap in another key's public half: the AAD no longer matches
    doc = dict(good, pub=crypto.b64u(other_pub))
    path.write_text(json.dumps(doc))
    with pytest.raises(WrongPassphrase):
        b.sign("dk", person_payload(), action="a")
    # a weakened KDF is refused before any work
    doc = json.loads(json.dumps(good))
    doc["kdf"]["n"] = 2
    path.write_text(json.dumps(doc))
    with pytest.raises(CustodyError, match="corrupt"):
        b.sign("dk", person_payload(), action="a")
    doc["kdf"]["n"] = 2**25
    path.write_text(json.dumps(doc))
    with pytest.raises(CustodyError, match="corrupt"):
        b.sign("dk", person_payload(), action="a")
    path.write_text("not json")
    with pytest.raises(CustodyError):
        b.public_key("dk")


def test_default_kdf_floor_applies_to_files(tmp_path):
    weak = pass_backend(tmp_path)  # n = 2^10 file
    weak.create("dk")
    strict = PassphraseBackend(tmp_path / "keys", passphrase_provider=lambda r: PHRASE)
    with pytest.raises(CustodyError, match="corrupt"):
        strict.sign("dk", person_payload(), action="a")


def test_bad_kdf_parameters_are_refused_by_the_constructor(tmp_path):
    for p in (KdfParams(n=2**10 + 1), KdfParams(n=2**21), KdfParams(n=2**10, r=1), KdfParams(n=2**10, p=2)):
        with pytest.raises(CustodyError):
            PassphraseBackend(tmp_path / "k", kdf=p, min_n=2**10)
    with pytest.raises(CustodyError):
        PassphraseBackend(tmp_path / "k", kdf=KdfParams(n=2**10))  # below the default floor


def test_default_parameters_are_scrypt_2_17():
    from orch.custody import DEFAULT_KDF

    assert (DEFAULT_KDF.n, DEFAULT_KDF.r, DEFAULT_KDF.p) == (2**17, 8, 1)


def test_key_ids_cannot_escape_the_directory(tmp_path):
    b = pass_backend(tmp_path)
    for bad in ("../x", "a/b", "", ".hidden", "A", "a..b", "x" * 65, 5):
        with pytest.raises(CustodyError):
            b.create(bad)  # type: ignore[arg-type]
    f = FileBackend(tmp_path / "f")
    with pytest.raises(CustodyError):
        f.create("../x")


def test_delete_and_missing(tmp_path):
    b = pass_backend(tmp_path)
    b.create("dk")
    assert b.exists("dk")
    b.delete("dk")
    assert not b.exists("dk")
    with pytest.raises(KeyNotFound):
        b.delete("dk")
    with pytest.raises(KeyNotFound):
        b.public_key("dk")
    with pytest.raises(KeyNotFound):
        b.sign("dk", person_payload(), action="a")


# --- file tier ------------------------------------------------------------------------------------------------------


def test_file_tier_signs_host_payloads_without_a_factor(tmp_path):
    f = FileBackend(tmp_path / "f")
    pub = f.create("wsk")
    payload = host_payload()
    assert crypto.verify(pub, f.sign("wsk", payload, action="host append"), payload)
    assert f.presence() == "none" and f.tier == "file"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(tmp_path / "f" / "wsk.filekey.json").st_mode) == 0o600


def test_file_tier_key_can_never_sign_person_events_or_person_key_objects(tmp_path):
    f = FileBackend(tmp_path / "f")
    f.create("wsk")
    for label in (
        "sig_ticket_event",
        "sig_ws_event",
    ):
        with pytest.raises(CustodyError):
            f.sign("wsk", canon.LABELS[label].encode() + b"{}", action="x")
    for key in ("sig_device_cert", "sig_revocation", "sig_ws_delegation", "sig_decision", "sig_ws_cosign"):
        with pytest.raises(CustodyError):
            f.sign("wsk", crypto.L[key] + b"{}", action="x")
    with pytest.raises(CustodyError):
        f.sign("wsk", person_payload(), action="x")
    with pytest.raises(CustodyError):
        f.sign("wsk", b"raw bytes", action="x")


def test_file_tier_import_and_corruption(tmp_path):
    f = FileBackend(tmp_path / "f")
    key = crypto.generate_private_key()
    assert f.create("k", secret=crypto.private_scalar(key)) == crypto.public_bytes(key)
    (tmp_path / "f" / "k.filekey.json").write_text("{}")
    with pytest.raises(CustodyError):
        f.public_key("k")
    with pytest.raises(KeyExists):
        f.create("k")


def test_person_and_host_label_sets_are_disjoint_where_it_matters():
    from orch.custody import HOST_LABELS, PERSON_LABELS

    only_person = {x for x in PERSON_LABELS if x not in HOST_LABELS}
    assert canon.LABELS["sig_ticket_event"].encode() in only_person
    assert crypto.L["sig_device_cert"] in only_person
    assert canon.LABELS["sig_host_event"].encode() not in PERSON_LABELS
    for a in PERSON_LABELS + HOST_LABELS:
        for b in PERSON_LABELS + HOST_LABELS:
            assert a == b or not b.startswith(a)


# --- unavailable backends and registry ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PLANNED))
def test_planned_backends_refuse_clearly(name, tmp_path):
    b = get_backend(name, tmp_path)
    assert b.person_capable is False
    with pytest.raises(BackendUnavailable, match="not available in P1"):
        b.sign("k", b"x", action="a")
    with pytest.raises(BackendUnavailable):
        b.create("k")


def test_registry(tmp_path):
    assert isinstance(get_backend("passphrase", tmp_path / "p", kdf=FAST, min_n=2**10), PassphraseBackend)
    assert isinstance(get_backend("file", tmp_path / "f"), FileBackend)
    with pytest.raises(CustodyError):
        get_backend("keychain", tmp_path)


def test_production_kdf_parameters_round_trip(tmp_path):
    """One real run at N = 2^17 (128 MiB): create, sign, and the file records the parameters."""
    b = PassphraseBackend(tmp_path / "k", passphrase_provider=lambda r: PHRASE)
    pub = b.create("dk")
    assert json.loads((tmp_path / "k" / "dk.key.json").read_text())["kdf"]["n"] == 2**17
    payload = person_payload()
    assert crypto.verify(pub, b.sign("dk", payload, action="a"), payload)
