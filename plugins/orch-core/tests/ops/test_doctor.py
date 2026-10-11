"""``orch doctor``: full verification, tamper cases, and the safe repairs only."""

from __future__ import annotations

import json
import shutil

import pytest

from tests.ops.helpers import Cli
from tests.store.helpers import snapshot


@pytest.fixture
def two(ws, cli):
    """Two tickets with a few events each, checkpoints written."""
    for t in ("one", "two"):
        assert cli("new", t, "-m", f"text {t}").code == 0
    assert cli("claim", "DEMO-0001").code == 0
    assert cli("log", "-m", "a note").code == 0
    assert cli("release").code == 0
    from orch.instructions import write_workspace_files

    write_workspace_files(ws.root)
    ws.store.checkpoint()
    return ws


def doctor(ws, *args):
    return Cli(ws, grant=False, session=None).j("doctor", *args)


def codes(r):
    return [f["what"].split(":")[0] for f in r.data["findings"]]


def log_path(ws, ref):
    return ws.root / "tickets" / ws.uid(ref) / "events.jsonl"


def test_a_healthy_workspace_is_clean_and_doctor_changes_nothing(two):
    ws = two
    before = snapshot(ws.root)
    r = doctor(ws)
    assert r.code == 0 and r.data["problems"] == 0, r.out
    assert snapshot(ws.root) == before


def test_an_edited_line_is_reported_with_log_seq_and_cause(two):
    ws = two
    p = log_path(ws, "DEMO-0001")
    lines = p.read_bytes().split(b"\n")
    lines[1] = lines[1].replace(b'"seq":2', b'"seq":2 ').replace(b"a note", b"a NOTE")
    p.write_bytes(b"\n".join(lines))
    r = doctor(two)
    assert r.code == 5
    f = [x for x in r.data["findings"] if x["level"] == "error"][0]
    assert f["where"].startswith("DEMO-0001 (") and f["where"].endswith("#2")
    assert f["what"].startswith("chain.broken")


def test_a_changed_byte_inside_a_line_breaks_the_chain_at_that_seq(two):
    ws = two
    p = log_path(ws, "DEMO-0001")
    raw = p.read_bytes()
    assert b"a note" in raw
    p.write_bytes(raw.replace(b"a note", b"a nope"))
    seq = next(i for i, ln in enumerate(raw.split(b"\n"), 1) if b"a note" in ln)
    r = doctor(ws)
    assert r.code == 5 and [x["where"].rpartition("#")[2] for x in r.data["findings"]] == [str(seq)]
    assert codes(r) == ["chain.broken"] and "host_sig" in r.data["findings"][0]["what"]


def test_a_truncated_log_is_diverged_from_its_checkpoint(two):
    ws = two
    p = log_path(ws, "DEMO-0001")
    lines = p.read_bytes().splitlines(keepends=True)
    p.write_bytes(b"".join(lines[:-1]))
    r = doctor(ws)
    assert r.code == 5
    assert any(c == "chain.diverged" for c in codes(r)), r.out


def test_swapped_logs_are_refused(two):
    ws = two
    a, b = log_path(ws, "DEMO-0001"), log_path(ws, "DEMO-0002")
    ta, tb = a.read_bytes(), b.read_bytes()
    a.write_bytes(tb)
    b.write_bytes(ta)
    r = doctor(ws)
    assert r.code == 5
    assert any(c.startswith(("chain", "trust")) for c in codes(r)), r.out


def test_a_forged_host_sig_is_reported(two):
    ws = two
    p = log_path(ws, "DEMO-0001")
    lines = p.read_bytes().split(b"\n")
    ev = json.loads(lines[1])
    sig = ev["host_sig"]
    ev["host_sig"] = ("A" if sig[0] != "A" else "B") + sig[1:]
    lines[1] = json.dumps(ev, sort_keys=True, separators=(",", ":")).encode()
    p.write_bytes(b"\n".join(lines))
    r = doctor(ws)
    assert r.code == 5
    f = [x for x in r.data["findings"] if x["level"] == "error"][0]
    assert f["where"].endswith("#2") and f["what"].startswith("chain.")


def test_a_bad_checkpoint_is_reported(two):
    ws = two
    cp = next((ws.root / ".state" / "checkpoints").glob("ticket-*.json"))
    doc = json.loads(cp.read_text())
    doc["sig"] = ("A" if doc["sig"][0] != "A" else "B") + doc["sig"][1:]
    cp.write_text(json.dumps(doc))
    r = doctor(ws)
    assert r.code == 5 and "checkpoint.bad" in codes(r)


def test_a_missing_pin_is_a_warning_and_a_wrong_pin_an_error(two):
    ws = two
    pin = ws.host_state / "hosts" / "705d40abbb8c1c90354a1acaa94c935c" / "genesis"
    keep = pin.read_text()
    pin.unlink()
    r = doctor(ws)
    assert r.code == 0 and "pin.missing" in codes(r)
    assert not pin.exists()  # doctor never pins
    pin.write_text("sha256:" + "0" * 64 + "\n")
    r = doctor(ws)
    assert r.code == 5 and "trust.genesis_mismatch" in codes(r)
    pin.write_text(keep)


def test_a_hand_edited_ticket_file_is_found_and_repaired_through_the_store(two):
    ws = two
    p = ws.root / "tickets" / ws.uid("DEMO-0001") / "ticket.json"
    p.write_text(p.read_text().replace('"priority": "medium"', '"priority": "urgent"'))
    r = doctor(ws)
    assert r.code == 5 and "projection.ticket" in codes(r)
    assert "urgent" in p.read_text()  # reporting did not repair
    r = doctor(ws, "--repair")
    assert r.code == 0, r.out
    assert "urgent" not in p.read_text()
    types = [e["type"] for e in ws.events("DEMO-0001")]
    assert "projection.repaired" in types  # the repair is an event, signed by the host


def test_an_edited_body_section_is_found(two):
    ws = two
    p = ws.root / "tickets" / ws.uid("DEMO-0001") / "body.md"
    p.write_text(p.read_text() + "\n## Context\n\nsneaked in\n")
    r = doctor(ws)
    assert r.code == 5 and "projection.body" in codes(r)


def test_a_replaced_artifact_is_found(two, tmp_path):
    ws, cli = two, Cli(two)
    f = tmp_path / "e.log"
    f.write_text("evidence\n")
    assert cli("artifact", "add", str(f), "--ref", "DEMO-0001").code == 0
    ws.store.checkpoint()
    (ws.root / "tickets" / ws.uid("DEMO-0001") / "artifacts" / "e.log").write_text("swapped\n")
    r = doctor(ws)
    assert "artifact.mismatch" in codes(r)


def test_a_stale_index_is_rebuilt_by_repair_only(two):
    ws = two
    ws.store.rebuild_index()
    idx = ws.root / ".state" / "index.sqlite"
    idx.write_bytes(b"not a database")
    r = doctor(ws)
    assert "index.stale" in codes(r) and r.code == 0
    assert idx.read_bytes() == b"not a database"
    r = doctor(ws, "--repair")
    assert "index.stale" not in codes(r)


def test_keys_of_a_dead_init_are_reported_and_swept_by_repair(two):
    ws = two
    d = ws.host_state / "hosts" / ("a" * 32)
    (d / "keys").mkdir(parents=True)
    (d / "keys" / "wsk.key").write_text("x")
    import subprocess
    import sys

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (d / ".init-incomplete").write_text(f"{dead.pid}\n")
    r = doctor(ws)
    assert "keys.orphan" in codes(r) and d.exists()
    r = doctor(ws, "--repair")
    assert "keys.orphan" not in codes(r) and not d.exists()
    other = (
        ws.host_state / "hosts" / ("b" * 32)
    )  # keys with no marker are not an init of ours: left alone, not reported
    (other / "keys").mkdir(parents=True)
    assert "keys.orphan" not in codes(doctor(ws))


def test_an_unobservable_repository_is_a_warning(two):
    ws = two
    ws.store = ws.other()  # one that knows the tickets the CLI made
    ws.repo("proj")
    shutil.rmtree(ws.tmp / "proj")
    r = doctor(ws)
    assert "repo.unobservable" in codes(r) and r.code == 0


def test_doctor_is_terse(two):
    r = Cli(two, grant=False, session=None)("doctor")
    assert r.code == 0 and len(r.out.splitlines()) <= 25 and r.out.startswith("ok doctor ")


def test_deleted_checkpoints_cannot_hide_a_rollback_where_the_host_key_is(two):
    ws = two
    p = log_path(ws, "DEMO-0001")
    lines = p.read_bytes().splitlines(keepends=True)
    p.write_bytes(b"".join(lines[:-1]))
    shutil.rmtree(ws.root / ".state" / "checkpoints")
    r = doctor(ws)
    assert r.code == 5 and "checkpoint.missing" in codes(r)
    assert Cli(ws, grant=False, session=None).j("check").code == 5  # the fast check says so too


def test_missing_checkpoints_are_only_a_warning_without_the_host_key(two):
    ws = two
    shutil.rmtree(ws.root / ".state" / "checkpoints")
    shutil.rmtree(ws.host_state / "hosts" / "705d40abbb8c1c90354a1acaa94c935c" / "keys")  # a clone: no workspace key
    r = doctor(ws)
    assert r.code == 0 and "checkpoint.missing" in codes(r)


def test_a_ticket_folder_without_a_log_and_an_unlisted_artifact_are_reported(two):
    ws = two
    uid = ws.uid("DEMO-0001")
    shutil.copytree(ws.root / "tickets" / uid, ws.root / "tickets" / "01J9ZK4Q7M3R8T2V6X0B5N1C9D")
    (ws.root / "tickets" / "01J9ZK4Q7M3R8T2V6X0B5N1C9D" / "events.jsonl").unlink()
    (ws.root / "tickets" / uid / "artifacts").mkdir(exist_ok=True)
    (ws.root / "tickets" / uid / "artifacts" / "stray.txt").write_text("x")
    r = doctor(ws)
    assert r.code == 5 and "ticket.nolog" in codes(r) and "artifact.unlisted" in codes(r)


def test_no_workspace_and_a_corrupt_config_are_a_declared_not_found(two, tmp_path):
    for target in (tmp_path / "nothing",):
        target.mkdir()
        env = {"ORCH_WORKSPACE": str(target), "ORCH_STATE_DIR": str(two.host_state), "HOME": str(tmp_path)}
        import io

        from orch.cli.main import main

        for cmd in ("doctor", "check"):
            out, err = io.StringIO(), io.StringIO()
            code = main([cmd, "--json"], env=env, stdout=out, stderr=err, now=lambda: two.clock[0])
            assert code == 2 and json.loads(out.getvalue())["error"]["code"] == "not_found"
    (two.root / "config.json").write_text("{not json")
    assert doctor(two).code == 2 and doctor(two).err_code == "not_found"


def test_a_failing_doctor_does_not_start_with_ok(two):
    p = two.root / "tickets" / two.uid("DEMO-0001") / "ticket.json"
    p.write_text(p.read_text().replace('"medium"', '"urgent"'))
    r = Cli(two, grant=False, session=None)("doctor")
    assert r.code == 5 and r.first.startswith("doctor: 1 errors") and not r.out.startswith("ok")
