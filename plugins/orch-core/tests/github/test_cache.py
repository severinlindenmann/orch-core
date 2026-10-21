from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest

from orch.github import cache, token

from .test_poller import SETTINGS


@pytest.fixture
def state(tmp_path):
    (tmp_path / "hosts" / "ws").mkdir(parents=True, mode=0o700)
    return tmp_path


def test_roundtrip(state, engine):
    snapshot = engine[0].poll_once(SETTINGS)
    cache.save(state, "ws", os.getuid(), cache.document(snapshot), snapshot.as_of)
    loaded = cache.load(state, "ws", os.getuid())
    assert not loaded.snapshot.verified and loaded.snapshot.state == "stale"
    assert loaded.as_of == snapshot.as_of
    assert loaded.snapshot.pulls == snapshot.pulls and loaded.snapshot.checks == snapshot.checks
    assert loaded.snapshot.issues == snapshot.issues and loaded.snapshot.reviews == snapshot.reviews
    directory = token.github_dir(state, "ws")
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "cache.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        cache.LoadedCache(replace(snapshot, verified=True))


@pytest.mark.parametrize("damage", ["huge", "types", "unknown", "nested", "symlink", "mode", "dir_mode", "json", "nan"])
def test_bad_cache(state, engine, damage, caplog):
    snapshot = engine[0].poll_once(SETTINGS)
    cache.save(state, "ws", os.getuid(), cache.document(snapshot), snapshot.as_of)
    path = token.github_dir(state, "ws") / "cache.json"
    doc = json.loads(path.read_text())
    if damage == "huge":
        path.write_text(" " * (cache.CAP + 1))
    elif damage == "symlink":
        path.unlink()
        path.symlink_to(state / "missing")
    elif damage == "mode":
        path.chmod(0o644)
    elif damage == "dir_mode":
        path.parent.chmod(0o755)
    elif damage == "json":
        path.write_text("{")
    else:
        if damage == "types":
            doc["snapshot"]["pulls"][0]["pull"]["number"] = True
        elif damage == "unknown":
            doc["unexpected"] = "secret"
        elif damage == "nested":
            doc["snapshot"]["pulls"][0]["reviews"] = []
        elif damage == "nan":
            doc["saved_at"] = float("nan")
        path.write_text(json.dumps(doc))
    assert cache.load(state, "ws", os.getuid()) is None
    assert "github cache rejected" in caplog.text and "secret" not in caplog.text


def test_tampered_text_reingested(state, engine):
    snapshot = engine[0].poll_once(SETTINGS)
    doc = cache.document(snapshot)
    doc["pulls"][0]["pull"]["title"] = "\x00\u202e" * 10000
    doc["pulls"][0]["pull"]["url"] = "https://evil.example"
    cache.save(state, "ws", os.getuid(), doc, 10)
    p = cache.load(state, "ws", os.getuid()).snapshot.pulls[0]
    assert len(p.title) <= 800 and "\u202e" not in p.title
    assert p.url == "https://github.com/team/alpha/pull/1"
