"""H5c follow-up: the click signer's durable per-request sign record (orch-v2-host §0.8.6 item 5, "Durable sign
records"). A request is signed at most once per (request_id, view_digest), across a crash and a restart. The device
key lives in a FakeStore: never the login keychain."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import textwrap

import pytest

from orch import crypto
from orch.click import records as records_mod
from orch.click.records import RETENTION, Records
from tests.conftest import child_env
from tests.host import human_helpers
from tests.host.lifecycle_helpers import UID
from tests.host.test_click import Click, pending, refused
from tests.host.test_human import logged

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


class Crash(BaseException):
    """Stands in for the process dying: nothing after it runs, nothing catches it."""


@pytest.fixture
def click(served):
    c = Click(served)
    yield c
    c.close()


def _ready(click, key, text="x"):
    p = click.prepare("log", {"ref": key, "text": text})
    v = click.view(p["request_id"])
    click.mono.t += 2
    return p["request_id"], v["view_digest"]


def _record_path(served, rid):
    return served.lab.state / "person" / "click" / served.wid / f"{rid}.json"


def _restart(click, served):
    """A new signer process: empty registry, new connections, the same person state and key."""
    click.close()
    return Click(served)


def _texts(served, key):
    return [e["text"] for e in logged(served, key) if e["type"] == "log.added"]


def test_the_record_is_durable_private_and_written_before_the_submit(click, served, human):
    _, key = human.ticket()
    rid, vd = _ready(click, key)
    seen = {}

    real = click.signer.records.complete

    def create(*a, **kw):
        rec = real(*a, **kw)
        seen["record"] = json.loads(_record_path(served, rid).read_bytes())  # on disk when create returns
        return rec

    click.signer.records.complete = create

    click.signer.close()
    orig_connect = click.signer._connect

    def connect(entry):
        c = orig_connect(entry)
        real_call = c.call

        def call(op, args, **kw):
            if op == "human.submit":
                seen["at_submit"] = _record_path(served, rid).exists()
                seen["sent"] = args["events"]
                seen["idem"] = kw.get("idem")
            return real_call(op, args, **kw)

        c.call = call
        return c

    click.signer._connect = connect
    res = click.sign(rid, vd)
    assert res["result"]["key"] == key
    assert seen["at_submit"] is True
    assert seen["record"]["events"] == seen["sent"] and seen["record"]["idem"] == seen["idem"]
    assert seen["record"]["view_digest"] == vd and seen["record"]["result"] is None
    path = _record_path(served, rid)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for d in (path.parent, path.parent.parent, path.parent.parent.parent):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    rec = json.loads(path.read_bytes())
    assert rec["result"] == res["result"]  # settled after the commit
    # never key material: neither the scalar nor its encodings
    scalar = crypto.private_scalar(click.owner.dk_sig)
    raw = path.read_bytes()
    for enc in (scalar.hex(), crypto.b64u(scalar), scalar.hex().upper()):
        assert enc.encode() not in raw
    assert scalar not in raw
    assert set(rec) == records_mod.KEYS


def test_a_crash_after_the_record_resends_the_identical_events_after_a_restart(click, served, human):
    _, key = human.ticket()
    rid, vd = _ready(click, key)

    sent = []
    orig = click._connect

    def connect(entry):
        c = orig(entry)
        real = c.call

        def call(op, args, **kw):
            if op == "human.submit":
                sent.append((json.dumps(args, sort_keys=True), kw.get("idem")))
                raise Crash  # dies before the submit leaves
            return real(op, args, **kw)

        c.call = call
        return c

    click.signer._connect = connect
    click.signer.close()
    with pytest.raises(Crash):
        click.sign(rid, vd)
    assert click.signatures == 1
    recorded = json.loads(_record_path(served, rid).read_bytes())
    assert pending(served, rid)["state"] == "bound"  # nothing reached the host

    again = _restart(click, served)
    try:
        orig2 = again._connect

        def connect2(entry):
            c = orig2(entry)
            real = c.call

            def call(op, args, **kw):
                if op == "human.submit":
                    sent.append((json.dumps(args, sort_keys=True), kw.get("idem")))
                return real(op, args, **kw)

            c.call = call
            return c

        again.signer._connect = connect2
        res = again.sign(rid, vd)  # no prepare, no view: the record alone
        assert res["result"]["key"] == key
        assert again.signatures == 0  # nothing signed again
        assert len(set(sent)) == 1  # identical events and idem
        assert json.loads(sent[0][0])["events"] == recorded["events"] and sent[0][1] == recorded["idem"]
        assert _texts(served, key) == ["x"]
        # and once more: the recorded answer, no host submit
        n = len(sent)
        assert again.sign(rid, vd) == {"result": res["result"], "duplicate": True}
        assert len(sent) == n
    finally:
        again.close()


def test_another_view_for_a_signed_request_is_refused_across_a_restart(click, served, human):
    _, key = human.ticket()
    rid, vd = _ready(click, key)
    click.sign(rid, vd)
    other = "sha256:" + "1" * 64
    refused(lambda: click.sign(rid, other), "human.click_refused", "already_signed")
    again = _restart(click, served)
    try:
        refused(lambda: again.sign(rid, other), "human.click_refused", "already_signed")
        assert again.signatures == 0
        assert _texts(served, key) == ["x"]
    finally:
        again.close()


def test_a_second_view_before_the_submit_is_refused_across_a_restart(click, served, human):
    """Signed and recorded, then the process dies before the submit; after the restart the dashboard prepares the same
    request again, builds another View (a changed registry name) and signs that: refused, nothing new is signed."""
    _, key = human.ticket()
    idem = __import__("orch.host.control_client", fromlist=["new_idem"]).new_idem()
    args = {"workspace_id": served.wid, "op": "log", "args": {"ref": key, "text": "x"}, "idem": idem}
    p = click.signer.prepare(args)
    rid = p["request_id"]
    vd = click.view(rid)["view_digest"]
    click.mono.t += 2
    orig = click._connect

    def connect(entry):
        c = orig(entry)
        real = c.call

        def call(op, a, **kw):
            if op == "human.submit":
                raise Crash
            return real(op, a, **kw)

        c.call = call
        return c

    click.signer._connect = connect
    click.signer.close()
    with pytest.raises(Crash):
        click.sign(rid, vd)

    again = _restart(click, served)
    try:
        again.entry["prefix"] = "ZZ"  # the View names the prefix: another digest
        assert again.signer.prepare(args)["request_id"] == rid  # the host's idem: the same request
        vd2 = again.view(rid)["view_digest"]
        assert vd2 != vd
        again.mono.t += 2
        refused(lambda: again.sign(rid, vd2), "human.click_refused", "already_signed")
        assert again.signatures == 0
        assert pending(served, rid)["state"] == "bound"
        # the original review still resends the recorded events
        assert again.sign(rid, vd)["result"]["key"] == key
        assert again.signatures == 0 and _texts(served, key) == ["x"]
    finally:
        again.close()


@pytest.mark.parametrize("how", ["garbage", "mode", "symlink", "other_request"])
def test_an_unreadable_record_fails_closed(click, served, human, how):
    _, key = human.ticket()
    rid, vd = _ready(click, key)
    path = _record_path(served, rid)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for d in (path.parent, path.parent.parent, path.parent.parent.parent):
        os.chmod(d, 0o700)
    if how == "garbage":
        path.write_bytes(b"{not json")
        os.chmod(path, 0o600)
    elif how == "mode":
        path.write_bytes(b"{}")
        os.chmod(path, 0o644)
    elif how == "symlink":
        target = served.lab.state / "elsewhere.json"
        target.write_bytes(b"{}")
        os.symlink(target, path)
    else:
        rid2, vd2 = _ready(click, key, "y")
        click.sign(rid2, vd2)
        os.rename(_record_path(served, rid2), path)
    n = click.signatures
    refused(lambda: click.sign(rid, vd), "human.click_refused", "record_unavailable")
    assert click.signatures == n
    assert pending(served, rid)["state"] == "bound"


def test_a_record_that_cannot_be_written_submits_nothing(click, served, human):
    _, key = human.ticket()
    rid, vd = _ready(click, key)
    root = served.lab.state / "person"
    root.mkdir(exist_ok=True, mode=0o700)
    os.chmod(root, 0o755)  # not private: the store refuses it
    try:
        refused(lambda: click.sign(rid, vd), "human.click_refused", "record_unavailable")
    finally:
        os.chmod(root, 0o700)
    assert pending(served, rid)["state"] == "bound"  # the signatures never left
    assert _texts(served, key) == []
    # once writable again, signing works (nothing was recorded, nothing was sent)
    assert click.sign(rid, vd)["result"]["key"] == key
    assert _texts(served, key) == ["x"]


def test_records_are_pruned_after_the_request_retention(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    now = [1_800_000_000.0]
    rs = Records(state, wall=lambda: now[0])
    wid = "a" * 32
    rid_old, rid_new = "hr_" + "0" * 26, "hr_" + "1" * 26
    ev = [{"type": "log.added", "sig": "x"}]
    stamp = lambda t: records_mod._stamp(t)  # noqa: E731
    rs.create(
        wid,
        rid_old,
        view_digest="sha256:" + "0" * 64,
        op="log",
        idem="a_" + "0" * 26,
        events=ev,
        expires_at=stamp(now[0] + 600),
    )
    now[0] += 3600
    rs.create(
        wid,
        rid_new,
        view_digest="sha256:" + "0" * 64,
        op="log",
        idem="a_" + "1" * 26,
        events=ev,
        expires_at=stamp(now[0] + 600),
    )
    assert rs.prune(force=True) == 0
    now[0] += RETENTION - 3600 + 601 + 1  # past the first request's expiry plus the retention, not the second's
    assert rs.prune(force=True) == 1
    assert rs.get(wid, rid_old) is None and rs.get(wid, rid_new) is not None
    now[0] += 3600
    assert rs.prune(force=True) == 1
    assert rs.get(wid, rid_new) is None


def test_a_lost_record_in_a_live_signer_never_signs_again(click, served, human):
    """Opus F1: the in-memory guard stays besides the record. An uncertain submit, the record deleted (a cleanup, a
    restore), sign again: the same events and idem from memory, no second signature."""
    _, key = human.ticket()
    rid, vd = _ready(click, key)
    sent = []
    orig = click._connect

    def connect(entry):
        c = orig(entry)
        real = c.call

        def call(op, args, **kw):
            if op == "human.submit":
                sent.append((json.dumps(args, sort_keys=True), kw.get("idem")))
                if len(sent) <= 2:
                    raise ConnectionError("lost")  # never reaches the host's answer
            return real(op, args, **kw)

        c.call = call
        return c

    click.signer._connect = connect
    click.signer.close()
    refused(lambda: click.sign(rid, vd), "human.click_refused", "uncertain")
    _record_path(served, rid).unlink()
    refused(lambda: click.sign(rid, "sha256:" + "3" * 64), "human.click_refused", "already_signed")
    res = click.sign(rid, vd)
    assert res["result"]["key"] == key
    assert click.signatures == 1
    assert len(set(sent)) == 1  # identical events and idem
    assert _texts(served, key) == ["x"]
    assert json.loads(_record_path(served, rid).read_bytes())["result"] == res["result"]  # written again
    assert click.sign(rid, vd)["duplicate"] is True and click.signatures == 1


def test_a_crash_between_link_and_unlink_leaves_a_readable_record(click, served, human, monkeypatch):
    """Opus F2: write_private links the record, then dies before it unlinks its temporary file. The next sign
    removes the same-inode temporary sibling and reads the record; prune removes stale temporary files."""
    from orch.custody import convenience as conv

    _, key = human.ticket()
    rid, vd = _ready(click, key)
    real_unlink = os.unlink

    def die_on_tmp(path, *a, **kw):
        if isinstance(path, str) and path.startswith(f".{rid}.json.tmp"):
            raise Crash
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(conv.os, "unlink", die_on_tmp)
    with pytest.raises(Crash):
        click.sign(rid, vd)
    monkeypatch.setattr(conv.os, "unlink", real_unlink)
    path = _record_path(served, rid)
    assert path.stat().st_nlink == 2
    assert [n for n in os.listdir(path.parent) if n.startswith(".")]
    again = _restart(click, served)
    try:  # the crash hit the claim's link: the record reads again (the claim, so no second signature)
        refused(lambda: again.sign(rid, vd), "human.click_refused", "record_unavailable")
        assert again.signatures == 0 and _texts(served, key) == []
        assert not [n for n in os.listdir(path.parent) if n.startswith(".")]
        assert path.stat().st_nlink == 1
        assert "claimed_at" in json.loads(path.read_bytes())
    finally:
        again.close()


def test_prune_removes_stale_temporary_files(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    now = [1_800_000_000.0]
    rs = Records(state, wall=lambda: now[0])
    wid, rid = "a" * 32, "hr_" + "0" * 26
    rs.create(
        wid,
        rid,
        view_digest="sha256:" + "0" * 64,
        op="log",
        idem="a_" + "0" * 26,
        events=[{"type": "log.added"}],
        expires_at=records_mod._stamp(now[0] + 600),
    )
    d = state / "person" / "click" / wid
    tmp = d / f".{rid}.json.tmp123.abcd"
    tmp.write_bytes(b"{}")
    os.utime(tmp, (now[0] - 601, now[0] - 601))
    fresh = d / f".{rid}.json.tmp124.abcd"
    fresh.write_bytes(b"{}")
    os.utime(fresh, (now[0], now[0]))
    assert rs.prune(force=True) == 1
    assert not tmp.exists() and fresh.exists() and rs.get(wid, rid) is not None


# --- a real kill (SIGKILL) between the record and the reply -----------------------------------------------------------

CHILD = textwrap.dedent(
    """
    import json, os, signal, sys
    from pathlib import Path
    from orch import crypto
    from orch.click.records import Records
    from orch.click.signer import ClickSigner
    from orch.custody.convenience import ConvenienceBackend, FakeStore
    from orch.custody.select import Selection
    from orch.host.client import Connection, Pin

    p = json.load(open(sys.argv[1]))
    store = FakeStore()
    store.put(p["wid"], p["device"], bytes.fromhex(p["scalar"]))
    pub = crypto.unb64u(p["pub"])
    sel = Selection({"v": 1, "custody": "convenience", "device": p["device"], "pub": p["pub"], "gen": 1,
                     "transition": None})
    mono = [1000.0]
    kill = lambda: os.kill(os.getpid(), signal.SIGKILL)

    def connect(entry):
        c = Connection(Pin(p["uid"], p["wid"], Path(p["sock"])))
        real = c.call
        def call(op, args, **kw):
            if op == "human.submit" and p["at"] == "before_submit":
                kill()
            out = real(op, args, **kw)
            if op == "human.submit" and p["at"] == "before_reply":
                kill()  # committed at the host; the answer never reaches the dashboard
            return out
        c.call = call
        return c

    signer = ClickSigner(
        p["state"], uid=p["uid"], connect=connect,
        custody=lambda s, w: (ConvenienceBackend(store, w, p["device"], pub), sel),
        workspaces=lambda: [p["entry"]], enabled=lambda e: True,
        mono=lambda: mono[0], wall=lambda: p["clock"],
    )
    pr = signer.prepare({"workspace_id": p["wid"], "op": "log", "args": {"ref": p["key"], "text": "killed"},
                         "idem": p["idem"]})
    vd = signer.view({"workspace_id": p["wid"], "request_id": pr["request_id"]})["view_digest"]
    print(json.dumps({"request_id": pr["request_id"], "view_digest": vd}), flush=True)
    mono[0] += 2
    signer.sign({"workspace_id": p["wid"], "request_id": pr["request_id"], "view_digest": vd})
    print("NOT KILLED", flush=True)
    """
)


@pytest.mark.parametrize("at", ["before_submit", "before_reply"])
def test_a_signer_killed_between_the_record_and_the_reply_gives_the_identical_signature_after_a_restart(
    click, served, human, tmp_path, at
):
    from orch.host.control_client import new_idem

    _, key = human.ticket()
    owner = click.owner
    params = {
        "wid": served.wid,
        "device": owner.device,
        "scalar": crypto.private_scalar(owner.dk_sig).hex(),
        "pub": crypto.b64u(owner.sig_pub),
        "uid": UID,
        "sock": str(served.lab.run / served.wid / "host.sock"),
        "state": str(served.lab.state),
        "entry": {**click.entry, "root": str(click.entry["root"])},
        "clock": served.lab.ws.clock[0],
        "key": key,
        "idem": new_idem(),
        "at": at,
    }
    pfile = tmp_path / "params.json"
    pfile.write_text(json.dumps(params))
    os.chmod(pfile, 0o600)
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    proc = subprocess.run(
        [sys.executable, str(script), str(pfile)], capture_output=True, text=True, env=child_env(tmp_path), timeout=120
    )
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    assert "NOT KILLED" not in proc.stdout
    got = json.loads(proc.stdout.splitlines()[0])
    rid, vd = got["request_id"], got["view_digest"]
    recorded = json.loads(_record_path(served, rid).read_bytes())
    assert recorded["result"] is None and recorded["view_digest"] == vd
    if at == "before_submit":
        assert pending(served, rid)["state"] == "bound"
    else:
        assert _texts(served, key) == ["killed"]

    # the restarted signer (a new process: in-process here, the same state and key)
    again = _restart(click, served)
    try:
        sent = []
        orig = again._connect

        def connect(entry):
            c = orig(entry)
            real = c.call

            def call(op, args, **kw):
                if op == "human.submit":
                    sent.append((args["events"], kw.get("idem")))
                return real(op, args, **kw)

            c.call = call
            return c

        again.signer._connect = connect
        res = again.sign(rid, vd)
        assert res["result"]["key"] == key
        assert res["duplicate"] is (at == "before_reply")
        assert sent == [(recorded["events"], recorded["idem"])]  # the identical signatures, the same idem
        assert again.signatures == 0
        assert _texts(served, key) == ["killed"]
        refused(lambda: again.sign(rid, "sha256:" + "2" * 64), "human.click_refused", "already_signed")
    finally:
        again.close()


def test_a_claim_that_cannot_be_written_never_signs(click, served, human, monkeypatch):
    _, key = human.ticket()
    rid, vd = _ready(click, key)

    def boom(*a, **kw):
        raise records_mod.RecordError("the sign record cannot be written: OSError")

    monkeypatch.setattr(click.signer.records, "claim", boom)
    refused(lambda: click.sign(rid, vd), "human.click_refused", "record_unavailable")
    assert click.signatures == 0  # no crypto signing happened
    assert pending(served, rid)["state"] == "bound"
    monkeypatch.undo()
    assert click.sign(rid, vd)["result"]["key"] == key  # nothing was claimed: signing works once writable
    assert click.signatures == 1


def test_a_crash_after_signing_before_the_completion_never_signs_again(click, served, human, monkeypatch):
    """The claim is on disk, the signature exists, the completion write dies: after a restart the claim alone refuses
    (record_unavailable), the crypto signer was called exactly once, nothing reached the host."""
    _, key = human.ticket()
    rid, vd = _ready(click, key)

    def die(*a, **kw):
        raise Crash  # the process dies between the signing and the completed record

    monkeypatch.setattr(click.signer.records, "complete", die)
    with pytest.raises(Crash):
        click.sign(rid, vd)
    assert click.signatures == 1
    on_disk = json.loads(_record_path(served, rid).read_bytes())
    assert "events" not in on_disk and on_disk["view_digest"] == vd and "claimed_at" in on_disk
    monkeypatch.undo()
    again = _restart(click, served)
    try:
        refused(lambda: again.sign(rid, vd), "human.click_refused", "record_unavailable")
        refused(lambda: again.sign(rid, "sha256:" + "3" * 64), "human.click_refused", "already_signed")
        assert again.signatures == 0
        assert pending(served, rid)["state"] == "bound" and _texts(served, key) == []
    finally:
        again.close()


def test_a_transient_completion_failure_retries_the_identical_events(click, served, human, monkeypatch):
    _, key = human.ticket()
    rid, vd = _ready(click, key)
    real = click.signer.records.complete
    fails = [1]

    def flaky(*a, **kw):
        if fails[0]:
            fails[0] -= 1
            raise records_mod.RecordError("the sign record cannot be updated: OSError")
        return real(*a, **kw)

    monkeypatch.setattr(click.signer.records, "complete", flaky)
    refused(lambda: click.sign(rid, vd), "human.click_refused", "record_unavailable")
    assert pending(served, rid)["state"] == "bound" and click.signatures == 1
    res = click.sign(rid, vd)  # in-process retry: the cached events, completed, then submitted
    assert res["result"]["key"] == key and click.signatures == 1
    rec = json.loads(_record_path(served, rid).read_bytes())
    assert rec["events"] and rec["result"] == res["result"]
    assert _texts(served, key) == ["x"]


def _assert_claim_only(served, rid, vd):
    on_disk = json.loads(_record_path(served, rid).read_bytes())
    assert "events" not in on_disk and on_disk["view_digest"] == vd and "claimed_at" in on_disk


def test_a_signer_that_raises_before_signing_keeps_the_claim(click, served, human, monkeypatch):
    """An exception does not prove that no signature exists: the claim stays and the request fails closed."""
    _, key = human.ticket()
    rid, vd = _ready(click, key)
    from orch.host import signer as host_signer

    calls = []

    def broken(*a, **kw):
        calls.append(1)
        raise RuntimeError("keychain")

    monkeypatch.setattr(host_signer, "sign", broken)
    with pytest.raises(Exception, match="keychain|RuntimeError|internal"):
        click.sign(rid, vd)
    _assert_claim_only(served, rid, vd)  # kept, not released
    refused(lambda: click.sign(rid, vd), "human.click_refused", "record_unavailable")
    refused(lambda: click.sign(rid, "sha256:" + "3" * 64), "human.click_refused", "already_signed")
    assert len(calls) == 1  # the crypto signer was called once
    assert pending(served, rid)["state"] == "bound" and _texts(served, key) == []


def test_a_claim_deleted_from_outside_after_a_failed_signing_is_not_signed_again(click, served, human, monkeypatch):
    """Defence in depth: the process remembers that it attempted the signature, whatever the files say."""
    _, key = human.ticket()
    rid, vd = _ready(click, key)
    from orch.host import signer as host_signer

    calls = []

    def broken(*a, **kw):
        calls.append(1)
        raise RuntimeError("keychain")

    monkeypatch.setattr(host_signer, "sign", broken)
    with pytest.raises(Exception, match="keychain|RuntimeError|internal"):
        click.sign(rid, vd)
    _record_path(served, rid).unlink()  # an outside deletion of the claim
    refused(lambda: click.sign(rid, vd), "human.click_refused", "record_unavailable")
    assert len(calls) == 1
    assert pending(served, rid)["state"] == "bound" and _texts(served, key) == []


def test_the_attempt_marker_is_kept_for_the_process_lifetime(click, served, human, monkeypatch):
    """Never pruned: survives sweeps past the approval deadline and past request expiry plus margin."""
    from orch.host import signer as host_signer
    from orch.host.control_client import new_idem

    _, key = human.ticket()
    idem = new_idem()
    args = {"workspace_id": served.wid, "op": "log", "args": {"ref": key, "text": "x"}, "idem": idem}
    sg = click.signer
    rid = sg.prepare(args)["request_id"]
    vd = click.view(rid)["view_digest"]
    click.mono.t += 2
    calls = []

    def broken(*a, **kw):
        calls.append(1)
        raise RuntimeError("keychain")

    monkeypatch.setattr(host_signer, "sign", broken)
    with pytest.raises(Exception, match="keychain|RuntimeError|internal"):
        click.sign(rid, vd)
    assert len(sg._attempted) == 1
    _record_path(served, rid).unlink()
    click.mono.t += 3600  # the approval deadline passed
    sg._sweep()
    assert len(sg._attempted) == 1 and sg.approvals == {}
    served.lab.ws.clock[0] += 3600  # the request's own expiry plus any margin
    sg._sweep()
    assert len(sg._attempted) == 1
    try:
        again = sg.prepare(args)["request_id"]
    except Exception:  # noqa: BLE001 - the host may refuse the expired request itself
        return
    vd = click.view(again)["view_digest"]
    click.mono.t += 2
    refused(lambda: click.sign(again, vd), "human.click_refused", "record_unavailable")
    assert len(calls) == 1


def test_a_marker_outlives_the_approval_deadline_while_the_request_is_open(click, served, human, monkeypatch):
    """Fail closed: the claim deleted from outside, the approval swept at its deadline (mono) while the host still
    holds the request open; an idem retry of prepare rebuilds the approval, and the sign is still refused."""
    from orch.host import signer as host_signer
    from orch.host.control_client import new_idem

    _, key = human.ticket()
    idem = new_idem()
    args = {"workspace_id": served.wid, "op": "log", "args": {"ref": key, "text": "x"}, "idem": idem}
    sg = click.signer
    rid = sg.prepare(args)["request_id"]
    vd = click.view(rid)["view_digest"]
    click.mono.t += 2
    calls = []

    def broken(*a, **kw):
        calls.append(1)
        raise RuntimeError("keychain")

    monkeypatch.setattr(host_signer, "sign", broken)
    with pytest.raises(Exception, match="keychain|RuntimeError|internal"):
        click.sign(rid, vd)
    _record_path(served, rid).unlink()
    click.mono.t += 3600  # approval deadline long past; the host request is still open
    sg._sweep()
    assert sg.approvals == {} and len(sg._attempted) == 1
    assert sg.prepare(args)["request_id"] == rid  # the idem retry rebuilds an approval, fresh deadline
    vd = click.view(rid)["view_digest"]
    click.mono.t += 2
    refused(lambda: click.sign(rid, vd), "human.click_refused", "record_unavailable")
    assert len(calls) == 1


def test_a_partial_signing_keeps_the_claim_across_a_restart(click, served, human, monkeypatch):
    """The first event signs, the second raises: the claim stays, nothing is submitted, a retry (same process or a
    restarted signer) is refused and never signs again."""
    import orch.identity as identity
    from tests.host.test_human import TwoNotes

    _, key = human.ticket()
    real = identity.sign_person_event
    count = []

    def second_fails(*a, **kw):
        count.append(1)
        if len(count) == 2:
            raise RuntimeError("keychain")
        return real(*a, **kw)

    with TwoNotes():
        rid, vd = _ready(click, key)
        monkeypatch.setattr(identity, "sign_person_event", second_fails)
        with pytest.raises(Exception, match="keychain|RuntimeError|internal"):
            click.sign(rid, vd)
        assert len(count) == 2
        _assert_claim_only(served, rid, vd)
        refused(lambda: click.sign(rid, vd), "human.click_refused", "record_unavailable")
        refused(lambda: click.sign(rid, "sha256:" + "3" * 64), "human.click_refused", "already_signed")
        assert len(count) == 2  # no further crypto call in this process
        again = _restart(click, served)
        try:
            refused(lambda: again.sign(rid, vd), "human.click_refused", "record_unavailable")
            refused(lambda: again.sign(rid, "sha256:" + "3" * 64), "human.click_refused", "already_signed")
        finally:
            again.close()
    assert len(count) == 2  # total crypto sign calls: the two of the first attempt
    assert pending(served, rid)["state"] == "bound" and _texts(served, key) == []


def test_a_returned_secret_keeps_the_claim(click, served, human, monkeypatch):
    _, key = human.ticket()
    rid, vd = _ready(click, key)
    from orch.host import signer as host_signer

    real = host_signer.sign
    calls = []

    def with_secret(*a, **kw):
        calls.append(1)
        out = real(*a, **kw)
        return host_signer.Signed(out.events, "grant-secret")

    monkeypatch.setattr(host_signer, "sign", with_secret)
    refused(lambda: click.sign(rid, vd), "human.click_refused", "terminal_only")
    _assert_claim_only(served, rid, vd)
    refused(lambda: click.sign(rid, vd), "human.click_refused", "record_unavailable")
    assert len(calls) == 1
    assert pending(served, rid)["state"] == "bound" and _texts(served, key) == []


def test_the_validator_accepts_only_a_claim_or_a_completed_record(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    rs = Records(state)
    wid, rid = "a" * 32, "hr_" + "0" * 26
    c = rs.claim(
        wid, rid, view_digest="sha256:" + "0" * 64, op="log", idem="a_" + "0" * 26,
        expires_at=records_mod._stamp(rs.wall() + 600),
    )  # fmt: skip
    assert set(c) == records_mod.CLAIM_KEYS and rs.get(wid, rid) == c
    with pytest.raises(records_mod.RecordError):
        rs.settle(c, {"x": 1})  # a claim has no result to settle
    with pytest.raises(records_mod.RecordError):
        records_mod._validate({**c, "events": [{"a": 1}]}, wid, rid)  # neither shape
    done = rs.complete(c, [{"type": "log.added"}])
    assert set(done) == records_mod.KEYS and rs.get(wid, rid) == done
