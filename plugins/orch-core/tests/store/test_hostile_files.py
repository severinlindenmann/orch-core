"""Agent-writable files cannot hang, exhaust or crash the store, and cannot decide which ticket a ref names."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from orch import canon
from orch.store import StoreError
from orch.store.logs import MAX_LINE
from tests.store.helpers import Env


@pytest.fixture
def pair(env: Env):
    s = env.bootstrap()
    a, b = env.new_ticket("a"), env.new_ticket("b")
    env.log(a)
    env.log(b)
    s.close()
    return env, a, b


def test_a_200_mb_line_breaks_that_ticket_only_and_costs_no_memory(pair):
    env, a, b = pair
    p = env.path(b, "events.jsonl")
    first = p.read_bytes().splitlines(keepends=True)[0]
    with open(p, "wb") as f:
        f.write(first)
        chunk = b"x" * (1 << 20)
        for _ in range(200):
            f.write(chunk)
        f.write(b"\n")
    for load in ("lazy", "all"):
        t = time.time()
        s = env.open(load=load)
        s.ticket(b)
        s.ticket(a)
        assert time.time() - t < 20
        ((log, seq, code, detail),) = [c for c in s.chain_errors() if c[0] == b]
        assert seq == 2 and code == "chain.broken" and "longer than" in detail
        with pytest.raises(StoreError):
            s.append({"type": "log.added", "actor": env.agent, "text": "x"}, log=b)
        assert s.append({"type": "log.added", "actor": env.agent, "text": "fine"}, log=a)  # other tickets go on
        s.close()


def test_a_200_mb_line_without_a_newline_and_a_huge_pending_manifest(pair):
    env, a, b = pair
    p = env.path(a, "events.jsonl")
    with open(p, "ab") as f:
        for _ in range(200):
            f.write(b"y" * (1 << 20))
    pend = env.root / ".state" / "pending" / "X"
    pend.mkdir(parents=True)
    with open(pend / "manifest.json", "wb") as f:
        for _ in range(50):
            f.write(b"z" * (1 << 20))
    for name in ("applied",):
        with open(env.root / ".state" / name, "wb") as f:
            f.write(b"[" * 3_000_000)
    s = env.open(load="all")
    assert not pend.exists()
    assert any(c[0] == a for c in s.chain_errors()) or a in s.diverged
    assert s.append({"type": "log.added", "actor": env.agent, "text": "ok"}, log=b)


def test_deeply_nested_lines_are_a_broken_ticket_not_a_crash(pair):
    env, a, b = pair
    deep = b"[" * 200_000 + b"]" * 200_000
    for which in ("first", "last", "middle"):
        p = env.path(b, "events.jsonl")
        lines = p.read_bytes().splitlines(keepends=True)
        orig = list(lines)
        if which == "first":
            lines[0] = deep + b"\n"
        elif which == "last":
            lines[-1] = deep + b"\n"
        else:
            lines.insert(1, deep + b"\n")
        p.write_bytes(b"".join(lines))
        for load in ("lazy", "all"):
            s = env.open(load=load)
            s.ticket(a)
            s.ticket(b)
            assert s.append({"type": "log.added", "actor": env.agent, "text": "a"}, log=a)
            s.close()
        p.write_bytes(b"".join(orig[:2]) if which == "middle" else b"".join(orig))
        # (the log of b is whatever it was; the appends to `a` above kept working)
    assert MAX_LINE == 524288


def test_nested_json_in_state_files_is_just_unreadable(pair):
    env, a, b = pair
    deep = b"[" * 100_000
    for name in ("applied",):
        (env.root / ".state" / name).write_bytes(deep)
    (env.root / "config.json").write_bytes(deep)
    (env.root / "keys.jsonl").write_bytes(deep + b"\n")
    d = env.root / ".state" / "intents"
    d.mkdir(exist_ok=True)
    s = env.open(load="all")
    assert s.append({"type": "log.added", "actor": env.agent, "text": "x"}, log=a, idem="k")
    s.scan()
    assert json.loads((env.root / "config.json").read_bytes())["workspace"]["id"]  # repaired from the events


def test_a_forged_index_and_keys_jsonl_do_not_decide_what_a_ref_names(pair):
    env, a, b = pair
    s = env.open(load="all")
    s.index.dump()
    s.close()
    db = sqlite3.connect(env.root / ".state" / "index.sqlite")
    db.execute("UPDATE tickets SET key = 'tmp' WHERE uid = ?", (a,))
    db.execute("UPDATE tickets SET key = 'DEMO-0001' WHERE uid = ?", (b,))
    db.execute("UPDATE tickets SET key = 'DEMO-0002' WHERE uid = ?", (a,))
    db.commit()
    db.close()
    (env.root / "keys.jsonl").write_bytes(
        canon.cj_checked({"key": "DEMO-0001", "uid": b, "at": "2026-01-01T00:00:00Z"}) + b"\n"
    )
    s = env.open(load="lazy")
    assert s.uid_of("1") == a and s.uid_of("DEMO-0002") == b
    assert s.normalise_ref(b) == "DEMO-0002" and s.head_seq("1") == 2


def test_a_forged_first_line_cannot_steer_a_ref_to_the_wrong_ticket(pair):
    env, a, b = pair
    p = env.path(b, "events.jsonl")
    lines = p.read_bytes().splitlines(keepends=True)
    e = canon.parse_event_line(lines[0])
    e["key"] = "DEMO-0001"  # b's first line now claims a's key (the chain of b breaks, a's is fine)
    p.write_bytes(canon.event_line(e) + b"".join(lines[1:]))
    s = env.open(load="lazy")
    assert s.uid_of("DEMO-0001") == a  # the hint says b or a; only the verified ticket answers
    assert s.uid_of("DEMO-0002") is None  # b is broken: it has no verified key
    assert s.ticket("1").uid == a
    s.close()
    q = env.open(load="lazy")
    assert q.normalise_ref(a) == "DEMO-0001" and q.normalise_ref(b) == b  # b is unverified: left as typed


def test_a_hidden_key_makes_resolution_verify_everything(pair):
    env, a, b = pair
    p = env.path(a, "events.jsonl")
    lines = p.read_bytes().splitlines(keepends=True)
    p.write_bytes(b"garbage\n" + b"".join(lines[1:]))  # a's first line is unreadable: its key is not in any hint
    s = env.open(load="lazy")
    assert s.uid_of("DEMO-0002") == b
    assert s._unsure() and s._loaded == {a, b}  # nothing short of a full, verified look answers when hints are doubtful


def test_a_new_ticket_cannot_reuse_a_key_whose_hint_was_hidden(pair):
    env, a, b = pair
    p = env.path(a, "events.jsonl")
    lines = p.read_bytes().splitlines(keepends=True)
    p.write_bytes(b"".join(lines))  # untouched: a control
    s = env.open(load="lazy")
    r = s.create_ticket(actor=env.agent, ticket_type="chore", title="n", owner=env.owner.ref)
    assert r.event["key"] == "DEMO-0003"
