"""The envelope verifier (install-update §4, U2): every vector in core.json and catalog.json reaches exactly the
expected reason and code; a flipped byte or an extra key never verifies. TEST root only, via the test hook."""

from __future__ import annotations

import base64
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from orch.ops.errors import ERRORS
from orch.update import keys as keys_mod
from orch.update.verify import Reason, UpdateRefused, verify_envelope

DIR = Path(__file__).resolve().parent.parent / "vectors" / "release"


def _gen():
    spec = importlib.util.spec_from_file_location("gen_release_vectors", DIR / "gen_release_vectors.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GEN = _gen()
KEYS = json.loads((DIR / "keys.json").read_text(encoding="utf-8"))
CORE = json.loads((DIR / "core.json").read_text(encoding="utf-8"))["cases"]
CATALOG = json.loads((DIR / "catalog.json").read_text(encoding="utf-8"))["cases"]
ALL = [("core", c) for c in CORE] + [("catalog", c) for c in CATALOG]
NOW = datetime(2026, 10, 22, 8, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _test_root():
    with keys_mod.enable_test_root_for_tests():
        yield


def run(case: dict[str, Any], envelope: bytes | None = None, **extra: Any) -> Any:
    st = case["state"]
    roots = {k: v for k, v in keys_mod.embedded_roots().items() if k in st["trusted_roots"]}
    files = None
    if "files" in case:
        files = {f["name"]: base64.b64decode(f["content_b64"]) for f in case["files"]}
    return verify_envelope(
        case["envelope"].encode("utf-8") if envelope is None else envelope,
        purpose=case["purpose"],
        channel=case["channel"],
        now=datetime.strptime(case["now"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC),
        seq_seen=st["seq_seen"],
        revocations_issued_at=st["revocations_issued_at"],
        files=files,
        roots=roots,
        **extra,
    )


def refusal(case: dict[str, Any], envelope: bytes | None = None, **extra: Any) -> UpdateRefused | None:
    try:
        run(case, envelope, **extra)
    except UpdateRefused as e:
        return e
    return None


@pytest.mark.parametrize("kind,case", ALL, ids=[f"{k}:{c['name']}" for k, c in ALL])
def test_vector(kind, case):
    expect = case["expect"]
    try:
        got = run(case)
    except UpdateRefused as e:
        assert (e.reason.value, e.code) == (expect["reason"], expect["code"])
        return
    assert expect["reason"] == "ok" and expect["code"] is None
    assert {
        "channel": got.channel,
        "seq": got.seq,
        "version": got.version,
        "min_serial": got.min_serial,
        "revocations_issued_at": got.revocations_issued_at,
    } == expect["accept"]


def test_every_reason_is_covered_by_a_vector_and_maps_to_a_catalog_code():
    seen = {c["expect"]["reason"] for _, c in ALL} - {"ok"}
    assert seen == {r.value for r in Reason}
    for r in Reason:
        assert r.code in ERRORS
        assert r.code == GEN.REASON_CODE[r.value]


# --- production never trusts the TEST root ---------------------------------------------------------------------


def test_test_root_is_only_trusted_inside_the_hook():
    assert "test-root-1" in keys_mod.embedded_roots()
    assert "test-root-1" not in keys_mod.PRODUCTION_ROOTS
    case = CORE[0]
    assert case["expect"]["reason"] == "ok"
    st = case["state"]
    args = {
        "purpose": case["purpose"],
        "channel": case["channel"],
        "now": NOW,
        "seq_seen": st["seq_seen"],
        "revocations_issued_at": st["revocations_issued_at"],
    }
    assert verify_envelope(case["envelope"].encode(), **args).seq == 42  # default roots, inside the hook
    # outside the hook (nested exits restore the state): the production set does not contain the TEST root
    keys_mod._test_hook_depth = 0
    try:
        assert "test-root-1" not in keys_mod.embedded_roots()
        with pytest.raises(UpdateRefused) as e:
            verify_envelope(case["envelope"].encode(), **args)
        assert (e.value.reason, e.value.code) == (Reason.UNKNOWN_ROOT, "update.unverified")
    finally:
        keys_mod._test_hook_depth = 1


def test_the_embedded_test_root_is_the_vector_root():
    assert base64.b64encode(keys_mod.TEST_ROOTS["test-root-1"]).decode() == KEYS["trusted_roots"]["test-root-1"]


# --- property tests ----------------------------------------------------------------------------------------------

OK_CORE = CORE[0]
OK_CATALOG = next(c for c in CATALOG if c["expect"]["reason"] == "ok")


@pytest.mark.parametrize("case", [OK_CORE, OK_CATALOG], ids=["core", "catalog"])
def test_a_flipped_byte_anywhere_is_never_ok(case):
    raw = case["envelope"].encode("utf-8")
    assert refusal(case) is None
    for i in range(len(raw)):
        flipped = raw[:i] + bytes([raw[i] ^ 0x01]) + raw[i + 1 :]
        assert refusal(case, flipped) is not None, i


@pytest.mark.parametrize("case", [OK_CORE, OK_CATALOG], ids=["core", "catalog"])
def test_truncation_and_trailing_bytes_are_never_ok(case):
    raw = case["envelope"].encode("utf-8")
    for bad in (raw[:-1], raw + b"x", b"", b"{}", b"[]", b"null"):
        e = refusal(case, bad)
        assert e is not None and e.reason is Reason.MALFORMED


def _extra_outer(env):
    env["extra"] = 1


def _extra_sig(env):
    env["sig"]["extra"] = 1


def _extra_cert_wrapper(env):
    env["key_cert"]["extra"] = 1


def _extra_rev_wrapper(env):
    env["revocations"]["extra"] = 1


def _missing_sig_alg(env):
    del env["sig"]["alg"]


@pytest.mark.parametrize("mut", [_extra_outer, _extra_sig, _extra_cert_wrapper, _extra_rev_wrapper, _missing_sig_alg])
def test_outer_key_set_must_be_exact(mut):
    env = json.loads(OK_CORE["envelope"])
    mut(env)
    e = refusal(OK_CORE, GEN.text(env).encode())
    assert e is not None and e.reason is Reason.MALFORMED


def _signed_core(*, cert=None, revs=None, manifest=None, payload=None):
    m = GEN.manifest("develop", 42, **(manifest or {})) if payload is None else None
    body = payload if payload is not None else GEN.jcs(m)
    kc = GEN.key_cert("rk-3", raw=GEN.jcs(cert)) if cert is not None else None
    rv = GEN.revocations(doc=revs) if revs is not None else None
    return GEN.text(GEN.envelope(body, "rk-3", cert=kc, revs=rv)).encode()


def test_helper_builds_an_accepted_envelope():
    assert refusal(OK_CORE, _signed_core()) is None


def _manifest_extra():
    m = GEN.manifest("develop", 42)
    m["extra"] = 1
    return {"payload": GEN.jcs(m)}


def _file_extra():
    m = GEN.manifest("develop", 42)
    m["files"][0]["extra"] = 1
    return {"payload": GEN.jcs(m)}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cert": {**GEN.CERTS["rk-3"], "extra": 1}},
        {"revs": {**GEN.REVOCATIONS, "extra": 1}},
        _manifest_extra(),
        _file_extra(),
        {"manifest": {"extra": 1}},
    ],
    ids=["cert", "revocations", "manifest", "manifest-file", "manifest-override"],
)
def test_signed_documents_with_extra_keys_are_refused(kwargs):
    e = refusal(OK_CORE, _signed_core(**kwargs))
    assert e is not None and e.reason is Reason.MALFORMED


def test_missing_keys_are_refused():
    cert = dict(GEN.CERTS["rk-3"])
    del cert["serial"]
    assert refusal(OK_CORE, _signed_core(cert=cert)).reason is Reason.MALFORMED
    m = GEN.manifest("develop", 42)
    del m["min_from"]
    assert refusal(OK_CORE, _signed_core(payload=GEN.jcs(m))).reason is Reason.MALFORMED


def test_catalog_extra_top_level_key_is_refused_but_addon_entries_are_free():
    def env(cat):
        return GEN.text(GEN.envelope(GEN.jcs(cat), "ck-1", domain=GEN.DOMAIN["addon-catalog"])).encode()

    ok = GEN.catalog("develop", 311)
    ok["addons"]["wiki"]["anything"] = {"x": [1, True, "y"]}
    assert refusal(OK_CATALOG, env(ok)) is None
    assert refusal(OK_CATALOG, env({**GEN.catalog("develop", 311), "extra": 1})).reason is Reason.MALFORMED


def test_floats_and_non_canonical_signed_bytes_are_refused():
    m = GEN.jcs(GEN.manifest("develop", 42))
    for body in (
        m.replace(b'"seq":42', b'"seq":42.0'),  # float
        m.replace(b'"seq":42', b'"seq": 42'),  # whitespace: not canonical, and never re-encoded
        b'{"seq":1,"seq":2}',  # duplicate key
        m.replace(b'"seq":42', b'"seq":NaN'),
        m.replace(b'"seq":42', b'"seq":-0'),
        m.replace(b'"seq":42', b'"seq":9007199254740992'),  # 2^53
    ):
        e = refusal(OK_CORE, _signed_core(payload=body))
        assert e is not None and e.reason is Reason.MALFORMED, body


def test_unpadded_or_noncanonical_base64_is_refused():
    env = json.loads(OK_CORE["envelope"])
    payload = env["payload"]
    for bad in (payload.rstrip("="), payload + "\n", payload[:-1] + "=", " " + payload, payload.replace("A", "А", 1)):
        env2 = {**env, "payload": bad}
        if bad == payload:
            continue
        e = refusal(OK_CORE, GEN.text(env2).encode())
        assert e is not None and e.reason is Reason.MALFORMED, bad


def test_bad_inputs_from_the_caller_raise_instead_of_passing():
    args = {"channel": "develop", "now": NOW, "seq_seen": None, "revocations_issued_at": None}
    raw = OK_CORE["envelope"].encode()
    with pytest.raises(ValueError):
        verify_envelope(raw, purpose="nope", **args)
    with pytest.raises(ValueError):
        verify_envelope(raw, purpose="addon-catalog", files={}, **args)
    with pytest.raises(ValueError):
        verify_envelope(raw, purpose="core", **{**args, "now": datetime(2026, 10, 22, 8)})


# --- allow_expired_manifest: skips the manifest expiry check and nothing else ----------------------------------

EXPIRED = {"expires_at": "2026-10-22T07:59:59Z"}  # now is 08:00:00, issued_at stays before it


def _expired_env(key_id="rk-3", *, channel="develop", **kw):
    m = GEN.manifest(channel, 42, **EXPIRED)
    return GEN.text(GEN.envelope(GEN.jcs(m), key_id, **kw)).encode()


def test_allow_expired_manifest_defaults_to_false():
    import inspect

    assert inspect.signature(verify_envelope).parameters["allow_expired_manifest"].default is False


def test_expired_vectors_are_ok_only_with_the_flag():
    cases = [c for _, c in ALL if c["expect"]["reason"] == "expired"]
    assert cases
    for c in cases:
        assert refusal(c).reason is Reason.EXPIRED
        got = run(c, allow_expired_manifest=True)
        assert got.seq == json.loads(base64.b64decode(json.loads(c["envelope"])["payload"]))["seq"]


def test_expired_only_envelope():
    env = _expired_env()
    assert refusal(OK_CORE, env).reason is Reason.EXPIRED
    assert refusal(OK_CORE, env, allow_expired_manifest=True) is None


def test_flag_never_hides_another_failure():
    flag = {"allow_expired_manifest": True}
    # expired and bad signature
    env = json.loads(_expired_env())
    env["payload"] = base64.b64encode(GEN.jcs(GEN.manifest("develop", 42, **EXPIRED)) + b" ").decode()
    assert refusal(OK_CORE, GEN.text(env).encode(), **flag).reason is Reason.BAD_SIG
    # expired and revoked certificate
    assert refusal(OK_CORE, _expired_env("rk-4"), **flag).reason is Reason.KEY_REVOKED
    # expired and expired certificate
    assert refusal(OK_CORE, _expired_env("rk-5"), **flag).reason is Reason.CERT_EXPIRED
    # expired and wrong channel
    wrong = {**OK_CORE, "channel": "main"}
    assert refusal(wrong, _expired_env(), **flag).reason is Reason.WRONG_CHANNEL
    # expired and rollback
    older = {**OK_CORE, "state": {**OK_CORE["state"], "seq_seen": 50}}
    assert refusal(older, _expired_env(), **flag).reason is Reason.ROLLBACK
    # expired and wrong purpose
    assert (
        refusal(
            {k: v for k, v in OK_CORE.items() if k != "files"} | {"purpose": "addon-catalog"}, _expired_env(), **flag
        ).reason
        is Reason.WRONG_PURPOSE
    )
    # expired and a bad file hash
    bad = {**OK_CORE, "files": [{**f, "content_b64": base64.b64encode(b"x").decode()} for f in OK_CORE["files"]]}
    assert refusal(bad, _expired_env(), **flag).reason is Reason.SIZE_MISMATCH


def test_no_production_code_sets_the_flag():
    import re

    root = Path(__file__).resolve().parents[2] / "src"
    hits = [p for p in root.rglob("*.py") if re.search(r"allow_expired_manifest\s*=\s*True", p.read_text())]
    assert hits == []
