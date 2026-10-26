"""H4a2: ``artifact.get`` end to end (§0.8): chunked reads by the manifest's digest, the chunk caps, the evidence digest
``ticket.get`` shows (H6b checks one against the other), and visibility (no-person, G, P; a restricted ticket is
``not_found`` exactly as a missing one, F6)."""

from __future__ import annotations

import base64
import hashlib
import json
import os

from orch.host.artifacts import CHUNK
from tests.host import human_helpers
from tests.host.dispatch_helpers import Served, err, ok
from tests.host.test_blobs import add, sha, upload
from tests.host.test_dispatch_matrix import as_agent_class
from tests.host.test_dispatch_reads import norm

lab, served = human_helpers.lab, human_helpers.served


def get(c, ref: str, name: str, offset: int = 0, *, grant: str | None = None, **kw):
    return c.req("artifact.get", {"ref": ref, "name": name, "offset": offset, **kw}, grant=grant)


def read_all(c, ref: str, name: str, grant: str | None = None) -> tuple[bytes, list[dict]]:
    parts, offset = [], 0
    while True:
        part = ok(c.req("artifact.get", {"ref": ref, "name": name, "offset": offset}, grant=grant))["data"]
        parts.append(part)
        offset += len(base64.b64decode(part["b64"]))
        if part["last"]:
            return b"".join(base64.b64decode(p["b64"]) for p in parts), parts


def stored(served: Served, data: bytes, name: str = "big.log", title: str = "A ticket") -> tuple[str, str]:
    uid, key = served.ticket(title)
    c = served.client()
    blob = upload(c, data, grant=served.grant)
    ok(add(served, c, key, {"path": {"blob": blob}}, name=name))
    return uid, key


def test_a_large_artifact_is_read_in_chunks_by_its_recorded_digest(served):
    data = os.urandom(2 * CHUNK + 1234)  # over the 512 KiB inline limit: it came as a blob
    uid, key = stored(served, data)
    c = served.client()
    got, parts = read_all(c, key, "big.log")
    assert got == data
    assert [len(base64.b64decode(p["b64"])) for p in parts] == [CHUNK, CHUNK, 1234]
    assert [p["offset"] for p in parts] == [0, CHUNK, 2 * CHUNK] and [p["last"] for p in parts] == [False, False, True]
    assert {p["size"] for p in parts} == {len(data)}
    # the digest is the manifest's: the evidence digest of the logged event (what ticket.get shows, H6b)
    event = served.lab.ws.events(key)[-1]
    assert event["type"] == "artifact.added" and {p["digest"] for p in parts} == {event["sha256"]} == {sha(data)}
    # length picks the chunk; offset = size is an empty last chunk; past it is invalid.input
    part = ok(get(c, key, "big.log", 10, length=5))["data"]
    assert base64.b64decode(part["b64"]) == data[10:15] and part["last"] is False
    end = ok(get(c, key, "big.log", len(data)))["data"]
    assert end["b64"] == "" and end["last"] is True and end["offset"] == len(data)
    assert err(get(c, key, "big.log", len(data) + 1)) == "invalid.input"


def test_the_chunk_caps_and_the_arguments(served):
    _, key = stored(served, b"small\n", name="s.log")
    c = served.client()
    assert err(get(c, key, "s.log", length=CHUNK + 1)) == "invalid.input"
    assert err(get(c, key, "s.log", length=0)) == "invalid.input"
    assert err(get(c, key, "s.log", -1)) == "invalid.input"
    assert err(c.req("artifact.get", {"ref": key, "name": "s.log", "offset": 0, "path": "/etc"})) == "invalid.input"
    assert err(c.req("artifact.get", {"ref": key, "name": "s.log", "offset": 0}, dry_run=True)) == "invalid.input"
    files = {"path": {"b64": "eA=="}}
    assert err(c.req("artifact.get", {"ref": key, "name": "s.log", "offset": 0}, files=files)) == "blob.invalid"
    assert ok(get(c, key, "s.log", length=CHUNK))["data"]["last"] is True


def test_a_name_is_resolved_through_the_manifest_never_joined_to_a_path(served):
    uid, key = stored(served, b"real\n", name="real.log")
    c = served.client()
    missing = get(c, key, "nope.log")
    assert err(missing) == "not_found"
    for name in ("../ticket.json", "../../config.json", "/etc/hosts", ".hidden", "a/b"):
        res = get(c, key, name)
        assert res == {**missing, "id": res["id"]}, name
    # a file in artifacts/ the log does not name is not served either
    (served.lab.ws.store_dir / "tickets" / uid / "artifacts" / "planted.log").write_bytes(b"planted")
    assert err(get(c, key, "planted.log")) == "not_found"


def test_a_file_changed_or_swapped_behind_the_log_is_not_served(served):
    uid, key = stored(served, b"original bytes\n", name="e.log")
    c = served.client()
    assert ok(get(c, key, "e.log"))
    path = served.lab.ws.store_dir / "tickets" / uid / "artifacts" / "e.log"
    path.write_bytes(b"tampered bytes\n")
    assert err(get(c, key, "e.log")) == "not_found"
    audit = served.lab.state / "host" / "audit-security.jsonl"  # a tamper signal is recorded (§0.11)
    line = json.loads(audit.read_text().splitlines()[-1])
    assert line["event"] == "artifact.mismatch" and line["ticket"] == uid and line["name"] == "e.log"
    assert line["expected"] == sha(b"original bytes\n") and line["workspace"] == served.wid
    assert audit.stat().st_mode & 0o777 == 0o600
    # the same version again: not_found without hashing it again, and no second audit line (no log growth)
    import orch.host.artifacts as artifacts

    hashed = []
    real = artifacts._hash
    artifacts._hash = lambda fd, size: hashed.append(size) or real(fd, size)
    try:
        for _ in range(20):
            assert err(get(c, key, "e.log")) == "not_found"
    finally:
        artifacts._hash = real
    assert hashed == [] and len(audit.read_text().splitlines()) == 1
    path.unlink()
    target = served.lab.ws.root / "elsewhere.log"
    target.write_bytes(b"original bytes\n")
    path.symlink_to(target)  # the same bytes through a symlink: never followed
    assert err(get(c, key, "e.log")) == "not_found"
    path.unlink()
    os.link(target, path)  # nor a hard link to a file elsewhere
    assert err(get(c, key, "e.log")) == "not_found"
    path.unlink()
    path.write_bytes(b"original bytes\n")
    assert base64.b64decode(ok(get(c, key, "e.log"))["data"]["b64"]) == b"original bytes\n"


def test_visibility_no_person_g_and_p(served):
    _, open_key = stored(served, b"open\n", name="o.log", title="open")
    uid, key = stored(served, b"hidden zebracorn\n", name="h.log", title="restricted")
    served.restrict(uid, [served.lab.owner])
    c = served.client()
    # P (the owner, on the list) and G (the grant's person, the owner) read it
    assert ok(get(c, key, "h.log"))["data"]["digest"] == sha(b"hidden zebracorn\n")
    assert ok(get(c, key, "h.log", grant=served.grant))
    as_agent_class(served)
    n = served.client()
    # N (no grant, agent class: no-person) reads a workspace ticket, never a restricted one
    assert ok(get(n, open_key, "o.log"))["data"]["digest"] == sha(b"open\n")
    hidden, missing = get(n, key, "h.log"), get(n, "DEMO-9999", "h.log")
    assert err(hidden) == "not_found" and norm(hidden, key) == norm(missing, "DEMO-9999")
    # a wrong grant secret is no identity: no-person too
    bad = served.grant[:-4] + ("AAAA" if not served.grant.endswith("AAAA") else "BBBB")
    assert norm(get(n, key, "h.log", grant=bad), key) == norm(get(n, "DEMO-9999", "h.log", grant=bad), "DEMO-9999")
    # G through the agent class still reads it (the grant's person is on the list)
    assert ok(get(n, key, "h.log", grant=served.grant))


def test_a_ticket_restricted_to_someone_else_is_not_found_for_p_and_g(served):
    other = served.lab.add_member()
    uid, key = stored(served, b"theirs\n", name="t.log", title="theirs")
    served.restrict(uid, [other.ref])
    c = served.client()
    for grant in (None, served.grant):
        hidden, missing = get(c, key, "t.log", grant=grant), get(c, "DEMO-9999", "t.log", grant=grant)
        assert err(hidden) == "not_found" and norm(hidden, key) == norm(missing, "DEMO-9999"), grant
    assert hashlib.sha256(b"theirs\n").hexdigest() not in str(get(c, key, "t.log"))


def test_the_audit_file_is_written_only_as_a_private_regular_file_of_the_host(tmp_path):
    from orch.host import securitylog

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    securitylog.record(state, os.getuid(), 1.0, "artifact.mismatch", name="a")
    audit = state / "host" / securitylog.NAME
    assert json.loads(audit.read_text())["name"] == "a"
    other = tmp_path / "elsewhere"
    other.write_text("")
    for plant in ("symlink", "hardlink", "mode"):
        audit.unlink()
        if plant == "symlink":
            audit.symlink_to(other)
        elif plant == "hardlink":
            os.link(other, audit)
        else:
            audit.write_text("")
            audit.chmod(0o644)
        securitylog.record(state, os.getuid(), 2.0, "artifact.mismatch", name="b")
        assert other.read_text() == "" and "b" not in (audit.read_text() if plant == "mode" else ""), plant
