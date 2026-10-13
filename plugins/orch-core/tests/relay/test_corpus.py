"""Drift guard for the vendored relay corpus (11A.10): the checks of orch-relay's
``tests/test_http_fixtures_manifest.py`` plus ``SOURCE`` (relay commit, MANIFEST revision, sha256 of the vector file).

Updating the corpus is one PR that bumps ``SOURCE`` and changes nothing else."""

from __future__ import annotations

import hashlib
import json

from . import corpus

MANIFEST_PATH = corpus.FIXTURES / "MANIFEST.json"
MANIFEST = json.loads(MANIFEST_PATH.read_text())


def _render(manifest: dict) -> str:  # the relay's manifest.render
    return json.dumps(manifest, sort_keys=True, indent=2) + "\n"


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
