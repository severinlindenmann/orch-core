"""M4 slice S0: the committed ``contracts/hub/vectors_hub_backup.json`` is exactly what the generator builds, and every case
(backup file, tar rules, file list, restore floor, ``hub_where``) gives its expected result under ``orch_hub.contract``.
An unknown ``check`` or a missing required case fails the run: nothing is skipped."""

from __future__ import annotations

import functools
import importlib.util
import json
from pathlib import Path

import pytest

from orch_hub.contract import backup as B
from orch_hub.contract import where as W
from orch_hub.contract.canon import b64u
from orch_hub.contract.hub import Refused
from orch_hub.contract.labels import ACCESS_CHANGE_LABEL, CUSTODIAL_PREFIX, HUB_LABELS, V2_REUSED

VEC = Path(__file__).resolve().parents[3] / "contracts" / "hub"
DOC = json.loads((VEC / "vectors_hub_backup.json").read_text())
CASES = DOC["cases"]

REQUIRED = {
    "backup_good_fresh_server", "backup_good_in_place", "backup_tampered_header_sig_fails",
    "backup_tampered_header_mac_fails", "backup_forged_other_root_copied_bk_pub_fresh",
    "backup_forged_other_root_copied_bk_pub_in_place", "backup_wrong_words", "backup_dev_refused_at_real_hub",
    "backup_dev_mismatch_at_dev_hub", "backup_other_hub_in_place", "backup_file_hash_mismatch",
    "backup_extra_file_not_in_header", "backup_symlink_entry", "tar_absolute_path", "tar_dotdot", "tar_symlink",
    "tar_hardlink", "tar_char_device", "tar_oversize_entry", "tar_extra_file", "files_excluded_setup_token",
    "files_excluded_route_queue_db", "files_excluded_event_deliver_db", "files_dir_of_another_with_sibling", "files_case_fold_duplicate", "files_missing_required_root_key", "floor_in_place_live_ahead",
    "floor_fresh_server_unix_seconds_win", "floor_double_restore_same_backup", "floor_boundary_exact_max",
    "floor_boundary_one_over_in_place", "where_ok", "where_five_entries", "where_bad_addr_no_port",
    "where_unknown_kind", "where_name_61_long", "where_name_control_and_bidi_stripped",
}  # fmt: skip
CHECKS = {"bk_key", "open_backup", "tar", "file_list", "restore_floor", "hub_where"}


def _gen():
    spec = importlib.util.spec_from_file_location("make_vectors_hub_backup", VEC / "make_vectors_hub_backup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _cached_scrypt(monkeypatch):
    """scrypt at the real parameters (N=2^17) costs ~0.3 s: cache it per words in this test process (same function)."""
    monkeypatch.setattr(B, "derive_keys", functools.cache(B.derive_keys))


def _result(fn):
    try:
        return {"ok": fn()}
    except Refused as e:
        got = {"error": e.code}
        if "reason" in e.extra:
            got["reason"] = e.extra["reason"]
        return got


def _sha(files: dict[str, bytes]) -> dict[str, str]:
    import hashlib

    return {p: hashlib.sha256(d).hexdigest() for p, d in files.items()}


def run(c: dict):
    a = c["args"]
    ck = c["check"]
    if ck == "bk_key":

        def go():
            k = B.derive_keys(a["words"])
            return {"okm_hex": _okm(a["words"]), "bk_pub": b64u(k.bk_pub), "k_auth_hex": k.k_auth.hex()}

        return _result(go)
    if ck == "open_backup":
        return _result(
            lambda: {
                "files": _sha(
                    B.open_backup(
                        bytes.fromhex(a["file_hex"]), words=a["words"], dev_mode=a["dev_mode"], live=a["live"]
                    ).files
                )
            }
        )
    if ck == "tar":
        return _result(lambda: {"files": _sha(B.check_tar(bytes.fromhex(a["tar_hex"]), a["files"]))})
    if ck == "file_list":
        return _result(lambda: bool(B.check_file_list(a["files"])))
    if ck == "restore_floor":
        return _result(lambda: B.restore_floor(a["live"], a["backup"], a["now_s"]))
    if ck == "hub_where":
        return _result(lambda: W.check_hub_where(a["answer"]))
    raise AssertionError(f"unknown check {ck}")


def _okm(words: str) -> str:
    import hashlib

    return hashlib.scrypt(
        B.normalise_words(words).encode(), salt=B.KDF_BK_SALT, n=2**17, r=8, p=1, dklen=40, maxmem=2**28
    ).hex()


def test_file_is_generated() -> None:
    assert DOC == json.loads(json.dumps(_gen().build()))


def test_required_cases_exist_and_names_are_unique() -> None:
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names))
    assert REQUIRED <= set(names), REQUIRED - set(names)
    assert {c["check"] for c in CASES} == CHECKS  # an unknown check, or a check nobody replays, fails here


def test_every_case_is_replayed() -> None:
    bad, n = [], 0
    for c in CASES:
        n += 1
        got = run(c)
        if got != c["expect"]:
            bad.append((c["name"], got, c["expect"]))
    assert n == len(CASES) >= 120
    assert not bad, bad[:3]


@pytest.mark.parametrize("c", [c for c in CASES if c["check"] == "open_backup"], ids=lambda c: c["name"])
def test_open_backup_error_is_the_documented_one(c) -> None:
    got = run(c)
    assert got == c["expect"], c["name"]


def test_constants_match_the_module() -> None:
    k = DOC["constants"]
    assert k["max_total"] == B.MAX_TOTAL and k["seq_jump"] == B.SEQ_JUMP
    assert k["max_addresses"] == W.MAX_ADDRESSES and k["max_name"] == W.MAX_NAME and k["magic"] == B.MAGIC.decode()
    lab = DOC["labels"]
    assert lab["sign_header"] == B.LABEL.decode() and lab["aad"] == B.AAD_LABEL.decode()
    assert lab["kdf_salt_hex"] == B.KDF_BK_SALT.hex() and lab["kdf_auth_info"] == B.KDF_AUTH_INFO.decode()


def test_labels_are_new_and_prefix_free_with_every_hub_label() -> None:
    mine = [B.LABEL.decode(), B.AAD_LABEL.decode()]
    others = [*HUB_LABELS.values(), *V2_REUSED.values(), CUSTODIAL_PREFIX, ACCESS_CHANGE_LABEL]
    for m in mine:
        assert m.startswith("orch/v2/") and m.endswith("|") and m not in others
        assert not any(o.startswith(m) or m.startswith(o) for o in others), m
    assert not (mine[0].startswith(mine[1]) or mine[1].startswith(mine[0]))


def test_a_tampered_example_never_opens() -> None:
    """Every single-byte change of the example file is refused (signature, mac, seal or container), never accepted."""
    ex = DOC["example"]
    good = B.pack_file(ex["envelope"], bytes.fromhex(ex["sealed_hex"]))
    words = DOC["words"]["a"]
    assert B.open_backup(good, words=words, dev_mode=True, live=None).header == ex["header"]
    for i in range(0, len(good), 97):
        bad = good[:i] + bytes([good[i] ^ 1]) + good[i + 1 :]
        with pytest.raises(Refused):
            B.open_backup(bad, words=words, dev_mode=True, live=None)


def test_seal_is_fresh_every_time() -> None:
    ex = DOC["example"]
    k = B.derive_keys(DOC["words"]["a"])
    t = bytes.fromhex(ex["tar_hex"])
    a, b = B.seal_payload(k.bk_pub, ex["header"], t), B.seal_payload(k.bk_pub, ex["header"], t)
    assert a != b and B.open_payload(k.bk, ex["header"], a) == t == B.open_payload(k.bk, ex["header"], b)
