"""H2a pure units: the §0.5 effective-column table row by row, intents, unit files, the release layout, pins."""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from orch.host import units
from orch.host.columns import Column, refusal, resolve
from orch.host.custody import check_tree
from orch.host.errors import HostError
from orch.host.intents import KEEP_SECONDS, TOMBSTONE_SECONDS, IntentStore
from orch.host.pins import AgentPins
from orch.host.release import ReleaseError, install_release, preload

UID = os.getuid()
WRITES = ("agent", "unattended")

# --- §0.5 effective column, one row at a time --------------------------------------------------------------


def test_row_person_no_grant_valid_mapping_is_p():
    col = resolve("person", "absent", True)
    assert (col.name, col.viewer) == ("P", "person")
    assert refusal(col, "read") is None and refusal(col, "person_read") is None
    assert refusal(col, "unattended") is None and refusal(col, "human_api") is None
    assert refusal(col, "agent") == "op.not_allowed"
    assert refusal(col, "human") == "human_only"


def test_row_person_no_grant_invalid_mapping_reads_as_no_person_and_every_write_is_op_not_allowed():
    col = resolve("person", "absent", False)
    assert (col.name, col.viewer) == ("N", "no-person")
    assert refusal(col, "read") is None
    for kind in WRITES:
        assert refusal(col, kind) == "op.not_allowed"  # L1: no unattended writes either
    assert refusal(col, "human_api") == "op.not_allowed"
    assert refusal(col, "person_read") == "op.not_allowed"


@pytest.mark.parametrize("mapping", [True, False])
def test_row_person_verified_grant_is_g(mapping):
    col = resolve("person", "verified", mapping)
    assert (col.name, col.viewer) == ("G", "grant")
    assert refusal(col, "agent") is None and refusal(col, "unattended") is None and refusal(col, "read") is None
    assert refusal(col, "human_api") == "human_only" and refusal(col, "human") == "human_only"
    assert refusal(col, "person_read") == "op.not_allowed"


@pytest.mark.parametrize("mapping", [True, False])
@pytest.mark.parametrize(("grant", "code"), [("malformed", "grant.required"), ("unverified", "grant.expired")])
def test_row_person_unverified_grant(mapping, grant, code):
    col = resolve("person", grant, mapping)
    assert (col.name, col.viewer) == ("N", "no-person")
    assert refusal(col, "read") is None
    for kind in WRITES:
        assert refusal(col, kind) == code  # not op.not_allowed, unattended writes included
    assert refusal(col, "human_api") == "human_only"


def test_row_agent_no_grant_is_n():
    col = resolve("agent", "absent", False)
    assert (col.name, col.viewer) == ("N", "no-person")
    assert refusal(col, "unattended") is None and refusal(col, "read") is None
    assert refusal(col, "agent") == "op.not_allowed" and refusal(col, "human_api") == "op.not_allowed"


def test_row_agent_verified_grant_is_g():
    col = resolve("agent", "verified", False)
    assert (col.name, col.viewer) == ("G", "grant")
    assert refusal(col, "agent") is None and refusal(col, "human_api") == "op.not_allowed"


@pytest.mark.parametrize(("grant", "code"), [("malformed", "grant.required"), ("unverified", "grant.expired")])
def test_row_agent_unverified_grant(grant, code):
    col = resolve("agent", grant, False)
    assert col.name == "N" and refusal(col, "agent") == code and refusal(col, "unattended") == code
    assert refusal(col, "read") is None and refusal(col, "human_api") == "op.not_allowed"


def test_rows_worker_and_control():
    assert resolve("worker", "absent", False) == Column("W", "none")
    assert resolve("control", "absent", False) == Column("C", "none")
    assert refusal(Column("W", "none"), "job") is None and refusal(Column("W", "none"), "read") == "op.not_allowed"
    assert refusal(Column("C", "none"), "control") is None and refusal(Column("C", "none"), "job") == "op.not_allowed"
    for peer in ("worker", "control"):
        with pytest.raises(ValueError, match="req.invalid"):
            resolve(peer, "verified", False)


# --- intents --------------------------------------------------------------------------------------------------


@pytest.fixture
def intents(tmp_path):
    now = [1_800_000_000.0]
    store = IntentStore(tmp_path / "state", UID, clock=lambda: now[0])
    (tmp_path / "state").mkdir(mode=0o700)
    store.open()
    return store, now


def test_intent_lifecycle_and_retention(intents):
    store, now = intents
    intent = store.begin("control:1", "uid:1", "a_x", "host.register", {"root": "/r"})
    path = store.dir / intent.key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(store.dir.stat().st_mode) == 0o700
    assert json.loads(path.read_text())["step"] == "started"
    assert store.outcome("control:1", "uid:1", "a_x") == {"state": "pending"}
    store.complete(intent, {"data": 1})
    assert store.begin("control:1", "uid:1", "a_x", "host.register", {"root": "/r"})["result"] == {"data": 1}
    with pytest.raises(HostError, match="idem.conflict"):
        store.begin("control:1", "uid:1", "a_x", "host.register", {"root": "/other"})
    with pytest.raises(HostError, match="outcome.unknown_idem"):
        store.outcome("control:1", "uid:2", "a_x")  # another principal's record is not there
    now[0] += KEEP_SECONDS + 1
    store.sweep()
    with pytest.raises(HostError, match="idem.expired"):
        store.outcome("control:1", "uid:1", "a_x")
    with pytest.raises(HostError, match="idem.expired"):
        store.begin("control:1", "uid:1", "a_x", "host.register", {"root": "/r"})
    tomb = json.loads(path.read_text())
    assert set(tomb) >= {"key", "binding", "state"} and "result" not in tomb
    now[0] += TOMBSTONE_SECONDS + 1
    store.sweep()
    assert not path.exists()


def test_a_concurrent_duplicate_waits_for_the_first(intents):
    store, _ = intents
    intent = store.begin("n", "p", "a_y", "host.unregister", {"workspace_id": "w"})
    got = []
    thread = threading.Thread(
        target=lambda: got.append(store.begin("n", "p", "a_y", "host.unregister", {"workspace_id": "w"}))
    )
    thread.start()
    thread.join(0.2)
    assert thread.is_alive() and not got  # waiting, not running the op a second time
    store.fail(intent, HostError("not_found", "nope"))
    thread.join(5)
    assert got[0]["step"] == "failed" and got[0]["error"]["code"] == "not_found"


def test_abandon_lets_the_same_key_run_again(intents):
    store, _ = intents
    intent = store.begin("n", "p", "a_z", "host.register", {})
    store.abandon(intent)
    assert store.started() == []
    assert store.begin("n", "p", "a_z", "host.register", {}).step == "started"


# --- units (content only; never installed here) -------------------------------------------------------------


def test_launchd_plist():
    doc = plistlib.loads(units.launchd_plist(Path("/py/bin/python3"), Path("/rel/2.0"), Path("/s"), log_dir=Path("/l")))
    assert doc["Label"] == "io.orch.host" and doc["RunAtLoad"] is True and doc["KeepAlive"] is True
    # the launcher is the program (macOS lists the item as "orch-host", not "python3.14"); its shebang has the -I
    assert doc["ProgramArguments"] == ["/rel/2.0/bin/orch-host", "run", "--state", "/s"]
    assert "AssociatedBundleIdentifiers" not in doc  # needs an installed signed app; orch ships none
    assert doc["Umask"] == 0o077 and doc["StandardErrorPath"] == "/l/host.log"


def test_systemd_units_and_tmpfiles():
    user = units.systemd_unit(Path("/py"), Path("/rel/v"), Path("/s"))
    assert 'ExecStart="/py" "-I" "/rel/v/bin/orch-host" "run" "--state" "/s"\n' in user  # systemd quoting (H7)
    assert "Restart=always" in user and "WantedBy=default.target" in user and "User=" not in user
    system = units.systemd_unit(Path("/py"), Path("/rel/v"), Path("/s"), system=True, user="orch", host_uid=990)
    assert '"--run" "/run/orch/990"' in system and "User=orch" in system and "WantedBy=multi-user.target" in system
    with pytest.raises(ValueError):
        units.systemd_unit(Path("/py"), Path("/rel/v"), Path("/s"), system=True)
    assert (
        units.tmpfiles_conf("orch", "orch", 990) == "d /run/orch 0755 root root -\nd /run/orch/990 0711 orch orch -\n"
    )


def test_orch_host_unit_prints_without_installing(tmp_path):
    import io

    from orch.cli.main import main

    out = io.StringIO()
    env = {"HOME": str(tmp_path), "ORCH_STATE_DIR": str(tmp_path / "s")}
    assert main(["host", "unit", "launchd"], env=env, stdout=out) == 0
    assert plistlib.loads(out.getvalue().encode())["Label"] == "io.orch.host"
    assert not (tmp_path / "Library").exists()


# --- release ----------------------------------------------------------------------------------------------------


def test_release_install_is_immutable_and_runnable(tmp_path):
    source = Path(__import__("orch").__file__).parent
    target = tmp_path / "releases"
    release = install_release("9.9.9", target=target, source=source)
    assert release == target / "9.9.9"
    manifest = json.loads((release / "RELEASE.json").read_text())
    assert manifest["version"] == "9.9.9" and "host/service.py" in manifest["files"]
    assert stat.S_IMODE((release / "orch" / "host" / "service.py").stat().st_mode) == 0o444
    assert stat.S_IMODE(release.stat().st_mode) == 0o555
    assert not list(release.rglob("__pycache__"))
    assert install_release("9.9.9", target=target, source=source) == release  # same bytes: a no-op
    changed = tmp_path / "src"
    import shutil

    shutil.copytree(source, changed, ignore=shutil.ignore_patterns("__pycache__"))
    (changed / "host" / "units.py").write_text("# changed\n")
    with pytest.raises(ReleaseError, match="already installed"):
        install_release("9.9.9", target=target, source=changed)
    run = subprocess.run(
        [sys.executable, "-I", str(release / "bin" / "orch-host"), "unit", "systemd", "--state", "/s"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )
    assert run.returncode == 0, run.stderr
    assert "ExecStart=" in run.stdout
    for path in [release, *release.rglob("*")]:
        os.chmod(path, 0o755 if path.is_dir() else 0o644)


def test_preload_imports_every_module():
    names = preload()
    assert "orch.host.service" in names and "orch.store.store" in names and "orch.cli.main" in names


# --- pins and custody -----------------------------------------------------------------------------------------


def test_pins_fail_closed(tmp_path):
    pins_dir = tmp_path / "pins"
    pins = AgentPins(pins_dir, owner=UID)
    assert pins.get(501) is None and not pins.pinned(501)  # no pin directory at all: nothing is pinned
    pins_dir.mkdir(mode=0o755)
    assert not pins.pinned(501)  # no pin file
    (pins_dir / "501.json").write_text(json.dumps({"class": "agent", "workspaces": ["a" * 32]}))
    assert pins.pinned(501) and pins.names_workspace(501, "a" * 32) and not pins.names_workspace(501, "b" * 32)
    assert AgentPins(pins_dir, owner=UID + 1).pinned(501)  # a directory of another owner: fail closed
    (pins_dir / "502.json").write_text("{broken")
    assert pins.pinned(502) and pins.names_workspace(502, "b" * 32)  # unreadable: pinned for every workspace
    os.chmod(pins_dir / "501.json", 0o666)
    assert pins.names_workspace(501, "b" * 32)  # writable by others: fail closed
    os.chmod(pins_dir / "501.json", 0o644)
    os.chmod(pins_dir, 0o775)
    assert pins.pinned(503)  # a pin directory others can write: fail closed for every UID
    os.chmod(pins_dir, 0o000)
    try:
        assert pins.pinned(503)  # EACCES is never "no pin"
    finally:
        os.chmod(pins_dir, 0o755)


def test_custody_tree_refuses_links_and_writable_ancestors(tmp_path):
    good = tmp_path / "a" / "b"
    good.mkdir(parents=True)
    check_tree(Path(os.path.realpath(good)), UID)
    os.chmod(tmp_path / "a", 0o777)
    with pytest.raises(HostError, match="custody.unsafe_mode"):
        check_tree(Path(os.path.realpath(good)), UID)
    os.chmod(tmp_path / "a", 0o755)
    (tmp_path / "link").symlink_to(good)
    with pytest.raises(HostError, match="custody.unsafe_mode"):
        check_tree(Path(os.path.realpath(tmp_path)) / "link", UID)
