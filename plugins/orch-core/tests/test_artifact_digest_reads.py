"""The digest of a served file: the cache cannot be fooled by an in-place rewrite that keeps size, inode and mtime,
and whatever is served under ?v=<digest> is hashed from the very bytes that are sent (one read, never check one read
and serve another)."""
import hashlib
import os

import pytest

from orch.core import store
from orch.core.artifacts import file_sha256, read_pinned


def test_an_in_place_rewrite_with_the_mtime_restored_is_not_a_cache_hit(tmp_path):
    p = tmp_path / "shot.png"
    p.write_bytes(b"\x89PNG-AAAA")
    first = file_sha256(p)
    st = p.stat()
    with open(p, "r+b") as f:  # same inode, same size
        f.write(b"\x89PNG-BBBB")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))  # mtime set back
    assert p.stat().st_size == st.st_size and p.stat().st_ino == st.st_ino and p.stat().st_mtime_ns == st.st_mtime_ns
    assert file_sha256(p) == hashlib.sha256(b"\x89PNG-BBBB").hexdigest() != first


def test_read_pinned_returns_the_bytes_it_hashed(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"one")
    full = hashlib.sha256(b"one").hexdigest()
    assert read_pinned(p, full) == b"one" and read_pinned(p, full[:16]) == b"one"
    assert read_pinned(p, full[:7]) is None and read_pinned(p, "XYZ" * 4) is None
    p.write_bytes(b"two")
    assert read_pinned(p, full) is None
    assert read_pinned(tmp_path / "gone", full) is None


def test_the_route_hashes_what_it_sends_not_a_cached_digest(dash, ws, aops, working, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    src = tmp_path / "shot.png"
    src.write_bytes(b"\x89PNG-one")
    aops.artifact_add(working, src)
    sha = store.load(ws, working)[1].meta["artifacts"][0]["sha256"]
    r = dash.get(f"/a/{working}/shot.png?v={sha}")
    assert r.status_code == 200 and r.content == b"\x89PNG-one"
    (ws.artifacts_dir / working / "shot.png").write_bytes(b"\x89PNG-two")
    # a digest check that still says "ok" (a stale cache, or a check made before the swap) must not let other bytes out
    from orch.core import artifacts
    monkeypatch.setattr(artifacts, "file_sha256", lambda path: sha)
    r = dash.get(f"/a/{working}/shot.png?v={sha[:16]}")
    assert r.status_code == 409 and b"PNG-two" not in r.content
