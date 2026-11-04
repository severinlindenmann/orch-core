"""The verifier embedded in install.sh (stdlib only) must agree with orch.update.verify on every vector, and its
pure-Python Ed25519 must agree with `cryptography`. TEST root only: the embedded copy is rebuilt with it."""

from __future__ import annotations

import base64
import importlib.util
import json
import random
import sys
from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from orch.update import keys as keys_mod
from orch.update.verify import UpdateRefused, verify_envelope

from ._release import VEC, trusting_test_root, verifier_source

CORE = json.loads((VEC / "core.json").read_text(encoding="utf-8"))["cases"]
CATALOG = json.loads((VEC / "catalog.json").read_text(encoding="utf-8"))["cases"]


@pytest.fixture(scope="module")
def emb(tmp_path_factory):
    path = tmp_path_factory.mktemp("verifier") / "verify_embedded.py"
    path.write_text(trusting_test_root(verifier_source(), allow_test_channels=True), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("verify_embedded", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["verify_embedded"] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("verify_embedded", None)


def _when(case: dict[str, Any]) -> datetime:
    return datetime.strptime(case["now"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def embedded(emb, case: dict[str, Any], envelope: bytes | None = None) -> tuple[str, Any]:
    st = case["state"]
    files = None
    if "files" in case:
        files = {f["name"]: base64.b64decode(f["content_b64"]) for f in case["files"]}
    roots = {k: base64.b64decode(v) for k, v in emb.ROOTS_B64.items() if k in st["trusted_roots"]}
    try:
        v = emb.verify(
            case["envelope"].encode() if envelope is None else envelope,
            case["channel"],
            _when(case),
            st["seq_seen"],
            st["revocations_issued_at"],
            files=files,
            roots=roots,
        )
    except emb.Refused as r:
        return r.reason, None
    return "ok", v


def core(case: dict[str, Any], envelope: bytes | None = None, purpose: str = "core") -> tuple[str, Any]:
    st = case["state"]
    files = None
    if "files" in case and purpose == "core":
        files = {f["name"]: base64.b64decode(f["content_b64"]) for f in case["files"]}
    with keys_mod.enable_test_root_for_tests():
        roots = {k: v for k, v in keys_mod.embedded_roots().items() if k in st["trusted_roots"]}
        try:
            v = verify_envelope(
                case["envelope"].encode() if envelope is None else envelope,
                purpose=purpose,
                channel=case["channel"],
                now=_when(case),
                seq_seen=st["seq_seen"],
                revocations_issued_at=st["revocations_issued_at"],
                files=files,
                roots=roots,
            )
        except UpdateRefused as r:
            return r.reason.value, None
    return "ok", v


@pytest.mark.slow
@pytest.mark.parametrize("case", CORE, ids=[c["name"] for c in CORE])
def test_core_vectors_give_the_expected_reason(emb, case):
    reason, v = embedded(emb, case)
    assert reason == case["expect"]["reason"]
    assert reason == core(case)[0]
    if reason == "ok":
        acc = case["expect"]["accept"]
        assert (v["channel"], v["seq"], v["version"], v["min_serial"], v["revocations_issued_at"]) == (
            acc["channel"],
            acc["seq"],
            acc["version"],
            acc["min_serial"],
            acc["revocations_issued_at"],
        )


@pytest.mark.slow
@pytest.mark.parametrize("case", CATALOG, ids=[c["name"] for c in CATALOG])
def test_catalog_vectors_agree_with_core_when_read_as_a_core_release(emb, case):
    """The installer only reads core releases: a catalog envelope must be refused the same way core refuses it."""
    c = {k: v for k, v in case.items() if k != "files"}
    assert embedded(emb, c)[0] == core(c, purpose="core")[0] != "ok"


@pytest.mark.slow
def test_mutations_of_a_good_envelope_agree_with_core(emb):
    case = next(c for c in CORE if c["name"] == "ok_develop")
    env = case["envelope"]
    rng = random.Random(20261030)
    positions = sorted(rng.sample(range(len(env)), 120))
    seen = set()
    for pos in positions:
        for repl in ("A", "0", "}", '"'):
            if env[pos] == repl:
                continue
            mutated = (env[:pos] + repl + env[pos + 1 :]).encode()
            a, b = embedded(emb, case, mutated)[0], core(case, mutated)[0]
            assert a == b, (pos, repl, a, b)
            seen.add(a)
    assert {"bad_sig", "malformed"} <= seen


def test_ed25519_agrees_with_cryptography(emb):
    rng = random.Random(7)
    for i in range(12):
        sk = Ed25519PrivateKey.from_private_bytes(rng.randbytes(32))
        pub = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        msg = rng.randbytes(i * 7)
        sig = sk.sign(msg)
        assert emb.ed25519_verify(pub, sig, msg)
        bad = bytearray(sig)
        bad[rng.randrange(64)] ^= 1 << rng.randrange(8)
        assert not emb.ed25519_verify(pub, bytes(bad), msg)
        assert not emb.ed25519_verify(pub, sig, msg + b"x")
        # S + L (a non-canonical second encoding of the same signature) must be refused
        s = int.from_bytes(sig[32:], "little") + emb.L
        if s < 1 << 256:
            assert not emb.ed25519_verify(pub, sig[:32] + s.to_bytes(32, "little"), msg)
    assert not emb.ed25519_verify(b"\x00" * 31, b"\x00" * 64, b"")
    assert not emb.ed25519_verify(b"\x01" + b"\x00" * 31, b"\x00" * 63, b"")


def test_the_embedded_verifier_is_stdlib_only():
    import ast

    stdlib = set(sys.stdlib_module_names)
    mods = set()
    for node in ast.walk(ast.parse(verifier_source())):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    assert mods <= stdlib, mods - stdlib


def test_the_verifier_runs_isolated(tmp_path):
    """`python -I verify.py` works without any third-party package (the way install.sh runs it)."""
    import subprocess

    f = tmp_path / "verify.py"
    f.write_text(trusting_test_root(verifier_source()))
    r = subprocess.run([sys.executable, "-I", "-S", str(f), "nonsense"], capture_output=True, text=True, env={})
    assert r.returncode == 2 and "unknown command" in r.stderr
