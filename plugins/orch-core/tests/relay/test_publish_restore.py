"""R3 startup classification (11A.5 table) and restores: H7 host-state loss, an unexplained fork (``chain.diverged``
and the degrade hook), a relay rolled back (to an ancestor, to nothing, across an accepted restore), a replayed
abandoned-branch checkpoint, an owner-signed restore with its abandoned suffix, an oversized restore refused before
staging, a stale H before a restore, a crash mid-restore, the relay's ``restore_source_mismatch``."""

from __future__ import annotations

import json
import shutil

import pytest

from orch import canon
from orch.relay import publish
from orch.store.checkpoints import sign_object
from tests.store.test_checkpoints import rollback

from .pubkit import Kit
from .sim import _err


class Crash(BaseException):
    pass


@pytest.fixture
def kit(tmp_path):
    k = Kit(tmp_path, tickets=2, events=1)
    yield k
    k.close()


def _reopen(k):
    k.store.close()
    k.store = k.env.open()
    k.publisher.stop()
    k.publisher = k.make_publisher()


def _restored(k, tmp_path, *, lost=3):
    """Publish ``lost`` events on ticket 0 (the relay holds C with them), roll the logs back to before them (a backup
    restore of the workspace; host state kept), then the owner's restore of that ticket: the outbox holds the restore
    entry, whose source C is what the relay holds. Returns ``(C hash, restore entry)``."""
    uid = k.uids[0]
    k.drain()
    k.store.close()
    backup = tmp_path / "backup"
    shutil.copytree(k.env.root, backup)
    k.store = k.env.open()
    for i in range(lost):
        k.env.log(uid, f"lost-{i}")
    k.publisher = k.make_publisher()
    k.drain()
    c_hash = k.sim.accepted[-1][1]
    k.store.close()
    rollback(k.env, backup)
    _reopen(k)
    assert uid in k.store.diverged
    facts = k.store.restore_facts(uid)
    k.store.append(k.env.person_event(k.env.owner, uid, "restore", **facts, reason="rollback"), log=uid)
    entry = k.store.outbox_entries()[0]
    assert entry.kind == "restore" and canon.checkpoint_hash(entry.restore["source"]["o"]) == c_hash
    return c_hash, entry


# -- classification --------------------------------------------------------------------------------------------------


def test_an_h_numbered_above_the_host_is_host_state_loss(kit):
    k = kit
    k.drain()
    o = json.loads(json.dumps(k.sim.held["o"]))
    o["n"] = 10_000
    k.sim.held = sign_object(k.env.signer.sign, o)
    before = len(k.sim.log)
    assert k.publisher.step() == ("wait", publish.HELD_RECHECK_S)
    assert k.alarms.codes() == ["relay_host_state_lost"] and k.sim.log[before:] == [] and k.diverged == []


def test_an_unexplained_fork_is_chain_diverged_and_degrades(kit):
    k = kit
    k.drain()
    o = json.loads(json.dumps(k.sim.held["o"]))
    o["tickets"][k.uids[1]]["head"] = "sha256:" + "5" * 64  # validly signed (a WSK holder), another branch
    k.sim.held = sign_object(k.env.signer.sign, o)
    k.env.log(k.uids[0], "x")
    before = len(k.sim.log)
    assert k.publisher.step() == ("wait", publish.HELD_RECHECK_S)
    assert k.alarms.codes() == ["chain.diverged"] and len(k.diverged) == 1
    assert k.sim.log[before:] == []  # nothing is published over a fork


def test_a_relay_rolled_back_to_an_ancestor_is_republished_from_the_logs(kit):
    k = kit
    k.drain()
    first = k.sim.accepted[0][1]
    k.env.log(k.uids[0], "x")
    k.drain()
    k.sim.roll_back_to(first)
    k.drain()
    assert "relay_rolled_back" in k.alarms.codes() and k.diverged == []
    assert k.current()["hash"] == canon.checkpoint_hash(k.sim.held["o"])


def test_a_false_success_followed_by_an_empty_relay_is_republished(kit):
    k = kit
    k.drain()
    k.sim.forget()
    k.drain()
    assert "relay_rolled_back" in k.alarms.codes()
    assert k.current()["hash"] == canon.checkpoint_hash(k.sim.held["o"])


# -- restores --------------------------------------------------------------------------------------------------------


def test_a_restore_with_an_abandoned_suffix_is_published_with_its_evidence(kit, tmp_path):
    k = kit
    c_hash, entry = _restored(k, tmp_path)
    assert k.drain()[-1] == ("idle", 0.0), k.alarms
    assert (entry.n, entry.hash) in k.sim.accepted
    lines = k.publisher.history.lines()
    assert {"n": entry.n, "hash": entry.hash, "kind": "restore"}.items() <= next(
        line for line in lines if line["n"] == entry.n).items()  # fmt: skip
    (rec,) = k.publisher.history.restores()
    assert canon.checkpoint_hash(rec["source"]["o"]) == c_hash and rec["body"]["restore"]["log"] == k.uids[0]
    assert k.store.outbox_entries() == [] and k.diverged == []
    kinds = {json.loads(s.body)["kind"] for s in k.mock.seen if "/pages/" in s.path}
    assert "abandoned" in kinds


def test_an_oversized_restore_is_refused_before_staging_and_never_sent(kit, tmp_path):
    k = kit
    _, entry = _restored(k, tmp_path, lost=6)
    p = k.make_publisher(max_publication_bytes=400)
    before = len(k.sim.log)
    assert p.step() == ("wait", publish.HELD_RECHECK_S)
    assert k.alarms.codes() == ["relay.too_large"] and k.sim.log[before:] == []
    assert k.store.outbox_entries()[0].n == entry.n


def test_a_stale_h_before_a_restore_first_gets_the_source_republished(kit, tmp_path):
    k = kit
    c_hash, entry = _restored(k, tmp_path)
    older = k.sim.accepted[0][1]
    k.sim.roll_back_to(older)  # the relay lost C (a relay restore) before the owner's restore reached it
    k.drain()
    hashes = [h for _, h in k.sim.accepted]
    assert hashes.index(c_hash, 1) < hashes.index(entry.hash)  # C again, then the restore
    assert k.store.outbox_entries() == [] and k.diverged == []


def test_a_relay_rolled_back_across_an_accepted_restore_republishes_source_restore_and_current(kit, tmp_path):
    k = kit
    c_hash, entry = _restored(k, tmp_path)
    k.drain()
    k.env.log(k.uids[1], "after the restore")
    k.drain()
    older = k.sim.accepted[0][1]
    k.sim.roll_back_to(older)
    k.sim.restore_ids.clear()  # the relay's restore record went with its backup
    k.drain()
    assert "relay_rolled_back" in k.alarms.codes() and k.diverged == []
    tail = [h for _, h in k.sim.accepted][-3:]
    assert tail[0] == c_hash and tail[1] == entry.hash
    assert k.current()["hash"] == canon.checkpoint_hash(k.sim.held["o"])


def test_a_replayed_abandoned_branch_checkpoint_is_explained_not_diverged(kit, tmp_path):
    k = kit
    c_hash, entry = _restored(k, tmp_path)
    k.drain()
    k.sim.roll_back_to(c_hash)  # the relay serves the pre-restore checkpoint again (abandoned in the logs)
    k.sim.restore_ids.clear()
    k.drain()
    assert "relay_rolled_back" in k.alarms.codes() and "chain.diverged" not in k.alarms.codes()
    assert k.diverged == [] and k.sim.accepted[-1][1] == k.current()["hash"]


def test_kill_mid_restore_resumes_the_same_set(kit, tmp_path, monkeypatch):
    k = kit
    _, entry = _restored(k, tmp_path)
    real = k.session.call
    hits = {"n": 0}

    def call(method, target, route, **kw):
        if "/pages/" in target:
            hits["n"] += 1
            if hits["n"] == 1:  # after the set's manifest was recorded, before its first page
                raise Crash()
        return real(method, target, route, **kw)

    monkeypatch.setattr(k.session, "call", call)
    with pytest.raises(Crash):
        k.drain()
    assert k.state.read("publish.json") is not None and entry.hash not in [h for _, h in k.sim.accepted]
    monkeypatch.setattr(k.session, "call", real)
    k.publisher = k.make_publisher()
    k.drain()
    assert (entry.n, entry.hash) in k.sim.accepted and k.store.outbox_entries() == []
    assert k.state.read("publish.json") is None


def test_the_relays_restore_source_mismatch_holds_for_the_owner(kit, tmp_path):
    k = kit
    _, entry = _restored(k, tmp_path)
    k.sim.fault = lambda s: (
        _err(409, "restore_source_mismatch") if s.path.endswith("/checkpoint") and s.method == "PUT" else None
    )
    assert k.publisher.step() == ("wait", publish.HELD_RECHECK_S)
    assert k.alarms.codes() == ["restore_source_mismatch"] and k.store.outbox_entries()[0].n == entry.n


def test_a_restore_missing_revocation_holds_for_the_evidence(kit, tmp_path):
    k = kit
    _restored(k, tmp_path)
    k.sim.fault = lambda s: (
        _err(409, "restore_missing_revocation") if s.path.endswith("/checkpoint") and s.method == "PUT" else None
    )
    assert k.publisher.step() == ("wait", publish.HELD_RECHECK_S)
    assert k.alarms.codes() == ["restore_missing_revocation"]


def test_a_restore_signed_with_convenience_waits_for_reader_level_one(kit, tmp_path):
    k = kit
    _, entry = _restored(k, tmp_path)
    p = k.make_publisher()
    p.cap()
    p.reader_level = 0
    r = dict(entry.restore)
    r["event"] = {**r["event"], "auth": "convenience"}
    with pytest.raises(publish.PublishHeld) as e:
        p._restore_pub(None, entry.checkpoint, r["event"], r["log"], r, [], 1 << 20, entry=entry,
                       acc_from=r["acc_from"])  # fmt: skip
    assert e.value.code == "relay_reader_old"
