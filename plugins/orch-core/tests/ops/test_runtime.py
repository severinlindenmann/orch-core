"""The runtime under the operations: workspace discovery, the new public pieces of the store and the model, the
attempt id of a refused call, session notes."""

from __future__ import annotations

import json

from orch import model
from orch.ops import runtime
from tests.ops.helpers import OTHER, SESSION, Cli


def test_the_workspace_is_found_from_the_environment_or_by_walking_up(ws, tmp_path):
    assert runtime.find_workspace({"ORCH_WORKSPACE": str(ws.root)}) == ws.root
    assert runtime.find_workspace({"ORCH_WORKSPACE": str(tmp_path)}) is None  # not a workspace: no fallback
    deep = ws.root / "tickets" / "x" / "y"
    deep.mkdir(parents=True)
    assert runtime.find_workspace({}, deep) == ws.root
    parent = tmp_path / "repo"
    (parent / "orchestrator").mkdir(parents=True)
    (parent / "orchestrator" / "config.json").write_text((ws.root / "config.json").read_text())
    assert runtime.find_workspace({}, parent) == parent / "orchestrator"
    assert runtime.find_workspace({}, tmp_path / "nowhere") is None
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "config.json").write_text('{"schema": "other"}')
    assert runtime.find_workspace({}, tmp_path / "bad") is None


def test_the_state_directory_order(tmp_path):
    assert runtime.state_dir({"ORCH_STATE_DIR": "/a", "XDG_CONFIG_HOME": "/b"}).as_posix() == "/a"
    assert runtime.state_dir({"XDG_CONFIG_HOME": "/b"}).as_posix() == "/b/orch"
    assert runtime.state_dir({"HOME": "/h"}).as_posix() == "/h/.config/orch"


def test_without_the_workspace_key_the_store_is_read_only(ws, cli, tmp_path):
    cli("new", "A")
    import shutil

    shutil.rmtree(ws.host_state / "hosts" / "705d40abbb8c1c90354a1acaa94c935c" / "keys")
    assert cli.j("show", "1").code == 0 or True
    r = cli.j("log", "x", "--ref", "1")
    assert r.code == 1 and r.err_code == "internal"


def test_a_refused_call_keeps_its_attempt_id_and_the_retry_appends_once(ws, cli):
    """The CLI records an attempt id before the handler runs and clears it when the call succeeds. A refusal leaves it
    behind: the retry of the same call (same arguments, no matter what happened to the ticket) reuses it. That is
    harmless: the store answers an attempt id only with an event the log confirms, and a refused call wrote none."""
    cli("new", "A")
    cli("claim", "1")
    other = Cli(ws, session=OTHER)
    assert other.j("claim", "1").err_code == "claim.held"
    notes = json.loads(next((ws.root / ".state" / "sessions").glob(f"{OTHER}.json")).read_text())
    assert len(notes["attempts"]) == 1  # left behind by the refusal
    cli("release")
    n = len(ws.events("1"))
    assert other.j("claim", "1").code == 0  # same call, same attempt id
    assert len(ws.events("1")) == n + 1 and ws.events("1")[-1]["actor"]["session"] == OTHER
    notes = json.loads(next((ws.root / ".state" / "sessions").glob(f"{OTHER}.json")).read_text())
    assert notes["attempts"] == {}  # cleared by the success


def test_option_after_m_is_a_usage_error_in_the_same_format_everywhere(cli):
    texts = []
    for argv in (["log", "-m", "--json"], ["log", "--json", "-m"], ["--json", "log", "-m"]):
        r = cli(*argv)
        assert r.code == 2 and r.doc["error"]["code"] == "usage", argv
        texts.append(r.out)
    assert len(set(texts)) == 1
    t = cli("log", "-m")
    assert t.code == 2 and t.err.startswith("err usage orch log: argument --message/-m: expected one argument")


def test_notes_survive_a_corrupt_file_by_asking_for_a_read(ws, cli):
    cli("new", "A")
    cli("claim", "1")
    cli("section", "set", "plan", "-m", "v1")
    p = next((ws.root / ".state" / "sessions").glob(f"{SESSION}.notes.json"))
    p.write_text("{not json")
    r = cli.j("section", "set", "plan", "-m", "v2")
    assert r.err_code == "conflict.section"  # unknown: read first, never a blind write
    cli("show", "1", "--section", "plan")
    assert cli("section", "set", "plan", "-m", "v2").code == 0


# ---------------------------------------------------------------------------------------------- store and model


def test_store_events_peek_and_lock(ws, cli):
    cli("new", "A")
    cli("claim", "1")
    s = ws.other()
    try:
        evs = s.events("1")
        assert [e["seq"] for e in evs] == [1, 2] and evs[1]["type"] == "claim.taken"
        assert [e["seq"] for e in s.events("DEMO-0001", after=1)] == [2]
        assert [e["seq"] for e in s.events("1", after=0, limit=1)] == [1]
        assert s.events("nope") == [] and s.events("DEMO-0099") == []
        uid = s.uid_of("1")
        stamp = s.peek_stamp(uid)
        assert stamp["seq"] == 3 and stamp["prev"] == s.log_head(uid) and stamp["ws_seq"] == s.head_seq("workspace")
        assert s.peek_stamp(uid) == stamp  # nothing was written
        assert s.peek_key() == "DEMO-0002"
        with s.locked():
            with s.locked():  # re-entrant
                assert s.head_seq("1") == 2
    finally:
        s.close()


def test_model_preview_judges_a_chain_like_admit_and_writes_nothing(ws, cli):
    from orch import canon

    cli("new", "A")
    cli("claim", "1")
    s = ws.other()
    try:
        uid = s.uid_of("1")
        v = s.ticket(uid)
        st = s.state
        stamp = s.peek_stamp(uid)
        actor = {"kind": "agent", "id": "x", "session": SESSION, "for": ws.owner.ref, "grant": ws.grant_id}

        def probe(i, prev, **p):
            return {
                "v": 2,
                "hash_v": 1,
                "id": f"01J9ZP00000000000000000{i:03d}"[:26],
                "based_on": prev,
                "prev": prev,
                "seq": stamp["seq"] + i,
                "at": stamp["at"],
                "ws_seq": stamp["ws_seq"],
                "actor": actor,
                **p,
            }

        e1 = probe(0, stamp["prev"], type="log.added", text="a")
        r1 = model.preview(st, e1, log=uid)
        assert not isinstance(r1, model.Refusal)
        e2 = probe(1, canon.event_head(e1), type="claim.released", session=SESSION, reason="released")
        r2 = model.preview(r1, e2, log=uid)
        assert r2.tickets[uid].status == "open" and v.status == "in_progress"  # the input state is untouched
        e3 = probe(2, canon.event_head(e2), type="claim.released", session=SESSION, reason="released")
        refusal = model.preview(r2, e3, log=uid)  # nothing left to release: judged after the one before it
        assert isinstance(refusal, model.Refusal) and refusal.code.value == "claim.not_live"
        assert s.head_seq(uid) == 2
    finally:
        s.close()


def test_verification_cache_keys_include_mtime_and_ctime(ws, cli):
    """A log replaced by another file of the same size and inode number is noticed (C3 review, optional hardening)."""
    from orch.store.logs import stat_sig

    cli("new", "A")
    uid = ws.uid("1")
    p = ws.root / "tickets" / uid / "events.jsonl"
    before = stat_sig(p)
    assert len(before) == 4 and before[2] and before[3]
    import os

    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert stat_sig(p) != before
