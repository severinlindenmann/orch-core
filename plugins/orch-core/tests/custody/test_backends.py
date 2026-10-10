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
        "type": "ticket.reopened",
        "actor": {"kind": "person", "id": "p_" + "0" * 32, "device": "d_" + "0" * 32},
        "hash_v": 1,
        "roster_v": 1,
        "auth": "passphrase",
        "text": extra,
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

    b = PassphraseBackend(tmp_path / "keys", _passphrase_provider=provider, _kdf=FAST, _min_n=2**10, **kw)
    b.seen = seen  # type: ignore[attr-defined]
    return b


# --- passphrase backend --------------------


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
    assert set(doc) == {"v", "key_id", "role", "pub", "kdf", "nonce", "ct"}


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
    bad = PassphraseBackend(tmp_path / "keys", _passphrase_provider=lambda r: "not the phrase", _kdf=FAST, _min_n=2**10)
    with pytest.raises(WrongPassphrase):
        bad.sign("dk", person_payload(), action="a")
    empty = PassphraseBackend(tmp_path / "keys", _passphrase_provider=lambda r: "", _kdf=FAST, _min_n=2**10)
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
    b = PassphraseBackend(tmp_path / "k", _kdf=FAST, _min_n=2**10)
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


def test_tty_provider_writes_the_prompt_to_the_tty_and_reads_from_it(monkeypatch):
    written = []
    monkeypatch.setattr(pp, "_open_tty", lambda: (10, 11, lambda: written.append("closed")))
    monkeypatch.setattr(pp, "_read_secret", lambda r, w, prompt: (written.append(prompt), "phrase-ok-1")[1])
    req = PassphraseRequest("unlock", "dk", "", "ab" * 16, (("type", "gate.approved"),))
    assert tty_passphrase_provider(req) == "phrase-ok-1"
    assert "type: gate.approved" in written[0] and "sha256: " + "ab" * 16 in written[0]
    assert written[-1] == "closed"


def _lines(req):
    return pp.render_prompt(req).split("\n")


def test_prompt_escapes_hostile_values_and_cannot_be_forged():
    evil = "approve\x1b[2J\x1b]0;pwned\x07\rSign: approve gate\nsha256: " + "0" * 32 + "\u202etxet\u200b"
    req = PassphraseRequest("unlock", "dk\x1b[31m", evil, "ab" * 16, (("type", evil), ("gate", "a\u202eb\rc")))
    text = pp.render_prompt(req)
    lines = text.split("\n")
    for bad in ("\x1b", "\r", "\x07", "\u202e", "\u200b"):
        assert bad not in text
    assert lines[2] == "sha256: " + "ab" * 16, "the real hash is the first line after the banner"
    assert sum(ln.startswith("sha256: ") for ln in lines) == 1
    assert not any(ln.startswith(("Sign:", "action:")) for ln in lines)
    type_line = "".join(ln for ln in lines if ln.startswith("type"))
    assert "U+001B" in type_line and "U+0007" in type_line and "U+000D" in type_line and "U+000A" in type_line
    gate_line = next(ln for ln in lines if ln.startswith("gate: "))
    assert "U+202E" in gate_line and "U+000D" in gate_line
    # every non-empty line is a banner or starts with a fixed label
    allowed = ("===", "sha256:", "key:", "type", "gate:", "note (caller text, not signed):")
    assert all(ln.startswith(allowed) for ln in lines if ln)


def test_a_value_cannot_open_a_fake_sha256_line():
    req = PassphraseRequest("unlock", "dk", "x\rsha256: deadbeef", "ab" * 16, (("type", "y\nsha256: deadbeef"),))
    lines = _lines(req)
    assert [ln for ln in lines if ln.startswith("sha256:")] == ["sha256: " + "ab" * 16]
    note = next(ln for ln in lines if ln.startswith("note"))
    assert note.startswith('note (caller text, not signed): "') and "U+000D" in note


def test_values_are_escaped_exactly_once():
    esc = pp.render_prompt(PassphraseRequest("unlock", "dk", "a\x1bb", "ab" * 16, (("type", "a\x1bb"),)))
    assert esc.count("\u27e8U+001B\u27e9") == 2
    assert "U+27E8" not in esc, "no double escaping of the marker bracket"


def test_prompt_values_are_length_limited():
    """A long value is shown in full over numbered continuation lines, never truncated; the note is cut."""
    text = pp.render_prompt(PassphraseRequest("unlock", "dk", "x" * 5000, "ab" * 16, (("text", "q" * 300),)))
    assert "text#1/3:" in text and "text#3/3:" in text and text.count("q") == 300
    assert len(pp._value("x" * 5000)) == pp.MAX_SHOWN and pp._value("x" * 5000).endswith("\u2026")
    with pytest.raises(CustodyError, match="too much"):
        pp.render_prompt(PassphraseRequest("unlock", "dk", "", "ab" * 16, (("text", "z" * 40000),)))


def test_non_hex_digest_is_escaped_too():
    text = pp.render_prompt(PassphraseRequest("unlock", "dk", "a", "\x1b[0m" + "f" * 100))
    assert "\x1b" not in text


def _event_payload(**extra):
    ev = {
        "v": 2,
        "id": "01J9ZP0000000000000000000B",
        "type": "gate.approved",
        "actor": {"kind": "person", "id": "p_" + "0" * 32, "device": "d_" + "0" * 32},
        "hash_v": 1,
        "roster_v": 1,
        "auth": "passphrase",
        "gate": "code",
        "hash": "sha256:" + "ab" * 32,
        "policy_hash": "sha256:" + "cd" * 32,
        "gate_gen": 3,
        "source_sha": [{"repo": "local:demo", "ref": "main", "sha": "c" * 40}],
    }
    ev.update(extra)
    return canon.person_signing_bytes(WS, TICKET, ev)


def test_the_shown_action_is_derived_from_the_signing_bytes(tmp_path):
    b = pass_backend(tmp_path)
    b.create("dk")
    b.sign("dk", _event_payload(), action="harmless-looking text")
    req = b.seen[-1]
    fields = dict(req.fields)
    assert fields["signs"] == "ticket-event" and fields["type"] == "gate.approved" and fields["gate"] == "code"
    assert fields["hash"] == "sha256:" + "ab" * 32 and fields["gate_gen"] == "3"
    assert fields["log"] == TICKET and fields["workspace"] == WS
    assert fields["source_sha[1].sha"] == "c" * 40 and fields["source_sha[1].repo"] == "local:demo"
    text = pp.render_prompt(req)
    assert "type: gate.approved" in text and 'note (caller text, not signed): "harmless-looking text"' in text


def test_a_lying_caller_cannot_change_the_derived_fields(tmp_path):
    b = pass_backend(tmp_path)
    b.create("dk")
    b.sign("dk", _event_payload(), action="type: ticket.reopened\ngate: plan")
    fields = dict(b.seen[-1].fields)
    assert fields["type"] == "gate.approved" and fields["gate"] == "code"
    lines = [ln for ln in pp.render_prompt(b.seen[-1]).split("\n") if ln.startswith(("type:", "gate:"))]
    assert lines == ["type: gate.approved", "gate: code"]


def test_hostile_event_values_are_shown_escaped():
    """canon refuses bidi in signed text, but the prompt must not rely on that: raw bytes with one are escaped."""
    from orch.custody.describe import describe_payload

    ev = {"type": "ticket.reopened", "text": "x\u202e\x1b", "actor": {"kind": "person", "id": "p", "device": "d"}}
    body = {"contract": 1, "suite": 2, "workspace_id": WS, "log": TICKET, "event": ev}
    import json

    payload = canon.LABELS["sig_ticket_event"].encode() + json.dumps(body, ensure_ascii=False).encode()
    text = pp.render_prompt(PassphraseRequest("unlock", "dk", "", "ab" * 16, tuple(describe_payload(payload))))
    assert "\u202e" not in text and "\x1b" not in text and "U+202E" in text and "U+001B" in text


def test_backend_passes_the_signing_hash_not_caller_text(tmp_path):
    b = pass_backend(tmp_path)
    b.create("dk")
    payload = person_payload()
    b.sign("dk", payload, action="approve\nsha256: 0000\x1b[2J")
    assert b.seen[-1].digest == crypto.sha256(payload).hex()[:32]


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
    strict = PassphraseBackend(tmp_path / "keys", _passphrase_provider=lambda r: PHRASE)
    with pytest.raises(CustodyError, match="corrupt"):
        strict.sign("dk", person_payload(), action="a")


def test_bad_kdf_parameters_are_refused_by_the_constructor(tmp_path):
    for p in (KdfParams(n=2**10 + 1), KdfParams(n=2**21), KdfParams(n=2**10, r=1), KdfParams(n=2**10, p=2)):
        with pytest.raises(CustodyError):
            PassphraseBackend(tmp_path / "k", _kdf=p, _min_n=2**10)
    with pytest.raises(CustodyError):
        PassphraseBackend(tmp_path / "k", _kdf=KdfParams(n=2**10))  # below the default floor


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


# --- file tier --------------------


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


# --- unavailable backends and registry --------------------


@pytest.mark.parametrize("name", sorted(PLANNED))
def test_planned_backends_refuse_clearly(name, tmp_path):
    b = get_backend(name, tmp_path)
    assert b.person_capable is False
    with pytest.raises(BackendUnavailable, match="not available in P1"):
        b.sign("k", b"x", action="a")
    with pytest.raises(BackendUnavailable):
        b.create("k")


def test_registry(tmp_path):
    assert isinstance(get_backend("passphrase", tmp_path / "p"), PassphraseBackend)
    assert isinstance(get_backend("file", tmp_path / "f"), FileBackend)
    with pytest.raises(CustodyError):
        get_backend("keychain", tmp_path)


def test_production_kdf_parameters_round_trip(tmp_path):
    """One real run at N = 2^17 (128 MiB): create, sign, and the file records the parameters."""
    b = PassphraseBackend(tmp_path / "k", _passphrase_provider=lambda r: PHRASE)
    pub = b.create("dk")
    assert json.loads((tmp_path / "k" / "dk.key.json").read_text())["kdf"]["n"] == 2**17
    payload = person_payload()
    assert crypto.verify(pub, b.sign("dk", payload, action="a"), payload)


# --- roles (security review item 9) --------------------


def _cert_payload():
    from tests.identity.helpers import Person

    return certs_payload(Person().cert()["o"])


def certs_payload(o):
    return crypto.L["sig_device_cert"] + canon.cj_checked(o)


def test_person_key_signs_only_person_key_labels_and_device_key_only_its_own(tmp_path):
    b = pass_backend(tmp_path)
    pk_pub = b.create("pk", role="person")
    b.create("dk", role="device")
    cert = _cert_payload()
    assert crypto.verify(pk_pub, b.sign("pk", cert, action=""), cert)
    with pytest.raises(CustodyError):
        b.sign("pk", person_payload(), action="")  # PK never signs events
    with pytest.raises(CustodyError):
        b.sign("dk", cert, action="")  # dk_sig never signs certificates
    b.sign("dk", person_payload(), action="")
    for label in ("sig_enroll_request", "sig_drop_object", "sig_relay_auth", "sig_decision"):
        # in the dk_sig role's list, but not prompted for in P1: refused before any prompt (fail closed)
        with pytest.raises(CustodyError, match="not prompted"):
            b.sign("dk", crypto.L[label] + b"{}", action="")
    for label in ("sig_sk_grant", "sig_card_wsk", "sig_revocation"):
        with pytest.raises(CustodyError):
            b.sign("dk", crypto.L[label] + b"{}", action="")


def test_role_is_authenticated_in_the_key_file(tmp_path):
    b = pass_backend(tmp_path)
    b.create("dk", role="device")
    path = tmp_path / "keys" / "dk.key.json"
    doc = json.loads(path.read_text())
    doc["role"] = "person"
    path.write_text(json.dumps(doc))
    os.chmod(path, 0o600)
    with pytest.raises(WrongPassphrase):
        b.sign("dk", _cert_payload(), action="")


def test_unknown_and_workspace_roles_are_refused_by_the_passphrase_backend(tmp_path):
    b = pass_backend(tmp_path)
    for role in ("workspace", "root", ""):
        with pytest.raises(CustodyError):
            b.create("k", role=role)
    with pytest.raises(CustodyError):
        FileBackend(tmp_path / "f").create("k", role="device")


# --- R1: atomic create --------------------


def test_concurrent_create_has_exactly_one_winner_whose_key_is_on_disk(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    for trial in range(30):
        d = tmp_path / f"t{trial}"
        n = 8
        gate = Barrier(n)

        def attempt(_, d=d, gate=gate):
            f = FileBackend(d)
            gate.wait()
            try:
                return f.create("k")
            except KeyExists:
                return None

        with ThreadPoolExecutor(n) as ex:
            results = list(ex.map(attempt, range(n)))
        winners = [r for r in results if r is not None]
        assert len(winners) == 1, f"trial {trial}: {len(winners)} creates succeeded"
        assert FileBackend(d).public_key("k") == winners[0]
        assert not [p for p in d.iterdir() if ".tmp" in p.name], "temporary files are cleaned up"


def test_concurrent_create_across_processes(tmp_path):
    import subprocess

    code = (
        "import sys, time\n"
        "from orch.custody import FileBackend, KeyExists\n"
        "d, go = sys.argv[1], float(sys.argv[2])\n"
        "f = FileBackend(d)\n"
        "time.sleep(max(0, go - time.time()))\n"
        "try:\n"
        "    print(f.create('k').hex())\n"
        "except KeyExists:\n"
        "    print('exists')\n"
    )
    import time

    go = time.time() + 1.0
    procs = [
        subprocess.Popen([sys.executable, "-c", code, str(tmp_path / "p"), str(go)], stdout=subprocess.PIPE, text=True)
        for _ in range(4)
    ]
    outs = [p.communicate()[0].strip() for p in procs]
    winners = [o for o in outs if o not in ("exists", "")]
    assert len(winners) == 1, outs
    assert FileBackend(tmp_path / "p").public_key("k").hex() == winners[0]


def test_create_never_replaces_an_existing_file(tmp_path):
    f = FileBackend(tmp_path)
    (tmp_path / "k.filekey.json").write_text("precious")
    with pytest.raises(KeyExists):
        f.create("k")
    assert (tmp_path / "k.filekey.json").read_text() == "precious"


def test_write_new_falls_back_to_exclusive_create_without_hard_links(tmp_path, monkeypatch):
    from orch.custody import files

    def no_link(*a, **k):
        raise OSError("hard links unsupported")

    monkeypatch.setattr(files.os, "link", no_link)
    files.write_new(tmp_path / "a", b"x")
    assert (tmp_path / "a").read_bytes() == b"x"
    with pytest.raises(KeyExists):
        files.write_new(tmp_path / "a", b"y")
    assert (tmp_path / "a").read_bytes() == b"x"


# --- R4: passphrase text --------------------


def test_nfc_and_nfd_passphrases_are_the_same_passphrase(tmp_path):
    import unicodedata

    nfc = "café au lait été"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    b = pass_backend(tmp_path, phrase=nfc)
    b.create("dk")
    other = PassphraseBackend(tmp_path / "keys", _passphrase_provider=lambda r: nfd, _kdf=FAST, _min_n=2**10)
    payload = person_payload()
    assert crypto.verify(b.public_key("dk"), other.sign("dk", payload, action=""), payload)


def test_invalid_utf8_from_the_terminal_is_refused_not_replaced():
    with pytest.raises(CustodyError, match="UTF-8"):
        pp._decode(b"abc\xff\xfe")
    assert pp._decode("héllo".encode()) == "héllo"


def test_lone_surrogate_passphrase_is_refused(tmp_path):
    b = pass_backend(tmp_path, phrase="abcdefgh\ud800")
    with pytest.raises(CustodyError):
        b.create("dk")
    assert not b.exists("dk")


def test_minimum_length_counts_normalised_characters(tmp_path):
    import unicodedata

    nfd7 = unicodedata.normalize("NFD", "é" * 7)  # 14 code points, 7 characters after NFC
    b = pass_backend(tmp_path, phrase=nfd7)
    with pytest.raises(CustodyError, match="at least"):
        b.create("dk")


# --- hardening seams, modes, presence --------------------


def test_no_public_path_lowers_the_floor_or_injects_a_provider():
    import inspect

    from orch import custody

    assert list(inspect.signature(custody.get_backend).parameters) == ["name", "directory"]
    for name in inspect.signature(PassphraseBackend).parameters:
        if name != "directory":
            assert name.startswith("_"), name
    assert pp.MIN_N == 2**15


def test_default_backend_applies_the_floor_to_files(tmp_path):
    weak = pass_backend(tmp_path)
    weak.create("dk")
    from orch.custody import get_backend

    strict = get_backend("passphrase", tmp_path / "keys")
    with pytest.raises(CustodyError, match="corrupt"):
        strict.public_key("dk")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_key_files_with_loose_mode_are_refused(tmp_path):
    b = pass_backend(tmp_path)
    b.create("dk")
    f = FileBackend(tmp_path / "f")
    f.create("w")
    for backend, name, path in (
        (b, "dk", tmp_path / "keys" / "dk.key.json"),
        (f, "w", tmp_path / "f" / "w.filekey.json"),
    ):
        os.chmod(path, 0o644)
        with pytest.raises(CustodyError, match="accessible by others"):
            backend.public_key(name)
        os.chmod(path, 0o600)
        backend.public_key(name)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner check")
def test_key_file_owned_by_another_user_is_refused(tmp_path, monkeypatch):
    from orch.custody import files

    b = pass_backend(tmp_path)
    b.create("dk")
    uid = os.getuid()
    monkeypatch.setattr(files.os, "getuid", lambda: uid + 1)
    with pytest.raises(CustodyError, match="another user"):
        b.public_key("dk")


def test_windows_fails_closed_for_human_signing(monkeypatch):
    monkeypatch.setattr(pp.os, "name", "nt")
    with pytest.raises(NoPrompt, match="Windows"):
        tty_passphrase_provider(PassphraseRequest("unlock", "dk", "", "ab" * 16))


def test_presence_is_auth_or_none(tmp_path):
    for backend in (pass_backend(tmp_path), FileBackend(tmp_path / "f"), *(get_backend(n, tmp_path) for n in PLANNED)):
        assert backend.presence() == (backend.auth or "none")
