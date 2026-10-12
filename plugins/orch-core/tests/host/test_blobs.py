"""H4a2: files and blobs (§0.3 ``files``, §0.10 ``blob.*``, quotas F9) on a served workspace, and the blob store and
the host quota as units."""

from __future__ import annotations

import base64
import hashlib
import json
import os

import pytest

from orch.host.blobs import BLOB_MAX, CHUNK_MAX, EXPIRE_SECONDS, MIB, BlobStore, Quota
from orch.host.control_client import new_idem
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
    from orch.host.blobs import Blob

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


def test_a_blob_held_by_a_prepared_request_counts_against_the_preparing_uid(served):
    human = Human(served)
    _, key = served.ticket()
    data = b"evidence for a person\n"
    blob = upload(human.c, data)
    tick(served, 300)
    prepared = human.prepare("artifact.add", {"ref": key, "path": "proof.txt"}, files={"path": {"blob": blob}})
    tick(served, 400)  # past the blob's own 10 minutes, within the request's
    assert quota(served)[2][UID] == len(data)  # held with the request, counted against the preparing UID
    ok(human.c.req("blob.drop", {"blob": blob}))  # the request's now: a drop leaves it
    assert quota(served)[2][UID] == len(data)
    ok(human.submit_res(prepared["request"]["id"], human.sign(prepared).events))
    assert quota(served)[2].get(UID, 0) == 0  # submitted: consumed
    assert served.lab.ws.events(key)[-1]["sha256"] == sha(data)


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
    assert quota(served)[2][UID] == 100
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
