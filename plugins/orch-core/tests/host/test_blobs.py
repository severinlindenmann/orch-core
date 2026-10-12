"""H4a2: files and blobs (§0.3 ``files``, §0.10 ``blob.*``, quotas F9) on a served workspace, and the blob store and
the host quota as units."""

from __future__ import annotations

import base64
import hashlib
import json
import os

import pytest

from orch.host.blobs import BLOB_MAX, CHUNK_MAX, EXPIRE_SECONDS, MIB, MIN_CHARGE, UID_BLOBS, Blob, BlobStore, Quota
from orch.host.control_client import new_idem
from orch.host.requests import RequestStore
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, UID, Served, err, ok
from tests.host.human_helpers import Human

lab, served = human_helpers.lab, human_helpers.served

KEY = "a_01J9ZP0000000000000000000K"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def put(c, *, grant=None, **args):
    return c.req("blob.put", args, grant=grant)


def upload(c, data: bytes, *, grant=None, key: str | None = None, chunk: int = CHUNK_MAX) -> str:
    """The whole of ``data`` as one blob, in chunks; returns its id."""
    first = data[:chunk]
    res = ok(
        put(
            c,
            grant=grant,
            offset=0,
            b64=b64(first),
            size=len(data),
            upload_key=key or new_idem(),
            last=len(first) == len(data),
            **({"sha256": sha(data)} if len(first) == len(data) else {}),
        )
    )["data"]
    blob, pos = res["blob"], len(first)
    while pos < len(data):
        part = data[pos : pos + chunk]
        last = pos + len(part) == len(data)
        args = {"blob": blob, "offset": pos, "b64": b64(part), "last": last, **({"sha256": sha(data)} if last else {})}
        ok(put(c, grant=grant, **args))
        pos += len(part)
    return blob


def add(served: Served, c, key: str, files: dict, *, name: str = "out.log", **kw):
    return c.req(
        "artifact.add",
        {"ref": key, "path": name},
        grant=served.grant,
        session=SESSION,
        idem=kw.pop("idem", True),
        files=files,
        **kw,
    )


def quota(served: Served) -> tuple[int, int, dict[int, int]]:
    return served.h.host.blob_quota.usage()


def tick(served: Served, seconds: int) -> None:
    served.lab.ws.clock[0] += seconds


# --- chunks -----------------------------------------------------------------------------------------------------------


def test_a_chunk_retry_with_the_same_bytes_is_accepted_with_different_bytes_refused(served):
    c = served.client()
    data = os.urandom(3000)
    first = ok(put(c, offset=0, b64=b64(data[:1000]), size=3000, upload_key=new_idem(), last=False))["data"]
    blob = first["blob"]
    assert first == {"blob": blob, "size": 3000, "received": 1000}
    second = {"blob": blob, "offset": 1000, "b64": b64(data[1000:2000]), "last": False}
    assert ok(put(c, **second))["data"]["received"] == 2000
    # the same chunk again: accepted without effect, and received says where to continue
    assert ok(put(c, **second))["data"] == {"blob": blob, "size": 3000, "received": 2000}
    # the same offset with other bytes, or a chunk overlapping the received end with other bytes: refused
    other = bytes(b ^ 1 for b in data[1000:2000])
    assert err(put(c, **{**second, "b64": b64(other)})) == "blob.invalid"
    assert err(put(c, blob=blob, offset=1500, b64=b64(other[:1000]), last=False)) == "blob.invalid"
    # a gap is refused; an overlapping chunk whose old part matches is accepted and continues
    assert err(put(c, blob=blob, offset=2500, b64=b64(data[2500:]), last=False)) == "blob.invalid"
    done = ok(put(c, blob=blob, offset=1500, b64=b64(data[1500:]), last=True, sha256=sha(data)))["data"]
    assert done == {"blob": blob, "size": 3000, "received": 3000}
    assert served.service.blobs.read(f"uid:{UID}", blob) == data


def test_a_wrong_final_digest_is_refused_and_leaves_the_blob_as_it_was(served):
    c = served.client()
    data = b"final digest" * 10
    blob = ok(put(c, offset=0, b64=b64(data[:60]), size=len(data), upload_key=new_idem(), last=False))["data"]["blob"]
    wrong = sha(data + b"x")
    assert err(put(c, blob=blob, offset=60, b64=b64(data[60:]), last=True, sha256=wrong)) == "blob.invalid"
    assert err(put(c, blob=blob, offset=60, b64=b64(data[60:]), last=True)) == "blob.invalid"  # no digest at all
    # nothing of the refused chunk was kept: the right digest completes it from the same offset
    assert served.service.blobs.digest(f"uid:{UID}", blob) is None
    assert ok(put(c, blob=blob, offset=60, b64=b64(data[60:]), last=True, sha256=sha(data)))["data"]["received"] == 120
    assert served.service.blobs.digest(f"uid:{UID}", blob) == sha(data)
    # a one-chunk upload with a wrong digest is refused and reserves nothing
    before = quota(served)[0]
    res = put(c, offset=0, b64=b64(b"abc"), size=3, upload_key=new_idem(), last=True, sha256=sha(b"abd"))
    assert err(res) == "blob.invalid" and quota(served)[0] == before
    # last needs the total to equal size
    blob2 = ok(put(c, offset=0, b64=b64(b"ab"), size=5, upload_key=new_idem(), last=False))["data"]["blob"]
    assert err(put(c, blob=blob2, offset=2, b64=b64(b"c"), last=True, sha256=sha(b"abc"))) == "blob.invalid"
    assert err(put(c, blob=blob2, offset=2, b64=b64(b"cdef"), last=False)) == "blob.invalid"  # past size


def test_a_repeated_final_chunk_returns_the_same_result(served):
    c = served.client()
    data = os.urandom(2000)
    blob = ok(put(c, offset=0, b64=b64(data[:1000]), size=2000, upload_key=new_idem(), last=False))["data"]["blob"]
    final = {"blob": blob, "offset": 1000, "b64": b64(data[1000:]), "last": True, "sha256": sha(data)}
    first = ok(put(c, **final))
    again = ok(put(c, **final))
    assert again == first and again["data"] == {"blob": blob, "size": 2000, "received": 2000}
    # the blob is immutable: other final bytes, or another digest, are refused
    assert err(put(c, **{**final, "b64": b64(os.urandom(1000))})) == "blob.invalid"
    assert err(put(c, **{**final, "sha256": sha(b"other")})) == "blob.invalid"


def test_a_zero_byte_blob_is_one_chunk_and_can_be_stored(served):
    c = served.client()
    _, key = served.ticket()
    res = ok(put(c, grant=served.grant, offset=0, b64="", size=0, upload_key=new_idem(), last=True, sha256=sha(b"")))
    blob = res["data"]["blob"]
    assert res["data"] == {"blob": blob, "size": 0, "received": 0}
    assert (
        ok(put(c, grant=served.grant, blob=blob, offset=0, b64="", last=True, sha256=sha(b"")))["data"] == res["data"]
    )
    added = ok(add(served, c, key, {"path": {"blob": blob}}, name="empty.log"))
    assert added["data"]["bytes"] == 0 and added["data"]["sha256"] == sha(b"")


def test_a_lost_first_chunk_response_is_recovered_with_the_same_upload_key(served):
    c = served.client()
    data = os.urandom(1500)
    first = {"offset": 0, "b64": b64(data[:500]), "size": 1500, "upload_key": KEY, "last": False}
    blob = ok(put(c, **first))["data"]["blob"]
    ok(put(c, blob=blob, offset=500, b64=b64(data[500:1000]), last=False))
    # the client never saw the first answer: it resends the first chunk with the same key and learns where to go on
    assert ok(put(c, **first))["data"] == {"blob": blob, "size": 1500, "received": 1000}
    assert ok(served.client().req("blob.put", first))["data"]["blob"] == blob  # a reconnect too (same principal)
    assert err(put(c, **{**first, "b64": b64(os.urandom(500))})) == "blob.invalid"  # other bytes at 0
    assert err(put(c, **{**first, "size": 1501})) == "blob.invalid"  # another upload under the same key
    # the key is the principal's: another principal using it allocates its own blob
    assert ok(put(c, grant=served.grant, **first))["data"]["blob"] != blob
    ok(put(c, blob=blob, offset=1000, b64=b64(data[1000:]), last=True, sha256=sha(data)))
    assert served.service.blobs.digest(f"uid:{UID}", blob) == sha(data)


def test_a_chunk_is_at_most_768_kib_and_a_blob_at_most_64_mib(served):
    c = served.client()
    big = b64(b"\0" * (CHUNK_MAX + 1))
    assert err(put(c, offset=0, b64=big, size=CHUNK_MAX + 1, upload_key=new_idem(), last=False)) == "blob.invalid"
    assert err(put(c, offset=0, b64="", size=BLOB_MAX + 1, upload_key=new_idem(), last=False)) == "invalid.input"
    assert err(put(c, offset=0, b64="", size=1, last=False)) == "blob.invalid"  # no upload_key
    assert err(put(c, offset=0, b64="", upload_key=new_idem(), last=False)) == "blob.invalid"  # no size
    assert err(put(c, offset=3, b64="", size=4, upload_key=new_idem(), last=False)) == "blob.invalid"
    assert err(c.req("blob.put", {"offset": 0, "b64": "", "last": True, "size": 0}, dry_run=True)) == "invalid.input"
    assert err(c.req("blob.drop", {"blob": "../../x"})) == "invalid.input"
    assert ok(c.req("blob.drop", {"blob": "b_" + "A" * 26}))["data"] == {}  # unknown: nothing to say


def test_a_chunk_carrying_a_grant_secret_is_refused(served):
    c = served.client()
    data = b"token " + served.grant.encode()
    res = put(c, offset=0, b64=b64(data), size=len(data), upload_key=new_idem(), last=True, sha256=sha(data))
    assert err(res) == "grant.secret_in_args"


# --- ownership (F9) ---------------------------------------------------------------------------------------------------


def test_a_blob_of_another_principal_answers_exactly_as_an_unknown_one(served):
    _, key = served.ticket()
    c = served.client()
    data = b"mine\n"
    mine = upload(c, data)  # principal uid:<n>, no grant
    unknown = "b_" + "A" * 26
    # used by the grant's principal (same UID, another principal): blob.invalid, the same answer as an unknown id
    foreign = add(served, c, key, {"path": {"blob": mine}})
    missing = add(served, c, key, {"path": {"blob": unknown}})
    assert err(foreign) == err(missing) == "blob.invalid"
    assert foreign["error"]["message"] == missing["error"]["message"]
    for blob in (mine, unknown):
        res = put(c, grant=served.grant, blob=blob, offset=0, b64=b64(data), last=True, sha256=sha(data))
        assert err(res) == "blob.invalid"
    ok(c.req("blob.drop", {"blob": mine}, grant=served.grant))  # says nothing, drops nothing
    assert served.service.blobs.digest(f"uid:{UID}", mine) == sha(data)
    # and the grant's own blob works
    theirs = upload(c, data, grant=served.grant)
    assert ok(add(served, c, key, {"path": {"blob": theirs}}))["data"]["sha256"] == sha(data)


def test_a_blob_of_another_workspace_is_unknown_there(tmp_path):
    os.mkdir(tmp_path / "s", 0o700)
    now = [1000.0]
    q = Quota(clock=lambda: now[0])
    a = BlobStore(tmp_path / "s", "w" * 32, os.getuid(), clock=lambda: now[0], quota=q)
    b = BlobStore(tmp_path / "s", "v" * 32, os.getuid(), clock=lambda: now[0], quota=q)
    a.open()
    b.open()
    try:
        data = b"cross"
        args = {"offset": 0, "b64": b64(data), "size": 5, "upload_key": KEY, "last": True, "sha256": sha(data)}
        blob = a.put("uid:1", 1, True, args)["blob"]
        assert a.digest("uid:1", blob) == sha(data)
        assert b.digest("uid:1", blob) is None  # same principal, other workspace: unknown
        with pytest.raises(Exception) as e:
            b.put("uid:1", 1, True, {"blob": blob, "offset": 0, "b64": b64(data), "last": True, "sha256": sha(data)})
        assert getattr(e.value, "code", None) == "blob.invalid"
        assert b.put("uid:1", 1, True, args)["blob"] != blob  # the upload key does not cross either
        assert (tmp_path / "s" / "hosts" / ("w" * 32) / "blobs" / blob).stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "s" / "hosts" / ("w" * 32) / "blobs").stat().st_mode & 0o777 == 0o700
    finally:
        a.close()
        b.close()


# --- quotas (F9) ------------------------------------------------------------------------------------------------------


def test_the_per_uid_quota_counts_every_principal_and_frees_on_drop(served):
    c = served.client()

    def reserve(grant=None) -> dict:
        return put(c, grant=grant, offset=0, b64="", size=BLOB_MAX, upload_key=new_idem(), last=False)

    blobs = [ok(reserve())["data"]["blob"], ok(reserve())["data"]["blob"]]
    blobs += [ok(reserve(served.grant))["data"]["blob"], ok(reserve(served.grant))["data"]["blob"]]
    assert quota(served)[2][UID] == 4 * BLOB_MAX  # 256 MiB reserved at the first chunks, nothing written
    for grant in (None, served.grant):
        res = reserve(grant)
        assert err(res) == "quota.host" and res["error"]["retryable"] is True
    ok(c.req("blob.drop", {"blob": blobs[0]}))
    assert ok(reserve(served.grant))
    assert err(put(c, offset=0, b64="", size=1, upload_key=new_idem(), last=False)) == "quota.host"


def test_the_host_quota_keeps_a_reserve_for_person_class_uids():
    now = [0.0]
    q = Quota(clock=lambda: now[0])

    def blob(uid: int, person: bool, size: int = BLOB_MAX) -> Blob:
        return Blob(f"b_{uid}_{len(q._blobs)}", "w", f"uid:{uid}", uid, person, size, None, now[0])

    for uid in (2001, 2002, 2003):  # three agent-class UIDs fill what agents may use: 768 MiB
        for _ in range(4):
            q.reserve(blob(uid, False))
    with pytest.raises(Exception) as e:
        q.reserve(blob(2004, False))
    assert e.value.code == "quota.host"  # type: ignore[attr-defined]
    for _ in range(4):  # the 256 MiB person reserve is still there
        q.reserve(blob(501, True))
    with pytest.raises(Exception) as e:
        q.reserve(blob(502, True, 1))
    assert e.value.code == "quota.host"  # type: ignore[attr-defined]  # 1 GiB host-wide
    assert q.usage()[0] == 1024 * MIB and q.usage()[1] == 768 * MIB
    now[0] += EXPIRE_SECONDS  # expired reservations free their bytes
    assert q.usage() == (0, 0, {})
    q.reserve(blob(2004, False))


def requests_bytes(served: Served) -> int:
    d = served.service.requests.dir
    return sum((d / n).stat().st_size for n in os.listdir(d))


def artifacts_in(served: Served, request_id: str) -> list:
    record = json.loads((served.service.requests.dir / request_id).read_text())
    return [e.get("artifacts") for e in record["extras"] if e.get("artifacts")]


def on_worker(served: Served, fn, *args):
    return served.service.submit(fn, *args).result(timeout=30)


def test_a_prepare_consumes_its_blob_and_charges_the_request_bytes_to_the_preparing_uid(served):
    human = Human(served)
    _, key = served.ticket()
    data = os.urandom(600_000)
    blob = upload(human.c, data)
    tick(served, 300)
    prepared = human.prepare("artifact.add", {"ref": key, "path": "p0.bin"}, files={"path": {"blob": blob}})
    rid = prepared["request"]["id"]
    assert served.service.blobs.digest(f"uid:{UID}", blob) is None  # consumed: the record holds the bytes now
    tick(served, 400)  # past the blob's own 10 minutes, within the request's
    assert quota(served)[2][UID] == len(data)  # the record's bytes, charged to the preparing UID
    # one blob cannot feed a second request, nor a direct write after the prepare
    for i in range(1, 5):
        res = human.prepare_res("artifact.add", {"ref": key, "path": f"p{i}.bin"}, files={"path": {"blob": blob}})
        assert err(res) == "blob.invalid"
    direct = human.c.req(
        "artifact.add", {"ref": key, "path": "b.bin"}, session=SESSION, idem=True, files={"path": {"blob": blob}}
    )
    assert err(direct) == "blob.invalid" and quota(served)[2][UID] == len(data)
    assert len(os.listdir(served.service.requests.dir)) == 1
    ok(human.submit_res(rid, human.sign(prepared).events))
    assert quota(served)[2].get(UID, 0) == 0  # submitted: the record is terminal and keeps no file bytes
    assert artifacts_in(served, rid) == []
    assert served.lab.ws.events(key)[-1]["sha256"] == sha(data)


def test_prepare_and_cancel_in_a_loop_cannot_grow_the_disk_or_escape_the_quota(served):
    human = Human(served)
    _, key = served.ticket()
    data = os.urandom(400_000)
    for i in range(30):  # far more than the 20 pending, and past what the UID quota would hold without stripping
        blob = upload(human.c, data)
        prepared = human.prepare("artifact.add", {"ref": key, "path": f"c{i}.bin"}, files={"path": {"blob": blob}})
        assert quota(served)[2][UID] == len(data)
        ok(human.c.req("human.cancel", {"request_id": prepared["request"]["id"]}))
        assert quota(served)[2].get(UID, 0) == 0 and artifacts_in(served, prepared["request"]["id"]) == []
    assert requests_bytes(served) < 30 * 100_000  # 30 stripped records, not 30 copies of 400 KB


def test_an_expired_requests_bytes_count_until_they_are_stripped(served):
    human = Human(served)
    _, key = served.ticket()
    inline = os.urandom(300_000)  # inline bytes are a request's file bytes too
    prepared = human.prepare("artifact.add", {"ref": key, "path": "x.bin"}, files={"path": {"b64": b64(inline)}})
    rid = prepared["request"]["id"]
    assert quota(served)[2][UID] == len(inline)
    tick(served, 601)
    assert quota(served)[2][UID] == len(inline)  # expired, not yet stripped: still on disk, still counted
    on_worker(served, served.service.requests.sweep)
    assert quota(served)[2].get(UID, 0) == 0 and artifacts_in(served, rid) == []


def test_the_stored_requests_bytes_are_charged_again_at_open(served):
    human = Human(served)
    _, key = served.ticket()
    kept = human.prepare("artifact.add", {"ref": key, "path": "k.bin"}, files={"path": {"b64": b64(b"k" * 5000)}})
    done = human.prepare("artifact.add", {"ref": key, "path": "d.bin"}, files={"path": {"b64": b64(b"d" * 7000)}})
    ok(human.c.req("human.cancel", {"request_id": done["request"]["id"]}))
    requests = RequestStore(served.lab.state, served.wid, UID, clock=lambda: served.lab.ws.clock[0])
    requests.open()
    assert list(requests.held()) == [(kept["request"]["id"], f"uid:{UID}", 5000)]
    q = Quota(clock=lambda: served.lab.ws.clock[0])
    store = BlobStore(served.lab.tmp / "other-state", served.wid, UID, clock=lambda: 0.0, quota=q)
    store.hold_request(kept["request"]["id"], f"uid:{UID}", UID, 5000, force=True)
    assert q.usage()[2] == {UID: 5000}


def test_pending_does_not_read_the_records(served, monkeypatch):
    human = Human(served)
    _, key = served.ticket()
    for i in range(3):
        human.prepare("artifact.add", {"ref": key, "path": f"r{i}.bin"}, files={"path": {"b64": b64(b"x" * 1000)}})
    requests = served.service.requests
    reads = []
    real = type(requests).read
    monkeypatch.setattr(type(requests), "read", lambda self, rid: reads.append(rid) or real(self, rid))
    assert on_worker(served, requests.pending, served.lab.owner) == 3
    assert reads == []  # from the index
    tick(served, 601)
    assert on_worker(served, requests.pending, served.lab.owner) == 0  # due ones are read once and expired
    assert len(reads) == 3


# --- consumption and expiry -------------------------------------------------------------------------------------------


def test_a_blob_is_consumed_on_commit_and_kept_after_a_refusal_or_a_dry_run(served):
    _, key = served.ticket()
    c = served.client()
    data = b"log line\n" * 100
    blob = upload(c, data, grant=served.grant)
    files = {"path": {"blob": blob}}
    # a refusal (no such ticket) keeps it
    assert err(add(served, c, "DEMO-9999", files)) in {"not_found", "invalid.input"}
    # a dry run keeps it
    ok(add(served, c, key, files, dry_run=True, idem=None))
    assert served.service.blobs.digest(f"g:{served.lab.ws.grant_id}@{UID}", blob) == sha(data)
    # the commit consumes it
    idem = new_idem()
    done = ok(add(served, c, key, files, idem=idem))
    assert done["data"]["sha256"] == sha(data)
    assert served.service.blobs.digest(f"g:{served.lab.ws.grant_id}@{UID}", blob) is None
    assert err(add(served, c, key, files, name="again.log")) == "blob.invalid"
    # a retry after the commit names the file by its digest and replays
    replay = ok(add(served, c, key, {"path": {"digest": sha(data)}}, idem=idem))
    assert replay["duplicate"] is True and replay["seq"] == done["seq"]
    assert quota(served)[2].get(UID, 0) == 0


def test_an_unused_or_incomplete_blob_expires_ten_minutes_after_its_last_chunk(served):
    _, key = served.ticket()
    c = served.client()
    data = b"x" * 100
    blob = upload(c, data, grant=served.grant)
    part = ok(put(c, grant=served.grant, offset=0, b64=b64(data[:10]), size=100, upload_key=KEY, last=False))
    tick(served, EXPIRE_SECONDS - 1)
    ok(put(c, grant=served.grant, blob=part["data"]["blob"], offset=10, b64=b64(data[10:20]), last=False))
    tick(served, 2)  # the complete blob is past 10 minutes, the incomplete one had a chunk 2 s ago
    assert err(add(served, c, key, {"path": {"blob": blob}})) == "blob.invalid"
    assert quota(served)[2][UID] == MIN_CHARGE  # 100 bytes count as 4 KiB
    ok(put(c, grant=served.grant, blob=part["data"]["blob"], offset=20, b64=b64(data[20:30]), last=False))
    tick(served, EXPIRE_SECONDS)
    res = put(c, grant=served.grant, blob=part["data"]["blob"], offset=30, b64=b64(data[30:]), last=False)
    assert err(res) == "blob.invalid"
    assert quota(served)[2].get(UID, 0) == 0
    assert os.listdir(served.service.blobs.dir) == []


# --- files on requests ------------------------------------------------------------------------------------------------


def test_an_x_file_argument_without_its_files_entry_is_refused(served):
    human = Human(served)
    _, key = served.ticket()
    c = served.client()
    res = c.req("artifact.add", {"ref": key, "path": "/etc/hosts"}, grant=served.grant, session=SESSION, idem=True)
    assert err(res) == "blob.invalid"
    res = c.req("log", {"ref": key, "file": "-"}, grant=served.grant, session=SESSION, idem=True)
    assert err(res) == "blob.invalid"
    assert err(human.prepare_res("artifact.add", {"ref": key, "path": "proof.txt"})) == "blob.invalid"
    # a files entry for an argument that is not marked x-file
    files = {"name": {"b64": b64(b"x")}, "path": {"b64": b64(b"x")}}
    res = c.req(
        "artifact.add",
        {"ref": key, "path": "a", "name": "b"},
        grant=served.grant,
        session=SESSION,
        idem=True,
        files=files,
    )
    assert err(res) == "blob.invalid"
    # the text a file argument names comes through files as well
    res = c.req(
        "log",
        {"ref": key, "file": "-"},
        grant=served.grant,
        session=SESSION,
        idem=True,
        files={"file": {"b64": b64(b"from stdin")}},
    )
    assert ok(res)
    assert served.lab.ws.events(key)[-1]["text"] == "from stdin"


def test_inline_files_are_at_most_512_kib_per_request(served):
    _, key = served.ticket()
    c = served.client()
    big = os.urandom(512 * 1024 + 1)
    assert err(add(served, c, key, {"path": {"b64": b64(big)}})) == "blob.invalid"
    assert ok(add(served, c, key, {"path": {"b64": b64(big[:-1])}}))["data"]["bytes"] == 512 * 1024


def test_a_prepared_request_takes_only_the_preparing_principals_blobs(served):
    human = Human(served)
    _, key = served.ticket()
    foreign = upload(human.c, b"an agent's bytes\n", grant=served.grant)
    res = human.prepare_res("artifact.add", {"ref": key, "path": "p.txt"}, files={"path": {"blob": foreign}})
    assert err(res) == "blob.invalid"
    incomplete = ok(put(human.c, offset=0, b64=b64(b"ab"), size=3, upload_key=new_idem(), last=False))["data"]["blob"]
    res = human.prepare_res("artifact.add", {"ref": key, "path": "p.txt"}, files={"path": {"blob": incomplete}})
    assert err(res) == "blob.invalid"
    res = human.prepare_res("artifact.add", {"ref": key, "path": "p.txt"}, files={"path": {"digest": sha(b"x")}})
    assert err(res) == "blob.invalid"
    mine = upload(human.c, b"the person's bytes\n")
    prepared = human.prepare("artifact.add", {"ref": key, "path": "p.txt"}, files={"path": {"blob": mine}})
    assert prepared["events"][0]["sha256"] == sha(b"the person's bytes\n")
    ok(human.c.req("human.cancel", {"request_id": prepared["request"]["id"]}))
    assert served.service.blobs.digest(f"uid:{UID}", mine) is None  # cancelled: the blob it held went with it


def test_the_blob_directory_is_private_and_no_handler_root(served):
    blobs = served.service.blobs.dir
    assert blobs == served.lab.state / "hosts" / served.wid / "blobs"
    assert blobs.stat().st_mode & 0o777 == 0o700
    roots = served.service.capabilities().files.roots
    assert all(not str(blobs).startswith(str(r) + "/") for r in roots)
    c = served.client()
    blob = upload(c, b"secret-ish")
    assert (blobs / blob).stat().st_mode & 0o777 == 0o600
    assert json.dumps(served.client().welcome["limits"]).count(str(BLOB_MAX)) == 1


# --- review round (#405) ----------------------------------------------------------------------------------------------


def test_tiny_blobs_cost_at_least_4_kib_and_a_uid_has_at_most_256_live_ones(served):
    c = served.client()
    empty = {"offset": 0, "b64": "", "size": 0, "last": True, "sha256": sha(b"")}
    refused = None
    for i in range(6000):
        res = put(c, upload_key=new_idem(), **empty)
        if not res["ok"]:
            refused = i
            break
    assert refused == UID_BLOBS and err(res) == "quota.host"
    assert len(os.listdir(served.service.blobs.dir)) == UID_BLOBS
    assert quota(served)[2][UID] == UID_BLOBS * MIN_CHARGE
    tick(served, EXPIRE_SECONDS)  # they expire, and the count frees
    assert ok(put(c, upload_key=new_idem(), **empty))


def test_a_resend_without_effect_does_not_extend_a_blobs_life(served):
    c = served.client()
    data = b"keep me"
    final = {"offset": 0, "b64": b64(data), "size": len(data), "last": True, "sha256": sha(data)}
    blob = ok(put(c, upload_key=KEY, **final))["data"]["blob"]
    tick(served, EXPIRE_SECONDS - 10)
    ok(put(c, blob=blob, **{k: v for k, v in final.items() if k != "size"}))
    tick(served, 20)
    assert served.service.blobs.digest(f"uid:{UID}", blob) is None


def test_a_grant_secret_split_across_chunks_is_refused_when_the_blob_is_used(served):
    _, key = served.ticket()
    c = served.client()
    data = b"see " + served.grant.encode() + b" here\n"
    cut = 4 + len(served.grant) // 2
    g = served.grant
    first = put(c, grant=g, offset=0, b64=b64(data[:cut]), size=len(data), upload_key=new_idem(), last=False)
    blob = ok(first)["data"]["blob"]  # neither half carries the grant
    ok(put(c, grant=g, blob=blob, offset=cut, b64=b64(data[cut:]), last=True, sha256=sha(data)))
    for op, args, arg in (
        ("log", {"ref": key, "file": "notes.txt"}, "file"),
        ("artifact.add", {"ref": key, "path": "s.log"}, "path"),
    ):
        res = c.req(op, args, grant=served.grant, session=SESSION, idem=True, files={arg: {"blob": blob}})
        assert err(res) == "grant.secret_in_args" and served.grant not in json.dumps(res), (op, res)
    assert all(served.grant not in json.dumps(e) for e in served.lab.ws.events(key))


def test_the_person_reserve_end_to_end(served):
    from tests.host.test_dispatch_matrix import as_agent_class

    q = served.h.host.blob_quota
    q.host, q.person_reserve = 3 * MIB, 1 * MIB  # agents may use 2 MiB of this host's 3
    registry = served.h.host.registry
    person_entry = registry.get(served.wid)
    as_agent_class(served)
    agent = served.client()
    assert ok(put(agent, offset=0, b64="", size=2 * MIB, upload_key=new_idem(), last=False))
    assert err(put(agent, offset=0, b64="", size=1, upload_key=new_idem(), last=False)) == "quota.host"
    with registry._lock:
        registry._entries[served.wid] = person_entry  # back to a person-class peer: the reserve is open to it
    person = served.client()
    assert ok(put(person, offset=0, b64="", size=MIB // 2, upload_key=new_idem(), last=False))


def test_a_replay_of_a_committed_write_consumes_the_blob_it_names(served):
    _, key = served.ticket()
    c = served.client()
    data = b"replayed\n"
    idem = new_idem()
    first = ok(add(served, c, key, {"path": {"blob": upload(c, data, grant=served.grant)}}, idem=idem))
    again = upload(c, data, grant=served.grant)  # the client lost the answer and uploaded once more
    replay = ok(add(served, c, key, {"path": {"blob": again}}, idem=idem))
    assert replay["duplicate"] is True and replay["seq"] == first["seq"]
    assert served.service.blobs.digest(f"g:{served.lab.ws.grant_id}@{UID}", again) is None


def test_closing_a_workspace_removes_its_blob_files(served):
    c = served.client()
    upload(c, b"one")
    upload(c, b"two")
    assert len(os.listdir(served.service.blobs.dir)) == 2
    on_worker(served, served.service.blobs.close)
    assert os.listdir(served.service.blobs.dir) == [] and quota(served)[2].get(UID, 0) == 0


def test_a_stored_request_with_an_unparseable_principal_is_skipped_at_open(served, caplog):
    from orch.host.workspace import principal_uid

    assert principal_uid(f"uid:{UID}") == UID and principal_uid(f"g:gr_X@{UID}") == UID
    assert principal_uid("bogus") is None and principal_uid("uid:x") is None
    human = Human(served)
    _, key = served.ticket()
    good = human.prepare("artifact.add", {"ref": key, "path": "g.bin"}, files={"path": {"b64": b64(b"g" * 3000)}})
    bad = human.prepare("artifact.add", {"ref": key, "path": "b.bin"}, files={"path": {"b64": b64(b"b" * 5000)}})
    path = served.service.requests.dir / bad["request"]["id"]
    record = json.loads(path.read_text())
    on_worker(served, served.service.requests.write, {**record, "principal": "not a principal"})
    with caplog.at_level("ERROR"):
        on_worker(served, served.service.open)
    assert served.service.state == "ok"
    assert quota(served)[2] == {UID: 3000}  # the good request is charged again; the bad one is skipped
    assert any(bad["request"]["id"] in r.getMessage() for r in caplog.records)
    assert good["request"]["id"]
