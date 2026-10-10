"""Store.append: stamping, validation, admit, the projection files, the pins, the key allocator."""

from __future__ import annotations

import json

import pytest

from orch import canon, crypto
from orch.identity import CryptoVerifier
from orch.model import replay
from orch.store import Store, StoreError, render_ticket
from tests.store.helpers import GRANT_SECRET, WS, Env, snapshot, ts


def replayed(env: Env):
    """A fresh replay of the files on disk, the way any reader sees the workspace."""
    s = env.store
    ws = env.read_events("workspace")
    tickets = {p.name: env.read_events(p.name) for p in (env.root / "tickets").iterdir() if p.is_dir()}
    return replay(
        ws,
        tickets,
        verifier=CryptoVerifier(),
        now=s.state.now,
        expected_workspace_id=WS,
        expected_genesis=s.genesis,
    )


def test_a_workspace_from_nothing_replays_to_the_returned_state(env):
    s = env.bootstrap()
    uid = env.new_ticket("Load tariffs")
    r = env.update(uid, {"ticket.title": "Load tariff tables", "ticket.priority": "high"}, {"context": "Why: **x**"})
    fresh = replayed(env)
    assert not fresh.chain_errors and not fresh.workspace.invalid
    assert fresh.tickets[uid].fields == r.state.tickets[uid].fields
    assert fresh.tickets[uid].sections == r.state.tickets[uid].sections
    assert s.chain_errors() == [] and s.reports == []


def test_every_event_carries_the_stamped_envelope_and_a_host_sig_that_verifies(env):
    env.bootstrap()
    uid = env.new_ticket()
    env.log(uid, "hello")
    evs = env.read_events(uid) + env.read_events("workspace")
    assert [e["seq"] for e in env.read_events(uid)] == [1, 2]
    for log, events in (("workspace", env.read_events("workspace")), (uid, env.read_events(uid))):
        prev = None
        for e in events:
            assert e["prev"] == prev and e["hash_v"] == 1 and e["v"] == 2
            prev = canon.event_head(e)
            assert CryptoVerifier().verify_host(e, log=log, wsk_pub=env.wsk_pub, workspace_id=WS)
    assert all(e["ws_seq"] == 2 for e in env.read_events(uid))  # the workspace head when they were appended
    assert len(evs) == 4


def test_ticket_json_is_the_fixed_order_projection_and_rebuilds_byte_for_byte(env):
    env.bootstrap()
    uid = env.new_ticket("Seeds")
    env.update(uid, {"ticket.labels": ["dbt", "tariffs"], "ticket.size": "m"})
    raw = env.path(uid, "ticket.json").read_bytes()
    doc = json.loads(raw)
    assert list(doc) == [
        "schema", "uid", "key", "title", "type", "priority", "size", "labels", "parent", "blocked_by", "due",
        "visibility", "links", "acceptance", "tasks", "questions", "addons",
    ]  # fmt: skip
    assert list(doc["links"]) == ["repos", "branches", "prs", "external"]
    v = replayed(env).tickets[uid]
    assert render_ticket(uid, v.key, v.fields) == raw  # rebuilt from events alone
    assert raw.endswith(b"}\n") and b"\n  " in raw  # pretty-printed, one value per line


def test_body_md_has_the_f1_layout_and_sections_round_trip(env):
    env.bootstrap()
    uid = env.new_ticket()
    assert env.path(uid, "body.md").read_bytes() == b""
    env.update(uid, body={"requirements": "R1\n\n```\n## not a heading\n```", "context": "ctx"})
    assert env.path(uid, "body.md").read_text() == (
        "## Context\n\nctx\n\n## Requirements\n\nR1\n\n```\n## not a heading\n```\n"
    )
    assert env.store.body_sections(uid) == {"context": "ctx", "requirements": "R1\n\n```\n## not a heading\n```"}
    env.update(uid, body={"context": ""})
    assert env.path(uid, "body.md").read_text().startswith("## Context\n\n## Requirements\n\nR1")
    env.update(uid, body={"context": None})
    assert "Context" not in env.path(uid, "body.md").read_text()


def test_handoff_rewrites_current_state(env):
    env.bootstrap()
    uid = env.new_ticket()
    s = env.store
    s.append({"type": "claim.taken", "actor": env.agent}, log=uid)
    s.append({"type": "handoff.written", "actor": env.agent, "text": "Where it stands:\nnext T2"}, log=uid)
    assert env.path(uid, "body.md").read_text() == "## Current state\n\nWhere it stands:\nnext T2\n"
    assert not s.scan()  # the file matches the log: nothing to heal


def test_a_refused_event_changes_nothing_on_disk(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    before = snapshot(env.root)
    bad_agent = {**env.agent, "grant": "gr_01J9ZP0000000000000000000X"}
    with pytest.raises(StoreError) as e:
        s.append({"type": "log.added", "actor": bad_agent, "text": "x"}, log=uid)
    assert e.value.code == "grant.invalid" and e.value.refusal is not None
    with pytest.raises(StoreError) as e:
        s.append({"type": "log.added", "actor": env.agent, "text": "x", "extra": 1}, log=uid)
    assert e.value.code == "validation.event"
    assert snapshot(env.root) == before
    assert not (env.root / ".state" / "pending").exists() or not list((env.root / ".state" / "pending").iterdir())
    env.log(uid)  # and the store is still fine


def test_host_fields_are_stamped_by_the_store_only(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    for k, v in (("seq", 9), ("at", "2026-01-01T00:00:00Z"), ("prev", None), ("ws_seq", 1), ("host_sig", "A" * 86)):
        with pytest.raises(StoreError) as e:
            s.append({"type": "log.added", "actor": env.agent, "text": "x", k: v}, log=uid)
        assert e.value.code == "validation.host_field"


def test_at_is_the_clock_and_bumps_only_when_the_merged_order_needs_it(env):
    env.bootstrap()
    a, b = sorted([env.new_ticket("a"), env.new_ticket("b")])
    base = env.clock[0]
    stamps = []
    for uid in (a, a, b, a, b, b):  # a(k) then b(1) then a(k+1): the same second would sort a(k+1) before b(1)
        stamps.append(env.log(uid).event["at"])
    assert stamps == sorted(stamps)
    bumped = sum(1 for x, y in zip(stamps, stamps[1:], strict=False) if x != y) + (stamps[0] != ts(base))
    assert bumped <= 2  # b then a again is the only ordering that needs +1 s, twice at most
    assert max(stamps) <= ts(base + 2)
    assert not replayed(env).chain_errors
    env.clock[0] = base + 100  # the clock moves on: at follows it, no bump
    assert env.log(a).event["at"] == ts(base + 100)
    env.clock[0] = base - 50  # a clock that went backwards never makes `at` go backwards
    assert env.log(b).event["at"] == ts(base + 100)


def test_bad_ws_seq_is_restamped_once_and_never_surfaces_for_the_stores_own_ordering(env):
    s = env.bootstrap()
    a, b = sorted([env.new_ticket("a"), env.new_ticket("b")])
    env.log(b)
    s._last_pos = None  # a store that forgot the order: admit still refuses, the store restamps once
    r = env.log(a)
    assert r.event["seq"] == 2
    assert not replayed(env).chain_errors


def test_artifact_bytes_are_written_and_checked(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    data = b"\x89PNG fake"
    ev = {
        "type": "artifact.added",
        "actor": env.agent,
        "name": "shot.png",
        "kind": "screenshot",
        "sha256": canon.artifact_digest(data),
        "bytes": len(data),
    }
    with pytest.raises(StoreError) as e:
        s.append(ev, log=uid, artifacts={"shot.png": b"other"})
    assert e.value.code == "validation.artifact"
    with pytest.raises(StoreError):
        s.append(ev, log=uid)
    s.append(ev, log=uid, artifacts={"shot.png": data})
    assert (env.root / "tickets" / uid / "artifacts" / "shot.png").read_bytes() == data


def test_inline_artifact_references_are_checked(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    text = "see (artifact:missing.png)"
    from orch.store import section_entry

    wrong = {"context": {"hash": canon.section_hash(text), "refs": []}}
    with pytest.raises(StoreError) as e:
        s.append(
            {"type": "ticket.updated", "actor": env.agent, "base_rev": env.base_rev(uid, {}, wrong), "sections": wrong},
            log=uid,
            body={"context": text},
        )
    assert e.value.code == "body.bad_refs"
    with pytest.raises(StoreError) as e:
        env.update(uid, body={"context": text})  # honest refs, but no such artifact
    assert e.value.code == "body.unknown_artifact"
    assert section_entry(text)["refs"] == ["missing.png"]


def test_body_text_must_match_the_event_and_the_text_rules(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    from orch.store import section_entry

    sec = {"context": section_entry("one")}
    ev = {"type": "ticket.updated", "actor": env.agent, "base_rev": env.base_rev(uid, {}, sec), "sections": sec}
    for body in ({"context": "two"}, {}, {"context": "one", "plan": "x"}):
        with pytest.raises(StoreError) as e:
            s.append(ev, log=uid, body=body)
        assert e.value.code == "validation.body"
    forged = {"context": section_entry("a\n## Requirements\nb")}
    with pytest.raises(StoreError) as e:
        s.append(
            {**ev, "sections": forged, "base_rev": env.base_rev(uid, {}, forged)},
            log=uid,
            body={"context": "a\n## Requirements\nb"},
        )
    assert e.value.code == "validation.body"  # a forged heading would corrupt body.md


def test_read_only_store_cannot_append(env):
    env.bootstrap()
    env.store.close()
    ro = Store.open(env.root, expected_workspace_id=WS, host_state_dir=env.host_state)
    with pytest.raises(StoreError) as e:
        ro.append({"type": "log.added", "actor": env.agent, "text": "x"}, log="workspace")
    assert e.value.code == "store.read_only"
    assert len(ro.state.tickets) == 0 and ro.state.workspace.members


def test_genesis_pin_is_written_outside_the_workspace_and_enforced(env):
    s = env.bootstrap()
    pin = (env.host_state / "hosts" / WS / "genesis").read_text().strip()
    assert pin == s.genesis == canon.event_head(env.read_events("workspace")[0])
    assert not any(p.name == "genesis" for p in env.root.rglob("*"))  # the pin is not a file of the workspace
    s.close()
    with pytest.raises(StoreError) as e:
        Store.open(env.root, expected_workspace_id=WS, expected_genesis="sha256:" + "0" * 64, host=env.signer)
    assert e.value.code == "trust.genesis_mismatch"
    with pytest.raises(StoreError) as e:
        Store.open(env.root, expected_workspace_id="f" * 32, host=env.signer)
    assert e.value.code == "trust.genesis_mismatch"
    ok = Store.open(env.root, expected_workspace_id=WS, expected_genesis=pin, host=env.signer)
    assert ok.genesis == pin


def test_a_swapped_workspace_is_refused_by_the_pin(env, tmp_path):
    env.bootstrap()
    env.store.close()
    other = Env(tmp_path / "other")
    other.owner = other.owner
    other.bootstrap()
    other.store.close()
    import shutil

    shutil.rmtree(env.root)
    shutil.copytree(other.root, env.root)
    with pytest.raises(StoreError) as e:
        env.open(host=other.signer)
    assert e.value.code == "trust.genesis_mismatch"


def test_the_genesis_must_name_the_hosts_workspace_key(env):
    s = env.open()
    g = env.genesis()
    other = Env(env.tmp / "o2")
    with pytest.raises(StoreError) as e:
        Store.open(other.root, expected_workspace_id=WS, host=other.signer, clock=lambda: env.clock[0]).append(
            g, log="workspace"
        )
    assert e.value.code == "validation.host_key"
    assert s.state.workspace.genesis is None


def test_keys_jsonl_is_cj_lines_and_the_allocator_never_reuses_a_number(env):
    s = env.bootstrap()
    a, b = env.new_ticket(), env.new_ticket()
    lines = (env.root / "keys.jsonl").read_bytes().splitlines(keepends=True)
    assert len(lines) == 2 and all(canon.parse_event_line.__name__ for _ in lines)
    assert json.loads(lines[1])["key"] == "DEMO-0002"
    assert lines[0] == canon.cj_checked(json.loads(lines[0])) + b"\n"
    assert s.next_key() == "DEMO-0003"
    (env.root / "keys.jsonl").write_bytes(b"")  # a deleted file can not make a number reusable
    assert s.next_key() == "DEMO-0003"
    (env.root / "keys.jsonl").write_bytes(canon.cj_checked({"key": "DEMO-0099", "uid": a, "at": ts(1)}) + b"\n")
    assert s.next_key() == "DEMO-0100"  # ... and a number seen in keys.jsonl is never handed out again
    assert b


def test_normalise_ref(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    for ref in ("1", "#1", "demo-1", "DEMO-1", "DEMO-0001", uid):
        assert s.normalise_ref(ref) == "DEMO-0001"
    assert s.normalise_ref("nonsense") == "nonsense"
    assert s.uid_of("1") == uid and s.uid_of("77") is None
    assert s.head_seq(uid) == 1 and s.head_seq("workspace") == 2


def test_no_secret_is_written_anywhere_but_the_host_key_file(env):
    env.bootstrap()
    uid = env.new_ticket()
    env.log(uid, "note")
    secret_b64 = crypto.b64u(GRANT_SECRET)
    needles = [GRANT_SECRET, secret_b64.encode()]
    for root in (env.root, env.host_state):
        for p in root.rglob("*"):
            if p.is_file():
                data = p.read_bytes()
                assert not any(n in data for n in needles), p
    wsk_file = next((env.tmp / "wsk").glob("*.filekey.json"))
    scalar = json.loads(wsk_file.read_bytes())["d"]
    for p in list(env.root.rglob("*")) + list(env.host_state.rglob("*")):
        if p.is_file():
            assert scalar.encode() not in p.read_bytes(), p
    assert (wsk_file.stat().st_mode & 0o777) == 0o600
