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


# ---- one hardened open for every pinned read (issue 12)
def _full(b):
    return hashlib.sha256(b).hexdigest()


def test_pinned_read_refuses_symlinks_file_and_directory(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f.bin").write_bytes(b"x")
    (tmp_path / "flink").symlink_to(tmp_path / "real" / "f.bin")
    (tmp_path / "dlink").symlink_to(tmp_path / "real")
    assert read_pinned(tmp_path / "real" / "f.bin", _full(b"x")) == b"x"
    assert read_pinned(tmp_path / "flink", _full(b"x")) is None
    assert read_pinned(tmp_path / "dlink" / "f.bin", _full(b"x")) is None


def test_pinned_read_refuses_a_hard_link(tmp_path):
    (tmp_path / "a").write_bytes(b"x")
    os.link(tmp_path / "a", tmp_path / "b")
    assert read_pinned(tmp_path / "a", _full(b"x")) is None


def test_pinned_read_does_not_block_on_a_fifo(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    assert read_pinned(tmp_path / "pipe", "0" * 64) is None


def test_pinned_read_is_capped(tmp_path):
    (tmp_path / "big").write_bytes(b"x" * 11)
    assert read_pinned(tmp_path / "big", _full(b"x" * 11), limit=10) is None
    assert read_pinned(tmp_path / "big", _full(b"x" * 11), limit=11) == b"x" * 11


def test_a_swap_after_the_check_serves_the_bytes_that_were_hashed(tmp_path, monkeypatch):
    p = tmp_path / "f"
    p.write_bytes(b"one")
    real_open, seen = os.open, []

    def swapping(path, flags, *a, **k):
        fd = real_open(path, flags, *a, **k)
        if path == "f":  # right after the handle is taken, replace the name with a symlink to other bytes
            seen.append(1)
            (tmp_path / "other").write_bytes(b"two")
            p.rename(tmp_path / "moved")
            p.symlink_to(tmp_path / "other")
        return fd

    monkeypatch.setattr(os, "open", swapping)
    assert read_pinned(p, _full(b"one")) == b"one" and seen
    assert read_pinned(p, _full(b"two")) is None  # now a symlink: refused


# ---- the same open serves /a/<ticket>/<name> without ?v=
def _folder(ws, working):
    d = ws.artifacts_dir / working
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_unpinned_artifact_is_served_from_the_open_handle(dash, ws, working):
    pytest.importorskip("fastapi")
    d = _folder(ws, working)
    (d / "sub" / "deep").mkdir(parents=True)
    (d / "sub" / "deep" / "x.png").write_bytes(b"\x89PNG-nested")
    r = dash.get(f"/a/{working}/sub/deep/x.png")
    assert r.status_code == 200 and r.content == b"\x89PNG-nested"
    assert r.headers["content-type"] == "image/png" and r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"] and r.headers["content-length"] == "11"


def test_unpinned_artifact_refuses_links_and_special_files(dash, ws, working, tmp_path):
    pytest.importorskip("fastapi")
    d = _folder(ws, working)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    os.link(outside, d / "hard.txt")
    (d / "real").mkdir()
    (d / "real" / "ok.txt").write_bytes(b"ok")
    (d / "link.txt").symlink_to(d / "real" / "ok.txt")
    (d / "dlink").symlink_to(d / "real")
    os.mkfifo(d / "pipe.txt")
    for name in ("hard.txt", "link.txt", "dlink/ok.txt", "pipe.txt", "gone.txt"):
        assert dash.get(f"/a/{working}/{name}").status_code == 404, name
    assert dash.get(f"/a/{working}/real/ok.txt").content == b"ok"


def test_nested_components_are_walked_without_following_links(tmp_path):
    from orch.core.artifacts import read_regular
    (tmp_path / "root" / "a" / "b").mkdir(parents=True)
    (tmp_path / "root" / "a" / "b" / "f").write_bytes(b"x")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "f").write_bytes(b"y")
    root = tmp_path / "root"
    assert read_regular(root / "a" / "b" / "f", root=root) == b"x"
    (root / "a" / "b").rename(tmp_path / "moved")
    (root / "a" / "b").symlink_to(tmp_path / "elsewhere")  # an intermediate component swapped for a link
    assert read_regular(root / "a" / "b" / "f", root=root) is None
    assert read_regular(tmp_path / "elsewhere" / "f", root=root) is None  # not under the root


def test_a_response_dropped_unread_closes_its_file(ws, working):
    import gc
    from types import SimpleNamespace

    from orch.dashboard.routes_ticket import artifact
    (_folder(ws, working) / "big.bin").write_bytes(b"x" * 1000)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ws=ws)))
    before = set(os.listdir("/dev/fd"))
    r = artifact(request, working, "big.bin")
    assert len(set(os.listdir("/dev/fd")) - before) == 1  # the one open handle
    del r
    gc.collect()
    assert set(os.listdir("/dev/fd")) <= before


def test_data_uri_refuses_a_link_inside_the_ticket_folder(ws, working):
    from orch.widgets.artifacts import data_uri
    d = _folder(ws, working)
    (d / "a.png").write_bytes(b"\x89PNG-1")
    (d / "l.png").symlink_to(d / "a.png")
    sha = _full(b"\x89PNG-1")
    assert data_uri(ws, working, f"artifacts/{working}/a.png", sha).startswith("data:image/png;base64,")
    assert data_uri(ws, working, f"artifacts/{working}/l.png", sha) is None
