"""Builds ``contracts/hub/vectors_hub_backup.json``: the hub backup file (protocol.md §17) and the ``hub_where`` answer (§10.9).

Test code. Keys, words, salts and ephemeral keys are FAKE and fixed here (never in ``src``); signatures are RFC 6979. The
expected results are written out by hand (never computed by the code under test); the key-derivation and seal values
are computed here with their own primitives and then compared with ``orch_hub.contract.backup``.
Run ``cd packages/hub && uv run python ../../contracts/hub/make_vectors_hub_backup.py`` to rewrite the file; the output is
deterministic. Consumers: the hub replays every case (``packages/hub/tests/test_hub_backup_vectors.py``); orch-core
replays the ``hub_where`` cases (``packages/core/tests/hub/test_hub_where_vectors.py``)."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import tarfile
from functools import cache
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from orch_hub.contract import backup as B
from orch_hub.contract import suite as S
from orch_hub.contract.canon import b64u, cj
from orch_hub.contract.hub import hub_id

NOW_MS = 1_790_000_000_000
NOW_S = 1_790_000_000
WORDS_A = " ".join(["abandon"] * 23 + ["art"])  # BIP-39 all-zero entropy (valid checksum)
WORDS_B = " ".join(["zoo"] * 23 + ["vote"])  # BIP-39 all-ones entropy
WORDS_ATTACKER = " ".join(["legal"] * 23 + ["title"])  # a second, unrelated list (shape only; the checksum is S5's)
MAX_TOTAL = 200 * 1024 * 1024


def key(name: str) -> ec.EllipticCurvePrivateKey:
    return S.key_from_seed(S.fake_seed("hub-backup/" + name))


def pub(k: ec.EllipticCurvePrivateKey) -> str:
    return b64u(S.public_bytes(k))


# --- independent derivation of BK, bk_pub, K_auth ------------------------------------------------------------------------


@cache
def derive(words: str) -> dict[str, Any]:
    okm = hashlib.scrypt(
        " ".join(words.split()).encode(), salt=b"orch/v2/hub-backup-key|\x02", n=2**17, r=8, p=1, dklen=40, maxmem=2**28
    )
    d = int.from_bytes(okm, "big") % (S.P256_N - 1) + 1
    bk = ec.derive_private_key(d, ec.SECP256R1())
    prk = hmac.new(bytes(32), okm, hashlib.sha256).digest()  # HKDF-Extract
    k_auth = hmac.new(prk, b"orch/v2/hub-backup-auth" + b"\x01", hashlib.sha256).digest()  # HKDF-Expand, one block
    return {"okm": okm, "bk": bk, "bk_pub": S.public_bytes(bk), "k_auth": k_auth}


# --- tar and files ---------------------------------------------------------------------------------------------------------

FILES: dict[str, bytes] = {
    "hk_root.key": b"FAKE hk_root key file\n",
    "hub-settings.json": b'{"v":1,"name":"Demo hub"}\n',
    "hxk.key": b"FAKE hxk key file\n",
    "meta.json": b'{"hub_id":"demo"}\n',
    "op/7/cert.json": b'{"op_seq":7}\n',
    "registry.db": b"SQLite format 3\x00FAKE registry snapshot",
}


def tar_of(entries: list[tuple[str, bytes, bytes]]) -> bytes:
    """``(name, data, type)`` entries in order, USTAR, zeroed times and owners, mode 0600 (names may be hostile)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for name, data, typ in entries:
            ti = tarfile.TarInfo(name)
            ti.type, ti.mode, ti.mtime = typ, 0o600, 0
            if typ in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                ti.linkname, ti.size = "meta.json", 0
                tf.addfile(ti)
            elif typ in (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE, tarfile.DIRTYPE):
                ti.size = 0
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def file_list(files: dict[str, bytes]) -> list[dict[str, Any]]:
    return [{"path": p, "size": len(files[p]), "sha256": hashlib.sha256(files[p]).hexdigest()} for p in sorted(files)]


def tar_files(files: dict[str, bytes]) -> bytes:
    return tar_of([(p, files[p], tarfile.REGTYPE) for p in sorted(files)])


# --- headers, seal, file -----------------------------------------------------------------------------------------------------


def header(root: ec.EllipticCurvePrivateKey, words: str, files: dict[str, bytes], **over: Any) -> dict[str, Any]:
    o = {
        "v": 2,
        "suite": 2,
        "kind": "hub.backup",
        "hv": 0,
        "hub_id": hub_id(S.public_bytes(root)),
        "hk_root_pub": pub(root),
        "made_ms": NOW_MS,
        "op_seq": 7,
        "card_seq": 12,
        "snapshot_seq": 40,
        "bk_pub": b64u(derive(words)["bk_pub"]),
        "files": file_list(files),
        "dev": True,
    }
    o.update(over)
    return o


def seal(o: dict[str, Any], tar: bytes, eph_name: str, bk_pub: bytes | None = None) -> bytes:
    bk_pub = bk_pub or __import__("base64").urlsafe_b64decode(o["bk_pub"] + "=")
    eph = key(eph_name)
    sealed = B._seal_with(eph, bk_pub, o, tar)
    # the same value from first principles
    ss = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), bk_pub))
    k = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=S.public_bytes(eph) + bk_pub,
        info=b"orch/v2/seal|hub-backup|" + o["hub_id"].encode(),
    ).derive(ss)
    assert sealed == S.public_bytes(eph) + AESGCM(k).encrypt(bytes(12), tar, b"orch/v2/hub-backup-aad|" + cj(o))
    return sealed


def envelope(root: ec.EllipticCurvePrivateKey, o: dict[str, Any], k_auth: bytes) -> dict[str, Any]:
    sig = S.sign(root, B.LABEL + cj(o), deterministic=True)
    return {"o": o, "sig": b64u(sig), "mac": b64u(hmac.new(k_auth, cj(o), hashlib.sha256).digest())}


def make(
    root_name: str = "root",
    words: str = WORDS_A,
    files: dict[str, bytes] | None = None,
    tar: bytes | None = None,
    eph: str = "eph",
    **over: Any,
) -> dict[str, Any]:
    root = key(root_name)
    files = files or FILES
    o = header(root, words, files, **over)
    t = tar if tar is not None else tar_files(files)
    return {"root": root, "o": o, "env": envelope(root, o, derive(words)["k_auth"]), "sealed": seal(o, t, eph)}


def file_hex(m: dict[str, Any]) -> str:
    return B.pack_file(m["env"], m["sealed"]).hex()


def repack(m: dict[str, Any], env: dict[str, Any] | None = None, sealed: bytes | None = None) -> str:
    return B.pack_file(env or m["env"], m["sealed"] if sealed is None else sealed).hex()


LIVE = lambda m: {"hub_id": m["o"]["hub_id"], "hk_root_pub": m["o"]["hk_root_pub"]}


def case(name: str, check: str, args: dict[str, Any], expect: dict[str, Any], note: str = "") -> dict[str, Any]:
    c = {"name": name, "check": check, "args": args, "expect": expect}
    if note:
        c["note"] = note
    return c


def err(code: str, reason: str | None = None) -> dict[str, Any]:
    e: dict[str, Any] = {"error": code}
    if reason:
        e["reason"] = reason
    return e


def flip(b: bytes, i: int) -> bytes:
    return b[:i] + bytes([b[i] ^ 1]) + b[i + 1 :]


# --- the cases ---------------------------------------------------------------------------------------------------------------


def open_cases() -> list[dict[str, Any]]:
    good = make()
    fresh = {"words": WORDS_A, "dev_mode": True, "live": None}
    place = {"words": WORDS_A, "dev_mode": True, "live": LIVE(good)}
    cs = []

    def oc(name: str, m: dict[str, Any] | str, args: dict[str, Any], expect: dict[str, Any], note: str = "") -> None:
        fh = m if isinstance(m, str) else file_hex(m)
        cs.append(case(name, "open_backup", {"file_hex": fh, **args}, expect, note))

    ok_files = {p: hashlib.sha256(d).hexdigest() for p, d in FILES.items()}
    oc("backup_good_fresh_server", good, fresh, {"ok": {"files": ok_files}})
    oc("backup_good_in_place", good, place, {"ok": {"files": ok_files}})

    # a header byte changed, signature and mac of the original kept
    env = json.loads(json.dumps(good["env"]))
    env["o"]["made_ms"] += 1
    oc("backup_tampered_header_sig_fails", repack(good, env), fresh, err("backup_invalid", "sig"))
    # the root re-signs a changed header but the mac is the old one (a holder of the root, not of the words)
    o2 = {**good["o"], "op_seq": 6}
    env = {"o": o2, "sig": b64u(S.sign(good["root"], B.LABEL + cj(o2), deterministic=True)), "mac": good["env"]["mac"]}
    oc("backup_tampered_header_mac_fails", repack(good, env), fresh, err("backup_invalid", "mac"))
    # mac flipped alone
    env = {**good["env"], "mac": b64u(flip(__import__("base64").urlsafe_b64decode(good["env"]["mac"] + "="), 0))}
    oc("backup_mac_bit_flipped", repack(good, env), fresh, err("backup_invalid", "mac"))
    # signature flipped alone
    sig = __import__("base64").urlsafe_b64decode(good["env"]["sig"] + "==")
    oc(
        "backup_sig_bit_flipped",
        repack(good, {**good["env"], "sig": b64u(flip(sig, 5))}),
        fresh,
        err("backup_invalid", "sig"),
    )

    # forged: another root, sealed to the copied bk_pub (the header is public), valid signature, mac without the words
    forged = make("attacker_root", WORDS_A, eph="eph_forged")
    forged_env = envelope(forged["root"], forged["o"], derive(WORDS_ATTACKER)["k_auth"])
    fh = repack(forged, forged_env)
    oc(
        "backup_forged_other_root_copied_bk_pub_fresh",
        fh,
        fresh,
        err("backup_invalid", "mac"),
        "valid signature under the attacker's root, sealed to the real bk_pub; only the mac (needs the words) stops it",
    )
    oc("backup_forged_other_root_copied_bk_pub_in_place", fh, place, err("other_hub"))
    # forged with the attacker's own words and bk_pub: a different key
    own = make("attacker_root", WORDS_ATTACKER, eph="eph_forged2")
    oc("backup_forged_own_words_opened_with_real_words", own, fresh, err("wrong_backup_key"))

    oc("backup_wrong_words", good, {**fresh, "words": WORDS_B}, err("wrong_backup_key"))
    oc("backup_words_not_24", good, {**fresh, "words": "abandon abandon"}, err("backup_invalid", "words"))

    oc("backup_dev_refused_at_real_hub", good, {**fresh, "dev_mode": False}, err("dev_refused"))
    real = make(dev=False)
    oc("backup_dev_mismatch_at_dev_hub", real, fresh, err("dev_mismatch"))
    oc("backup_real_at_real_hub", real, {**fresh, "dev_mode": False, "live": LIVE(real)}, {"ok": {"files": ok_files}})

    other = make("other_root", WORDS_A, eph="eph_other")
    oc("backup_other_hub_in_place", other, place, err("other_hub"))
    oc(
        "backup_other_hub_fresh_server_is_accepted_shape",
        other,
        fresh,
        {"ok": {"files": ok_files}},
        "a fresh server has no running hub: the fingerprint shown to the person is what decides",
    )

    # contents
    bad_files = {**FILES, "meta.json": b'{"hub_id":"EVIL"}\n'}
    oc("backup_file_hash_mismatch", make(files=FILES, tar=tar_files(bad_files)), fresh, err("backup_invalid", "hash"))
    short = {**FILES, "meta.json": b"x"}
    oc("backup_file_size_mismatch", make(files=FILES, tar=tar_files(short)), fresh, err("backup_invalid", "tar_size"))
    extra = tar_of(
        [(p, FILES[p], tarfile.REGTYPE) for p in sorted(FILES)] + [("evil.sh", b"rm -rf /\n", tarfile.REGTYPE)]
    )
    oc("backup_extra_file_not_in_header", make(tar=extra), fresh, err("backup_invalid", "tar_unlisted"))
    sym = tar_of(
        [("meta.json", b"", tarfile.SYMTYPE)]
        + [(p, FILES[p], tarfile.REGTYPE) for p in sorted(FILES) if p != "meta.json"]
    )
    oc("backup_symlink_entry", make(tar=sym), fresh, err("backup_invalid", "tar_type"))
    miss = tar_of([(p, FILES[p], tarfile.REGTYPE) for p in sorted(FILES) if p != "hxk.key"])
    oc("backup_listed_file_missing", make(tar=miss), fresh, err("backup_invalid", "tar_missing"))
    oc(
        "backup_ciphertext_bit_flipped",
        repack(good, sealed=flip(good["sealed"], 100)),
        fresh,
        err("backup_invalid", "seal"),
    )
    oc(
        "backup_sealed_payload_of_another_backup",
        repack(good, sealed=make(eph="eph_x", made_ms=NOW_MS + 5)["sealed"]),
        fresh,
        err("backup_invalid", "seal"),
        "the header is the AAD: a payload moved under another header does not open",
    )

    # container
    raw = bytes.fromhex(file_hex(good))
    oc("container_bad_magic", flip(raw, 0).hex(), fresh, err("backup_invalid", "container"))
    oc("container_truncated", raw[:40].hex(), fresh, err("backup_invalid", "container"))
    oc("container_empty", "", fresh, err("backup_invalid", "container"))
    env = {**good["env"], "extra": 1}
    oc("envelope_extra_key", repack(good, env), fresh, err("backup_invalid", "envelope"))
    o3 = {**good["o"], "extra": 1}
    env = {"o": o3, "sig": b64u(S.sign(good["root"], B.LABEL + cj(o3), deterministic=True)), "mac": good["env"]["mac"]}
    oc("header_extra_field_signed", repack(good, env), fresh, err("backup_invalid", "fields"))
    o4 = {**good["o"], "hub_id": "00" * 16}
    env = {"o": o4, "sig": b64u(S.sign(good["root"], B.LABEL + cj(o4), deterministic=True)), "mac": good["env"]["mac"]}
    oc("header_hub_id_not_the_root_s", repack(good, env), fresh, err("backup_invalid", "hub_id"))
    oc(
        "header_wrong_label_signature",
        repack(
            good,
            {
                **good["env"],
                "sig": b64u(S.sign(good["root"], b"orch/v2/hub-card|" + cj(good["o"]), deterministic=True)),
            },
        ),
        fresh,
        err("backup_invalid", "sig"),
    )
    return cs


def tar_cases() -> list[dict[str, Any]]:
    fl = file_list(FILES)
    base = [(p, FILES[p], tarfile.REGTYPE) for p in sorted(FILES)]
    cs = []

    def tc(
        name: str, entries: list[tuple[str, bytes, bytes]], files: list[dict[str, Any]], expect: dict[str, Any]
    ) -> None:
        cs.append(case(name, "tar", {"files": files, "tar_hex": tar_of(entries).hex()}, expect))

    tc("tar_good", base, fl, {"ok": {"files": {p: hashlib.sha256(d).hexdigest() for p, d in FILES.items()}}})
    rest = lambda skip: [e for e in base if e[0] != skip]
    tc(
        "tar_absolute_path",
        rest("meta.json") + [("/etc/cron.d/x", b"x", tarfile.REGTYPE)],
        fl,
        err("backup_invalid", "tar_path"),
    )
    tc(
        "tar_dotdot",
        rest("meta.json") + [("../meta.json", FILES["meta.json"], tarfile.REGTYPE)],
        fl,
        err("backup_invalid", "tar_path"),
    )
    tc(
        "tar_dotdot_inside",
        rest("meta.json") + [("op/../meta.json", FILES["meta.json"], tarfile.REGTYPE)],
        fl,
        err("backup_invalid", "tar_path"),
    )
    tc("tar_symlink", rest("meta.json") + [("meta.json", b"", tarfile.SYMTYPE)], fl, err("backup_invalid", "tar_type"))
    tc("tar_hardlink", rest("meta.json") + [("meta.json", b"", tarfile.LNKTYPE)], fl, err("backup_invalid", "tar_type"))
    tc(
        "tar_char_device",
        rest("meta.json") + [("meta.json", b"", tarfile.CHRTYPE)],
        fl,
        err("backup_invalid", "tar_type"),
    )
    tc(
        "tar_block_device",
        rest("meta.json") + [("meta.json", b"", tarfile.BLKTYPE)],
        fl,
        err("backup_invalid", "tar_type"),
    )
    tc("tar_fifo", rest("meta.json") + [("meta.json", b"", tarfile.FIFOTYPE)], fl, err("backup_invalid", "tar_type"))
    tc("tar_directory_entry", base + [("op/", b"", tarfile.DIRTYPE)], fl, err("backup_invalid", "tar_type"))
    tc(
        "tar_oversize_entry",
        rest("meta.json") + [("meta.json", b"y" * 5000, tarfile.REGTYPE)],
        fl,
        err("backup_invalid", "tar_size"),
    )
    tc("tar_extra_file", base + [("extra.txt", b"x", tarfile.REGTYPE)], fl, err("backup_invalid", "tar_unlisted"))
    tc("tar_duplicate_entry", base + [base[0]], fl, err("backup_invalid", "tar_dup"))
    tc("tar_missing_entry", rest("hxk.key"), fl, err("backup_invalid", "tar_missing"))
    tc(
        "tar_hash_mismatch",
        rest("meta.json") + [("meta.json", b'{"hub_id":"EVIL"}\n', tarfile.REGTYPE)],
        [{**f, "size": 18} if f["path"] == "meta.json" else f for f in fl],
        err("backup_invalid", "hash"),
    )
    cs.append(
        case("tar_not_a_tar", "tar", {"files": fl, "tar_hex": (b"not a tar" * 100).hex()}, err("backup_invalid", "tar"))
    )
    return cs


def list_cases() -> list[dict[str, Any]]:
    fl = file_list(FILES)
    cs = []

    def lc(name: str, files: Any, expect: dict[str, Any]) -> None:
        cs.append(case(name, "file_list", {"files": files}, expect))

    lc("files_good", fl, {"ok": True})
    lc("files_empty", [], err("backup_invalid", "files"))
    lc("files_not_a_list", {"meta.json": 1}, err("backup_invalid", "files"))
    lc("files_extra_key", [{**fl[0], "mode": 0}] + fl[1:], err("backup_invalid", "files"))
    lc("files_absolute_path", [{**fl[0], "path": "/etc/passwd"}] + fl[1:], err("backup_invalid", "files_path"))
    lc("files_dotdot_path", [fl[0], {**fl[1], "path": "../x"}] + fl[2:], err("backup_invalid", "files_path"))
    lc("files_backslash_path", [fl[0], {**fl[1], "path": "a\\b"}] + fl[2:], err("backup_invalid", "files_path"))
    lc("files_empty_segment", [fl[0], {**fl[1], "path": "op//x"}] + fl[2:], err("backup_invalid", "files_path"))
    for ex in (
        "setup-token",
        "route-queue.db",
        "route-queue.db-wal",
        "event-deliver.db",
        "backups/hub-1.orchbak",
        "tmp/x",
        "logs/hub.log",
        "hub.log",
    ):
        entry = {"path": ex, "size": 1, "sha256": "0" * 64}
        lc(
            "files_excluded_" + ex.replace("/", "_").replace(".", "_").replace("-", "_"),
            sorted(fl + [entry], key=lambda f: f["path"]),
            err("backup_invalid", "files_path"),
        )
    lc("files_unsorted", [fl[1], fl[0]] + fl[2:], err("backup_invalid", "files_order"))
    lc("files_duplicate_path", [fl[0], fl[0]] + fl[1:], err("backup_invalid", "files_order"))
    lc(
        "files_file_is_also_a_directory",
        sorted(fl + [{"path": "meta.json/x", "size": 1, "sha256": "0" * 64}], key=lambda f: f["path"]),
        err("backup_invalid", "files_order"),
    )
    extra = lambda *paths: sorted(fl + [{"path": x, "size": 1, "sha256": "0" * 64} for x in paths], key=lambda f: f["path"])
    lc("files_dir_of_another_with_sibling", extra("keys", "keys-x", "keys/a"), err("backup_invalid", "files_order"))
    lc("files_case_fold_duplicate", extra("HK_ROOT.key"), err("backup_invalid", "files_order"))
    lc(
        "files_missing_required_root_key",
        [f for f in fl if f["path"] != "hk_root.key"],
        err("backup_invalid", "files_required"),
    )
    lc("files_size_negative", [{**fl[0], "size": -1}] + fl[1:], err("backup_invalid", "files_size"))
    lc("files_size_over_limit", [{**fl[0], "size": MAX_TOTAL + 1}] + fl[1:], err("backup_invalid", "files_size"))
    lc("files_sum_over_limit", [{**f, "size": MAX_TOTAL // 2} for f in fl], err("backup_invalid", "files_size"))
    lc("files_sha_uppercase", [{**fl[0], "sha256": "A" * 64}] + fl[1:], err("backup_invalid", "files_size"))
    lc("files_size_float", [{**fl[0], "size": 1.0}] + fl[1:], err("backup_invalid", "files_size"))
    return cs


M = 2**53 - 1


def floor_cases() -> list[dict[str, Any]]:
    cs = []

    def fc(name: str, live: Any, backup: dict[str, int], now_s: int, expect: dict[str, Any], note: str = "") -> None:
        cs.append(case(name, "restore_floor", {"live": live, "backup": backup, "now_s": now_s}, expect, note))

    b = {"op_seq": 7, "card_seq": 12, "snapshot_seq": 40}
    fc(
        "floor_in_place_live_ahead",
        {"op_seq": 30, "card_seq": 31, "snapshot_seq": 90},
        b,
        NOW_S,
        {"ok": {"op_seq": 1030, "card_seq": 1031, "snapshot_seq": 1090}},
        "max(live, backup) + 1000",
    )
    fc(
        "floor_in_place_backup_ahead",
        {"op_seq": 1, "card_seq": 2, "snapshot_seq": 3},
        b,
        NOW_S,
        {"ok": {"op_seq": 1007, "card_seq": 1012, "snapshot_seq": 1040}},
    )
    fc(
        "floor_in_place_mixed",
        {"op_seq": 50, "card_seq": 2, "snapshot_seq": 40},
        b,
        NOW_S,
        {"ok": {"op_seq": 1050, "card_seq": 1012, "snapshot_seq": 1040}},
    )
    fc(
        "floor_double_restore_same_backup",
        {"op_seq": 1007, "card_seq": 1012, "snapshot_seq": 1040},
        b,
        NOW_S,
        {"ok": {"op_seq": 2007, "card_seq": 2012, "snapshot_seq": 2040}},
        "the second restore of the same backup lands above the first: a bound workspace accepts both",
    )
    fc(
        "floor_fresh_server_unix_seconds_win",
        None,
        b,
        NOW_S,
        {"ok": {"op_seq": NOW_S, "card_seq": NOW_S, "snapshot_seq": NOW_S}},
    )
    fc(
        "floor_fresh_server_backup_ahead_of_clock",
        None,
        {"op_seq": NOW_S, "card_seq": 5, "snapshot_seq": NOW_S + 7},
        NOW_S,
        {"ok": {"op_seq": NOW_S + 1000, "card_seq": NOW_S, "snapshot_seq": NOW_S + 1007}},
    )
    fc(
        "floor_fresh_server_twice_from_same_backup",
        None,
        b,
        NOW_S + 60,
        {"ok": {"op_seq": NOW_S + 60, "card_seq": NOW_S + 60, "snapshot_seq": NOW_S + 60}},
        "a fresh restore a minute later is higher again",
    )
    top = M - 1000
    fc(
        "floor_boundary_exact_max",
        {"op_seq": top, "card_seq": 0, "snapshot_seq": 0},
        {"op_seq": 0, "card_seq": 0, "snapshot_seq": 0},
        NOW_S,
        {"ok": {"op_seq": M, "card_seq": 1000, "snapshot_seq": 1000}},
        "2^53 - 1 is the last value",
    )
    fc(
        "floor_boundary_one_over_in_place",
        {"op_seq": top + 1, "card_seq": 0, "snapshot_seq": 0},
        {"op_seq": 0, "card_seq": 0, "snapshot_seq": 0},
        NOW_S,
        err("backup_invalid", "seq_overflow"),
    )
    fc(
        "floor_boundary_backup_over_fresh",
        None,
        {"op_seq": top + 1, "card_seq": 0, "snapshot_seq": 0},
        NOW_S,
        err("backup_invalid", "seq_overflow"),
    )
    fc(
        "floor_boundary_backup_ok_fresh",
        None,
        {"op_seq": top, "card_seq": 0, "snapshot_seq": 0},
        NOW_S,
        {"ok": {"op_seq": M, "card_seq": NOW_S, "snapshot_seq": NOW_S}},
    )
    fc(
        "floor_backup_value_unsafe",
        None,
        {"op_seq": 2**53, "card_seq": 0, "snapshot_seq": 0},
        NOW_S,
        err("backup_invalid", "fields"),
    )
    fc(
        "floor_backup_value_negative",
        None,
        {"op_seq": -1, "card_seq": 0, "snapshot_seq": 0},
        NOW_S,
        err("backup_invalid", "fields"),
    )
    for nm, bad in (("bool", True), ("float", 1.5), ("negative", -1), ("over_max", M + 1), ("string", "1")):
        fc("floor_now_s_" + nm, None, b, bad, err("backup_invalid", "fields"), "now_s must be an int in 0..2^53-1")
    fc(
        "floor_now_s_zero_ok",
        {"op_seq": 1, "card_seq": 1, "snapshot_seq": 1},
        b,
        0,
        {"ok": {"op_seq": 1007, "card_seq": 1012, "snapshot_seq": 1040}},
    )
    return cs


def where_cases() -> list[dict[str, Any]]:
    cs = []

    def wc(name: str, ans: Any, expect: dict[str, Any], note: str = "") -> None:
        cs.append(case(name, "hub_where", {"answer": ans}, expect, note))

    a = lambda kind="private", link="100.64.1.2:8105": {"kind": kind, "link": link}
    ok = {"addresses": [a(), a("office", "192.168.1.20:8105")], "name": "Demo hub"}
    wc("where_ok", ok, {"ok": ok})
    wc(
        "where_ok_all_four_kinds",
        {
            "addresses": [
                a("private"),
                a("office", "10.0.0.5:8105"),
                a("server", "127.0.0.1:8105"),
                a("public", "hub.example.com:443"),
            ],
            "name": "H",
        },
        {
            "ok": {
                "addresses": [
                    a("private"),
                    a("office", "10.0.0.5:8105"),
                    a("server", "127.0.0.1:8105"),
                    a("public", "hub.example.com:443"),
                ],
                "name": "H",
            }
        },
    )
    wc(
        "where_ok_ipv6",
        {"addresses": [a("server", "[::1]:8105")], "name": "H"},
        {"ok": {"addresses": [a("server", "[::1]:8105")], "name": "H"}},
    )
    wc(
        "where_name_control_and_bidi_stripped",
        {"addresses": [a()], "name": "  Dem\u202eo\u200b \u0007hub\u2028 "},
        {"ok": {"addresses": [a()], "name": "Demo hub"}},
        "Cc, Cf (bidi, zero-width), Zl, Zp removed; outer spaces trimmed",
    )
    wc(
        "where_name_surrogate_private_use_unassigned_stripped",
        {"addresses": [a()], "name": "De\ud800m\ue000o\uffff hub"},
        {"ok": {"addresses": [a()], "name": "Demo hub"}},
        "Cs (lone surrogate), Co (private use) and Cn (U+FFFF, a permanent noncharacter) removed",
    )
    wc("where_name_only_surrogate_private_use", {"addresses": [a()], "name": "\ud800\ue000"}, err("malformed"))
    wc("where_name_only_controls", {"addresses": [a()], "name": "\u202e\u200b"}, err("malformed"))
    wc("where_name_60_ok", {"addresses": [a()], "name": "n" * 60}, {"ok": {"addresses": [a()], "name": "n" * 60}})
    wc("where_name_61_long", {"addresses": [a()], "name": "n" * 61}, err("malformed"))
    wc("where_name_empty", {"addresses": [a()], "name": ""}, err("malformed"))
    wc("where_name_not_a_string", {"addresses": [a()], "name": 7}, err("malformed"))
    wc(
        "where_five_entries",
        {"addresses": [a(link=f"10.0.0.{i}:8105") for i in range(1, 6)], "name": "H"},
        err("malformed"),
    )
    wc(
        "where_four_entries_ok",
        {"addresses": [a(link=f"10.0.0.{i}:8105") for i in range(1, 5)], "name": "H"},
        {"ok": {"addresses": [a(link=f"10.0.0.{i}:8105") for i in range(1, 5)], "name": "H"}},
    )
    wc("where_no_entries", {"addresses": [], "name": "H"}, err("malformed"))
    for nm, link in (
        ("no_port", "10.0.0.1"),
        ("port_zero", "10.0.0.1:0"),
        ("port_too_big", "10.0.0.1:65536"),
        ("port_leading_zero", "10.0.0.1:08105"),
        ("scheme", "http://10.0.0.1:8105"),
        ("path", "10.0.0.1:8105/x"),
        ("space", "10.0.0.1 :8105"),
        ("empty_host", ":8105"),
        ("host_dash_edge", "-a.example.com:8105"),
        ("v6_unbracketed", "::1:8105"),
        ("userinfo", "me@10.0.0.1:8105"),
        ("newline", "10.0.0.1:8105\n"),
        ("long", "a" * 301 + ":8105"),
        ("null", None),
        ("number", 8105),
    ):
        wc("where_bad_addr_" + nm, {"addresses": [a(link=link)], "name": "H"}, err("malformed"))
    wc("where_unknown_kind", {"addresses": [a("vpn")], "name": "H"}, err("malformed"))
    wc("where_kind_case", {"addresses": [a("Private")], "name": "H"}, err("malformed"))
    wc("where_kind_not_string", {"addresses": [a(7)], "name": "H"}, err("malformed"))
    wc("where_duplicate_link", {"addresses": [a("private"), a("office")], "name": "H"}, err("malformed"))
    wc("where_entry_extra_key", {"addresses": [{**a(), "tls": "x"}], "name": "H"}, err("malformed"))
    wc("where_entry_missing_link", {"addresses": [{"kind": "private"}], "name": "H"}, err("malformed"))
    wc("where_answer_extra_key", {"addresses": [a()], "name": "H", "x": 1}, err("malformed"))
    wc("where_answer_missing_name", {"addresses": [a()]}, err("malformed"))
    wc("where_addresses_not_list", {"addresses": a(), "name": "H"}, err("malformed"))
    wc("where_not_an_object", [a()], err("malformed"))
    return cs


def build() -> dict[str, Any]:
    d = derive(WORDS_A)
    kat = [
        case(
            "bk_key_words_a",
            "bk_key",
            {"words": WORDS_A},
            {
                "ok": {
                    "okm_hex": derive(WORDS_A)["okm"].hex(),
                    "bk_pub": b64u(d["bk_pub"]),
                    "k_auth_hex": d["k_auth"].hex(),
                }
            },
            "scrypt N=2^17 r=8 p=1 dklen=40, salt 'orch/v2/hub-backup-key|' + 0x02; K_auth = HKDF-SHA256(okm, salt 0x00*32, info 'orch/v2/hub-backup-auth')",
        ),
        case(
            "bk_key_words_b",
            "bk_key",
            {"words": WORDS_B},
            {
                "ok": {
                    "okm_hex": derive(WORDS_B)["okm"].hex(),
                    "bk_pub": b64u(derive(WORDS_B)["bk_pub"]),
                    "k_auth_hex": derive(WORDS_B)["k_auth"].hex(),
                }
            },
        ),
        case(
            "bk_key_words_normalised",
            "bk_key",
            {"words": "  ABANDON\tabandon " + " ".join(["abandon"] * 21) + "\n Art "},
            {
                "ok": {
                    "okm_hex": derive(WORDS_A)["okm"].hex(),
                    "bk_pub": b64u(d["bk_pub"]),
                    "k_auth_hex": d["k_auth"].hex(),
                }
            },
            "NFKD, lower case, any white space",
        ),
        case("bk_key_23_words", "bk_key", {"words": " ".join(["abandon"] * 23)}, err("backup_invalid", "words")),
        case("bk_key_25_words", "bk_key", {"words": " ".join(["abandon"] * 25)}, err("backup_invalid", "words")),
    ]
    good = make()
    labels = {
        "sign_header": B.LABEL.decode(),
        "aad": B.AAD_LABEL.decode(),
        "kdf_salt_hex": B.KDF_BK_SALT.hex(),
        "kdf_auth_info": B.KDF_AUTH_INFO.decode(),
        "seal_info": "orch/v2/seal|hub-backup|<hub_id>",
    }
    return {
        "contract": "orch.hub-backup/0",
        "note": "FAKE keys and words, test-only. Cases are replayed by the hub (all) and orch-core (hub_where only).",
        "labels": labels,
        "constants": {
            "max_total": MAX_TOTAL,
            "seq_jump": 1000,
            "max_addresses": 4,
            "max_name": 60,
            "magic": B.MAGIC.decode(),
        },
        "words": {"a": WORDS_A, "b": WORDS_B, "attacker": WORDS_ATTACKER},
        "keys": {"root": pub(key("root")), "attacker_root": pub(key("attacker_root"))},
        "example": {
            "header": good["o"],
            "envelope": good["env"],
            "sealed_hex": good["sealed"].hex(),
            "tar_hex": tar_files(FILES).hex(),
        },
        "cases": kat + open_cases() + tar_cases() + list_cases() + floor_cases() + where_cases(),
    }


if __name__ == "__main__":
    out = Path(__file__).with_name("vectors_hub_backup.json")
    out.write_text(json.dumps(build(), indent=1, sort_keys=True) + "\n")
    print(f"wrote {out} ({len(build()['cases'])} cases)")
