"""M4 S5 (#1462): the hub backup key, making / keeping / scheduling / downloading backups (restore is S6).

Every Opus condition of the slice has a test: 7 (password + code inside each dangerous request, no switch/phone/pairing
session), 8 (words shown once, no-store, in no file, log, audit line or activity row), 9 (words only on loopback, https or the
private network), 10 (a new key makes a new backup, then the old ones go), 11 (exclusions and stripped rows inside the copies),
12 (header signed by HK_root, MAC from the words, a forged file refused), 23 (audit lines) and the S0 carry-forward."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sqlite3
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from webapi_helpers import Web

from orch_hub import cli as CLI
from orch_hub.activity import ActivityLog
from orch_hub.backup import cli as BCLI
from orch_hub.backup import snapshot as SN
from orch_hub.backup import words as W
from orch_hub.backup.service import DAY_MS, KEEP, NAME, BackupError, BackupService
from orch_hub.contract import audit as AU
from orch_hub.contract import backup as B
from orch_hub.contract import keystore as KS
from orch_hub.contract import suite as S
from orch_hub.contract.canon import b64u, cj
from orch_hub.contract.hub import hub_id as hub_id_of
from orch_hub.invites import Invites
from orch_hub.webapi import backup_api as BA

WORDLIST_SHA = "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"  # bitcoin/bips english.txt
OFFICE = "http://192.168.1.5:9000"


@pytest.fixture
def w(tmp_path):
    x = Web(tmp_path)
    x.state = tmp_path / "hub"
    act = x.api.d.activity = ActivityLog(":memory:")
    yield x
    act.close()
    x.close()


def _key(w, tok, **kw):
    return w.call("POST", "/admin/backup/key", {**w.creds(w.admin), **kw}, token=tok)


def _later(w):
    """Past the 'Back up now' pause (10 minutes); a fresh session, as the old one may have run out."""
    w.clock.advance(11 * 60_000)
    return w.login(w.admin)


def _first(w, tok=None):
    tok = tok or w.login(w.admin)
    s, o, _ = _key(w, tok)
    assert s == 200, o
    return tok, o["words"], o["backup"]["name"]


def _types(w):
    return [r["o"]["type"] for _, r, _e in w.api.d.audit.store.after(0, 1000)]


def _events(w, typ):
    return [e for _, r, e in w.api.d.audit.store.after(0, 1000) if r["o"]["type"] == typ]


def _words_nowhere(state, words, blobs=()):
    """The words (or three of them in a row) are in no file of the state dir and in none of the given blobs."""
    needle, part = words.encode(), " ".join(words.split()[:3]).encode()
    for root, _d, files in os.walk(state):
        for f in files:
            raw = Path(root, f).read_bytes()
            assert needle not in raw and part not in raw, f
    for blob in blobs:
        assert needle not in blob and part not in blob


def _open_db(tmp_path, data, name):
    p = tmp_path / ("open-" + name)
    p.write_bytes(data)
    return sqlite3.connect(p)


# --- the words ---------------------------------------------------------------------------------------------------------


def test_the_vendored_wordlist_is_pinned_and_words_have_a_checksum():
    assert hashlib.sha256(("\n".join(W.WORDS) + "\n").encode()).hexdigest() == WORDLIST_SHA
    assert len(W.WORDS) == 2048 and len(set(W.WORDS)) == 2048
    ws = W.generate_words()
    assert len(ws.split()) == 24 and W.check_words(ws) == ws
    assert W.check_words("  " + ws.upper().replace(" ", "\n") + " ") == ws  # S0 normalisation: case and white space
    assert W.words_from_entropy(bytes(32)) == " ".join(["abandon"] * 23 + ["art"])  # BIP-39 vector
    for bad, code in (
        (" ".join(ws.split()[:23]), "words"),
        (" ".join(ws.split()[:23] + ["notaword"]), "unknown_word"),
        (" ".join(["abandon"] * 24), "checksum"),
        (None, "words"),
    ):
        with pytest.raises(W.WordsError) as e:
            W.check_words(bad)
        assert e.value.code == code


# --- cond 7: who may call, step-up inside every dangerous request --------------------------------------------------------

_DL = {"password": "x", "totp": "000000", "name": "hub-00000000-20260101T000000Z.orchbak"}
ROUTES = [
    ("GET", "/admin/backup", None),
    ("POST", "/admin/backup/key", {"password": "x", "totp": "000000"}),
    ("POST", "/admin/backups", {}),
    ("POST", "/admin/backups/download", _DL),
]


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_backup_routes_are_admin_only_and_refuse_switch_phone_and_pairing_sessions(w, method, path, body):
    assert w.call(method, path, body)[0] == 401
    assert w.call(method, path, body, token=w.login(w.ann))[:2] == (403, {"error": "forbidden_role"})
    app = w.login(w.admin, "app")  # a phone's pairing session
    assert w.call(method, path, body, token=app)[0] in (401, 403)
    ticket = w.env.svc.open_session(w.admin)["token"]  # a workspace vouched for it: no password sign-in
    w.browser_tokens.add(ticket)
    assert w.call(method, path, body, token=ticket)[:2] == (403, {"error": "reauth_required"})
    adm = w.login(w.admin)
    if method == "POST":
        assert w.call(method, path, body, token=adm, csrf=False)[:2] == (403, {"error": "not_allowed"})
        assert w.call(method, path, body, token=adm, origin="http://evil.example")[:2] == (
            403,
            {"error": "not_allowed"},
        )
    assert not (w.state / SN.KEY_FILE).exists() and not (w.state / "backups").exists()
    assert "hub_backup" not in _types(w)


def test_key_setup_and_download_check_password_and_code_in_the_request(w):
    tok = w.login(w.admin)
    good = w.creds(w.admin)
    for body in ({}, {"password": good["password"]}, {**good, "extra": "x"}, {"password": 1, "totp": good["totp"]}):
        assert w.call("POST", "/admin/backup/key", body, token=tok)[:2] == (400, {"error": "malformed"})
    s, o, _ = w.call("POST", "/admin/backup/key", {"password": "wrong password", "totp": good["totp"]}, token=tok)
    assert (s, o) == (401, {"error": "bad_credentials"})
    assert w.call("POST", "/admin/backup/key", {"password": good["password"], "totp": "000000"}, token=tok)[0] == 401
    assert not (w.state / SN.KEY_FILE).exists()
    assert w.call("POST", "/admin/backup/key", good, token=tok)[0] == 200
    # per request: the same code a second time is refused (single use), a stolen cookie alone cannot do it again
    assert w.call("POST", "/admin/backup/key", good, token=tok)[:2] == (401, {"error": "bad_credentials"})
    name = w.call("GET", "/admin/backup", token=tok)[1]["backups"][0]["name"]
    assert w.call("POST", "/admin/backups/download", {"name": name}, token=tok)[0] == 400
    wrong = {"name": name, "password": "wrong password", "totp": w.creds(w.admin)["totp"]}
    assert w.call("POST", "/admin/backups/download", wrong, token=tok)[0] == 401
    assert "downloaded" not in [e["change"] for e in _events(w, "hub_backup")]
    s, o, _ = w.call("POST", "/admin/backups/download", {"name": name, **w.creds(w.admin)}, token=tok)
    assert s == 200 and o["name"] == name


# --- cond 8, 9: the words ----------------------------------------------------------------------------------------------


def test_the_words_are_shown_once_with_no_store_and_are_nowhere_else(w, caplog):
    caplog.set_level(logging.DEBUG)
    tok = w.login(w.admin)
    s, o, h = _key(w, tok)
    assert s == 200 and h["Cache-Control"] == "no-store" and h["Pragma"] == "no-cache"
    words = o["words"]
    assert W.check_words(words) == words and set(o) == {"words", "backup"}
    info = json.loads((w.state / SN.KEY_FILE).read_text())  # only bk_pub + K_auth are stored, 0600
    assert (
        set(info) == {"v", "bk_pub", "k_auth", "set_ms", "pending"}
        and (w.state / SN.KEY_FILE).stat().st_mode & 0o777 == 0o600
    )
    keys = B.derive_keys(words)
    assert info["bk_pub"] == b64u(keys.bk_pub)
    assert info["k_auth"] == b64u(keys.k_auth)
    s, st, _ = w.call("GET", "/admin/backup", token=tok)  # the status route never repeats them
    assert s == 200 and st["key_set"] is True and words not in json.dumps(st)
    tok = _later(w)
    s, mk, _ = w.call("POST", "/admin/backups", {}, token=tok)
    assert s == 200 and words not in json.dumps(mk)
    audit_blob = json.dumps([(r, e) for _, r, e in w.api.d.audit.store.after(0, 1000)]).encode()
    _words_nowhere(w.state, words, (audit_blob, caplog.text.encode()))


@pytest.mark.parametrize(
    "origin,ok",
    [
        ("http://127.0.0.1:9000", True),
        ("http://localhost:9000", True),
        ("http://[::1]:9000", True),
        ("http://100.64.1.2:9000", True),  # this hub's own private-network (VPN) address (given below)
        ("http://100.127.255.254:9000", False),  # another address of the same range: not this hub's
        ("http://100.64.1.2:9001", False),  # the same host on another port
        ("https://hub.example:9443", True),
        ("http://192.168.1.5:9000", False),  # office network, plain http
        ("http://10.0.0.5:9000", False),
        ("http://203.0.113.9:9000", False),  # public address, plain http
        ("http://100.128.0.1:9000", False),  # just outside 100.64/10
        ("http://hub.example:9000", False),
        ("ftp://127.0.0.1:9000", False),
        (None, False),
        ("", False),
    ],
)
def test_words_are_only_for_loopback_https_or_the_private_network(origin, ok):
    assert BA.safe_to_show_words(origin, frozenset({"http://100.64.1.2:9000"})) is ok


def test_a_plain_http_office_address_gets_a_refusal_and_nothing_is_made(w):
    w.api.allow_origin(OFFICE)
    tok = w.login(w.admin)
    kw = {"token": tok, "origin": OFFICE, "host": "192.168.1.5:9000"}
    good = w.creds(w.admin)
    assert w.call("POST", "/admin/backup/key", good, **kw)[:2] == (403, {"error": "insecure_origin"})
    assert not (w.state / SN.KEY_FILE).exists()
    assert not (w.state / "backups").exists() and "hub_backup" not in _types(w)
    s, st, _ = w.call("GET", "/admin/backup", **kw)  # the same console still reads the status and the command to run
    assert s == 200 and st["cli_line"] == "orch-hub backup-key" and st["key_set"] is False
    # the refusal comes before the credentials are spent: the same code still works on a safe console
    assert w.call("POST", "/admin/backup/key", good, token=tok)[0] == 200


# --- the backup file: signature, MAC, seal ---------------------------------------------------------------------------------


def test_the_backup_is_signed_by_the_root_maced_and_opens_with_the_words(w):
    _tok, words, name = _first(w)
    path = w.state / "backups" / name
    assert NAME.fullmatch(name) and name.startswith("hub-" + w.env.keys.hub_id[:8])
    assert path.stat().st_mode & 0o777 == 0o600 and (w.state / "backups").stat().st_mode & 0o777 == 0o700
    data = path.read_bytes()
    live = {"hub_id": w.env.keys.hub_id, "hk_root_pub": b64u(w.env.keys.hk_root_pub)}
    got = B.open_backup(data, words=words, dev_mode=True, live=live)  # the S0 open path: every check
    h = got.header
    assert h["hub_id"] == w.env.keys.hub_id and h["op_seq"] == w.env.keys.op_seq
    assert h["card_seq"] == w.env.keys.card_seq and h["dev"] is True
    assert h["bk_pub"] == b64u(B.derive_keys(words).bk_pub)
    for need in (
        "hk_root.key",
        "hxk.key",
        "meta.json",
        "tls.key",
        SN.KEY_FILE,
        "channels.db",
        "access.db",
    ):
        assert need in got.files, need
    assert json.loads(got.files[SN.KEY_FILE])["k_auth"] == b64u(B.derive_keys(words).k_auth)  # the sealed K_auth
    assert any(p.startswith("op/") for p in got.files) and any(p.startswith("uk/") for p in got.files)
    with pytest.raises(B.Refused) as e:  # wrong words: not this backup's key
        B.open_backup(data, words=W.generate_words(), dev_mode=True, live=None)
    assert e.value.code == "wrong_backup_key"
    _words_nowhere(w.state, words, (data,))


def test_only_the_root_signer_signs_a_backup_header(w):
    k = w.env.keys
    hdr = {"v": 2, "suite": 2, "kind": "hub.backup", "hv": 0}
    assert isinstance(k.sign_backup_header(hdr), bytes)
    assert not hasattr(KS.OpSigner, "sign_backup_header") and not hasattr(KS.UkSigner, "sign_backup_header")
    for bad in ({**hdr, "kind": "hub.card"}, {**hdr, "v": 1}, {**hdr, "hv": 1}, {}):
        with pytest.raises(KS.KeyUseError):
            k.sign_backup_header(bad)
    with pytest.raises(KS.KeyUseError):
        k.op_signer.sign_object(hdr)  # HK_op has no such kind


def test_a_file_forged_under_another_root_and_sealed_to_the_copied_bk_pub_is_refused(w):
    _tok, words, name = _first(w)
    env, _sealed = B.unpack_file((w.state / "backups" / name).read_bytes())
    other = ec.generate_private_key(ec.SECP256R1())
    files = {"hk_root.key": b"x", "hxk.key": b"y", "meta.json": b"{}"}
    fo = {
        **env["o"],
        "hk_root_pub": b64u(S.public_bytes(other)),
        "hub_id": hub_id_of(S.public_bytes(other)),
        "files": SN.file_list(files),
    }
    forged = {"o": fo, "sig": b64u(S.sign(other, B.LABEL + cj(fo))), "mac": b64u(bytes(32))}  # no K_auth: no MAC
    blob = B.pack_file(forged, B.seal_payload(B.derive_keys(words).bk_pub, fo, SN.tar_files(files)))
    B.read_header(blob, dev_mode=True, live=None)  # signature, ids and list all check out ...
    with pytest.raises(B.Refused) as e:
        B.open_backup(blob, words=words, dev_mode=True, live=None)  # ... the MAC does not
    assert e.value.code == "backup_invalid"
    assert str(e.value.fields.get("reason") if hasattr(e.value, "fields") else "mac") == "mac"


# --- cond 11 and the S0 carry-forward: exclusions and stripped rows inside the copies ---------------------------------------


def _dirty(w):
    """Fill the live stores with everything that must NOT travel, plus the credentials that must."""
    st = w.state
    (st / "setup-token").write_text("TOKEN-SECRET")
    os.chmod(st / "setup-token", 0o600)
    for n in ("route-queue.db", "event-deliver.db"):
        (st / n).write_bytes(b"SQLite format 3\x00queue")
    (st / "logs").mkdir()
    (st / "logs" / "hub.log").write_text("log")
    (st / "serve.log").write_text("log")
    (st / "hub.pid").write_text("1")
    w.env.svc.open_session(w.ann)  # a switch-ticket style session (born without a password sign-in)
    w.env.svc.create_console_pairing_code(w.admin)
    w.env.svc.begin_totp(w.ann)
    db = sqlite3.connect(st / "channels.db")
    db.execute("INSERT INTO failures(kind, subject, ts_ms) VALUES ('pw','x',1)")
    db.execute(
        "INSERT INTO challenges(challenge_id,user_id,kind,key_pub,binding,label,expires_ms) "
        "VALUES ('c',?, 'browser','k','b','l',9)",
        (w.admin,),
    )
    db.execute("INSERT INTO push_keys(channel_id, push_pub, issued_ms) VALUES ('ch','pp',1)")
    db.execute("INSERT OR REPLACE INTO known_sources(user_id, source, ts_ms) VALUES (?, '1.2.3.4', 1)", (w.admin,))
    db.commit()
    db.close()
    inv = Invites(st / "invites.db", now_ms=w.clock)
    inv.create("reset", created_by=w.admin, for_user=w.ann)
    token, used_id, _ = inv.create("join", created_by=w.admin, name="Zed")
    inv.redeem(token, lambda i: None)
    inv.close()
    adb = sqlite3.connect(st / "access.db")
    adb.execute("INSERT INTO recovery_notes(user_id, by_user, at_ms) VALUES (?,?,1)", (w.ann, w.admin))
    adb.commit()
    adb.close()
    return used_id


def test_nothing_that_must_not_travel_is_in_a_backup_and_the_copies_are_stripped(w, tmp_path):
    used_id = _dirty(w)
    _tok, words, name = _first(w)
    got = B.open_backup((w.state / "backups" / name).read_bytes(), words=words, dev_mode=True, live=None)
    paths = set(got.files)
    for banned in ("setup-token", "route-queue.db", "event-deliver.db", "serve.log", "hub.pid"):
        assert banned not in paths
    assert not any(p.startswith(("backups/", "logs/", "tmp/")) for p in paths)
    assert not any(p.endswith(("-wal", "-shm", "-journal", ".tmp", ".lock")) for p in paths)
    assert b"TOKEN-SECRET" not in b"".join(got.files.values())
    ch = _open_db(tmp_path, got.files["channels.db"], "channels")
    for t in ("sessions", "pairing", "failures", "challenges", "push_keys", "known_sources", "pending_totp"):
        assert ch.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t
    assert ch.execute("SELECT COUNT(*) FROM credentials").fetchone()[0] >= 2  # people can still sign in after a restore
    live = sqlite3.connect(w.state / "channels.db")
    assert live.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 2  # the live hub keeps its own rows
    live.close()
    inv = _open_db(tmp_path, got.files["invites.db"], "invites")
    assert inv.execute("SELECT COUNT(*) FROM invites WHERE used_ms IS NULL").fetchone()[0] == 0  # EVERY unused invite
    assert inv.execute("SELECT id FROM invites").fetchall() == [(used_id,)]  # the used one is history and stays
    acc = _open_db(tmp_path, got.files["access.db"], "access")
    assert acc.execute("SELECT COUNT(*) FROM recovery_notes").fetchone()[0] == 0
    assert acc.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= 2
    for blob in (
        got.files["channels.db"],
        got.files["invites.db"],
    ):  # VACUUM: deleted values are not left in free pages
        assert b"1.2.3.4" not in blob
    live_inv = sqlite3.connect(w.state / "invites.db")
    assert live_inv.execute("SELECT COUNT(*) FROM invites").fetchone()[0] == 2  # the copy did not touch the live store
    live_inv.close()


def test_a_symlink_in_the_state_dir_refuses_the_backup_and_odd_names_are_skipped(w):
    _first(w)
    (w.state / ".hidden").write_text("x")  # not a valid backup path: skipped, never an error
    files, _ = SN.collect(w.state)
    assert ".hidden" not in files
    (w.state / "evil").symlink_to("/etc/passwd")
    with pytest.raises(SN.SnapshotError) as e:
        SN.collect(w.state)
    assert e.value.code == "symlink"
    with pytest.raises(BackupError):
        BackupService(w.env.keys, audit=w.api.d.audit).make(None)


# --- cond 10: replacing the key ---------------------------------------------------------------------------------------------


def test_replacing_the_key_makes_a_new_backup_and_the_old_ones_go_only_after_the_words_are_confirmed(w):
    tok, words1, n1 = _first(w)
    w.clock.advance(11 * 60_000)
    assert w.call("POST", "/admin/backups", {}, token=tok)[0] == 200
    old = {r["name"] for r in w.call("GET", "/admin/backup", token=tok)[1]["backups"]}
    assert len(old) == 2
    w.clock.advance(5000)
    s, o, _ = _key(w, tok)
    assert s == 200 and o["words"] != words1
    words2 = o["words"]
    names = {r["name"] for r in w.call("GET", "/admin/backup", token=tok)[1]["backups"]}
    assert old < names and o["backup"]["name"] in names  # NOT deleted yet: the person has not said they saved the words
    assert w.call("GET", "/admin/backup", token=tok)[1]["key_pending"] is True
    other = w.login(w.admin)  # another session of the same admin cannot confirm for this one
    assert w.call("POST", "/admin/backup/key/confirm", {}, token=other)[:2] == (403, {"error": "confirm_denied"})
    assert {r["name"] for r in BackupService(w.env.keys).list()} == names
    assert w.call("POST", "/admin/backup/key/confirm", {"x": 1}, token=tok)[0] == 400
    s, o2, _ = w.call("POST", "/admin/backup/key/confirm", {}, token=tok)  # no step-up: the same session
    assert (s, o2) == (200, {"ok": True, "removed": 2})
    st = w.call("GET", "/admin/backup", token=tok)[1]
    assert {r["name"] for r in st["backups"]} == {o["backup"]["name"]} and st["key_pending"] is False
    assert not (w.state / "backups" / n1).exists()
    assert w.call("POST", "/admin/backup/key/confirm", {}, token=tok)[:2] == (200, {"ok": True, "removed": 0})
    data = (w.state / "backups" / o["backup"]["name"]).read_bytes()
    assert B.open_backup(data, words=words2, dev_mode=True, live=None).header["bk_pub"] == b64u(
        B.derive_keys(words2).bk_pub
    )
    with pytest.raises(B.Refused):
        B.open_backup(data, words=words1, dev_mode=True, live=None)
    w.clock.advance(11 * 60_000)  # later backups use the new key
    tok = w.login(w.admin)
    r = w.call("POST", "/admin/backups", {}, token=tok)
    assert r[0] == 200, r[:2]
    mk = r[1]["backup"]["name"]
    assert B.open_backup((w.state / "backups" / mk).read_bytes(), words=words2, dev_mode=True, live=None)
    assert json.loads((w.state / SN.KEY_FILE).read_text())["k_auth"] == b64u(B.derive_keys(words2).k_auth)
    assert [e["change"] for e in _events(w, "hub_backup")].count("key_set") == 2


def test_confirm_needs_an_admin_session_and_a_key_and_a_pending_replacement_survives_a_second_one(w):
    assert w.call("POST", "/admin/backup/key/confirm", {})[0] == 401
    tok = w.login(w.admin)
    assert w.call("POST", "/admin/backup/key/confirm", {}, token=tok)[:2] == (409, {"error": "no_backup_key"})
    assert w.call("POST", "/admin/backup/key/confirm", {}, token=w.login(w.ann))[0] == 403
    _first(w, tok)
    w.clock.advance(5000)
    _key(w, tok)
    w.clock.advance(5000)
    s, o, _ = _key(w, tok)  # replaced twice without confirming: the confirm drops everything not under the last key
    assert s == 200
    assert w.call("POST", "/admin/backup/key/confirm", {}, token=tok)[1] == {"ok": True, "removed": 2}
    assert [r["name"] for r in BackupService(w.env.keys).list()] == [o["backup"]["name"]]
    assert "pending" not in (w.state / SN.KEY_FILE).read_text()
    got = B.open_backup(
        (w.state / "backups" / o["backup"]["name"]).read_bytes(), words=o["words"], dev_mode=True, live=None
    )
    assert "pending" not in got.files[SN.KEY_FILE].decode()  # the session marker never travels


def test_a_failed_key_setup_puts_everything_back(w, monkeypatch):
    tok, _words1, _n1 = _first(w)
    before = {p: (w.state / p).read_bytes() for p in (SN.KEY_FILE,)}
    names = {r["name"] for r in BackupService(w.env.keys).list()}

    def boom(**kw):
        raise RuntimeError("chain broken")

    monkeypatch.setattr(w.api.d.audit, "hub_backup", boom)
    w.clock.advance(5000)
    assert _key(w, tok)[0] == 500
    assert {p: (w.state / p).read_bytes() for p in before} == before  # the old key is still the key
    assert {r["name"] for r in BackupService(w.env.keys).list()} == names  # no stray backup under the lost new key
    for p in (SN.KEY_FILE,):  # a first setup that fails leaves no key at all
        (w.state / p).unlink()
    with pytest.raises(RuntimeError):
        BackupService(w.env.keys, audit=w.api.d.audit).set_key(None)
    assert not (w.state / SN.KEY_FILE).exists()


# --- make, keep, schedule --------------------------------------------------------------------------------------------------


def test_make_needs_a_key_and_keeps_only_the_newest_seven(w):
    tok = w.login(w.admin)
    assert w.call("POST", "/admin/backups", {}, token=tok)[:2] == (409, {"error": "no_backup_key"})
    assert w.call("POST", "/admin/backups", {"x": 1}, token=tok)[0] == 400
    _first(w, tok)
    for _ in range(9):
        tok = _later(w)
        assert w.call("POST", "/admin/backups", {}, token=tok)[0] == 200
    kept = w.call("GET", "/admin/backup", token=tok)[1]["backups"]
    assert len(kept) == KEEP == 7
    assert [r["name"] for r in kept] == sorted((r["name"] for r in kept), reverse=True)  # newest first
    assert all(r["size"] > 0 for r in kept)


def test_two_backups_in_one_second_do_not_overwrite_each_other(w):
    _first(w)
    svc = BackupService(w.env.keys, now_ms=w.clock)
    a, b = svc.make(None)["name"], svc.make(None)["name"]  # the clock did not move
    assert a != b and (w.state / "backups" / a).exists() and (w.state / "backups" / b).exists()


def test_back_up_now_waits_ten_minutes_after_the_last_backup(w):
    tok, _words, _n = _first(w)
    assert w.call("POST", "/admin/backups", {}, token=tok)[:2] == (429, {"error": "rate_limited"})
    assert len(BackupService(w.env.keys).list()) == 1 and _events(w, "hub_backup")[-1]["change"] == "key_set"
    w.clock.advance(9 * 60_000)
    assert w.call("POST", "/admin/backups", {}, token=w.login(w.admin))[0] == 429
    tok = _later(w)  # now past 10 minutes since the first backup
    assert w.call("POST", "/admin/backups", {}, token=tok)[0] == 200
    assert w.call("POST", "/admin/backups", {}, token=tok)[0] == 429  # and again right after
    # the schedule and the server command are not held back by it
    assert BackupService(w.env.keys, now_ms=w.clock).make(None)["name"]


def test_the_daily_schedule_runs_on_the_tick_without_a_secret(w):
    svc = BackupService(w.env.keys, audit=w.api.d.audit, activity=w.api.d.activity, now_ms=w.clock)
    assert svc.tick() is None  # no key: nothing
    assert not (w.state / "backups").exists()
    _first(w)
    n0 = len(svc.list())
    assert svc.tick() is None  # the newest backup is less than a day old
    w.clock.advance(DAY_MS - 1000)
    assert svc.tick() is None
    w.clock.advance(2000)
    assert svc.tick() is not None and len(svc.list()) == n0 + 1
    ev = _events(w, "hub_backup")[-1]
    assert ev["change"] == "made" and ev["actor_user_id"] is None  # the schedule, not a person
    assert svc.tick() is None


def test_a_failing_scheduled_backup_is_retried_after_an_hour_not_at_every_tick(w, monkeypatch):
    svc = BackupService(w.env.keys, audit=w.api.d.audit, now_ms=w.clock)
    _first(w)
    w.clock.advance(DAY_MS + 1000)
    calls = []

    def boom(self, actor=None):
        calls.append(1)
        raise BackupError("backup_failed")

    monkeypatch.setattr(BackupService, "make", boom)
    with pytest.raises(BackupError):
        svc.tick()
    for _ in range(3):
        assert svc.tick() is None  # inside the hour: not tried again
    assert len(calls) == 1
    w.clock.advance(3_600_000 + 1000)
    with pytest.raises(BackupError):
        svc.tick()
    assert len(calls) == 2


def test_serve_runs_the_schedule_on_its_state_tick():
    import inspect

    from orch_hub import serve as SV

    src = inspect.getsource(SV.serve)
    assert "BackupService(" in src and "backup_svc.tick()" in src


# --- download, audit (cond 23) ----------------------------------------------------------------------------------------------


def test_download_hands_out_the_sealed_file_and_names_are_checked(w):
    tok, words, name = _first(w)
    s, o, h = w.call("POST", "/admin/backups/download", {"name": name, **w.creds(w.admin)}, token=tok)
    assert s == 200 and h["Cache-Control"] == "no-store"
    raw = base64.urlsafe_b64decode(o["data"] + "=" * (-len(o["data"]) % 4))
    assert raw == (w.state / "backups" / name).read_bytes() and raw.startswith(B.MAGIC)
    assert words.encode() not in raw
    link = w.state / "backups" / "hub-aaaaaaaa-20260101T000000Z.orchbak"
    link.symlink_to(w.state / "meta.json")
    absent = "hub-00000000-20260101T000000Z.orchbak"
    for bad in ("../meta.json", absent, name + "/../../meta.json", "/etc/passwd", name + "\n", link.name):
        s, o, _ = w.call("POST", "/admin/backups/download", {"name": bad, **w.creds(w.admin)}, token=tok)
        assert (s, o) == (404, {"error": "backup_not_found"}), bad


def test_the_audit_chain_and_history_record_key_made_downloaded_without_secrets(w):
    tok, words, name = _first(w)
    tok = _later(w)
    w.call("POST", "/admin/backups", {}, token=tok)
    w.call("POST", "/admin/backups/download", {"name": name, **w.creds(w.admin)}, token=tok)
    evs = _events(w, "hub_backup")
    assert [e["change"] for e in evs] == ["key_set", "made", "downloaded"]
    for e in evs:
        assert set(e) == {"change", "actor_user_id", "nonce"} and e["actor_user_id"] == w.admin
    recs = [r["o"] for _, r, _e in w.api.d.audit.store.after(0, 1000) if r["o"]["type"] == "hub_backup"]
    assert all(r["scope_tag"] is None for r in recs)  # hub-wide
    blob = json.dumps(evs)
    assert words not in blob and name not in blob
    from orch_hub.activity.feed import AUDIT_WORDS, WORDS

    assert "hub_backup" in AUDIT_WORDS and all(f"hub.backup.{k}" in WORDS for k in ("key_set", "made", "downloaded"))
    rows = w.api.d.activity._db.execute("SELECT type, detail FROM event WHERE type LIKE 'hub.backup.%'").fetchall()
    assert sorted(t for t, _d in rows) == ["hub.backup.downloaded", "hub.backup.key_set", "hub.backup.made"]
    assert all(words not in d and name not in d for _t, d in rows)


def test_a_chain_that_cannot_be_written_refuses_make_and_download_and_leaves_no_unaudited_file(w, monkeypatch):
    tok, _words, name = _first(w)
    before = {r["name"] for r in BackupService(w.env.keys).list()}

    def boom(**kw):
        raise RuntimeError("chain broken")

    monkeypatch.setattr(w.api.d.audit, "hub_backup", boom)
    tok = _later(w)
    assert w.call("POST", "/admin/backups", {}, token=tok)[0] == 500
    assert {r["name"] for r in BackupService(w.env.keys).list()} == before
    assert w.call("POST", "/admin/backups/download", {"name": name, **w.creds(w.admin)}, token=tok)[0] == 500


def test_the_contract_event_is_closed():
    ok = {"change": "made", "actor_user_id": None, "nonce": "A" * 43}
    assert AU.check_event("hub_backup", ok) == ok
    for bad in (
        {**ok, "change": "restored"},
        {**ok, "file": "x"},
        {k: v for k, v in ok.items() if k != "nonce"},
        {**ok, "actor_user_id": "xyz"},
    ):
        with pytest.raises(Exception):  # noqa: B017  # Refused("malformed")
            AU.check_event("hub_backup", bad)


# --- the server command ------------------------------------------------------------------------------------------------------


def _cli(w, *extra, out=None, ask=None):
    args = CLI._parser().parse_args(["backup-key", "--state", str(w.state), *extra])
    assert args.group == "backup-key"
    return BCLI.run(args, out=out or (lambda _l: None), now_ms=w.clock, tty=lambda: True, ask=ask or (lambda _p: ""))


def test_the_server_command_prints_the_words_once_and_refuses_a_second_setup(w, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    lines: list[str] = []
    assert _cli(w, out=lines.append) == 0
    rows = [ln.split() for ln in "\n".join(lines).splitlines() if ln[:2].strip().isdigit()]
    got = " ".join(t for row in rows for t in row[1::2])
    assert W.check_words(got) == got
    info = json.loads((w.state / SN.KEY_FILE).read_text())
    assert info["bk_pub"] == b64u(B.derive_keys(got).bk_pub)
    assert len(BackupService(w.env.keys).list()) == 1
    assert _cli(w) == 1  # already set: --replace is explicit
    assert "already" in capsys.readouterr().err
    _words_nowhere(w.state, got, (caplog.text.encode(), capsys.readouterr().out.encode()))
    evs = _events(w, "hub_backup")
    assert [e["change"] for e in evs] == ["key_set", "key_confirmed"] and evs[0]["actor_user_id"] is None
    assert _cli(w, "--replace") == 0
    assert len(BackupService(w.env.keys).list()) == 1  # the old key's backup is gone


# --- M1 (review): the server command and the hub process share one lock and one key file ---------------------------------

_CHILD = """
import sys
from pathlib import Path
from orch_hub.backup.service import BackupService
from orch_hub.keys import HubKeys
state = Path(sys.argv[1])
keys = HubKeys.open(state, now_ms=lambda: 1_800_000_000_000)
svc = BackupService(keys, now_ms=lambda: 1_800_000_000_000)
for _ in range(int(sys.argv[2])):
    words, _made = svc.set_key(None)
    if hasattr(svc, "confirm"):
        svc.confirm("cli")
    print(words, flush=True)
"""


def test_the_server_command_and_the_hub_make_backups_at_once_and_every_kept_file_opens_with_the_last_words(w, tmp_path):
    import subprocess
    import sys

    _first(w)
    svc = BackupService(w.env.keys, now_ms=w.clock)
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(w.state), "3"], stdout=subprocess.PIPE, text=True, env={**os.environ}
    )
    made = 0
    while child.poll() is None:
        svc.make(None)  # the hub's own backups while the command replaces the key
        made += 1
    out, _ = child.communicate()
    assert child.returncode == 0 and made >= 1
    last = out.strip().splitlines()[-1]
    svc.make(None)
    kept = svc.list()
    assert kept
    bk_pub = b64u(B.derive_keys(last).bk_pub)
    for r in kept:  # every kept file belongs to the last key
        env, _sealed = B.unpack_file((w.state / "backups" / r["name"]).read_bytes())
        assert env["o"]["bk_pub"] == bk_pub, r["name"]
    newest = (w.state / "backups" / kept[0]["name"]).read_bytes()
    assert B.open_backup(newest, words=last, dev_mode=True, live=None)  # and the MAC matches the stored K_auth
    assert (w.state / SN.KEY_FILE).stat().st_mode & 0o777 == 0o600
    assert not (w.state / "backups.lock").read_bytes()  # the lock file is only a lock


def test_the_key_is_one_file_and_the_lock_file_never_travels(w):
    _tok, words, name = _first(w)
    info = json.loads((w.state / SN.KEY_FILE).read_text())
    assert set(info) == {"v", "bk_pub", "k_auth", "set_ms", "pending"} and info["k_auth"] == b64u(
        B.derive_keys(words).k_auth
    )
    assert not any(p.name.startswith("backup-auth") for p in w.state.iterdir())
    got = B.open_backup((w.state / "backups" / name).read_bytes(), words=words, dev_mode=True, live=None)
    assert SN.KEY_FILE in got.files and "backups.lock" not in got.files


@pytest.mark.parametrize("var", ["ORCH_GRANT", "ORCH_SESSION"])
def test_the_server_command_refuses_an_agent_and_changes_nothing(w, monkeypatch, capsys, var):
    monkeypatch.setenv(var, "x")
    lines: list[str] = []
    assert _cli(w, out=lines.append) == 1
    assert "agent" in capsys.readouterr().err and lines == []
    assert not (w.state / SN.KEY_FILE).exists() and not (w.state / "backups").exists()
    assert "hub_backup" not in _types(w)


def test_the_server_command_refuses_when_its_output_is_not_a_terminal(w, monkeypatch, capsys):
    for v in ("ORCH_GRANT", "ORCH_SESSION"):
        monkeypatch.delenv(v, raising=False)
    args = CLI._parser().parse_args(["backup-key", "--state", str(w.state)])
    lines: list[str] = []
    assert BCLI.run(args, out=lines.append, now_ms=w.clock, tty=lambda: False) == 1
    assert "terminal" in capsys.readouterr().err and lines == []
    assert not (w.state / SN.KEY_FILE).exists() and not (w.state / "backups").exists()
    assert (
        BCLI.run(args, out=lines.append, now_ms=w.clock, tty=lambda: True, ask=lambda _p: "") == 0
    )  # the same call at a terminal works


def test_a_plain_http_private_range_address_is_trusted_only_when_it_is_the_settings_private_address(w):
    from orch_hub import settings as ST

    mine, other = "http://100.64.1.2:9000", "http://100.64.9.9:9000"
    for o in (mine, other):
        w.api.allow_origin(o)
    ST.save(
        w.state,
        {
            "v": 1,
            "name": "H",
            "company": "",
            "addresses": [{"kind": "private", "web": mine, "link": "100.64.1.2:9005"}],
        },
    )
    w.api.d.settings_file = w.state / ST.SETTINGS_FILE
    tok = w.login(w.admin)
    good = w.creds(w.admin)
    assert w.call("POST", "/admin/backup/key", good, token=tok, origin=other, host="100.64.9.9:9000")[:2] == (
        403,
        {"error": "insecure_origin"},
    )
    assert not (w.state / SN.KEY_FILE).exists()
    assert w.call("POST", "/admin/backup/key", good, token=tok, origin=mine, host="100.64.1.2:9000")[0] == 200


def test_while_a_new_key_waits_new_backups_never_prune_the_old_key_ones(w):
    tok, _words1, _n = _first(w)
    for _ in range(3):
        tok = _later(w)
        assert w.call("POST", "/admin/backups", {}, token=tok)[0] == 200
    tok = _later(w)
    assert _key(w, tok)[0] == 200  # replaced, not confirmed
    old = {r["name"] for r in BackupService(w.env.keys).list()} - {
        r["name"] for r in BackupService(w.env.keys).list()[:1]
    }
    assert len(old) == 4
    for _ in range(9):  # many new-key backups: the old-key ones stay (each key keeps its own 7)
        tok = _later(w)
        assert w.call("POST", "/admin/backups", {}, token=tok)[0] == 200
    names = {r["name"] for r in BackupService(w.env.keys).list()}
    assert old <= names
    new_pub = json.loads((w.state / SN.KEY_FILE).read_text())["bk_pub"]
    mine = [n for n in names if B.unpack_file((w.state / "backups" / n).read_bytes())[0]["o"]["bk_pub"] == new_pub]
    assert len(mine) == KEEP
    assert w.call("GET", "/admin/backup", token=tok)[1]["key_pending"] is True
    assert w.call("POST", "/admin/backup/key/confirm", {}, token=tok)[0] == 403  # a later session is not the one


def test_confirming_is_audited_without_secrets_and_a_broken_chain_deletes_nothing(w, monkeypatch):
    tok, _w1, _n = _first(w)
    tok = _later(w)
    _s, o, _ = _key(w, tok)
    before = {r["name"] for r in BackupService(w.env.keys).list()}
    real = w.api.d.audit.hub_backup

    def boom(**kw):
        if kw["change"] == "key_confirmed":
            raise RuntimeError("chain broken")
        return real(**kw)

    monkeypatch.setattr(w.api.d.audit, "hub_backup", boom)
    assert w.call("POST", "/admin/backup/key/confirm", {}, token=tok)[0] == 500
    assert {r["name"] for r in BackupService(w.env.keys).list()} == before
    monkeypatch.setattr(w.api.d.audit, "hub_backup", real)
    assert w.call("POST", "/admin/backup/key/confirm", {}, token=tok)[0] == 200
    evs = _events(w, "hub_backup")
    assert [e["change"] for e in evs][-1] == "key_confirmed" and evs[-1]["actor_user_id"] == w.admin
    assert set(evs[-1]) == {"change", "actor_user_id", "nonce"} and o["words"] not in json.dumps(evs)
    rows = w.api.d.activity._db.execute("SELECT type FROM event WHERE type='hub.backup.key_confirmed'").fetchall()
    assert len(rows) == 1


def test_the_server_command_waits_for_enter_and_reports_a_refused_confirm(w, capsys, monkeypatch):
    _first(w)

    # no Enter (end of input): the new key is made, the older backups stay, nothing is lost
    def eof(_p):
        raise EOFError

    assert _cli(w, "--replace", ask=eof) == 0
    assert "not confirmed" in capsys.readouterr().err
    assert len(BackupService(w.env.keys).list()) == 2
    # Enter: confirmed, the older one goes
    asked = []
    assert _cli(w, "--replace", ask=lambda p: asked.append(p) or "") == 0 and "Enter" in asked[0]
    assert len(BackupService(w.env.keys).list()) == 1

    # a confirm that is refused is said in plain words, exit 1
    def denied(self, pending="cli", actor=None):
        raise BackupError("confirm_denied")

    monkeypatch.setattr(BackupService, "confirm", denied)
    assert _cli(w, "--replace") == 1
    assert "could not confirm" in capsys.readouterr().err
