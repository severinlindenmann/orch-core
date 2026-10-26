"""Drift guard for the relay corpus in ``contracts/relay`` (11A.10): the checks of the relay's fixture manifest.

The corpus is one copy in this repository; a change to it is a contract change and lists every consumer (relay, core,
mobile, publish) in its PR."""

from __future__ import annotations

import hashlib
import json

from . import corpus

MANIFEST_PATH = corpus.FIXTURES / "MANIFEST.json"
MANIFEST = json.loads(MANIFEST_PATH.read_text())


def _render(manifest: dict) -> str:  # the relay's manifest.render
    return json.dumps(manifest, sort_keys=True, indent=2) + "\n"


def test_the_manifest_revision_is_the_one_the_phone_and_core_expect():
    assert MANIFEST["revision"] == 19


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
