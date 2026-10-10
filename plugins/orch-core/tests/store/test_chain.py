"""The chain on disk: tamper, truncate, reorder, swap and forge; every one is found, none is written over."""

from __future__ import annotations

import json
import shutil

import pytest

from orch import canon, crypto
from orch.custody import FileBackend
from orch.store import BackendSigner, StoreError
from tests.store.helpers import WS, Env


@pytest.fixture
def ws(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    for i in range(3):
        env.log(uid, f"n{i}")
    s.close()
    return env, uid


def lines(env, uid):
    p = env.path(uid, "events.jsonl")
    return p, p.read_bytes().splitlines(keepends=True)


def reopen(env):
    return env.open()


def broken(s, log):
    return [e for e in s.chain_errors() if e[0] == log]


def test_a_clean_workspace_reads_clean(ws):
    env, uid = ws
    s = reopen(env)
    assert s.chain_errors() == [] and s.diverged == {} and s.reports == []


def test_changing_a_field_breaks_the_host_sig_at_that_event(ws):
    env, uid = ws
    p, ls = lines(env, uid)
    e = canon.parse_event_line(ls[2])
    e["text"] = "forged"
    ls[2] = canon.event_line(e)  # still exactly cj: only host_sig tells
    p.write_bytes(b"".join(ls))
    s = reopen(env)
    (err,) = broken(s, uid)
    assert err[1] == 3 and err[2] == "chain.broken"
    with pytest.raises(StoreError) as ex:
        s.append({"type": "log.added", "actor": env.agent, "text": "after"}, log=uid)
    assert ex.value.code == "chain.broken"
    assert s.append({"type": "log.added", "actor": env.agent, "text": "elsewhere"}, log=env.new_ticket())  # others live


def test_a_line_that_is_not_exactly_cj_ends_the_readable_log(ws):
    env, uid = ws
    p, ls = lines(env, uid)
    ls[1] = ls[1].replace(b'"type"', b' "type"', 1)  # one stray space
    p.write_bytes(b"".join(ls))
    s = reopen(env)
    ((log, seq, code, detail),) = broken(s, uid)
    assert (seq, code) == (2, "chain.broken") and "canonical" in detail
    with pytest.raises(StoreError) as ex:
        s.append({"type": "log.added", "actor": env.agent, "text": "x"}, log=uid)
    assert ex.value.code == "chain.broken"
    assert s.state.tickets[uid].key  # what was before the bad line still counts


def test_reordered_lines_break_the_chain(ws):
    env, uid = ws
    p, ls = lines(env, uid)
    ls[1], ls[2] = ls[2], ls[1]
    p.write_bytes(b"".join(ls))
    s = reopen(env)
    assert broken(s, uid)[0][1] == 2


def test_a_deleted_line_in_the_middle_breaks_the_chain(ws):
    env, uid = ws
    p, ls = lines(env, uid)
    del ls[1]
    p.write_bytes(b"".join(ls))
    assert broken(reopen(env), uid)[0][1] == 2


def test_a_torn_last_line_is_unreadable_not_silently_dropped(ws):
    env, uid = ws
    p, ls = lines(env, uid)
    p.write_bytes(b"".join(ls) + ls[-1][:40])  # a partial append that no manifest explains
    s = reopen(env)
    assert broken(s, uid)[0][1] == 5


def test_a_log_swapped_into_another_tickets_directory_fails_host_sig(env):
    s = env.bootstrap()
    a, b = env.new_ticket("a"), env.new_ticket("b")
    env.log(a)
    env.log(b)
    s.close()
    shutil.copy(env.path(a, "events.jsonl"), env.path(b, "events.jsonl"))
    s = reopen(env)
    assert broken(s, b)[0][1] == 1  # the host signed it as the log of ticket a
    assert not broken(s, a)


def test_a_workspace_log_from_another_workspace_is_not_trusted(env, tmp_path):
    env.bootstrap()
    env.store.close()
    other = Env(tmp_path / "other")
    other.bootstrap()
    other.store.close()
    shutil.copy(other.root / "events" / "workspace.jsonl", env.root / "events" / "workspace.jsonl")
    with pytest.raises(StoreError) as ex:
        env.open()
    assert ex.value.code == "trust.genesis_mismatch"


def test_a_line_signed_by_another_key_is_not_a_host_event(ws, tmp_path):
    env, uid = ws
    forger = BackendSigner(FileBackend(tmp_path / "forger"), "k")
    forger._backend.create("k")
    p, ls = lines(env, uid)
    e = canon.parse_event_line(ls[3])
    e["text"] = "from the forger"
    e["host_sig"] = crypto.b64u(forger.sign(canon.host_signing_bytes(WS, uid, e)))
    ls[3] = canon.event_line(e)
    p.write_bytes(b"".join(ls))
    s = reopen(env)
    assert broken(s, uid)[0][1] == 4


def test_a_forged_event_with_the_real_key_but_no_grant_does_not_count(ws):
    """P1 limit (N4): whoever holds the workspace key can append agent events. They still grant nothing: replay
    refuses an agent event whose grant does not exist, and the ticket is frozen for decisions."""
    env, uid = ws
    s = reopen(env)
    title = s.state.tickets[uid].title
    info = s._logs[uid]
    e = {
        "v": 2, "id": "01J9ZP0000000000000000F0RG", "seq": info.seq + 1, "at": "2026-09-21T14:30:00Z",
        "type": "ticket.updated", "actor": {**env.agent, "grant": "gr_01J9ZP0000000000000000FAKE"[:29]},
        "based_on": info.head, "prev": info.head, "hash_v": 1, "ws_seq": s._logs["workspace"].seq,
        "base_rev": {"ticket.title": canon.value_hash(title)}, "set": {"ticket.title": "pwned"},
    }  # fmt: skip
    e["host_sig"] = crypto.b64u(env.signer.sign(canon.host_signing_bytes(WS, uid, e)))
    s.close()
    with open(env.path(uid, "events.jsonl"), "ab") as f:
        f.write(canon.event_line(e))
    s = reopen(env)
    v = s.state.tickets[uid]
    assert v.title == title and v.frozen  # the event is absent for state and freezes decisions
    assert broken(s, uid) == []  # the chain itself is fine: the line is in its place


def test_host_sig_is_checked_on_every_read_not_trusted_from_a_cache(ws):
    env, uid = ws
    s = reopen(env)
    s.close()
    p, ls = lines(env, uid)
    e = canon.parse_event_line(ls[1])
    e["host_sig"] = canon.parse_event_line(ls[2])["host_sig"]
    ls[1] = canon.event_line(e)
    p.write_bytes(b"".join(ls))
    assert broken(reopen(env), uid)[0][1] == 2
    assert json.loads(env.path(uid, "ticket.json").read_bytes())["uid"] == uid
