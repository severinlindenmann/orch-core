"""Every path builder refuses ``..``, absolute paths, NUL, separators and symlinks (security review)."""

from __future__ import annotations

import json
import os

import pytest

from orch import canon
from orch.store import StoreError
from orch.store.paths import check_artifact, check_uid, safe_join, target_ok

BAD_UIDS = [
    "../x",
    "/etc/passwd",
    "a\x00b",
    "01J9ZK4Q7M3R8T2V6X0B5N1C9D/..",
    "01j9zk4q7m3r8t2v6x0b5n1c9d",
    "",
    "workspace",
]
BAD_NAMES = ["../x.png", "/abs.png", "a/b.png", "a\x00.png", ".hidden", "..", "x" * 129, "a\\b"]


@pytest.mark.parametrize("bad", BAD_UIDS)
def test_uid_pattern(bad):
    with pytest.raises(StoreError) as e:
        check_uid(bad)
    assert e.value.code == "validation.path"


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_artifact_pattern(bad):
    with pytest.raises(StoreError):
        check_artifact(bad)


@pytest.mark.parametrize(
    "bad", ["../config.json", "/etc/passwd", "tickets/../../x/ticket.json", "tickets/a/ticket.json", ".state/applied",
            "events/workspace.jsonl", "tickets/01J9ZK4Q7M3R8T2V6X0B5N1C9D/../../evil", "config.json\x00"]
)  # fmt: skip
def test_manifest_targets_are_an_allowlist(bad):
    with pytest.raises(StoreError):
        target_ok(bad)


def test_safe_join_refuses_escapes_and_symlinks(tmp_path):
    root = tmp_path / "ws"
    (root / "tickets").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    for bad in ("../x", "/abs", "a/../../x", "a\x00b", "a\\b"):
        with pytest.raises(StoreError):
            safe_join(root, bad)
    os.symlink(outside, root / "tickets" / "01J9ZK4Q7M3R8T2V6X0B5N1C9D")
    with pytest.raises(StoreError):
        safe_join(root, "tickets/01J9ZK4Q7M3R8T2V6X0B5N1C9D/ticket.json")
    assert safe_join(root, "tickets/OTHER/ticket.json") == root / "tickets/OTHER/ticket.json"


def test_log_names_are_validated(env):
    s = env.bootstrap()
    for bad in BAD_UIDS[:-1]:
        with pytest.raises(StoreError) as e:
            s.append({"type": "log.added", "actor": env.agent, "text": "x"}, log=bad)
        assert e.value.code == "validation.path"
        with pytest.raises(StoreError):
            s.body_sections(bad)
        with pytest.raises(StoreError):
            s.ticket_json(bad)
        with pytest.raises(StoreError):
            s.log_path(bad)


def test_a_symlinked_ticket_directory_is_not_followed(env, tmp_path):
    s = env.bootstrap()
    uid = env.new_ticket()
    s.close()
    real = env.root / "tickets" / uid
    moved = tmp_path / "moved"
    real.rename(moved)
    os.symlink(moved, real)
    s = env.open()
    assert uid not in s.state.tickets  # the linked directory is not read as a ticket
    before = (moved / "ticket.json").read_bytes()
    assert not (moved / "extra").exists()
    assert (moved / "ticket.json").read_bytes() == before


def test_a_symlinked_projection_file_is_replaced_not_written_through(env, tmp_path):
    s = env.bootstrap()
    uid = env.new_ticket()
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    p = env.path(uid, "ticket.json")
    p.unlink()
    os.symlink(victim, p)
    (ev,) = s.scan()
    assert ev["type"] == "projection.repaired"
    assert victim.read_text() == "precious"
    assert not p.is_symlink() and json.loads(p.read_bytes())["uid"] == uid


def test_a_symlinked_log_is_refused(env, tmp_path):
    s = env.bootstrap()
    uid = env.new_ticket()
    s.close()
    log = env.path(uid, "events.jsonl")
    data = log.read_bytes()
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_bytes(data)
    log.unlink()
    os.symlink(elsewhere, log)
    s = env.open()
    assert uid in s.diverged  # a symlinked log is not a log: the ticket's checkpoint finds it missing
    with pytest.raises(StoreError):
        env.log(uid)
    assert elsewhere.read_bytes() == data


def test_a_hostile_pending_manifest_cannot_write_outside_the_files_the_store_owns(env, tmp_path):
    s = env.bootstrap()
    uid = env.new_ticket()
    s.close()
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    pdir = env.root / ".state" / "pending" / "01J9ZP0000000000000000EVIL"
    pdir.mkdir(parents=True)
    (pdir / "0").write_bytes(b"pwned")
    line = canon.event_line({"hash_v": 1})
    for target in ("../../../victim.txt", str(victim), "tickets/../../victim.txt"):
        m = {"v": 1, "id": "01J9ZP0000000000000000EVIL", "log": uid, "seq": 9, "line": line.decode(),
             "files": [{"to": target, "n": 0, "sha256": "0" * 64}], "body_copy": "../x"}  # fmt: skip
        (pdir / "manifest.json").write_text(json.dumps(m))
        with open(env.path(uid, "events.jsonl"), "ab") as f:
            f.write(line)
        try:
            env.open()
        except StoreError:
            pass
        assert victim.read_text() == "precious"
        env.path(uid, "events.jsonl").write_bytes(env.path(uid, "events.jsonl").read_bytes()[: -len(line)])
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "0").write_bytes(b"pwned")


def test_artifact_names_cannot_leave_the_artifacts_directory(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    data = b"x"
    for bad in ("../ticket.json", "/tmp/evil", "a/b", "a\x00b"):
        with pytest.raises(StoreError):
            s.append(
                {"type": "artifact.added", "actor": env.agent, "name": bad, "kind": "log",
                 "sha256": canon.artifact_digest(data), "bytes": 1},
                log=uid, artifacts={bad: data},
            )  # fmt: skip
    assert not (env.root / "tickets" / "ticket.json").exists()
