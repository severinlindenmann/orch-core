"""A fake machine for ``orch update``: a signed fake release (TEST vectors), stub services, a stub installer."""

from __future__ import annotations

import importlib.util
import io
import json
import stat
from datetime import UTC, datetime
from pathlib import Path

from orch.migrate import init_root
from orch.update import keys as keys_mod
from orch.update.engine import Seams, Updater
from orch.update.layout import InstallLayout, Record, flip_current, write_record

DIR = Path(__file__).resolve().parent.parent / "vectors" / "release"
_spec = importlib.util.spec_from_file_location("gen_release_vectors_upd", DIR / "gen_release_vectors.py")
GEN = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(GEN)
NOW = datetime(2026, 10, 22, 8, 0, 0, tzinfo=UTC)
OLD = "2.0.0.dev41"


def use_test_root():
    return keys_mod.enable_test_root_for_tests()


class FakeSource:
    def __init__(self) -> None:
        self.env: dict[str, bytes] = {}
        self.blobs: dict[str, bytes] = {}
        self.fetched: list[str] = []

    def publish(self, channel: str, seq: int, *, tamper_file: bool = False, **over) -> str:
        m = GEN.manifest(channel, seq, **over)
        self.env[channel] = GEN.text(GEN.envelope(GEN.jcs(m), "rk-3")).encode()
        for f in m["files"]:
            data = GEN.FILE_BYTES[f["role"]]
            self.blobs[f["name"]] = data + b"x" if tamper_file and f["role"] == "core" else data
        return m["version"]

    def envelope(self, channel: str) -> bytes:
        return self.env[channel]

    def fetch(self, entry) -> bytes:
        self.fetched.append(entry["name"])
        return self.blobs[entry["name"]]


class FakeServices:
    def __init__(self, state: Path, layout: InstallLayout) -> None:
        self.state, self.layout = state, layout
        self.up = ["host", "runner", "dashboard"]
        self.events: list[str] = []
        self.problems_quarantine: list[str] = []
        self.problems_normal: list[str] = []
        self.started_with: list[tuple[str, bool]] = []

    def running(self) -> list[str]:
        return list(self.up)

    def stop(self) -> None:
        self.events.append("stop")
        self.up = []

    def start(self, orch_bin: Path, names, *, quarantine: bool) -> None:
        from orch.update import quarantine as q

        self.events.append("start-q" if quarantine else "start")
        self.started_with.append((orch_bin.parent.parent.parent.name, quarantine))
        assert q.active(self.state) == quarantine, "the quarantine marker must match the start mode"
        self.up = list(names)

    def health(self, *, quarantine: bool) -> list[str]:
        return list(self.problems_quarantine if quarantine else self.problems_normal)


def record(version: str = OLD, seq: dict | None = None, rev: str | None = "2026-10-20T00:00:00Z", min_serial: int = 1):
    return Record(
        "main", version, {"main": 41} if seq is None else seq, rev, min_serial, "root-1", "rk-3", ["root-1"],
        "2026-10-01T00:00:00Z", "/x", {"path": "/bin/true", "version": "0.13.3"}, [],
    )  # fmt: skip


class World:
    def __init__(self, tmp: Path) -> None:
        self.home = tmp / "home"
        self.home.mkdir()
        self.state = tmp / "state"
        self.env = {"HOME": str(self.home), "ORCH_STATE_DIR": str(self.state)}
        init_root(self.state, "host")
        (self.state / "data.txt").write_text("before\n")
        (self.state / "keys").mkdir()
        (self.state / "keys" / "wsk").write_text("secret-key-material\n")
        self.layout = InstallLayout.of(self.env)
        self.layout.ensure()
        self.source = FakeSource()
        self.services = FakeServices(self.state, self.layout)
        self.install_old()
        self.now = NOW
        self.free = 10**12
        self.runs: list = []
        self.migrate_fail = False
        self.migrate_calls = 0
        self.old_cannot_read_new = False
        self.out = io.StringIO()
        self.crash_at: str | None = None
        self.phases: list[str] = []

    # -- the installed old version
    def fake_bin(self, ver: str) -> None:
        d = self.layout.tool_dir(ver)
        (d / "venv" / "bin").mkdir(parents=True, exist_ok=True)
        b = d / "venv" / "bin" / "orch"
        b.write_text(f"#!/bin/sh\n# fake {ver}\n")
        b.chmod(b.stat().st_mode | stat.S_IXUSR)
        (d / ".complete").write_text("")

    def install_old(self) -> None:
        self.fake_bin(OLD)
        flip_current(self.layout, OLD)
        write_record(self.layout, record())

    # -- seams
    def installer(self, layout, ver, files, rec) -> None:
        assert set(files) >= {"core", "constraints"}
        self.fake_bin(ver)

    def run_orch(self, orch_bin: Path, args) -> tuple[int, str]:
        ver = orch_bin.parent.parent.parent.name.removeprefix("orch-")
        if list(args) == ["--version", "--json"]:
            return 0, json.dumps({"version": ver})
        if list(args)[:1] == ["migrate"] and "--apply" in args:
            self.migrate_calls += 1
            if self.migrate_fail:
                return 5, json.dumps({"ok": False})
            (self.state / "data.txt").write_text("migrated\n")
            (self.state / "keys" / "wsk").write_text("changed\n")
            return 0, json.dumps({"ok": True, "roots": []})
        if list(args)[:1] == ["migrate"] and "--check" in args:
            if self.old_cannot_read_new:
                return 3, '{"error": {"code": "format.newer"}}'
            return 0, json.dumps({"ok": True, "roots": []})
        raise AssertionError(args)

    def after_phase(self, phase: str) -> None:
        self.phases.append(phase)
        if self.crash_at is not None and phase.split(":")[0] == self.crash_at:
            from orch.update.engine import Crash

            raise Crash(phase)

    def seams(self) -> Seams:
        return Seams(
            source=self.source,
            services=self.services,
            installer=self.installer,
            run_orch=self.run_orch,
            free_bytes=lambda p: self.free,
            live_runs=lambda state: self.runs,
            now=lambda: self.now,
            after_phase=self.after_phase,
            roots=lambda: ([("this computer's orch data", "host", self.state)], []),
            health_wait=0.0,
            sleep=lambda s: None,
        )

    def updater(self) -> Updater:
        return Updater(self.env, self.seams(), self.out)
