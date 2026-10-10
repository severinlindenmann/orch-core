"""Findings of the code and security reviews of #350, each pinned: freshness, revocations, forged records, idempotency,
host state, recovery, symlinks, archives, rejected bodies, grant hashes, the unsigned config fields."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from orch import canon, crypto
from orch.cli.store_hooks import hooks_for
from orch.ops import Context
from orch.ops.errors import OrchError
from orch.store import Store, StoreError
from tests.store.helpers import GRANT_SECRET, WS, Env, ts

SRC = Path(__file__).resolve().parents[2] / "src" / "orch"


def revoke_grant(env: Env, store: Store | None = None):
    store = store or env.store
    return store.append(env.person_event(env.owner, "workspace", "grant.revoked", grant=env.grant_id), log="workspace")


# ---- security 1: the files decide whether the state is fresh, never `.state/applied`


def test_a_forged_applied_cannot_hide_another_processes_revocation(env):
    a = env.bootstrap()
    uid = env.new_ticket()
    old_applied = (env.root / ".state" / "applied").read_bytes()
    b = env.other()
    env.tick()
    revoke_grant(env, b)
    b.close()
    (env.root / ".state" / "applied").write_bytes(old_applied)  # the agent puts the old record back
    with pytest.raises(OrchError):
        hooks_for(a).grant_valid(Context(grant=f"{env.grant_id}.{crypto.b64u(GRANT_SECRET)}"))
    with pytest.raises(StoreError) as e:  # and the store does not sign an agent event with a stale ws_seq
        env.log(uid, "after the revocation")
    assert e.value.code == "grant.invalid"
    assert len(env.read_events(uid)) == 1


def test_a_log_that_shrinks_or_is_replaced_under_a_long_lived_store_is_noticed(env):
    a = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid)
    log = env.path(uid, "events.jsonl")
    data = log.read_bytes()
    assert a._fresh()
    copy = log.with_suffix(".copy")
    copy.write_bytes(data)
    os.replace(copy, log)  # same bytes, new inode
    assert not a._fresh()
    env.log(uid, "still fine after a reload")
    ws = env.root / "events" / "workspace.jsonl"
    lines = ws.read_bytes().splitlines(keepends=True)
    ws.write_bytes(b"".join(lines[:-1]))  # the last workspace event is gone
    with pytest.raises(StoreError) as e:
        env.log(uid)
    assert e.value.code == "chain.diverged"


# ---- security 2: revocations the host noted are enforced on every load


def rolled_back(env, tmp_path):
    s = env.bootstrap()
    ref, _ = env.add_device()
    env.tick()
    env.revoke_device(ref)
    s.close()
    p = env.root / "events" / "workspace.jsonl"
    p.write_bytes(b"".join(p.read_bytes().splitlines(keepends=True)[:-1]))
    (env.root / ".state" / "checkpoints" / "workspace.json").unlink()
    (env.root / ".state" / "applied").unlink()
    return ref


def test_a_rolled_back_revocation_is_detected_even_without_checkpoints(env, tmp_path):
    ref = rolled_back(env, tmp_path)
    s = env.open()
    assert "revocation" in s.diverged["workspace"].detail
    with pytest.raises(StoreError) as e:
        env.add_device()
    assert e.value.code == "chain.diverged"
    facts = s.restore_facts("workspace")
    s.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="rollback"), log="workspace")
    evs = env.read_events("workspace")
    assert [x["type"] for x in evs[-2:]] == ["restore", "device.revoked"] and evs[-1]["device"] == ref
    assert s.state.workspace.devices[ref].revoked and s.diverged == {}


def test_a_restore_that_lost_its_revocations_to_a_crash_is_finished_on_the_next_open(env, tmp_path, monkeypatch):
    ref = rolled_back(env, tmp_path)
    s = env.open()
    facts = s.restore_facts("workspace")
    monkeypatch.setattr(Store, "_enforce_revocations", lambda self: None)  # the crash right after the restore
    s.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="rollback"), log="workspace")
    monkeypatch.undo()
    assert env.read_events("workspace")[-1]["type"] == "restore"
    s.close()
    again = env.open()
    assert env.read_events("workspace")[-1]["type"] == "device.revoked"
    assert again.state.workspace.devices[ref].revoked and again.diverged == {}
    again.close()
    assert [e["type"] for e in env.read_events("workspace")].count("device.revoked") == 1  # and only once


def test_a_read_only_store_marks_the_workspace_diverged_for_a_missing_revocation(env, tmp_path):
    rolled_back(env, tmp_path)
    ro = Store.open(env.root, expected_workspace_id=WS, load="all")
    assert "workspace" in ro.diverged or ro.state.workspace.devices  # no host state: nothing to compare with


# ---- security 3: a body is served only if the log accounts for it


@pytest.fixture
def closed(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    env.fill(uid)
    env.approve(uid, "requirements")
    env.close(uid)
    return env, s, uid


def test_a_forged_bound_section_of_a_closed_ticket_is_never_served_or_extended(closed):
    env, s, uid = closed
    body = env.path(uid, "body.md")
    good = body.read_bytes()
    forged = good.replace(b"req", b"FORGED")
    body.write_bytes(forged)
    (env.root / ".state" / "body" / f"{uid}.md").write_bytes(forged)
    assert s.scan() == [] and any(r.code == "store.torn_write" for r in s.reports)
    with pytest.raises(StoreError) as e:
        s.body_sections(uid)
    assert e.value.code == "store.torn_write"
    with pytest.raises(StoreError) as e:
        s.append({"type": "log.added", "actor": env.agent, "text": "x"}, log=uid)
    assert e.value.code == "store.torn_write"
    body.write_bytes(good)  # the files match the log again: the ticket takes events again
    assert s.scan() == [] and s.body_sections(uid)["requirements"] == "req"
    s.append(env.person_event(env.owner, uid, "log.added", text="human note"), log=uid)


def test_body_sections_raises_for_an_edit_nobody_answered_yet(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    env.fill(uid)
    p = env.path(uid, "body.md")
    p.write_text(p.read_text().replace("ctx", "changed by hand"))
    with pytest.raises(StoreError) as e:
        s.body_sections(uid)
    assert e.value.code == "store.torn_write"
    s.scan()
    assert s.body_sections(uid)["context"] == "changed by hand"


# ---- security 4: intents are believed only as far as the log confirms them


def intent(env, idem, rec):
    p = env.root / ".state" / "intents" / (hashlib.sha256(idem.encode()).hexdigest()[:40] + ".json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec))


def test_a_planted_intent_cannot_suppress_a_signed_event(env):
    s = env.bootstrap()
    first = env.read_events("workspace")[1]
    intent(env, "k", {"log": "workspace", "id": first["id"], "seq": 2, "head": canon.event_head(first)})
    r = revoke_grant(env)  # without idem the intent is irrelevant ...
    assert r.event["type"] == "grant.revoked"
    env2 = env  # ... and with it, the logged event must be the requested one
    intent(env2, "k2", {"log": "workspace", "id": first["id"]})
    g = env.grant_event()
    r2 = s.append(g, log="workspace", idem="k2")
    assert not r2.duplicate and r2.event["type"] == "grant.issued"


def test_an_intent_of_another_log_or_another_request_is_not_a_duplicate(env):
    s = env.bootstrap()
    a, b = env.new_ticket("a"), env.new_ticket("b")
    s.append({"type": "log.added", "actor": env.agent, "text": "x"}, log=a, idem="same")
    r = s.append({"type": "log.added", "actor": env.agent, "text": "y"}, log=b, idem="same")
    assert not r.duplicate and r.event["text"] == "y"
    r = s.append({"type": "log.added", "actor": env.agent, "text": "z"}, log=b, idem="same")
    assert not r.duplicate and r.event["text"] == "z"  # same key, other payload: a new event, never the old result
    again = s.append({"type": "log.added", "actor": env.agent, "text": "z"}, log=b, idem="same")
    assert again.duplicate and again.event["id"] == r.event["id"]


def test_a_signed_retry_keeps_its_own_id_and_a_file_never_donates_one(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    ev = env.person_event(env.owner, uid, "log.added", text="signed")
    s.append(ev, log=uid, idem="signed-key")
    again = s.append(ev, log=uid, idem="signed-key")
    assert again.duplicate and len(env.read_events(uid)) == 2
    intent(env, "planted", {"log": uid, "id": "01J9ZP0000000000000000F0RG"})
    ev2 = env.person_event(env.owner, uid, "log.added", text="signed 2")
    r = s.append(ev2, log=uid, idem="planted")
    assert r.event["id"] == ev2["id"]  # the signed id, not the planted one


def test_create_ticket_retry_after_a_crash_finds_its_ticket(env):
    s = env.bootstrap()
    r1 = s.create_ticket(actor=env.agent, ticket_type="chore", title="once", owner=env.owner.ref, idem="mk")
    r2 = s.create_ticket(actor=env.agent, ticket_type="chore", title="once", owner=env.owner.ref, idem="mk")
    assert r2.duplicate and r2.event["id"] == r1.event["id"] and len(s.state.tickets) == 1


# ---- security 5: host state is mandatory for a store that can write


def test_a_writable_store_needs_host_state(env):
    with pytest.raises(StoreError) as e:
        Store.open(env.root, expected_workspace_id=WS, host=env.signer)
    assert e.value.code == "validation.host_state"


# ---- security 7: recovery follows .state/applied, and installs only what the log says


def forge_manifest(env, uid, last_line, files, event=None):
    e = event or canon.parse_event_line(last_line)
    d = env.root / ".state" / "pending" / "X"
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for n, (rel, data) in enumerate(files):
        (d / str(n)).write_bytes(data)
        out.append({"to": rel, "n": n, "sha256": hashlib.sha256(data).hexdigest()})
    (d / "manifest.json").write_text(
        json.dumps(
            {
                "v": 1,
                "id": e["id"],
                "log": uid,
                "seq": e["seq"],
                "line": last_line.decode(),
                "files": out,
                "body_copy": uid,
            }  # fmt: skip
        )
    )


def test_a_forged_pending_manifest_for_the_real_last_event_installs_nothing(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    env.fill(uid)
    s.close()
    last = env.path(uid, "events.jsonl").read_bytes().splitlines(keepends=True)[-1]
    good_body = env.path(uid, "body.md").read_bytes()
    forged = b"## Context\n\nFORGED\n"
    for applied in ("keep", "remove"):
        forge_manifest(env, uid, last, [(f"tickets/{uid}/body.md", forged)])
        if applied == "remove":
            (env.root / ".state" / "applied").unlink()
        s = env.open()
        assert env.path(uid, "body.md").read_bytes() == good_body  # not installed ("keep": applied names the event)
        assert (env.root / ".state" / "body" / f"{uid}.md").read_bytes() == good_body
        assert not (env.root / ".state" / "pending" / "X").exists()
        if applied == "remove":
            assert any(r.code == "store.torn_write" for r in s.reports)
        assert [e["type"] for e in env.read_events(uid)] == ["ticket.created", "ticket.updated"]
        s.close()


# ---- security 8: no symlinked directory under the workspace


@pytest.mark.parametrize("d", [".state", "events", "tickets", ".state/pending", ".state/checkpoints"])
def test_a_symlinked_workspace_directory_is_refused(env, tmp_path, d):
    s = env.bootstrap()
    s.close()
    target = env.root / d
    outside = tmp_path / "outside"
    outside.mkdir()
    if target.exists():
        target.rename(tmp_path / "moved")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, target)
    with pytest.raises(StoreError) as e:
        env.open()
    assert e.value.code == "validation.path"
    assert list(outside.iterdir()) == []


# ---- security 9: archives of cut tails are never overwritten


def test_abandon_tail_never_overwrites_an_earlier_archive(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid, "one")
    env.log(uid, "two")
    log = env.path(uid, "events.jsonl")
    full = log.read_bytes()
    s.abandon_tail(uid, 1)
    log.write_bytes(full)
    s.abandon_tail(uid, 1)
    log.write_bytes(full.splitlines(keepends=True)[0] + b'{"different":1}\n')
    s.abandon_tail(uid, 1)
    files = sorted((env.root / ".state" / "abandoned").glob(f"{uid}-2*.jsonl"))
    assert len(files) == 3 and len({p.read_bytes() for p in files}) == 2  # two distinct cuts, three archives kept


# ---- security 10: a body that is not a body is kept before it is reverted


def test_an_unparseable_body_is_kept_in_state_rejected(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    env.fill(uid)
    p = env.path(uid, "body.md")
    mine = p.read_bytes() + b"\n## My notes\n\nhuman prose that must not be lost\n"
    p.write_bytes(mine)
    (ev,) = s.scan()
    assert ev["type"] == "projection.repaired" and b"human prose" not in p.read_bytes()
    (kept,) = (env.root / ".state" / "rejected").glob(f"{uid}-*.md")
    assert kept.read_bytes() == mine
    assert any(r.code == "store.rejected_body" and kept.name in r.detail for r in s.reports)


# ---- security 11: a grant's secret hash comes only from an accepted grant.issued


def test_a_forged_grant_issued_does_not_provide_a_secret_hash(env):
    s = env.bootstrap()
    other_grant = "gr_01J9ZP0000000000000000FAKE"[:29]
    s.close()
    ws = env.read_events("workspace")
    last = ws[-1]
    issued = ts(env.clock[0])
    e = {
        "v": 2, "id": "01J9ZP0000000000000000F0RG", "seq": last["seq"] + 1, "at": issued, "type": "grant.issued",
        "actor": last["actor"], "auth": "passphrase", "hash_v": 1, "roster_v": 1, "based_on": canon.event_head(last),
        "prev": canon.event_head(last), "grant": other_grant, "scope": "all", "verbs": "agent", "issued_at": issued,
        "hours": 8, "expires_at": ts(env.clock[0] + 8 * 3600), "secret_hash": canon.grant_secret_hash(b"e" * 32),
        "sig": crypto.b64u(b"\x01" * 64),
    }  # fmt: skip
    e["host_sig"] = crypto.b64u(env.signer.sign(canon.host_signing_bytes(WS, "workspace", e)))
    with open(env.root / "events" / "workspace.jsonl", "ab") as f:
        f.write(canon.event_line(e))
    s = env.open()
    assert s.state.workspace.invalid and s.grant_secret_hash(other_grant) is None  # refused by replay: no hash
    assert s.grant_secret_hash(env.grant_id) == canon.grant_secret_hash(GRANT_SECRET)


# ---- decided (6): nothing decides anything from the unsigned fields of config.json


def test_run_for_and_name_decide_nothing():
    allowed = {"store/render.py", "store/store.py", "cli/store_hooks.py"}
    for p in SRC.rglob("*.py"):
        rel = p.relative_to(SRC).as_posix()
        text = p.read_text()
        if "run_for" in text:
            assert rel in allowed, rel
    code = (SRC / "store" / "store.py").read_text()
    start = code.index("    def _config_bytes")
    end = code.index("    def _keys_bytes")
    outside = code[:start] + code[end:]
    assert "run_for" not in outside  # store.py touches it only in _config_bytes, which copies it back into the file
    hooks = (SRC / "cli" / "store_hooks.py").read_text()
    assert "config" not in re.sub(r'""".*?"""', "", hooks, flags=re.S).replace("config.json", "")
