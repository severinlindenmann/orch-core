"""Offset append: shared vectors replayed through signed HTTP plus storage/lifecycle races."""

import json
import os
import threading
import time
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


def test_part_budgets(two, monkeypatch):
    stage(two)
    monkeypatch.setattr(shares, "PART_BYTES_PER_MIN", 6)
    assert part(two, 0, b"abcdef").status_code == 200
    assert part(two, 0, b"a").json() == {"error": "rate_limited"}
    # Reads and the ordinary write budget remain separate.
    assert two.req("a", "GET", f"/shares/{SID}").status_code == 200
    two.now += 60_000
    monkeypatch.setattr(shares, "PARTS_PER_MIN", 2)
    assert part(two, 0, b"").status_code == 200
    assert part(two, 0, b"").status_code == 200
    assert part(two, 0, b"").status_code == 429
    assert two.req("a", "POST", f"/shares/{SID}/commit").status_code == 200


def test_part_budget_boundaries():
    now = 0
    limiter = shares._PartLimiter(lambda: now)
    assert limiter.allow("a", 64 * 1024 * 1024)
    assert not limiter.allow("a", 1)
    assert limiter.allow("b", 1)
    for _ in range(599):
        assert limiter.allow("a", 0)
    assert not limiter.allow("a", 0)
    now = 60_000
    assert limiter.allow("a", 64 * 1024 * 1024)


def test_part_byte_budget_refuses_over_64_mib_and_recovers(two):
    chunk = b"x" * (1024 * 1024)
    stage(two, chunk + b"x")
    # Retried prefixes consume bandwidth too; keep the manifest small and valid.
    for _ in range(64):
        response = part(two, 0, chunk)
        assert response.status_code == 200, response.text
    response = part(two, len(chunk), b"x")
    assert response.status_code == 429
    assert response.json() == {"error": "rate_limited"}
    assert partial(two).stat().st_size == len(chunk)
    two.now += 60_000
    response = part(two, len(chunk), b"x")
    assert response.status_code == 200, response.text
    assert response.json()["done"] is True


def test_part_count_budget_refuses_601st_tiny_part(two):
    stage(two, b"x")
    for _ in range(600):
        response = part(two, 0, b"")
        assert response.status_code == 200, response.text
    response = part(two, 0, b"")
    assert response.status_code == 429
    assert response.json() == {"error": "rate_limited"}
    two.now += 60_000
    assert part(two, 0, b"x").json()["done"] is True


def test_finalize_hash_allows_other_workspace_write(two, monkeypatch):
    chunk = b"x" * (1024 * 1024)
    stage(two, chunk * 3)
    for offset in (0, len(chunk)):
        assert part(two, offset, chunk).status_code == 200
    hashing = threading.Event()
    release = threading.Event()
    timed_out = threading.Event()
    digest = shares._part_digest

    class PausedReader:
        def __init__(self, data):
            self.data = data
            self.reads = 0

        def seek(self, offset):
            return self.data.seek(offset)

        def read(self, size):
            assert size == 1024 * 1024
            if self.reads == 1:
                # The real digest loop has consumed and hashed its first chunk.
                hashing.set()
                assert release.wait(10), "finalize hash was never released"
            self.reads += 1
            return self.data.read(size)

    monkeypatch.setattr(shares, "_part_digest", lambda data: digest(PausedReader(data)))

    def deadline():
        timed_out.set()
        release.set()  # Let both requests unwind even if the write lock regresses.

    watchdog = threading.Timer(2, deadline)
    with ThreadPoolExecutor(1) as pool:
        final = pool.submit(part, two, len(chunk) * 2, chunk)
        try:
            assert hashing.wait(5), "finalize did not reach the first hash chunk"
            started = time.monotonic()
            watchdog.start()
            response = two.put_meta("b", "2" * 32, two.meta({"small": b"ok"}))
            elapsed = time.monotonic() - started
            assert not timed_out.is_set() and elapsed < 2, (
                "other workspace write timed out while finalize hash was blocked"
            )
            assert response.status_code == 200, response.text
            assert not release.is_set()
            assert not final.done()
        finally:
            watchdog.cancel()
            release.set()
        response = final.result(timeout=5)
        assert response.status_code == 200, response.text
        assert response.json()["done"] is True


@pytest.mark.parametrize("complete", [False, True])
def test_noop_rolls_back_without_db_write(two, monkeypatch, complete):
    stage(two)
    body = b"abcdef" if complete else b"ab"
    assert part(two, 0, body).status_code == 200
    statements = []
    connect = shares.db.connect

    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(shares.db, "connect", traced)
    wsk = two.sql("SELECT wsk_pub FROM workspaces WHERE workspace_id=?", WS_A)[0][0]
    reply = shares.op_put_part(
        two.app.state.settings, two.app.state.store, WS_A, wsk, SID, "f", 0, body
    )
    assert reply["result"] == "unchanged"
    assert "ROLLBACK" in statements
    assert not any(
        q.split()[0].upper() in {"UPDATE", "INSERT", "DELETE", "COMMIT"} for q in statements
    )


@pytest.mark.parametrize("change", ["inode", "size", "present", "revoked"])
@pytest.mark.parametrize("bad_hash", [False, True])
def test_finalize_rechecks_after_unlocked_hash(two, monkeypatch, change, bad_hash):
    stage(two)
    digest = shares._part_digest

    def race(data):
        # This second writer succeeds only if hashing released BEGIN IMMEDIATE.
        conn = shares.db.connect(two.path)
        conn.execute("PRAGMA busy_timeout=1")
        conn.execute("BEGIN IMMEDIATE")
        if change == "present":
            conn.execute("UPDATE share_files SET present=1 WHERE share_id=?", (SID,))
        elif change == "revoked":
            conn.execute("UPDATE shares SET state='revoked' WHERE id=?", (SID,))
        elif change == "inode":
            replacement = partial(two).with_name("replacement")
            replacement.write_bytes(b"abcdef")
            replacement.replace(partial(two))
        else:
            with partial(two).open("ab") as out:
                out.write(b"x")
        conn.execute("COMMIT")
        conn.close()
        return digest(data)

    monkeypatch.setattr(shares, "_part_digest", race)
    r = part(two, 0, b"XXXXXX" if bad_hash else b"abcdef")
    assert r.status_code == (410 if change == "revoked" else 409)
    assert partial(two).exists()  # A stale mismatch must never unlink the replacement.


def test_hash_reads_in_one_mib_chunks():
    import hashlib
    import io

    class Bounded(io.BytesIO):
        def read(self, size=-1):
            assert size == 1024 * 1024
            return super().read(size)

    body = b"a" * (2 * 1024 * 1024 + 3)
    assert shares._part_digest(Bounded(body)) == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize("state", ["live", "revoked", "expired", "deleted", "unexpected"])
def test_only_staged_state_accepts_parts(two, state):
    stage(two)
    conn = shares.db.connect(two.path)
    conn.execute("PRAGMA ignore_check_constraints=ON")
    conn.execute("UPDATE shares SET state=? WHERE id=?", (state, SID))
    conn.close()
    r = part(two, 0, b"a")
    assert r.json() == (
        {"error": "share_committed"} if state == "live" else {"error": "gone", "state": state}
    )
    assert not partial(two).exists()


@pytest.mark.parametrize("complete", [False, True])
def test_nonregular_file_refused(two, complete):
    stage(two)
    if complete:
        assert part(two, 0, b"abcdef").status_code == 200
        partial(two).unlink()
    os.mkfifo(partial(two))
    r = part(two, 0, b"a")
    assert r.status_code == 400
    assert r.json() == {"error": "malformed"}


def test_manifest_path_collision(two):
    r = two.put_meta("a", SID, two.meta({"a": b"x", "a/b": b"y"}))
    assert r.status_code == 400
    assert r.json() == {"error": "bad_path"}


@pytest.mark.parametrize("path", ["a", "a/b"])
def test_staging_path_collision(two, path):
    stage(two, b"x", path)
    root = partial(two).parent
    if path == "a":
        (root / "a").mkdir()
    else:
        (root / "a").write_bytes(b"x")
    r = part(two, 0, b"x", path=path)
    assert r.status_code == 400
    assert r.json() == {"error": "malformed"}


def test_handler_uses_authenticated_target(two):
    import asyncio

    from starlette.requests import Request

    stage(two)
    endpoint = next(
        r.endpoint
        for included in two.app.routes
        for r in getattr(getattr(included, "original_router", None), "routes", [included])
        if getattr(r, "path", "").endswith("/parts/{offset}/{fpath:path}")
    )
    wsk = two.sql("SELECT wsk_pub FROM workspaces WHERE workspace_id=?", WS_A)[0][0]
    scope = {
        "type": "http",
        "method": "PUT",
        "path": "/irrelevant",
        "raw_path": b"/different/\xff",
        "orch_target": f"/v1/ws/{WS_A}/shares/{SID}/parts/0/f",
        "orch_ws": WS_A,
        "orch_wsk": wsk,
        "path_params": {"ws": WS_A, "sid": "2" * 32},
    }
    request = Request(scope)
    request._body = b"ab"
    response = asyncio.run(endpoint("2" * 32, "9", "different", request))
    assert response.status_code == 200
    assert partial(two).read_bytes() == b"ab"
    scope["orch_target"] = scope["orch_target"].replace(WS_A, "f" * 32)
    with pytest.raises(shares.Refusal) as exc:
        asyncio.run(endpoint(SID, "0", "f", request))
    assert exc.value.body() == {"error": "wrong_workspace"}


def test_non_ascii_raw_path_is_404(two):
    async def raw_app(scope, receive, send):
        scope["raw_path"] = f"/v1/ws/{WS_A}/shares/{SID}/parts/0/".encode() + b"\xff"
        await two.app(scope, receive, send)

    response = TestClient(raw_app).put(f"/v1/ws/{WS_A}/shares/{SID}/parts/0/f", content=b"a")
    assert response.status_code == 404
    assert response.json() == {"error": "not_found"}


@pytest.mark.parametrize("offset", [0, 2, 6])
def test_zero_length_recovers_interrupted_finalize(two, offset):
    stage(two)
    partial(two).write_bytes(b"abcdef")
    assert part(two, offset, b"").json() == {"received": 6, "done": True, "result": "stored"}
    assert part(two, offset, b"").json() == {"received": 6, "done": True, "result": "unchanged"}
