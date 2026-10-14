"""Drift guard for the vendored relay corpus (11A.10): the checks of orch-relay's
``tests/test_http_fixtures_manifest.py`` plus ``SOURCE`` (relay commit, MANIFEST revision, sha256 of the vector file).

Updating the corpus is one PR that bumps ``SOURCE`` and changes nothing else.

What it cannot do: notice that orch-relay moved on. It checks the vendored copy against itself and against the pinned
commit below. Only the real-relay CI job (T2, 11A.10), or the optional cross-check with ``ORCH_RELAY_DIR`` set to an
orch-relay clone, compares the corpus with the relay itself."""

from __future__ import annotations

import hashlib
import json
import lzma

import pytest

from . import corpus

MANIFEST_PATH = corpus.FIXTURES / "MANIFEST.json"
MANIFEST = json.loads(MANIFEST_PATH.read_text())


def _render(manifest: dict) -> str:  # the relay's manifest.render
    return json.dumps(manifest, sort_keys=True, indent=2) + "\n"


PINNED_RELAY_COMMIT = "772d7b07fcd4aad13060cabd3921681dfdc700f4"
UPDATE = (
    "the vendored relay corpus changed. To update it: run scripts/sync-vectors.sh <relay ref> (it writes "
    "tests/relay/vectors/vectors_v2.json.xz, the http_fixtures/ files MANIFEST.json lists plus MANIFEST.json, and "
    "SOURCE: relay_commit, http_fixtures_manifest_revision, vectors_v2_sha256 over the decompressed bytes), bump "
    "PINNED_RELAY_COMMIT in tests/relay/test_corpus.py, and land it as one PR that changes nothing else"
)


def test_source_pins_the_relay_commit():
    assert corpus.source()["relay_commit"] == PINNED_RELAY_COMMIT, UPDATE


def test_corpus_equals_the_relay_at_the_pinned_commit():
    """Optional cross-check against an orch-relay clone (``ORCH_RELAY_DIR``); skipped when none is configured."""
    import os
    import subprocess

    d = os.environ.get("ORCH_RELAY_DIR")
    if not d:
        pytest.skip("ORCH_RELAY_DIR not set")

    def show(path: str) -> bytes:
        return subprocess.run(
            ["git", "-C", d, "show", f"{PINNED_RELAY_COMMIT}:{path}"], capture_output=True, check=True, timeout=60
        ).stdout

    assert show("tests/vectors_v2.json") == corpus.vectors_bytes(), UPDATE
    for name in MANIFEST["files"]:
        assert show(f"tests/http_fixtures/{name}") == (corpus.FIXTURES / name).read_bytes(), f"{name}: {UPDATE}"


def test_source_names_commit_revision_and_vector_hash():
    src = corpus.source()
    assert set(src) == {"relay_repo", "relay_commit", "http_fixtures_manifest_revision", "vectors_v2_sha256"}
    assert len(src["relay_commit"]) == 40 and int(src["relay_commit"], 16) >= 0
    assert int(src["http_fixtures_manifest_revision"]) == MANIFEST["revision"] == 11
    assert hashlib.sha256(corpus.vectors_bytes()).hexdigest() == src["vectors_v2_sha256"]  # decompressed bytes


def test_manifest_shape():
    assert set(MANIFEST) == {"revision", "files", "aggregate"}
    assert isinstance(MANIFEST["revision"], int) and MANIFEST["revision"] >= 1
    assert "MANIFEST.json" not in MANIFEST["files"]


def test_manifest_is_not_stale():
    actual = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in corpus.FIXTURES.iterdir()
        if p.is_file() and p.name != "MANIFEST.json"
    }
    files = MANIFEST["files"]
    assert sorted(set(actual) - set(files)) == [], "added"
    assert sorted(set(files) - set(actual)) == [], "removed"
    assert [n for n in actual if actual[n] != files[n]] == [], "changed"


def test_manifest_aggregate():
    lines = "".join(f"{n}\t{h}\n" for n, h in sorted(MANIFEST["files"].items()))
    assert MANIFEST["aggregate"] == hashlib.sha256(lines.encode()).hexdigest()


def test_manifest_file_is_canonical():
    assert MANIFEST_PATH.read_text() == _render(MANIFEST)


def test_vector_file_is_suite_2_v2():
    assert corpus.VECTORS["version"] == 2
    assert corpus.S2["suite"] == 2


def test_the_vector_file_is_stored_compressed_only():
    """Round 6 (orch-52 #6): only the xz file is vendored (no raw 23 MB copy here or in tests/vectors/), it is a
    complete xz stream with an integrity check, and it is small."""
    d = corpus.VECTORS_PATH.parent
    assert corpus.VECTORS_PATH.name == "vectors_v2.json.xz"
    assert not (d / "vectors_v2.json").exists()
    assert not (d.parents[1] / "vectors" / "vectors_v2.json").exists()
    raw = corpus.VECTORS_PATH.read_bytes()
    assert raw[:6] == b"\xfd7zXZ\x00" and len(raw) < 2 << 20
    assert lzma.decompress(raw, format=lzma.FORMAT_XZ) == corpus.vectors_bytes()


def test_a_tampered_compressed_file_fails_the_pin(tmp_path, monkeypatch):
    """The pin is over the decompressed bytes: a different (validly compressed) vector file fails it."""
    other = tmp_path / "vectors_v2.json.xz"
    other.write_bytes(lzma.compress(corpus.vectors_bytes() + b" ", format=lzma.FORMAT_XZ))
    monkeypatch.setattr(corpus, "VECTORS_PATH", other)
    assert hashlib.sha256(corpus.vectors_bytes()).hexdigest() != corpus.source()["vectors_v2_sha256"]
