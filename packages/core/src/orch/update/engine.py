"""``orch update``: phases, journal, commit point and rollback (pilot plan sections 2, 5, 9: B2, B3, S1-S3, S9).

Phases (each one is written to the P1-A journal ``<state>/install/update.json`` as it is reached)::

    fetch+verify -> space -> no agent runs                  (nothing is changed; no journal)
    stop services, journal "stopped", prove no one holds a root
    backup           journal "backed-up"   (raw copy of the stopped data, keys included, 0700)
    install          journal "installed"   (new tools dir, from the verified files only)
    migrate          journal "migrated:<root>" per root (the NEW version's ``orch migrate --apply``)
    health           the new code runs in QUARANTINE (:mod:`orch.update.quarantine`) and is checked
    COMMIT           flip ``current``, journal "committed", record, normal restart

Before the commit any failure rolls back code AND data: ``current`` is flipped back, every root is restored from its
raw backup (revocations are merged, never replaced), the services start on the old code. After the commit nothing is
rolled back: a failing health check only reports, and ``--use <old version>`` re-selects the old code while the data
stays (the old code refuses the newer format by itself).

Everything that touches the machine is a seam on :class:`Seams`, so the tests run with stubs.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TextIO

from orch import oslock
from orch.migrate import backup as backup_mod
from orch.migrate.format import lock_root, read_format
from orch.migrate.journal import clear_journal, read_journal, write_journal
from orch.migrate.steps import CURRENT, atomic_write
from orch.ops.errors import OrchError
from orch.ops.runtime import state_dir

from . import quarantine
from .envelope import parse_envelope
from .floor import check_revocations
from .keys import embedded_roots
from .layout import (
    InstallLayout,
    Record,
    check_version,
    flip_current,
    new_record,
    prune_cache,
    prune_tools,
    read_record,
    version_key,
    write_record,
)
from .verify import UpdateRefused, Verified, verify_envelope

__all__ = ["Crash", "Outcome", "Seams", "Source", "Updater"]


class Crash(BaseException):
    """Test seam: simulates the process dying (not an ``Exception``, so no rollback code runs)."""


class Source(Protocol):
    def envelope(self, channel: str) -> bytes:
        """The signed pointer of ``channel`` (``channel.json``). Raises ``OrchError('update.unreachable')``."""

    def fetch(self, entry: Mapping[str, Any]) -> bytes:
        """The bytes of one manifest file entry (``name``, ``url``, ``size`` ...)."""


class Services(Protocol):
    def running(self) -> list[str]: ...
    def stop(self) -> None: ...
    def start(self, orch_bin: Path, names: Sequence[str], *, quarantine: bool) -> None: ...
    def health(self, *, quarantine: bool) -> list[str]:
        """Problems found (plain words); empty when everything answers."""


@dataclass
class Seams:
    source: Source
    services: Services
    installer: Callable[[InstallLayout, str, Mapping[str, Path], Record], None]
    run_orch: Callable[[Path, Sequence[str]], tuple[int, str]]
    free_bytes: Callable[[Path], int] = lambda p: shutil.disk_usage(p).free
    live_runs: Callable[[Path], list[Any]] | None = None
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    after_phase: Callable[[str], None] | None = None
    #: the data roots to protect: ``[(label, kind, path)]`` and notes about workspaces that cannot be reached
    roots: Callable[[], tuple[list[tuple[str, str, Path]], list[str]]] | None = None
    health_wait: float = 60.0
    sleep: Callable[[float], None] = time.sleep


@dataclass
class Outcome:
    ok: bool
    action: str  # updated | current | rolled-back | check | used
    from_version: str | None = None
    to_version: str | None = None
    message: str = ""
    log: Path | None = None
    warnings: list[str] = field(default_factory=list)
    code: str | None = None


def _fmt(root: Path, kind: str) -> int:
    n = read_format(root, kind)
    return n if n is not None else CURRENT[kind]


def dir_size(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


class Updater:
    def __init__(self, env: Mapping[str, str], seams: Seams, out: TextIO) -> None:
        self.env, self.s, self.out = env, seams, out
        self.layout = InstallLayout.of(env)
        self.state = state_dir(env)
        self._log: TextIO | None = None
        self.log_path: Path | None = None

    # ------------------------------------------------------------------------------------------ small helpers
    def say(self, line: str) -> None:
        self.out.write(line + "\n")
        self.out.flush()
        self.note(line)

    def note(self, line: str) -> None:
        if self._log is not None:
            self._log.write(f"{datetime.now(UTC).strftime('%H:%M:%S')} {line}\n")
            self._log.flush()

    def _open_log(self) -> None:
        self.layout.ensure()
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self.log_path = self.layout.logs / f"update-{stamp}-{os.getpid()}.log"
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._log = os.fdopen(fd, "a")

    def _phase(self, phase: str, **fields: Any) -> None:
        write_journal(phase, self.env, **fields)
        self.note(f"phase {phase}")
        if self.s.after_phase is not None:
            self.s.after_phase(phase)

    def _lock(self) -> int:
        self.layout.ensure()
        fd = os.open(self.layout.lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            oslock.flock(fd, oslock.LOCK_EX | oslock.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise OrchError("migrate.running", "another `orch update` is running") from None
        return fd

    # ------------------------------------------------------------------------------------------ verify
    def check(self, channel: str | None = None) -> tuple[Record, str, Verified]:
        """Fetch and verify the channel pointer. Nothing is changed. Returns (record, channel, verified)."""
        rec = read_record(self.layout)
        ch = channel or rec.channel
        env_bytes = self.s.source.envelope(ch)
        try:
            ver = verify_envelope(
                env_bytes,
                purpose="core",
                channel=ch,
                now=self.s.now(),
                seq_seen=rec.seq(ch),
                revocations_issued_at=rec.revocations_issued_at,
            )
        except UpdateRefused as exc:
            raise OrchError(exc.code, f"the release was refused ({exc.reason.value}); nothing was changed") from None
        check_revocations(
            ver, self.s.now(), stored_issued_at=rec.revocations_issued_at, stored_min_serial=rec.min_serial
        )
        self._envelope = env_bytes
        return rec, ch, ver

    def installed_version(self, rec: Record) -> str:
        return self.layout.current_version() or rec.version

    # ------------------------------------------------------------------------------------------ the update
    def update(self, channel: str | None = None, *, assume: str | None = None) -> Outcome:
        """The whole update. ``assume`` is ``finish``/``rollback`` for an unfinished journal."""
        self.layout.ensure()
        lock = self._lock()
        try:
            self._open_log()
            try:
                return self._update(channel, assume)
            finally:
                if self._log is not None:
                    self._log.close()
        finally:
            os.close(lock)

    def _update(self, channel: str | None, assume: str | None) -> Outcome:
        doc = read_journal_safe(self.env)
        if doc is not None and doc["phase"] != "committed":
            return self._resume(doc, assume)
        if assume == "rollback":
            raise OrchError(
                "update.after_commit", "the last update is committed: its data stays; use `orch update --use VERSION`"
            )
        rec = read_record(self.layout)
        self._heal(rec, doc)
        rec = read_record(self.layout)
        rec, ch, ver = self.check(channel)
        cur = self.installed_version(rec)
        new = check_version(ver.version)
        if version_key(new) < version_key(cur):
            raise OrchError("update.downgrade", f"{new} on channel {ch} is older than the installed {cur}")
        manifest = ver.document
        if version_key(cur) < version_key(manifest["min_from"]):
            raise OrchError("update.too_old", f"{cur} is too old to update directly: run the installer again")
        if new == cur:
            return Outcome(True, "current", cur, cur, f"orch {cur} is the newest version on channel {ch}")
        self.say(f"orch {cur} -> {new} (channel {ch})")
        files = self._download(manifest, new)
        roots, notes = self._roots()
        self._space(roots, manifest)
        runs = self.s.live_runs(self.state) if self.s.live_runs else []
        if runs:
            raise OrchError(
                "update.agents_running",
                f"{len(runs)} agent run(s) are active: let them finish or stop them (orch stop all), then update again",
            )
        # ---- from here on the machine changes
        names = self.s.services.running()
        self.s.services.stop()
        info = {
            "from_version": cur, "to_version": new, "channel": ch, "seq": ver.seq, "running": names, "backups": [],
            "roots": [str(r[2].resolve()) for r in roots],
            "revocations_issued_at": ver.revocations_issued_at, "root_key_id": self._root_key_id(),
            "min_serial": ver.min_serial, "release_key_id": self._release_key_id(),
            "trusted_roots": sorted(embedded_roots()), "log": str(self.log_path),
            "files": [{k: f[k] for k in ("role", "name", "sha256", "size")} for f in manifest["files"]],
        }  # fmt: skip
        try:
            self._phase("stopped", **info)
            self._probe_locks(roots)
            return self._forward(info, "stopped", roots, files, rec)
        except Exception as exc:  # noqa: BLE001 - any failure before the commit puts everything back
            return self._fail(exc, info)

    def _heal(self, rec: Record, doc: dict[str, Any] | None) -> None:
        """A committed journal whose record write was lost: write the record from the journal."""
        if doc is None or doc["phase"] != "committed" or "to_version" not in doc:
            return
        if self.layout.current_version() == doc["to_version"] and rec.version != doc["to_version"]:
            self._write_record(doc, rec)

    def _root_key_id(self) -> str:
        try:
            env = json.loads(self._envelope)
            return str(env["key_cert"]["root_key_id"])
        except Exception:  # noqa: BLE001
            return ""

    def _release_key_id(self) -> str:
        try:
            return parse_envelope(self._envelope).sig_key_id
        except Exception:  # noqa: BLE001
            return ""

    def _roots(self) -> tuple[list[tuple[str, str, Path]], list[str]]:
        if self.s.roots is not None:
            return self.s.roots()
        from orch.migrate.cli import _workspace_roots

        wss, notes = _workspace_roots(self.env, None)
        roots = [("this computer's orch data", "host", self.state)]
        for label, _project, store in wss:
            if store.is_dir():
                roots.append((label, "workspace", store))
            else:
                notes.append(f"{label}: not reachable, skipped")
        return roots, notes

    # ------------------------------------------------------------------------------------------ download, space
    def _download(self, manifest: Mapping[str, Any], ver: str) -> dict[str, Path]:
        cache = self.layout.cache_dir(ver)
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        blobs = {f["name"]: self.s.source.fetch(f) for f in manifest["files"]}
        try:  # verified again with the bytes: size and sha256 of every listed file
            verify_envelope(
                self._envelope,
                purpose="core",
                channel=manifest["channel"],
                now=self.s.now(),
                seq_seen=None,
                revocations_issued_at=None,
                files=blobs,
            )
        except UpdateRefused as exc:
            raise OrchError(exc.code, f"the release was refused ({exc.reason.value}); nothing was changed") from None
        out: dict[str, Path] = {}
        for f in manifest["files"]:
            p = cache / f["name"]
            atomic_write(p, blobs[f["name"]])
            out[f["role"]] = p
        atomic_write(cache / "release.json", self._envelope)
        return out

    def _space(self, roots: list[tuple[str, str, Path]], manifest: Mapping[str, Any]) -> None:
        wheels = sum(f["size"] for f in manifest["files"])
        data = sum(dir_size(r[2]) for r in roots)
        need = 2 * (data + wheels)
        free = self.s.free_bytes(self.layout.data if self.layout.data.exists() else self.layout.data.parent)
        if free < need:
            mb = 1 << 20
            raise OrchError(
                "update.no_space",
                f"not enough free disk space: {need // mb} MB are needed (twice the backup plus the new files), "
                f"{free // mb} MB are free",
            )

    def _probe_locks(self, roots: list[tuple[str, str, Path]]) -> None:
        for _label, _kind, root in roots:
            fd = lock_root(root, exclusive=True)  # raises migrate.running while a service holds the root
            os.close(fd)

    def _cached(self, info: Mapping[str, Any]) -> dict[str, Path]:
        """role -> path of the cache files of ``info['to_version']``, hashed again just before they are installed."""
        import hashlib

        cache = self.layout.cache_dir(info["to_version"])
        self._envelope = (cache / "release.json").read_bytes()
        files: dict[str, Path] = {}
        for f in info["files"]:
            if hashlib.sha256((cache / f["name"]).read_bytes()).hexdigest() != f["sha256"]:
                raise OrchError("update.unverified", f"{f['name']} in the download cache does not match its hash")
            files[f["role"]] = cache / f["name"]
        return files

    # ------------------------------------------------------------------------------------------ forward phases
    def _forward(
        self,
        info: dict[str, Any],
        phase: str,
        roots: list[tuple[str, str, Path]],
        files: Mapping[str, Path],
        rec: Record,
    ) -> Outcome:
        to = info["to_version"]
        order = ["stopped", "backed-up", "installed", "migrated"]
        at = order.index("migrated" if phase.startswith("migrated:") else phase)
        if at < 1:
            self._backup(info, roots)
        if at < 2:
            self.s.installer(self.layout, to, files, read_record(self.layout))
            check_bin = self.layout.tool_bin(to)
            rc, out = self.s.run_orch(check_bin, ["--version", "--json"])
            if rc != 0 or to not in out:
                raise OrchError("update.unverified", f"the new orch does not report version {to}")
            self._phase("installed")
        if at < 3:
            self._migrate(to, roots)
        self._quarantine_check(to, info)
        return self._commit(info, rec)

    def _backup(self, info: dict[str, Any], roots: list[tuple[str, str, Path]]) -> None:
        done: list[dict[str, str]] = []
        for label, kind, root in roots:
            n = _fmt(root, kind)
            b = backup_mod.create_backup(root, kind, n, n, self.env)
            done.append({"root": str(root), "kind": kind, "label": label, "backup": str(b)})
            self.say(f"backed up {label}")
        info["backups"] = done
        self._phase("backed-up", backups=done)

    def _migrate(self, to: str, roots: list[tuple[str, str, Path]]) -> None:
        rc, out = self.s.run_orch(self.layout.tool_bin(to), ["migrate", "--apply", "--json"])
        try:
            doc = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except ValueError:
            doc = {}
        if rc != 0 or not doc.get("ok"):
            raise OrchError("migrate.step_failed", "the new version could not migrate your data")
        self.note(f"migrate: {out.strip()[-500:]}")
        for label, _kind, root in roots:
            self._phase(f"migrated:{root}", migrated=label)

    def _quarantine_check(self, to: str, info: Mapping[str, Any]) -> None:
        bin_ = self.layout.tool_bin(to)
        quarantine.activate(self.state)
        try:
            self.s.services.start(bin_, info.get("running") or [], quarantine=True)
            problems = self._wait_healthy(True)
            if problems:
                raise OrchError("update.rolled_back", "the new version did not start cleanly: " + "; ".join(problems))
        finally:
            self.s.services.stop()
            quarantine.clear(self.state)
        self.note("quarantine health check passed")
        if self.s.after_phase is not None:
            self.s.after_phase("health")

    def _wait_healthy(self, quar: bool) -> list[str]:
        end = time.monotonic() + self.s.health_wait
        while True:
            problems = self.s.services.health(quarantine=quar)
            if not problems or time.monotonic() >= end:
                return problems
            self.s.sleep(0.5)

    def _commit(self, info: dict[str, Any], rec: Record) -> Outcome:
        to, frm = info["to_version"], info["from_version"]
        flip_current(self.layout, to)
        self._phase("committed")  # the commit point: from here on, forward only
        self._write_record(read_journal_safe(self.env) or info, rec)
        warnings: list[str] = []
        try:
            self.s.services.start(self.layout.tool_bin(to), info.get("running") or [], quarantine=False)
            problems = self._wait_healthy(False)
        except Exception as exc:  # noqa: BLE001 - after the commit nothing is rolled back
            problems = [str(exc)]
        for p in problems:
            warnings.append(p)
        for _label, _kind, root in roots_of(info):
            backup_mod.prune_backups(self.env, root)  # only now: a failing update never eats its own way back
        prune_tools(self.layout, to)
        prune_cache(self.layout, {to, frm})
        if problems:
            msg = (
                f"orch {to} is installed and your data is migrated, but it is not healthy ({'; '.join(problems)}). "
                f"Your data stays as it is. To use the old version again: orch update --use {frm}. Log: {self.log_path}"
            )
            return Outcome(False, "updated", frm, to, msg, self.log_path, warnings, "update.after_commit")
        return Outcome(True, "updated", frm, to, f"orch is now {to}", self.log_path)

    def _write_record(self, info: Mapping[str, Any], old: Record) -> None:
        write_record(
            self.layout,
            new_record(
                old,
                channel=info["channel"],
                version=info["to_version"],
                seq=info["seq"],
                revocations_issued_at=info["revocations_issued_at"],
                min_serial=info["min_serial"],
                root_key_id=info["root_key_id"],
                release_key_id=info["release_key_id"],
                trusted_roots=info["trusted_roots"],
                tool_dir=str(self.layout.tool_dir(info["to_version"])),
                files=info["files"],
            ),
        )

    # ------------------------------------------------------------------------------------------ rollback
    def _fail(self, exc: Exception, info: Mapping[str, Any] | None = None) -> Outcome:
        self.note(f"FAILED: {type(exc).__name__}: {exc}")
        why = exc.message if isinstance(exc, OrchError) else str(exc)
        doc = read_journal_safe(self.env)
        if doc is not None and doc.get("phase") == "committed":  # after the commit: report only, data stays
            return Outcome(False, "updated", doc.get("from_version"), doc.get("to_version"),
                           f"orch was updated but something failed afterwards ({why}). Your data stays as it is. "
                           f"To use the old version: orch update --use {doc.get('from_version')}. Log: {self.log_path}",
                           self.log_path, [why], "update.after_commit")  # fmt: skip
        merged = {**(info or {}), **(doc or {})}
        out = self._rollback(merged)
        out.message = (
            f"The update did not work ({why}). Your old version and your data were put back. Log: {self.log_path}"
        )
        out.code = "update.rolled_back"
        return out

    def _rollback(self, doc: dict[str, Any] | None) -> Outcome:
        """Put the old code and the old data back (only before the commit). Idempotent: a crash inside is resumed."""
        doc = doc or {}
        if doc.get("phase") == "committed":
            raise OrchError("update.after_commit", "the update is committed: it can only be fixed forward")
        frm = doc.get("from_version") or self.layout.current_version()
        self.s.services.stop()
        quarantine.clear(self.state)
        if frm and self.layout.current_version() != frm and self.layout.tool_dir(frm).is_dir():
            flip_current(self.layout, frm)
        for b in doc.get("backups") or []:
            if Path(b["backup"]).is_dir():
                backup_mod.restore(Path(b["backup"]), Path(b["root"]), self.env)
        quarantine.clear(self.state)
        clear_journal(self.env)  # restore may have brought back a copy of the journal: it is gone either way
        if frm:
            self.s.services.start(self.layout.tool_bin(frm), doc.get("running") or [], quarantine=False)
        return Outcome(False, "rolled-back", frm, doc.get("to_version"), "", self.log_path)

    def _resume(self, doc: dict[str, Any], assume: str | None) -> Outcome:
        phase = doc["phase"]
        if assume is None:
            raise OrchError(
                "update.interrupted",
                f"an earlier update stopped at '{phase}': run `orch update --finish` or `orch update --rollback`",
            )
        self.say(f"an earlier update stopped at '{phase}'")
        try:
            if assume == "rollback" or "to_version" not in doc:
                return self._rollback(doc)
            info = dict(doc)
            files = self._cached(info)
            rec = read_record(self.layout)
            roots, _ = self._roots()
            self.s.services.stop()
            return self._forward(info, phase, roots, files, rec)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, doc)

    # ------------------------------------------------------------------------------------------ --use
    def use(self, ver: str) -> Outcome:
        """Select an installed version again (also the way back after a committed update). The data stays."""
        ver = check_version(ver)
        lock = self._lock()
        try:
            self._open_log()
            rec = read_record(self.layout)
            cur = self.installed_version(rec)
            if ver == cur:
                return Outcome(True, "current", cur, cur, f"orch {cur} is already the selected version")
            if not self.layout.tool_dir(ver).is_dir():
                have = ", ".join(self.layout.installed_versions()) or "none"
                raise OrchError("update.no_record", f"version {ver} is not installed here (installed: {have})")
            rc, out = self.s.run_orch(self.layout.tool_bin(ver), ["migrate", "--check", "--json"])
            if rc != 0 or "format.newer" in out:
                raise OrchError(
                    "format.newer",
                    f"your data was made by a newer orch than {ver}: it cannot be used with that version",
                )
            running = self.s.services.running()
            self.s.services.stop()
            flip_current(self.layout, ver)
            write_record(
                self.layout,
                dataclasses.replace(rec, version=ver, tool_dir=str(self.layout.tool_dir(ver))),
            )
            self.s.services.start(self.layout.tool_bin(ver), running, quarantine=False)
            problems = self._wait_healthy(False)
            msg = f"orch {ver} is selected again; your data was not changed"
            if problems:
                return Outcome(False, "used", cur, ver, msg + f", but it is not healthy ({'; '.join(problems)})",
                               self.log_path, problems, "update.after_commit")  # fmt: skip
            return Outcome(True, "used", cur, ver, msg, self.log_path)
        finally:
            if self._log is not None:
                self._log.close()
            os.close(lock)


def read_journal_safe(env: Mapping[str, str]) -> dict[str, Any] | None:
    """The journal; a damaged one is treated as 'stopped, nothing known' so that a rollback is still offered."""
    try:
        return read_journal(env)
    except OrchError:
        return {"phase": "stopped", "v": 1}


def roots_of(info: Mapping[str, Any]) -> list[tuple[str, str, Path]]:
    return [(b["label"], b["kind"], Path(b["root"])) for b in info.get("backups") or []]
