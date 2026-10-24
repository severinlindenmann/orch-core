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


PINNED_RELAY_COMMIT = "4a9f95474bdac8dc3ffae8be4099a9b4b2e6cdcc"
UPDATE = (
    "the vendored relay corpus changed. To update it: run scripts/sync-vectors.sh <relay ref> (it writes "
    "tests/wsproto/vectors/vectors_v2.json.xz, the http_fixtures/ files MANIFEST.json lists plus MANIFEST.json, and "
    "SOURCE: relay_commit, http_fixtures_manifest_revision, vectors_v2_sha256 over the decompressed bytes), bump "
    "PINNED_RELAY_COMMIT in tests/wsproto/test_corpus.py, and land it as one PR that changes nothing else"
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
    assert int(src["http_fixtures_manifest_revision"]) == MANIFEST["revision"] == 19
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
    """The pin is over the decompressed bytes and checked at load time (round 7): a different, validly compressed
    vector file is refused by the loader itself."""
    good = corpus.vectors_bytes()
    other = tmp_path / "vectors_v2.json.xz"
    other.write_bytes(lzma.compress(good + b" ", format=lzma.FORMAT_XZ, preset=0))
    monkeypatch.setattr(corpus, "VECTORS_PATH", other)
    with pytest.raises(corpus.CorpusError, match="vectors_v2_sha256"):
        corpus.vectors_bytes()
    other.write_bytes(lzma.compress(good, format=lzma.FORMAT_XZ, preset=0))  # recompressed, same bytes: accepted
    assert corpus.vectors_bytes() == good


def test_an_oversized_vector_file_is_refused_without_inflating_it(tmp_path, monkeypatch):
    """A decompression bomb: 64 MiB + 1 zero bytes compress to a few KB; the loader stops at the bound."""
    bomb = tmp_path / "vectors_v2.json.xz"
    bomb.write_bytes(lzma.compress(bytes(corpus.MAX_VECTORS_BYTES + 1), format=lzma.FORMAT_XZ, preset=0))
    monkeypatch.setattr(corpus, "VECTORS_PATH", bomb)
    with pytest.raises(corpus.CorpusError, match="more than"):
        corpus.vectors_bytes()
    assert corpus.decompress(lzma.compress(bytes(10)), limit=10) == bytes(10)  # exactly at the bound: fine
    with pytest.raises(corpus.CorpusError, match="more than"):
        corpus.decompress(lzma.compress(bytes(11)), limit=10)


def test_decompression_stays_bounded_by_the_cap(monkeypatch):
    """Round 8 (M12): the refusal is not enough; the decompressor itself never produces more than cap + 1 bytes, so a
    bomb is never inflated in memory (a decompress-then-check loader would produce all 8 MiB here)."""
    produced: list[int] = []
    real = lzma.LZMADecompressor

    class Recording:
        def __init__(self, *a, **kw):
            self._d = real(*a, **kw)

        def decompress(self, data, max_length=-1):
            out = self._d.decompress(data, max_length)
            produced.append(len(out))
            return out

        def __getattr__(self, name):
            return getattr(self._d, name)

    monkeypatch.setattr(corpus.lzma, "LZMADecompressor", Recording)
    cap = 4096
    with pytest.raises(corpus.CorpusError, match="more than"):
        corpus.decompress(lzma.compress(bytes(8 << 20), format=lzma.FORMAT_XZ, preset=0), limit=cap)
    assert produced and sum(produced) <= cap + 1


def test_a_truncated_or_padded_vector_file_is_refused():
    raw = lzma.compress(b"{}", format=lzma.FORMAT_XZ)
    for bad in (raw[:-4], raw + b"x", b"not xz"):
        with pytest.raises(corpus.CorpusError):
            corpus.decompress(bad)
