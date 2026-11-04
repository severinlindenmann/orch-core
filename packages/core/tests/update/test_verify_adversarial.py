"""[rev-release] Opus adversarial tests for the U2 verifier. TEST root only, via the test hook."""

from __future__ import annotations

import base64
import copy
import importlib.util
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orch.update import keys as keys_mod
from orch.update.verify import Reason, UpdateRefused, verify_envelope

DIR = Path(__file__).resolve().parent.parent / "vectors" / "release"
spec = importlib.util.spec_from_file_location("gen_release_vectors_adv", DIR / "gen_release_vectors.py")
GEN = importlib.util.module_from_spec(spec)
spec.loader.exec_module(GEN)
NOW = datetime(2026, 10, 22, 8, 0, 0, tzinfo=UTC)
FILES = {GEN.file_names("2.0.0.dev42")[r]: b for r, b in GEN.FILE_BYTES.items()}


@pytest.fixture(autouse=True)
def _test_root():
    with keys_mod.enable_test_root_for_tests():
        yield


def env_bytes(payload: bytes, key_id: str = "rk-3", **kw) -> bytes:
    return GEN.text(GEN.envelope(payload, key_id, **kw)).encode()


def reason(env: bytes, *, purpose="core", channel="develop", seq_seen=None, rev_at=None, files=None, **kw):
    try:
        verify_envelope(
            env,
            purpose=purpose,
            channel=channel,
            now=kw.pop("now", NOW),
            seq_seen=seq_seen,
            revocations_issued_at=rev_at,
            files=files,
            **kw,
        )
    except UpdateRefused as e:
        return e.reason
    return None


def core(**over) -> bytes:
    return GEN.jcs(GEN.manifest("develop", 42, **over))


def test_baseline_ok():
    assert reason(env_bytes(core()), files=FILES) is None


# --- purpose / domain separation ------------------------------------------------------------------------------


def test_catalog_key_cannot_sign_a_core_manifest_even_with_the_core_domain():
    assert reason(env_bytes(core(), "ck-1", domain=GEN.DOMAIN["core"])) is Reason.WRONG_PURPOSE


def test_core_signature_is_not_a_catalog_signature():
    cat = GEN.jcs(GEN.catalog("develop", 7))
    # core key, catalog bytes, signed under the CORE domain, presented as a catalog: wrong purpose first
    assert reason(env_bytes(cat, "rk-3"), purpose="addon-catalog") is Reason.WRONG_PURPOSE
    # catalog key signs core bytes under the catalog domain, presented as core: wrong purpose
    assert reason(env_bytes(core(), "ck-1", domain=GEN.DOMAIN["addon-catalog"])) is Reason.WRONG_PURPOSE


def test_test_key_cannot_sign_main_or_develop():
    assert reason(env_bytes(core(), "tk-1")) is Reason.CHANNEL_NOT_ALLOWED
    m = GEN.jcs(GEN.manifest("main", 42))
    assert reason(env_bytes(m, "tk-1"), channel="main") is Reason.CHANNEL_NOT_ALLOWED


def test_requesting_the_literal_wildcard_channel_never_verifies():
    m = GEN.jcs(GEN.manifest("test-e2e", 42))
    assert reason(env_bytes(m, "tk-1"), channel="test-e2e") is None
    assert reason(env_bytes(m, "tk-1"), channel="test-*") is Reason.WRONG_CHANNEL


# --- revocations monotonicity and stored state ----------------------------------------------------------------


def test_revocations_equal_ok_older_refused_newer_ok():
    env = env_bytes(core())
    at = GEN.REVOCATIONS["issued_at"]
    assert reason(env, rev_at=at) is None
    assert reason(env, rev_at="2026-10-20T00:00:01Z") is Reason.REVOCATIONS_ROLLBACK
    assert reason(env, rev_at="2026-10-19T23:59:59Z") is None


def test_corrupt_stored_revocations_time_fails_closed():
    assert reason(env_bytes(core()), rev_at="garbage") is Reason.MALFORMED


def test_old_revocation_list_cannot_unrevoke_after_a_newer_one_was_stored():
    old = dict(GEN.REVOCATIONS, min_serial=1, revoked=[], issued_at="2026-10-01T00:00:00Z")
    env = env_bytes(core(), "rk-4", revs=GEN.revocations(doc=old))  # rk-4 revoked in the newer list
    assert reason(env) is None  # fresh install with no stored state: accepted (TOFU on the list)
    assert reason(env, rev_at=GEN.REVOCATIONS["issued_at"]) is Reason.REVOCATIONS_ROLLBACK


def test_revocations_signed_by_a_cert_key_are_refused():
    revs = GEN.revocations(signer=GEN.RK3)  # a release key, not the root, signs a list that un-revokes everything
    assert reason(env_bytes(core(), revs=revs)) is Reason.BAD_REVOCATIONS_SIG


# --- cert validity at verification time -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "want"),
    [
        (datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC), Reason.CERT_NOT_YET_VALID),
        (datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC), None),
        (datetime(2027, 3, 29, 23, 59, 59, tzinfo=UTC), None),
        (datetime(2027, 3, 30, 0, 0, 0, tzinfo=UTC), Reason.CERT_EXPIRED),
    ],
)
def test_cert_window_boundaries(now, want):
    m = core(issued_at="2026-09-01T00:00:00Z", expires_at="2027-12-01T00:00:00Z")
    assert reason(env_bytes(m), now=now) is want


def test_expired_flag_does_not_bypass_cert_window_or_rollback_or_revocation_rollback():
    m = core(expires_at="2026-10-22T07:00:00Z")
    flag = {"allow_expired_manifest": True}
    assert reason(env_bytes(m), **flag) is None
    assert reason(env_bytes(m), now=datetime(2027, 4, 1, tzinfo=UTC), **flag) is Reason.CERT_EXPIRED
    assert reason(env_bytes(m), seq_seen=43, **flag) is Reason.ROLLBACK
    assert reason(env_bytes(m), rev_at="2026-10-21T00:00:00Z", **flag) is Reason.REVOCATIONS_ROLLBACK


def test_equal_seq_with_different_content_is_accepted_by_contract():
    """Documents the contract choice: equal seq is fine, so a key holder can swap content at the same seq."""
    a = env_bytes(core(commit="1" * 40))
    b = env_bytes(core(commit="2" * 40))
    assert reason(a, seq_seen=42) is None and reason(b, seq_seen=42) is None


# --- files ----------------------------------------------------------------------------------------------------


def test_missing_extra_and_swapped_files():
    env = env_bytes(core())
    assert reason(env, files={**FILES, "evil.whl": b"x"}) is None  # extra files are ignored by design
    names = list(FILES)
    assert reason(env, files={k: v for k, v in FILES.items() if k != names[0]}) is Reason.SIZE_MISMATCH
    swapped = dict(FILES)
    swapped[names[0]], swapped[names[1]] = FILES[names[1]], FILES[names[0]]
    assert reason(env, files=swapped) in (Reason.SIZE_MISMATCH, Reason.HASH_MISMATCH)
    same_len = {**FILES, names[0]: bytes(len(FILES[names[0]]))}
    assert reason(env, files=same_len) is Reason.HASH_MISMATCH


@pytest.mark.parametrize("name", ["../x.whl", "/abs.whl", ".hidden", "a/b", "a\\b", "x" * 129, "é.whl", "a b"])
def test_unsafe_file_names_are_malformed(name):
    m = GEN.manifest("develop", 42)
    m["files"][0]["name"] = name
    assert reason(env_bytes(GEN.jcs(m))) is Reason.MALFORMED


def test_case_colliding_file_names_are_refused():
    """Names differing only by case collide on case-insensitive file systems (APFS)."""
    m = GEN.manifest("develop", 42)
    m["files"][1]["name"] = m["files"][0]["name"].upper()
    assert reason(env_bytes(GEN.jcs(m))) is Reason.MALFORMED


# --- parser robustness ----------------------------------------------------------------------------------------


def test_deeply_nested_signed_doc_is_a_refusal_not_a_crash():
    """A signed payload nested deeper than the recursion limit must give UpdateRefused, never RecursionError."""
    depth = 50_000
    deep = (
        b'{"addons":{"x":'
        + b"[" * depth
        + b"]" * depth
        + b'},"channel":"develop","expires_at":"2026-11-11T07:40:00Z","format":"orch.addon-catalog/1",'
        + b'"issued_at":"2026-10-21T07:40:00Z","seq":7}'
    )
    env = env_bytes(deep, "ck-1", domain=GEN.DOMAIN["addon-catalog"])
    try:
        r = reason(env, purpose="addon-catalog")
    except RecursionError:
        pytest.fail("RecursionError escaped the verifier")
    assert r is Reason.MALFORMED


@pytest.mark.parametrize("seq", ["٤٢", "4\u00002"])
def test_non_ascii_or_nul_digits_never_parse(seq):
    raw = GEN.text(GEN.envelope(core())).replace('"format"', '"format"', 1)
    payload = core().replace(b'"seq":42', ('"seq":' + seq).encode())
    env = json.loads(raw)
    env["payload"] = base64.b64encode(payload).decode()
    assert reason(json.dumps(env).encode()) in (Reason.MALFORMED, Reason.BAD_SIG)


@pytest.mark.parametrize("prefix", [b"\xef\xbb\xbf", b"\x00", b"[", b"//"])
def test_outer_prefix_garbage_is_malformed(prefix):
    assert reason(prefix + env_bytes(core())) is Reason.MALFORMED


def test_outer_duplicate_key_is_malformed():
    env = env_bytes(core())
    dup = env[:-1] + b',"format":"orch.release-envelope/1"}'
    assert reason(dup) is Reason.MALFORMED


# --- roots ----------------------------------------------------------------------------------------------------


def test_without_the_hook_nothing_verifies():
    env = env_bytes(core())
    depth = keys_mod._test_hook_depth
    keys_mod._test_hook_depth = 0
    try:
        assert "test-root-1" not in keys_mod.PRODUCTION_ROOTS  # the TEST root is never a production root
        assert reason(env) is Reason.UNKNOWN_ROOT
    finally:
        keys_mod._test_hook_depth = depth


def test_no_source_file_enables_the_test_root():
    src = Path(__file__).resolve().parents[2] / "src"
    hits = [p for p in src.rglob("*.py") if re.search(r"enable_test_root_for_tests\s*\(", p.read_text())]
    hits = [p for p in hits if p.name != "keys.py"]
    assert hits == []


def test_test_root_id_must_not_shadow_a_production_root_id():
    """TEST_ROOTS must not override PRODUCTION_ROOTS on an id clash inside the hook (the test id is test-root-1)."""
    clash = set(keys_mod.TEST_ROOTS) & set(keys_mod.PRODUCTION_ROOTS)
    assert not clash


def test_a_rogue_root_cert_is_unknown_root():
    kc = GEN.key_cert("rk-3", root=GEN.ROGUE_ROOT, root_key_id="root-9")
    assert reason(env_bytes(core(), cert=kc)) is Reason.UNKNOWN_ROOT
    kc = GEN.key_cert("rk-3", root=GEN.ROGUE_ROOT)  # claims test-root-1, signed by root-9
    assert reason(env_bytes(core(), cert=kc)) is Reason.BAD_CERT_SIG


def test_mutating_returned_document_cannot_affect_a_second_verify():
    env = env_bytes(core())
    v = verify_envelope(env, purpose="core", channel="develop", now=NOW, seq_seen=None, revocations_issued_at=None)
    doc = copy.deepcopy(v.document)
    v.document["seq"] = 1
    v2 = verify_envelope(env, purpose="core", channel="develop", now=NOW, seq_seen=None, revocations_issued_at=None)
    assert v2.document == doc


def test_the_hook_refuses_a_test_root_id_that_collides_with_production(monkeypatch):
    monkeypatch.setattr(keys_mod, "PRODUCTION_ROOTS", {"test-root-1": b"\x00" * 32})
    with pytest.raises(RuntimeError), keys_mod.enable_test_root_for_tests():
        pass


def test_production_refuses_test_channels_without_the_hook():
    m = GEN.jcs(GEN.manifest("test-e2e", 42))
    env = env_bytes(m, "tk-1")
    assert reason(env, channel="test-e2e") is None
    depth = keys_mod._test_hook_depth
    keys_mod._test_hook_depth = 0
    try:
        roots = dict(keys_mod.TEST_ROOTS)  # even with the TEST root passed explicitly
        assert reason(env, channel="test-e2e", roots=roots) is Reason.CHANNEL_NOT_ALLOWED
    finally:
        keys_mod._test_hook_depth = depth


# --- the manifest file list: duplicate roles, unknown roles, missing core / constraints ---------------------------


def _with_files(mutate):
    m = GEN.manifest("develop", 42)
    mutate(m["files"])
    return reason(env_bytes(GEN.jcs(m)))


def test_file_list_duplicate_role_is_refused():
    assert _with_files(lambda fs: fs[3].update(role="hub")) is Reason.MALFORMED


def test_file_list_unknown_role_is_refused():
    assert _with_files(lambda fs: fs[3].update(role="installer")) is Reason.MALFORMED


def test_file_list_without_core_is_refused():
    assert _with_files(lambda fs: fs.pop(0)) is Reason.MALFORMED


def test_file_list_without_constraints_is_refused():
    assert _with_files(lambda fs: fs.pop()) is Reason.MALFORMED


def test_file_list_with_only_the_mandatory_roles_is_accepted():
    assert _with_files(lambda fs: fs.__setitem__(slice(1, 5), [])) is None


def test_seq_seen_refuses_a_lower_seq_and_it_is_a_required_argument():
    import inspect

    assert reason(env_bytes(core()), seq_seen=43) is Reason.ROLLBACK  # manifest seq is 42
    assert reason(env_bytes(core()), seq_seen=42) is None
    p = inspect.signature(verify_envelope).parameters
    assert (
        p["seq_seen"].default is inspect.Parameter.empty
        and p["revocations_issued_at"].default is inspect.Parameter.empty
    )
