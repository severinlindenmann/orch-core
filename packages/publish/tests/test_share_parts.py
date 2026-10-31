"""Offset append: shared vectors replayed through signed HTTP plus storage/lifecycle races."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pubsrv import shares, sweep
from pubsrv.serve import create_share_app
from scripts.make_share_parts_vectors import vectors
from tests.shares_helpers import WS_A, Two

VECTORS = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts/publish/publish_v2_parts.json").read_text()
)
SID = "1" * 32


@pytest.fixture
def two(tmp_path):
    return Two(tmp_path)


def stage(two, content=b"abcdef", path="f"):
    assert two.put_meta("a", SID, two.meta({path: content})).status_code == 200


def part(two, offset, body, path="f", who="a", **kw):
    return two.req(who, "PUT", f"/shares/{SID}/parts/{offset}/{path}", body, **kw)


def partial(two):
    return Path(two.data, "incoming", SID, "files", "f")


def test_vectors_fresh():
    assert VECTORS == vectors()


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_vectors(two, case):
    meta = two.meta({case["path"]: b"abcdef"})
    meta["files"][0].update(size=case["size"], sha256=case["sha256"])
    assert two.put_meta("a", SID, meta).status_code == 200
    for step in case["steps"]:
        if step["op"] == "part":
            r = part(two, step["offset"], bytes.fromhex(step["body_hex"]), case["path"])
            assert {"status": r.status_code, "body": r.json()} == step["expect"]
        elif step["op"] == "file":
            r = two.put_file("a", SID, case["path"], bytes.fromhex(step["body_hex"]))
            assert {"status": r.status_code, "body": r.json()} == step["expect"]
        else:
            method, tail = ("POST", "/commit") if step["op"] == "commit" else ("DELETE", "")
            assert two.req("a", method, f"/shares/{SID}{tail}").status_code == 200


@pytest.mark.parametrize("case", VECTORS["route_cases"], ids=lambda c: c["name"])
def test_raw_routes(case):
    assert (shares.part_route("PUT", case["target"]) is not None) == case["valid"]


@pytest.mark.parametrize("suffix", ["01/f", "0/f?x=1", "%30/f", "27262977/f"])
def test_invalid_http_route(two, suffix):
    stage(two)
    assert two.req("a", "PUT", f"/shares/{SID}/parts/{suffix}", b"a").status_code == 404
    assert not partial(two).exists()


def test_one_byte_concurrent_duplicate(two):
    stage(two)
    settings, store = two.app.state.settings, two.app.state.store
    wsk = two.sql("SELECT wsk_pub FROM workspaces WHERE workspace_id=?", WS_A)[0][0]

    def send(_):
        return shares.op_put_part(settings, store, WS_A, wsk, SID, "f", 0, b"a")

    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(send, range(2)))
    assert sorted(r["result"] for r in replies) == ["stored", "unchanged"]
    assert all(r["received"] == 1 for r in replies)
    assert partial(two).read_bytes() == b"a"


def test_whole_put_discards_partial_and_commit_waits(two):
    stage(two)
    part(two, 0, b"a")
    r = two.req("a", "POST", f"/shares/{SID}/commit")
    assert r.json() == {"error": "incomplete", "missing": ["f"]}
    assert two.put_file("a", SID, "f", b"abcdef").status_code == 200
    assert partial(two).read_bytes() == b"abcdef"
    assert part(two, 0, b"ab").json()["done"] is True


@pytest.mark.parametrize("end", ["revoke", "remove", "stale"])
def test_cleanup_and_no_resurrection(two, end):
    stage(two)
    part(two, 0, b"a")
    if end == "revoke":
        two.req("a", "DELETE", f"/shares/{SID}")
    elif end == "remove":
        two.app.state.store.remove(SID)
        assert not Path(two.data, "incoming", SID).exists()
        return
    else:
        two.now += sweep.STAGED_MAX_AGE_MS + 1
        os.utime(Path(two.data, "incoming", SID), (0, 0))
        sweep.run(two.path, two.data, two.now)
    assert not partial(two).exists()
    assert part(two, 1, b"b").status_code == 410
    assert not partial(two).exists()


def test_live_partial_survives_sweep(two):
    stage(two)
    part(two, 0, b"a")
    os.utime(partial(two), (0, 0))
    sweep.run(two.path, two.data, two.now)
    assert part(two, 1, b"bcdef").json()["done"]


def test_namespace_auth_and_body_cap(two):
    stage(two)
    assert part(two, 0, b"a", who="b").status_code == 404
    assert part(two, 0, b"a", ts=two.now - 120001).status_code == 401
    assert part(two, 0, b"a" * (1024 * 1024 + 1)).status_code == 413
    assert not partial(two).exists()


@pytest.mark.slow
def test_25_mib_in_66_parts(two):
    content = b"x" * shares.MAX_FILE
    stage(two, content)
    chunk = (len(content) + 65) // 66
    for offset in range(0, len(content), chunk):
        r = part(two, offset, content[offset : offset + chunk])
        assert r.status_code == 200, r.text
    assert r.json() == {"received": len(content), "done": True, "result": "stored"}
    assert two.req("a", "POST", f"/shares/{SID}/commit").status_code == 200
    assert Path(two.data, "shares", SID, "files/f").read_bytes() == content


def test_hash_failure_can_restart(two):
    stage(two)
    assert part(two, 0, b"XXXXXX").json() == {"error": "content_mismatch", "received": 0}
    assert not partial(two).exists()
    assert part(two, 0, b"abcdef").json()["done"]


def test_empty_file(two):
    stage(two, b"")
    assert part(two, 0, b"").json() == {"received": 0, "done": True, "result": "stored"}
    assert part(two, 0, b"").json()["result"] == "unchanged"


def test_unknown_share_and_path_do_not_create_state(two):
    assert part(two, 0, b"a").json() == {"error": "not_found"}
    assert not partial(two).exists()
    stage(two)
    assert part(two, 0, b"a", path="missing").json() == {"error": "not_in_manifest"}
    assert not partial(two).exists()


def test_manifest_reserves_quota_before_any_part(two, monkeypatch):
    monkeypatch.setattr(shares, "MAX_WORKSPACE", 6)
    stage(two)
    assert part(two, 0, b"a").status_code == 200
    r = two.put_meta("a", "2" * 32, two.meta({"g": b"x"}))
    assert r.json() == {"error": "quota_exceeded"}
    assert part(two, 1, b"bcdef").json()["done"]


def test_expired_before_sweep_does_not_create_state(two):
    stage(two)
    two.now += 8 * 86400_000
    assert part(two, 0, b"a").json() == {"error": "gone", "state": "expired"}
    assert not partial(two).exists()


def test_restart_uses_fsynced_length(two):
    stage(two)
    part(two, 0, b"ab")
    # A fresh Store uses only the persisted staging bytes.
    store = shares.Store(two.data)
    assert partial(two).stat().st_size == 2
    wsk = two.sql("SELECT wsk_pub FROM workspaces WHERE workspace_id=?", WS_A)[0][0]
    assert shares.op_put_part(two.app.state.settings, store, WS_A, wsk, SID, "f", 0, b"ab") == {
        "received": 2,
        "done": False,
        "result": "unchanged",
    }
    assert part(two, 2, b"cdef").json()["done"]


@pytest.mark.parametrize("path", ["f//x", "f/./x", "f/../x", "f/%61t/0"])
def test_raw_path_restrictions(path):
    target = f"/v1/ws/{WS_A}/shares/{SID}/parts/0/{path}"
    assert shares.part_route("PUT", target) is None


def test_partial_is_not_served_and_commit_refuses(two):
    stage(two)
    assert part(two, 0, b"ab").status_code == 200
    assert partial(two).read_bytes() == b"ab"
    assert not two.app.state.store.verify_file(
        "incoming", SID, "f", 6, two.meta({"f": b"abcdef"})["files"][0]["sha256"]
    )
    with TestClient(create_share_app(two.data, clock=lambda: two.now)) as public:
        assert public.get(f"/s/{SID}").status_code == 404
        assert public.post(f"/s/{SID}/open", data={"t": "t"}).status_code == 404
        assert public.get(f"/s/{SID}/v/{'a' * 32}/f").status_code == 404
    r = two.req("a", "POST", f"/shares/{SID}/commit")
    assert r.status_code == 409
    assert r.json() == {"error": "incomplete", "missing": ["f"]}


@pytest.mark.parametrize("path", [".partial-file", ".parts/f", "a/.hidden"])
def test_leading_dot_paths_stay_refused(two, path):
    assert two.put_meta("a", SID, two.meta({path: b"a"})).json() == {"error": "bad_path"}
    assert shares.part_route("PUT", f"/v1/ws/{WS_A}/shares/{SID}/parts/0/{path}") is None


def test_finalized_part_conflict(two):
    stage(two)
    assert part(two, 0, b"abcdef").json()["done"]
    assert part(two, 0, b"XX").json() == {"error": "part_conflict", "received": 6}
    assert partial(two).read_bytes() == b"abcdef"
