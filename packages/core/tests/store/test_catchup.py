"""H12b: catch-up checkpoints (§11A.5, P3-D37): ``Store.mint_catchup`` and ``Checkpoints.sign_prefix``.

Invariants: every minted ``n`` is above every existing ``n``; a queued restore (anchor) is never re-signed, renumbered
or superseded; a prefix lists only what its cut holds (never a later ticket entry); the last prefix is a fresh
snapshot; the outbox replacement is atomic; ``sign_prefix`` refuses an inconsistent prefix.
"""

from __future__ import annotations

import json

import pytest

from orch import canon
from orch.store import StoreError
from orch.store.checkpoints import sign_object, verify_object
from orch.store.logs import WORKSPACE
from orch.store.outbox import Segment
from tests.store.helpers import WS, Env
from tests.store.test_outbox import _boom, _Crash, link, outbox_dir, workspace_cp


def measure(segments: tuple[Segment, ...]) -> int:
    """A stand-in for R3's exact counter: a manifest line per segment plus 74 bytes per head."""
    assert all(isinstance(s, Segment) and s.heads for s in segments)
    return sum(64 + 74 * len(s.heads) for s in segments)


@pytest.fixture
def grown(env):
    """A linked workspace with three tickets and some history; returns (env, uids)."""
    env.bootstrap()
    link(env)
    env.store.close()
    env.open()
    uids = []
    for t in range(3):
        uid = env.new_ticket(title=f"t{t}")
        uids.append(uid)
        for i in range(3):
            env.log(uid, f"{t}-{i}")
    return env, uids


def _entries(cp: dict) -> dict:
    o = cp["o"]
    return {WORKSPACE: o["workspace_log"], **o["tickets"]}


def _existing_ns(env: Env) -> int:
    s = env.store
    return max(s._checkpoints.highest_n(), s._pins.high_n(), max((e.n for e in s.outbox_entries()), default=0))


def test_catchup_from_nothing_splits_under_the_cap_and_ends_at_a_fresh_snapshot(grown):
    env, uids = grown
    s = env.store
    m = _existing_ns(env)
    steps: list[int] = []

    def counting(segments):
        size = measure(segments)
        steps.append(size)
        return size

    out = s.mint_catchup(source=None, cap=600, measure=counting)
    assert len(out) >= 3
    assert [e.n for e in out] == list(range(m + 1, m + 1 + len(out)))
    assert len({e.checkpoint["o"]["at"] for e in out}) == 1  # one `at` for all
    assert all(e.kind == "plain" and e.catchup for e in out)
    assert [e.n for e in s.outbox_entries()] == [e.n for e in out]  # the superseded plain entries are gone
    # nested prefixes, each step under the cap
    prev: dict = {}
    for e in out:
        assert verify_object(env.wsk_pub, e.checkpoint)
        cut = _entries(e.checkpoint)
        for log, entry in prev.items():
            assert cut[log]["seq"] >= entry["seq"]
        segs = tuple(
            Segment(log, prev.get(log, {}).get("seq", 0), prev.get(log, {}).get("acc", "sha256:" + "0" * 64),
                    tuple(s._logs[log].heads[prev.get(log, {}).get("seq", 0) : c["seq"]]))
            for log, c in sorted(cut.items(), key=lambda kv: (kv[0] != WORKSPACE, kv[0]))
            if c["seq"] > prev.get(log, {}).get("seq", 0)
        )  # fmt: skip
        assert measure(segs) <= 600
        prev = cut
    last = out[-1].checkpoint
    assert _entries(last)[WORKSPACE]["head"] == s.log_head(WORKSPACE)
    for uid in uids:
        assert last["o"]["tickets"][uid]["head"] == s.log_head(uid)
    assert workspace_cp(env) == last and s._pins.high_n() == out[-1].n
    env.log(uids[0], "after")  # the next ordinary checkpoint numbers above the catch-up
    assert s.outbox_entries()[-1].n == out[-1].n + 1


def test_a_prefix_never_lists_a_later_ticket_entry(grown):
    env, uids = grown
    out = env.store.mint_catchup(source=None, cap=400, measure=measure)
    first = out[0].checkpoint["o"]
    ws_cut = first["workspace_log"]["seq"]
    for uid, entry in first["tickets"].items():
        events = env.read_events(uid)
        assert entry["seq"] <= len(events)
        assert events[entry["seq"] - 1]["ws_seq"] <= ws_cut
        assert entry["head"] == canon.event_head(events[entry["seq"] - 1])
    assert set(first["tickets"]) < set(uids) or len(out) == 1  # tickets created later are absent, not carried


def test_catchup_from_a_held_source_extends_it(grown):
    env, uids = grown
    s = env.store
    source = s.outbox_entries()[0].checkpoint  # an older checkpoint the relay holds (the lowest entry)
    out = s.mint_catchup(source=source, cap=500, measure=measure)
    for log, entry in _entries(source).items():
        assert _entries(out[0].checkpoint)[log]["seq"] >= entry["seq"]
    assert all(e.n > _existing_ns(env) - len(out) for e in out)


def test_a_source_that_is_not_in_the_logs_is_refused(grown):
    env, _ = grown
    s = env.store
    o = json.loads(json.dumps(workspace_cp(env)["o"]))
    o["workspace_log"]["head"] = "sha256:" + "2" * 64  # validly signed, but another branch
    forked = sign_object(env.signer.sign, o)
    before = sorted(p.name for p in outbox_dir(env).iterdir())
    with pytest.raises(StoreError) as exc:
        s.mint_catchup(source=forked, cap=500, measure=measure)
    assert exc.value.code == "chain.diverged"
    assert sorted(p.name for p in outbox_dir(env).iterdir()) == before


def test_an_unsigned_source_is_refused(grown):
    env, _ = grown
    cp = json.loads(json.dumps(workspace_cp(env)))
    cp["o"]["at"] = "2030-01-01T00:00:00Z"
    with pytest.raises(StoreError) as exc:
        env.store.mint_catchup(source=cp, cap=500, measure=measure)
    assert exc.value.code == "checkpoint.prefix_invalid"


def test_a_queued_restore_is_an_anchor_and_catchup_mints_nothing(grown):
    env, _ = grown
    s = env.store
    facts = s.restore_facts("workspace")
    s.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="other"), log="workspace")
    before = {p.name: p.read_bytes() for p in outbox_dir(env).iterdir()}
    with pytest.raises(StoreError) as exc:
        s.mint_catchup(source=None, cap=500, measure=measure)
    assert exc.value.code == "relay.too_large"
    assert {p.name: p.read_bytes() for p in outbox_dir(env).iterdir()} == before


def test_a_step_that_cannot_fit_mints_nothing(grown):
    env, _ = grown
    before = {p.name: p.read_bytes() for p in outbox_dir(env).iterdir()}
    cp_before = workspace_cp(env)
    with pytest.raises(StoreError) as exc:
        env.store.mint_catchup(source=None, cap=100, measure=measure)
    assert exc.value.code == "relay.too_large"
    assert {p.name: p.read_bytes() for p in outbox_dir(env).iterdir()} == before
    assert workspace_cp(env) == cp_before


def test_a_crash_while_signing_changes_nothing(grown, monkeypatch):
    env, _ = grown
    before = {p.name: p.read_bytes() for p in outbox_dir(env).iterdir()}
    monkeypatch.setattr(env.store._checkpoints, "sign_prefix", _boom)
    with pytest.raises(_Crash):
        env.store.mint_catchup(source=None, cap=500, measure=measure)
    assert {p.name: p.read_bytes() for p in outbox_dir(env).iterdir()} == before


def test_a_crash_while_replacing_the_outbox_is_finished_at_the_next_lock(grown, monkeypatch):
    env, uids = grown
    s = env.store
    from orch.store import outbox as outbox_mod

    calls = {"n": 0}
    real = outbox_mod.Outbox.write

    def die_on_second(self, entry):
        calls["n"] += 1
        if calls["n"] == 2:
            raise _Crash()
        return real(self, entry)

    monkeypatch.setattr(outbox_mod.Outbox, "write", die_on_second)
    with pytest.raises(_Crash):
        s.mint_catchup(source=None, cap=500, measure=measure)
    monkeypatch.undo()
    s.close()
    s = env.open(load="lazy")
    entries = s.outbox_entries()
    assert entries and all(e.catchup for e in entries)
    assert [e.n for e in entries] == list(range(entries[0].n, entries[0].n + len(entries)))
    assert entries[-1].checkpoint == workspace_cp(env)
    assert not (outbox_dir(env) / "catchup.json").exists()


# --- sign_prefix ---------------------------------------------------------------------------------------------------


def _prefix_args(env: Env, **over):
    s = env.store
    s.load_all()
    ws = s._logs[WORKSPACE]
    cut = {WORKSPACE: (ws.seq, ws.head, ws.acc)}
    for uid, info in s._logs.items():
        if uid != WORKSPACE:
            cut[uid] = (info.seq, info.head, info.acc)
    keys = {}
    for log in s._logs:
        for e in env.read_events(log):
            keys[(log, e["seq"])] = (
                (e["seq"], 0, 0, "", e["seq"]) if log == WORKSPACE else (e["ws_seq"], 1, e["at"], log, e["seq"])
            )
    args = dict(
        genesis=s._pin, n=_existing_ns(env) + 1, at=workspace_cp(env)["o"]["at"], cut=cut, logs=s._logs, base=None,
        key=lambda log, seq: keys[(log, seq)], floor_n=_existing_ns(env),
    )  # fmt: skip
    args.update(over)
    return args


def test_sign_prefix_signs_a_consistent_full_cut(grown):
    env, _ = grown
    cp = env.store._checkpoints.sign_prefix(env.signer.sign, **_prefix_args(env))
    assert verify_object(env.wsk_pub, cp) and cp["o"]["kind"] == "workspace_checkpoint"
    assert cp["o"]["workspace_id"] == WS


def _refused(env: Env, **over) -> str:
    with pytest.raises(StoreError) as exc:
        env.store._checkpoints.sign_prefix(env.signer.sign, **_prefix_args(env, **over))
    return exc.value.code


def test_sign_prefix_refuses_an_n_at_or_below_an_existing_one(grown):
    env, _ = grown
    top = _existing_ns(env)
    assert _refused(env, n=top) == "checkpoint.prefix_invalid"
    assert _refused(env, n=top + 1, floor_n=top + 1) == "checkpoint.prefix_invalid"


def test_sign_prefix_refuses_a_head_the_log_does_not_hold(grown):
    env, uids = grown
    args = _prefix_args(env)
    seq, _head, acc = args["cut"][uids[0]]
    cut = dict(args["cut"], **{uids[0]: (seq, "sha256:" + "3" * 64, acc)})
    assert _refused(env, cut=cut) == "checkpoint.prefix_invalid"


def test_sign_prefix_refuses_a_seq_beyond_the_log(grown):
    env, uids = grown
    args = _prefix_args(env)
    seq, head, acc = args["cut"][uids[0]]
    assert _refused(env, cut=dict(args["cut"], **{uids[0]: (seq + 1, head, acc)})) == "checkpoint.prefix_invalid"


def test_sign_prefix_refuses_a_ticket_resting_on_a_later_workspace_head(grown):
    env, uids = grown
    s = env.store
    args = _prefix_args(env)
    ws = s._logs[WORKSPACE]
    cut = dict(args["cut"])
    cut[WORKSPACE] = (1, ws.heads[0], ws.accs[0])  # only the genesis; the tickets rest on later workspace heads
    assert _refused(env, cut=cut) == "checkpoint.prefix_invalid"


def test_sign_prefix_refuses_a_cut_that_is_not_downward_closed(grown):
    env, uids = grown
    s = env.store
    args = _prefix_args(env)
    cut = dict(args["cut"])
    first = s._logs[uids[0]]
    cut[uids[0]] = (1, first.heads[0], first.accs[0])  # drop t0's later events but keep t1's and t2's (merged later)
    assert _refused(env, cut=cut) == "checkpoint.prefix_invalid"


def test_sign_prefix_refuses_a_cut_that_drops_part_of_its_base(grown):
    env, uids = grown
    s = env.store
    base = workspace_cp(env)
    args = _prefix_args(env, base=base)
    cut = dict(args["cut"])
    info = s._logs[uids[2]]
    cut[uids[2]] = (info.seq - 1, info.heads[-2], info.accs[-2])
    assert _refused(env, cut=cut, base=base) == "checkpoint.prefix_invalid"


def test_sign_prefix_refuses_an_at_before_its_base(grown):
    env, _ = grown
    base = workspace_cp(env)
    assert _refused(env, base=base, at="2000-01-01T00:00:00Z") == "checkpoint.prefix_invalid"
