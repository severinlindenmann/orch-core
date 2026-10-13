"""Drift guard for the vendored relay corpus (11A.10): the checks of orch-relay's
``tests/test_http_fixtures_manifest.py`` plus ``SOURCE`` (relay commit, MANIFEST revision, sha256 of the vector file).

Updating the corpus is one PR that bumps ``SOURCE`` and changes nothing else.

What it cannot do: notice that orch-relay moved on. It checks the vendored copy against itself and against the pinned
commit below. Only the real-relay CI job (T2, 11A.10), or the optional cross-check with ``ORCH_RELAY_DIR`` set to an
orch-relay clone, compares the corpus with the relay itself."""

from __future__ import annotations

import hashlib
import json

import pytest

from . import corpus

MANIFEST_PATH = corpus.FIXTURES / "MANIFEST.json"
MANIFEST = json.loads(MANIFEST_PATH.read_text())


def _render(manifest: dict) -> str:  # the relay's manifest.render
    return json.dumps(manifest, sort_keys=True, indent=2) + "\n"


PINNED_RELAY_COMMIT = "772d7b07fcd4aad13060cabd3921681dfdc700f4"
UPDATE = (
    "the vendored relay corpus changed. To update it: copy tests/vectors_v2.json and tests/http_fixtures/ (the files "
    "MANIFEST.json lists, plus MANIFEST.json) from orch-relay at the new commit into tests/relay/vectors/, set SOURCE "
    "(relay_commit, http_fixtures_manifest_revision, vectors_v2_sha256), bump PINNED_RELAY_COMMIT in "
    "tests/relay/test_corpus.py, and land it as one PR that changes nothing else"
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

    assert show("tests/vectors_v2.json") == corpus.VECTORS_PATH.read_bytes(), UPDATE
    for name in MANIFEST["files"]:
        assert show(f"tests/http_fixtures/{name}") == (corpus.FIXTURES / name).read_bytes(), f"{name}: {UPDATE}"


def test_source_names_commit_revision_and_vector_hash():
    src = corpus.source()
    assert set(src) == {"relay_repo", "relay_commit", "http_fixtures_manifest_revision", "vectors_v2_sha256"}
    assert len(src["relay_commit"]) == 40 and int(src["relay_commit"], 16) >= 0
    assert int(src["http_fixtures_manifest_revision"]) == MANIFEST["revision"] == 11
    assert hashlib.sha256(corpus.VECTORS_PATH.read_bytes()).hexdigest() == src["vectors_v2_sha256"]


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
