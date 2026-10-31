"""Builds ``contracts/hub/vectors_hub_backup.json``: the hub backup file (protocol.md §17), the ``hub_where`` answer (§10.9)
and the signed ``hub.publish_where`` object with its origin rule (§10.9.1).

Test code. Keys, words, salts and ephemeral keys are FAKE and fixed here (never in ``src``); signatures are RFC 6979. The
expected results are written out by hand (never computed by the code under test); the key-derivation and seal values
are computed here with their own primitives and then compared with ``orch_hub.contract.backup``.
Run ``cd packages/hub && uv run python ../../contracts/hub/make_vectors_hub_backup.py`` to rewrite the file; the output is
deterministic. Consumers: the hub replays every case (``packages/hub/tests/test_hub_backup_vectors.py``); orch-core
replays the ``hub_where``, ``publish_where`` and ``canonical_origin`` cases (``packages/core/tests/hub/test_hub_where_vectors.py``)."""

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
        "hub-removal.json",
        "remove-order.json",
        "remove-order.used",
        "remove-result.json",
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
    extra = lambda *paths: sorted(
        fl + [{"path": x, "size": 1, "sha256": "0" * 64} for x in paths], key=lambda f: f["path"]
    )
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
    for nm, pub in (("https", "https://share.example.com"), ("https_port", "https://share.example.com:8443"), ("loopback", "http://127.0.0.1:8102")):
        wc("where_publish_ok_" + nm, {**ok, "publish": pub}, {"ok": ok}, "#1838: the legacy `publish` string is accepted in shape and dropped, never returned")
    for nm, pub in (
        ("plain_http_elsewhere", "http://100.64.1.2:8102"),
        ("with_path", "https://share.example.com/x"),
        ("userinfo", "https://u:p@share.example.com"),
        ("not_a_string", 5),
        ("upper_case", "HTTPS://SHARE.example.com"),
        ("port_zero_over", "https://share.example.com:70000"),
    ):
        wc("where_publish_" + nm + "_refused", {**ok, "publish": pub}, {"error": "malformed"}, "P02: any other publish is malformed")
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
    # #1838: the optional signed `publish_where` is checked for its shape only (the signature needs the pinned card)
    good = pw_signed(pw_o())
    wc("where_publish_where_ok", {**ok, "publish_where": good}, {"ok": {**ok, "publish_where": good}}, "returned as sent")
    wc(
        "where_publish_where_and_legacy_publish",
        {**ok, "publish": "https://share.example.com", "publish_where": good},
        {"ok": {**ok, "publish_where": good}},
        "the legacy `publish` string is ignored even next to a publish_where",
    )
    wc(
        "where_publish_where_not_verified_here",
        {**ok, "publish_where": pw_signed(pw_o(server="https://10.0.0.5"), "op_other")},
        {"ok": {**ok, "publish_where": pw_signed(pw_o(server="https://10.0.0.5"), "op_other")}},
        "shape only: a bad signature or a refused origin is check_publish_where's, with the pinned card",
    )
    bad_sig_len = {"o": good["o"], "sig": b64u(bytes(63))}
    for nm, pwv, expect in (
        ("not_an_object", "x", err("malformed")),
        ("null", None, err("malformed")),
        ("envelope_extra_key", {**good, "mac": "x"}, err("malformed")),
        ("sig_missing", {"o": good["o"]}, err("malformed")),
        ("sig_wrong_length", bad_sig_len, err("malformed")),
        ("sig_not_base64url", {"o": good["o"], "sig": "!!"}, err("malformed")),
        ("field_missing", {"o": {k: v for k, v in good["o"].items() if k != "share"}, "sig": good["sig"]}, err("malformed")),
        ("field_extra", {"o": {**good["o"], "path": "/x"}, "sig": good["sig"]}, err("malformed")),
        ("seq_zero", {"o": {**good["o"], "seq": 0}, "sig": good["sig"]}, err("malformed")),
        ("seq_bool", {"o": {**good["o"], "seq": True}, "sig": good["sig"]}, err("malformed")),
        ("seq_over_2_53", {"o": {**good["o"], "seq": 2**53}, "sig": good["sig"]}, err("malformed")),
        ("origin_not_a_string", {"o": {**good["o"], "share": 7}, "sig": good["sig"]}, err("malformed")),
        ("hub_id_not_hex16", {"o": {**good["o"], "hub_id": "ab"}, "sig": good["sig"]}, err("malformed")),
        ("dev_not_bool", {"o": {**good["o"], "dev": 0}, "sig": good["sig"]}, err("malformed")),
        ("wrong_kind", {"o": {**good["o"], "kind": "hub.allow"}, "sig": good["sig"]}, err("malformed")),
        ("wrong_suite", {"o": {**good["o"], "suite": 1}, "sig": good["sig"]}, err("wrong_suite")),
        ("wrong_v", {"o": {**good["o"], "v": 3}, "sig": good["sig"]}, err("malformed")),
    ):
        wc("where_publish_where_" + nm, {**ok, "publish_where": pwv}, expect)
    return cs


# --- #1838: hub.publish_where ------------------------------------------------------------------------------------------------

PW_LABEL = b"orch/v2/hub-publish-where|"  # written out here, not read from the code under test
PW_HUB = hub_id(S.public_bytes(key("root")))
PW_OTHER_HUB = hub_id(S.public_bytes(key("attacker_root")))
PW_SERVER = "https://pub.example.com"
PW_SHARE = "https://share.example.com:8443"


def pw_o(**over: Any) -> dict[str, Any]:
    o = {
        "v": 2,
        "suite": 2,
        "kind": "hub.publish_where",
        "hv": 0,
        "hub_id": PW_HUB,
        "server": PW_SERVER,
        "share": PW_SHARE,
        "seq": 5,
        "issued_ms": NOW_MS,
        "dev": False,
    }
    o.update(over)
    return o


def pw_signed(o: dict[str, Any], signer: str = "op") -> dict[str, Any]:
    return {"o": o, "sig": b64u(S.sign(key(signer), PW_LABEL + cj(o), deterministic=True))}


#: origins refused without ``dev`` (name, origin); every one is a ``bad_origin``
PW_REFUSED: list[tuple[str, str]] = [
    ("loopback_http", "http://127.0.0.1:8103"),
    ("loopback_https", "https://127.0.0.1:8443"),
    ("loopback_name_http", "http://localhost:8103"),
    ("loopback_v6", "https://[::1]:8443"),
    ("private_ip_10", "https://10.0.0.5"),
    ("private_ip_192_168", "https://192.168.1.20:8443"),
    ("private_ip_172_16", "https://172.16.0.1"),
    ("private_ip_172_31", "https://172.31.255.254"),
    ("cgnat_ip", "https://100.64.1.2"),
    ("link_local_ip", "https://169.254.10.1"),
    ("this_network_ip", "https://0.0.0.0"),
    ("documentation_ip", "https://203.0.113.9"),
    ("multicast_ip", "https://224.0.0.1"),
    ("ula_ip", "https://[fd12:3456::1]"),
    ("link_local_v6", "https://[fe80::1]"),
    ("mapped_v6", "https://[::ffff:8.8.8.8]"),
    ("zone_v6", "https://[fe80::1%25eth0]"),
    ("local_name", "https://hub.local"),
    ("internal_name", "https://pub.corp.internal"),
    ("localhost", "https://localhost"),
    ("localhost_port", "https://localhost:8443"),
    ("localhost_upper", "https://LOCALHOST"),
    ("localhost_trailing_dot", "https://localhost."),
    ("localhost_subdomain", "https://pub.localhost"),
    ("userinfo", "https://u:p@pub.example.com"),
    ("userinfo_user_only", "https://u@pub.example.com"),
    ("plain_http_public", "http://pub.example.com"),
    ("path", "https://pub.example.com/x"),
    ("path_slash", "https://pub.example.com/"),
    ("query", "https://pub.example.com?x=1"),
    ("fragment", "https://pub.example.com#x"),
    ("port_zero", "https://pub.example.com:0"),
    ("port_too_big", "https://pub.example.com:65536"),
    ("port_leading_zero", "https://pub.example.com:08443"),
    ("port_empty", "https://pub.example.com:"),
    ("port_not_a_number", "https://pub.example.com:80a"),
    ("upper_case_scheme", "HTTPS://pub.example.com"),
    ("no_scheme", "pub.example.com"),
    ("other_scheme", "ftp://pub.example.com"),
    ("wss_scheme", "wss://pub.example.com"),
    ("short_ip", "https://127.1"),
    ("decimal_ip", "https://2130706433"),
    ("leading_zero_ip", "https://010.0.0.1"),
    ("space", "https://pub.example.com "),
    ("percent", "https://pub%2eexample.com"),
    ("backslash", "https://pub.example.com\\x"),
    ("empty_host", "https://"),
    ("empty_label", "https://pub..example.com"),
    ("dash_edge_label", "https://-pub.example.com"),
    ("underscore", "https://pub_x.example.com"),
    ("unbracketed_v6", "https://::1"),
    ("idna_sharp_s", "https://stra\u00dfe.example"),
    ("sixtofour_ip", "https://[2002:c000:204::1]"),  # 6to4 2002::/16 embeds an IPv4 address
    ("sixtofour_ip_private_inside", "https://[2002:0a00:0001::1]"),
    ("teredo_ip", "https://[2001:0:4136:e378:8000:63bf:3fff:fdd2]"),  # Teredo 2001::/32 embeds an IPv4 address
    ("teredo_ip_port", "https://[2001::1]:8443"),
    ("single_label", "https://publish"),  # a name with no dot, without dev
    ("single_label_port", "https://publish:8443"),
    ("single_label_trailing_dot", "https://publish."),
    ("home_arpa_name", "https://hub.home.arpa"),
    ("home_arpa_bare", "https://home.arpa"),
    ("lan_name", "https://pub.lan"),
    ("lan_bare", "https://lan"),
]
#: (name, origin given, canonical origin) accepted without ``dev``
PW_CANONICAL: list[tuple[str, str, str]] = [
    ("plain", "https://pub.example.com", "https://pub.example.com"),
    ("port", "https://share.example.com:8443", "https://share.example.com:8443"),
    ("idn_a_label", "https://B\u00dcCHER.example", "https://xn--bcher-kva.example"),
    ("idn_lower_case", "https://b\u00fccher.example:8443", "https://xn--bcher-kva.example:8443"),
    ("default_port_https", "https://pub.example.com:443", "https://pub.example.com"),
    ("trailing_dot", "https://pub.example.com.", "https://pub.example.com"),
    ("trailing_dot_and_default_port", "https://Pub.Example.COM.:443", "https://pub.example.com"),
    ("upper_case_host", "https://PUB.Example.com:8443", "https://pub.example.com:8443"),
    ("http_default_port_is_not_https", "https://pub.example.com:80", "https://pub.example.com:80"),
    ("public_ip", "https://93.184.216.34:8443", "https://93.184.216.34:8443"),
    ("public_v6", "https://[2606:4700:4700:0:0:0:0:1111]", "https://[2606:4700:4700::1111]"),
    ("public_v6_port", "https://[2606:4700:4700::1111]:8443", "https://[2606:4700:4700::1111]:8443"),
    ("local_in_the_middle", "https://local.example.com", "https://local.example.com"),
    ("localhost_prefix_only", "https://localhost.example.com", "https://localhost.example.com"),
    ("172_32_is_public", "https://172.32.0.1", "https://172.32.0.1"),
    ("100_128_is_public", "https://100.128.0.1", "https://100.128.0.1"),
    ("v6_2001_outside_teredo", "https://[2001:4860:4860::8888]", "https://[2001:4860:4860::8888]"),
    ("v6_2003_is_public", "https://[2003::1]", "https://[2003::1]"),
    ("lan_in_the_middle", "https://lan.example.com", "https://lan.example.com"),
    ("home_arpa_in_the_middle", "https://home.arpa.example.com", "https://home.arpa.example.com"),
]
#: accepted only with ``dev`` (name, origin given, canonical origin)
PW_DEV_ONLY: list[tuple[str, str, str]] = [
    ("loopback_http", "http://127.0.0.1:8103", "http://127.0.0.1:8103"),
    ("loopback_http_default_port", "http://127.0.0.1:80", "http://127.0.0.1"),
    ("loopback_https", "https://127.0.0.1:8443", "https://127.0.0.1:8443"),
    ("loopback_127_1_2_3", "http://127.1.2.3:8103", "http://127.1.2.3:8103"),
    ("localhost_http", "http://localhost:8102", "http://localhost:8102"),
    ("localhost_upper_trailing_dot", "http://LocalHost.:8102", "http://localhost:8102"),
    ("loopback_v6_http", "http://[::1]:8102", "http://[::1]:8102"),
    ("loopback_v6_long", "http://[0:0:0:0:0:0:0:1]:8102", "http://[::1]:8102"),
    ("single_label_https", "https://publish", "https://publish"),  # the lab's docker names; a real hub needs a dot
]
#: refused even with ``dev``: dev adds loopback only
PW_DEV_REFUSED: list[tuple[str, str]] = [
    ("private_ip", "https://10.0.0.5"),
    ("private_ip_http", "http://192.168.1.20:8103"),
    ("plain_http_public", "http://pub.example.com"),
    ("local_name", "http://hub.local"),
    ("internal_name", "https://pub.corp.internal"),
    ("localhost_with_path", "http://localhost:8102/x"),
    ("loopback_with_userinfo", "http://u@127.0.0.1:8102"),
    ("link_local_http", "http://169.254.1.1"),
    ("sixtofour_ip", "https://[2002:c000:204::1]"),
    ("teredo_ip", "https://[2001:0:4136:e378:8000:63bf:3fff:fdd2]"),
    ("home_arpa_name", "https://hub.home.arpa"),
    ("lan_name", "https://pub.lan"),
]


def publish_where_cases() -> list[dict[str, Any]]:
    cs: list[dict[str, Any]] = []
    op_pub = pub(key("op"))

    def pc(
        name: str,
        signed: Any,
        expect: dict[str, Any],
        *,
        dev: bool = False,
        hub: str = PW_HUB,
        held: Any = None,
        note: str = "",
    ) -> None:
        args: dict[str, Any] = {"signed": signed, "op_pub": op_pub, "hub_id": hub, "dev": dev}
        if held is not None:
            args["held"] = held
        cs.append(case("pw_" + name, "publish_where", args, expect, note))

    def co(name: str, origin: Any, dev: bool, expect: dict[str, Any]) -> None:
        cs.append(case("origin_" + name, "canonical_origin", {"origin": origin, "dev": dev}, expect))

    good = pw_o()
    pc("good", pw_signed(good), {"ok": good}, note="the canonical object is the result")
    good_dev = pw_o(dev=True, server="http://127.0.0.1:8103", share="http://127.0.0.1:8102")
    pc("good_dev", pw_signed(good_dev), {"ok": good_dev}, dev=True, note="a dev pin takes a dev object; loopback http is the lab")
    pc("good_seq_max", pw_signed(pw_o(seq=2**53 - 1)), {"ok": pw_o(seq=2**53 - 1)})
    pc("good_seq_1_issued_0", pw_signed(pw_o(seq=1, issued_ms=0)), {"ok": pw_o(seq=1, issued_ms=0)})
    pc("good_server_is_share", pw_signed(pw_o(share=PW_SERVER)), {"ok": pw_o(share=PW_SERVER)}, note="one origin may serve both")
    pc("wrong_hub_id", pw_signed(pw_o(hub_id=PW_OTHER_HUB)), err("other_hub"), note="signed by the right key, names another hub")
    pc("pinned_hub_is_another", pw_signed(good), err("other_hub"), hub=PW_OTHER_HUB)
    good_sig = pw_signed(good)["sig"]

    def flip_sig(signed: dict[str, Any]) -> dict[str, Any]:
        raw = __import__("base64").urlsafe_b64decode(signed["sig"] + "==")
        return {"o": signed["o"], "sig": b64u(flip(raw, 10))}

    pc("bad_signature", flip_sig(pw_signed(good)), err("bad_signature"))
    pc("signed_over_other_content", {"o": pw_o(seq=6), "sig": good_sig}, err("bad_signature"), note="the signature of seq 5 on an object that says seq 6")
    pc("signed_under_another_op_key", pw_signed(good, "op_other"), err("bad_signature"))
    pc("signed_under_the_root_key", pw_signed(good, "root"), err("bad_signature"), note="only HK_op signs it")
    pc("signed_under_the_wrong_label", {"o": good, "sig": b64u(S.sign(key("op"), b"orch/v2/hub-allow|" + cj(good), deterministic=True))}, err("bad_signature"))
    pc("dev_true_at_a_real_pin", pw_signed(pw_o(dev=True)), err("dev_refused"))
    pc("dev_false_at_a_dev_pin", pw_signed(pw_o(dev=False)), err("dev_mismatch"), dev=True)
    pc("dev_true_at_a_real_pin_loopback", pw_signed(good_dev), err("dev_refused"), note="loopback http needs the pin to be dev too")
    pc("loopback_http_dev_false_refused", pw_signed(pw_o(server="http://127.0.0.1:8103")), err("bad_origin"))
    pc("loopback_http_dev_true_accepted", pw_signed(pw_o(dev=True, server="http://127.0.0.1:8103")), {"ok": pw_o(dev=True, server="http://127.0.0.1:8103")}, dev=True)
    # the origin rule, on each field
    for nm, origin in PW_REFUSED:
        pc("server_" + nm + "_refused", pw_signed(pw_o(server=origin)), err("bad_origin"))
    share_checked = ("loopback_http", "private_ip_10", "cgnat_ip", "ula_ip", "local_name", "localhost", "userinfo", "path", "plain_http_public")
    for nm, origin in [r for r in PW_REFUSED if r[0] in share_checked]:
        pc("share_" + nm + "_refused", pw_signed(pw_o(share=origin)), err("bad_origin"))
    for nm, origin in PW_DEV_REFUSED:
        pc("dev_server_" + nm + "_refused", pw_signed(pw_o(dev=True, server=origin)), err("bad_origin"), dev=True)
    for nm, origin, canon in PW_CANONICAL:
        pc("server_" + nm, pw_signed(pw_o(server=origin)), {"ok": pw_o(server=canon)})
    for nm, origin, canon in PW_CANONICAL[:6]:
        pc("share_" + nm, pw_signed(pw_o(share=origin)), {"ok": pw_o(share=canon)})
    for nm, origin, canon in PW_DEV_ONLY:
        pc("dev_server_" + nm, pw_signed(pw_o(dev=True, server=origin)), {"ok": pw_o(dev=True, server=canon)}, dev=True)
        pc("dev_only_server_" + nm + "_refused_at_a_real_pin", pw_signed(pw_o(server=origin)), err("bad_origin"))
    pc("idn_canonicalised_to_the_a_label", pw_signed(pw_o(server="https://b\u00fccher.example")), {"ok": pw_o(server="https://xn--bcher-kva.example")}, note="the signature covers the host as the hub sent it; the result carries the A-label")
    pc("default_port_and_trailing_dot_canonicalised", pw_signed(pw_o(server="https://pub.example.com.:443", share="https://share.example.com.:8443")), {"ok": pw_o()})
    pc("server_bad_share_good_order", pw_signed(pw_o(server="https://10.0.0.5", share="https://localhost")), err("bad_origin"))
    # shape and header
    pc("not_an_object", "x", err("malformed"))
    pc("envelope_extra_key", {**pw_signed(good), "op_cert": {}}, err("malformed"))
    pc("field_missing", pw_signed({k: v for k, v in good.items() if k != "issued_ms"}), err("malformed"))
    pc("field_extra", pw_signed({**good, "path": "/x"}), err("malformed"))
    pc("seq_zero", pw_signed(pw_o(seq=0)), err("malformed"))
    pc("seq_bool", pw_signed(pw_o(seq=True)), err("malformed"))
    pc("seq_negative", pw_signed(pw_o(seq=-1)), err("malformed"))
    pc("seq_over_2_53", {"o": pw_o(seq=2**53), "sig": good_sig}, err("malformed"), note="not even canonical JSON (a uint above 2^53 - 1)")
    pc("issued_ms_negative", pw_signed(pw_o(issued_ms=-1)), err("malformed"))
    pc("hub_id_not_hex16", pw_signed(pw_o(hub_id=PW_HUB.upper())), err("malformed"))
    pc("origin_not_a_string", pw_signed(pw_o(server=None)), err("malformed"))
    pc("dev_not_a_bool", pw_signed(pw_o(dev=1)), err("malformed"))
    pc("wrong_kind", pw_signed(pw_o(kind="hub.allow")), err("malformed"))
    pc("wrong_suite", pw_signed(pw_o(suite=1)), err("wrong_suite"))
    pc("wrong_v", pw_signed(pw_o(v=3)), err("malformed"))
    pc("wrong_hv", pw_signed(pw_o(hv=1)), err("malformed"))
    pc("sig_wrong_length", {"o": good, "sig": b64u(bytes(63))}, err("malformed"))
    # ordering against the held object
    held5 = pw_signed(pw_o(seq=5))
    pc("lower_seq_refused", pw_signed(pw_o(seq=4)), err("stale_seq"), held=held5)
    pc("lowest_seq_after_5_refused", pw_signed(pw_o(seq=1)), err("stale_seq"), held=held5)
    pc("higher_seq_accepted", pw_signed(pw_o(seq=6, share="https://share2.example.com")), {"ok": pw_o(seq=6, share="https://share2.example.com")}, held=held5)
    pc("higher_seq_same_content_accepted", pw_signed(pw_o(seq=6)), {"ok": pw_o(seq=6)}, held=held5)
    pc("equal_seq_byte_identical_accepted", held5, {"ok": pw_o(seq=5)}, held=held5, note="the very same envelope again (a re-read, a re-send)")
    pc("equal_seq_other_content_refused", pw_signed(pw_o(seq=5, share="https://share2.example.com")), err("seq_conflict"), held=held5)
    pc("equal_seq_other_issued_ms_refused", pw_signed(pw_o(seq=5, issued_ms=NOW_MS + 1)), err("seq_conflict"), held=held5)
    pc("equal_seq_corrupt_signature_refused", flip_sig(held5), err("bad_signature"), held=held5, note="a changed signature never gets as far as the order rule")
    pc("held_none_is_first", pw_signed(pw_o(seq=1)), {"ok": pw_o(seq=1)})
    pc("order_after_origin_rule", pw_signed(pw_o(seq=4, server="https://10.0.0.5")), err("bad_origin"), held=held5, note="the origin rule is checked before the order")
    pc("order_after_hub_id", pw_signed(pw_o(seq=4, hub_id=PW_OTHER_HUB)), err("other_hub"), held=held5)
    # the same origin rule without a signature (what the hub applies before it signs)
    for nm, origin in PW_REFUSED:
        co(nm + "_refused", origin, False, err("bad_origin"))
    for nm, origin in PW_DEV_REFUSED:
        co("dev_" + nm + "_refused", origin, True, err("bad_origin"))
    for nm, origin, canon in PW_CANONICAL:
        co(nm, origin, False, {"ok": canon})
        co(nm + "_idempotent", canon, False, {"ok": canon})
    for nm, origin, canon in PW_DEV_ONLY:
        co("dev_" + nm, origin, True, {"ok": canon})
        co("dev_" + nm + "_without_dev_refused", origin, False, err("bad_origin"))
    for nm, origin in (("number", 5), ("null", None), ("list", ["https://pub.example.com"]), ("empty", ""), ("too_long", "https://" + "a" * 300 + ".example")):
        co(nm, origin, False, err("bad_origin"))
    co("dev_not_a_bool", "https://pub.example.com", 1, err("bad_origin"))
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
        "sign_publish_where": PW_LABEL.decode(),
    }
    return {
        "contract": "orch.hub-backup/0",
        "note": "FAKE keys and words, test-only. Cases are replayed by the hub (all) and orch-core (hub_where, publish_where, canonical_origin).",
        "labels": labels,
        "constants": {
            "max_total": MAX_TOTAL,
            "seq_jump": 1000,
            "max_addresses": 4,
            "max_name": 60,
            "magic": B.MAGIC.decode(),
            "publish_where_seq_max": 2**53 - 1,
        },
        "words": {"a": WORDS_A, "b": WORDS_B, "attacker": WORDS_ATTACKER},
        "keys": {
            "root": pub(key("root")),
            "attacker_root": pub(key("attacker_root")),
            "op": pub(key("op")),
            "op_other": pub(key("op_other")),
        },
        "example": {
            "header": good["o"],
            "envelope": good["env"],
            "sealed_hex": good["sealed"].hex(),
            "tar_hex": tar_files(FILES).hex(),
        },
        "cases": kat + open_cases() + tar_cases() + list_cases() + floor_cases() + where_cases() + publish_where_cases(),
    }


if __name__ == "__main__":
    out = Path(__file__).with_name("vectors_hub_backup.json")
    out.write_text(json.dumps(build(), indent=1, sort_keys=True) + "\n")
    print(f"wrote {out} ({len(build()['cases'])} cases)")
