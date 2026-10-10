"""No path of the store trusts an event without the model's full verification (security review)."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from pathlib import Path

import pytest

from orch import canon, crypto
from orch.cli.store_hooks import hooks_for
from orch.ops import Context
from orch.ops.errors import OrchError
from orch.store import Store, StoreError
from tests.store.helpers import GRANT_SECRET, WS, Env, snapshot

SRC = Path(__file__).resolve().parents[2] / "src" / "orch"


def test_no_verification_bypass_is_reachable_from_production_code():
    public = inspect.signature(Store.open).parameters
    assert "verifier" not in public and "validate" not in public and "skip_verify" not in public
    assert "_verifier" in public and not any(n.startswith("_") is False and "verif" in n for n in public)
    for p in (SRC / "store").rglob("*.py"):
        text = p.read_text()
        assert "FakeVerifier" not in text and "model.testing" not in text, p
        assert "validate=False" not in text, p
        assert not re.search(r"verifier\s*=\s*None", text), p
    for p in SRC.rglob("*.py"):  # nothing outside the store passes the seam either
        if "store" in p.parts and p.name == "store.py":
            continue
        assert "_verifier=" not in p.read_text(), p


def test_every_event_is_checked_by_the_real_crypto_verifier(env):
    s = env.bootstrap()
    assert type(s._verifier).__name__ == "CryptoVerifier"
    assert s.state._ctx.verifier is s._verifier and s.state._ctx.expected_workspace_id == WS
    assert s.state._ctx.expected_genesis is not None  # the pin reaches every replay once it exists
    s.close()
    again = env.open()
    assert again.state._ctx.expected_genesis == again.genesis and again.state._ctx.expected_workspace_id == WS


def test_a_person_event_without_a_valid_signature_is_refused_and_writes_nothing(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    before = snapshot(env.root)
    good = env.person_event(env.owner, uid, "log.added", text="from a person")
    unsigned = {k: v for k, v in good.items() if k != "sig"}
    wrong = {**good, "text": "changed after signing"}
    other = {**good, "sig": crypto.b64u(crypto.sign(crypto.generate_private_key(), b"whatever"))}
    for bad in (unsigned, wrong, other):
        with pytest.raises(StoreError) as e:
            s.append(bad, log=uid)
        assert e.value.code in ("sig.invalid", "validation.event")
    assert snapshot(env.root) == before
    s.append(good, log=uid)


def test_a_foreign_genesis_is_not_appended_even_when_signed(env, tmp_path):
    other = Env(tmp_path / "other")
    s = other.open()
    g = other.genesis()
    s.close()
    mine = env.open(expected_genesis="sha256:" + "0" * 64)
    with pytest.raises(StoreError) as e:
        mine.append(g, log="workspace")  # not this host's key, not the pinned genesis
    assert e.value.code in ("validation.host_key", "trust.genesis_mismatch")
    assert mine.genesis is None


def test_open_refuses_to_trust_a_log_that_fails_and_checkpoints_that_fail_do_not_unlock_appends(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    s.close()
    p = env.path(uid, "events.jsonl")
    e = canon.parse_event_line(p.read_bytes())
    e["title"] = "forged"
    p.write_bytes(canon.event_line(e))
    s = env.open()
    assert s.chain_errors()
    for f in (env.root / ".state" / "checkpoints").glob("ticket-*.json"):
        f.unlink()  # deleting the checkpoints must not make a broken log appendable
    s.close()
    s = env.open()
    with pytest.raises(StoreError):
        env.log(uid)


def test_a_forged_pending_manifest_installs_nothing(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    s.close()
    pdir = env.root / ".state" / "pending" / "01J9ZP0000000000000000F0RG"
    pdir.mkdir(parents=True)
    evil = b'{"evil":true}'
    (pdir / "0").write_bytes(evil)
    tail = env.path(uid, "events.jsonl").read_bytes().splitlines(keepends=True)[-1]
    for line in (
        tail.decode(),
        "not an event line\n",
        '{"id":"x"}\n',
    ):  # the tail is real but not this manifest's event
        m = {"v": 1, "id": "01J9ZP0000000000000000F0RG", "log": uid, "seq": 1, "line": line,
             "files": [{"to": f"tickets/{uid}/ticket.json", "n": 0, "sha256": hashlib.sha256(evil).hexdigest()}],
             "body_copy": None}  # fmt: skip
        (pdir / "manifest.json").write_text(json.dumps(m))
        (pdir / "0").write_bytes(evil)
        before = env.path(uid, "ticket.json").read_bytes()
        s = env.open()
        assert env.path(uid, "ticket.json").read_bytes() == before
        assert b"evil" not in env.path(uid, "ticket.json").read_bytes()
        s.close()
        pdir.mkdir(parents=True, exist_ok=True)


def test_recovery_and_repair_only_append_host_events_through_admit(env, monkeypatch):
    s = env.bootstrap()
    uid = env.new_ticket()
    seen = []
    import orch.store.store as sm

    real = sm.admit
    monkeypatch.setattr(sm, "admit", lambda *a, **k: seen.append(a[1]["type"]) or real(*a, **k))
    doc = json.loads(env.path(uid, "ticket.json").read_bytes())
    doc["title"] = "x"
    env.path(uid, "ticket.json").write_text(json.dumps(doc))
    env.path(uid, "body.md").write_text("## Context\n\nhand\n")
    (env.root / "keys.jsonl").write_bytes(b"")
    s.scan()
    assert seen and set(seen) <= {"projection.repaired", "edit.external"}
    kinds = {e["type"] for e in env.read_events(uid)[1:]} | {e["type"] for e in env.read_events("workspace")[2:]}
    assert kinds == {"projection.repaired", "edit.external"}


def test_the_index_is_never_consulted_for_authorization(env):
    import sqlite3

    s = env.bootstrap()
    env.new_ticket()
    token = f"{env.grant_id}.{crypto.b64u(GRANT_SECRET)}"
    hooks = hooks_for(s)
    hooks.grant_valid(Context(grant=token))
    db = sqlite3.connect(env.root / ".state" / "index.sqlite")
    db.execute("UPDATE members SET role = 'viewer'")
    db.commit()
    db.close()
    hooks.grant_valid(Context(grant=token))  # still valid: decided from the logs
    s.append(env.person_event(env.owner, "workspace", "grant.revoked", grant=env.grant_id), log="workspace")
    db = sqlite3.connect(env.root / ".state" / "index.sqlite")
    db.execute("DELETE FROM members")
    db.commit()
    db.close()
    with pytest.raises(OrchError):
        hooks.grant_valid(Context(grant=token))
    for p in (SRC / "store").rglob("*.py"):
        if p.name != "index.py":
            assert ".index.query" not in p.read_text() and "index.dump" not in p.read_text(), p


def test_a_long_lived_store_sees_a_revocation_made_by_another_process(env):
    s = env.bootstrap()
    token = f"{env.grant_id}.{crypto.b64u(GRANT_SECRET)}"
    hooks = hooks_for(s)
    hooks.grant_valid(Context(grant=token))
    other = Store.open(
        env.root, expected_workspace_id=WS, host=env.signer, host_state_dir=env.host_state, clock=lambda: env.clock[0]
    )
    other.append(env.person_event(env.owner, "workspace", "grant.revoked", grant=env.grant_id), log="workspace")
    with pytest.raises(OrchError):
        hooks.grant_valid(Context(grant=token))
