"""Server ops (S9): the health check and the nightly backup copy, run by two systemd user timers.

``python -m orch.install.ops health --config FILE`` and ``... backup-copy --src DIR --dest DIR [--keep N]``.
Problems go to stderr (the journal) and to a status file; the exit code is 1 when anything is wrong, so the unit
shows as failed. The hub has no admin-warning path of its own (its notifications are phone pushes to a person), so
the journal and the status file are the warning. The unit generators are pure and tested; nothing here needs root."""

from __future__ import annotations

import calendar
import contextlib
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from orch import childenv

__all__ = ["backup_copy", "health_unit_files", "backup_unit_files", "run_health", "main"]

MIN_MEM_MB = 200
MIN_DISK_GB = 2.0
CERT_WARN_DAYS = 14
BACKUP_KEEP = 14


# ----------------------------------------------------------------------------------------------------- unit files


def _service(description: str, argv: Sequence[str]) -> str:
    return (
        f"[Unit]\nDescription={description}\n\n[Service]\nType=oneshot\nExecStart={shlex.join(argv)}\n"
        "Nice=10\nNoNewPrivileges=yes\nPrivateTmp=yes\n"
    )


def health_unit_files(python: str, config: Path) -> dict[str, str]:
    """``orch-health.service`` + ``orch-health.timer`` (every 5 minutes, first run 2 minutes after boot)."""
    argv = [python, "-P", "-m", "orch.install.ops", "health", "--config", str(config)]
    return {
        "orch-health.service": _service("orch server health check", argv),
        "orch-health.timer": (
            "[Unit]\nDescription=orch server health check, every 5 minutes\n\n"
            "[Timer]\nOnBootSec=2min\nOnUnitActiveSec=5min\nAccuracySec=30s\n\n[Install]\nWantedBy=timers.target\n"
        ),
    }


def backup_unit_files(
    python: str, src: Path, dest: Path, shared: bool = False, record: Path | None = None
) -> dict[str, str]:
    """``orch-hub-backup.service`` + ``.timer`` (every night 03:40): copy the sealed hub backups to the pull folder."""
    argv = [python, "-P", "-m", "orch.install.ops", "backup-copy", "--src", str(src), "--dest", str(dest)]
    argv += ["--shared", "--owner", "orch-backup"] if shared else []
    argv += ["--record", str(record)] if record else []
    return {
        "orch-hub-backup.service": _service("copy the sealed hub backups to the pull folder", argv),
        "orch-hub-backup.timer": (
            "[Unit]\nDescription=copy the sealed hub backups every night\n\n"
            "[Timer]\nOnCalendar=*-*-* 03:40:00\nPersistent=true\nRandomizedDelaySec=10min\n\n"
            "[Install]\nWantedBy=timers.target\n"
        ),
    }


# ------------------------------------------------------------------------------------------------------- health


def _http_ok(url: str) -> str | None:
    """``None`` when the part answers (any status below 500, like the hub's own service check), else the reason."""
    try:
        with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - loopback http url from our own config
            return None if r.status < 500 else f"answered {r.status}"
    except urllib.error.HTTPError as e:
        return None if e.code < 500 else f"answered {e.code}"
    except (OSError, ValueError) as e:
        return f"no answer ({type(e).__name__})"


def _ntp_synced() -> bool | None:
    try:
        r = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
            env=childenv.sanitize(os.environ),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() == "yes" if r.returncode == 0 else None


def _cert_days(name: str) -> float | None:
    """Days until the certificate Caddy serves for ``name`` expires; ``None`` when it cannot be read."""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((name, 443), timeout=8) as s, ctx.wrap_socket(s, server_hostname=name) as t:
            cert = t.getpeercert()
        return (ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) / 86400
    except (OSError, KeyError, ValueError, ssl.SSLError):
        return None


def _mem_available_mb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def run_health(
    cfg: dict[str, Any],
    *,
    http: Callable[[str], str | None] = _http_ok,
    ntp: Callable[[], bool | None] = _ntp_synced,
    cert_days: Callable[[str], float | None] = _cert_days,
    mem_mb: Callable[[], float | None] = _mem_available_mb,
    disk_free_gb: Callable[[str], float] = lambda p: shutil.disk_usage(p).free / 2**30,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """The checks of one run: ``{"ts", "ok", "problems": [...]}`` (plain sentences)."""
    problems: list[str] = []
    for p in cfg.get("parts", []):
        why = http(p["url"])
        if why:
            problems.append(f"{p['name']} is not healthy at {p['url']}: {why}")
    free = disk_free_gb(cfg.get("disk_path", "/"))
    if free < cfg.get("min_disk_gb", MIN_DISK_GB):
        problems.append(f"only {free:.1f} GB of disk space is left")
    if ntp() is False:
        problems.append("the clock is not synchronised (NTP): signed messages may be refused")
    for name in cfg.get("cert_names", []):
        days = cert_days(name)
        if days is None:
            problems.append(f"cannot read the certificate of {name}")
        elif days < cfg.get("cert_warn_days", CERT_WARN_DAYS):
            problems.append(f"the certificate of {name} expires in {max(days, 0):.0f} days")
    drop = cfg.get("backup_drop")
    if drop:  # a file in the shared drop that this hub did not copy: someone else in the group put it there
        strange = drop_strangers(Path(drop), Path(cfg["backup_record"]))
        if strange:
            # a foreign name is data, never text we print: only its count and names of the strict shape (via ascii)
            shown = [ascii(n) for n in strange if _NAME.fullmatch(n)][:3]
            problems.append(
                f"the backup drop folder holds {len(strange)} unexpected or changed file(s)"
                + (f", such as {', '.join(shown)}" if shown else "")
            )
    mem = mem_mb()
    if mem is not None and mem < cfg.get("min_mem_mb", MIN_MEM_MB):
        problems.append(f"only {mem:.0f} MB of memory is available")
    return {"ts": int(now()), "ok": not problems, "problems": problems}


def _write_status(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)


# ------------------------------------------------------------------------------------------------- backup copy


def _check_dest(dest: Path, shared: bool, owner: str | None) -> str | None:
    """A problem sentence when ``dest`` is not the folder we expect (a link, wrong owner, wrong mode), else ``None``.
    Nothing is created, re-moded or copied by this check; it never follows a link."""
    try:
        st = os.lstat(dest)
    except FileNotFoundError:
        return f"the backup drop folder {dest} does not exist: do the root steps in docs/pilot-server.md"
    except OSError as e:
        return f"cannot look at the backup folder {dest}: {e.strerror}"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return f"the backup folder {dest} is a link or not a folder: nothing was copied"
    if shared:
        if owner is not None:
            try:
                want = pwd.getpwnam(owner).pw_uid
            except KeyError:
                return f"the backup user {owner} does not exist: do the root steps in docs/pilot-server.md"
            if st.st_uid != want:
                return f"the backup folder {dest} is not owned by {owner}: nothing was copied"
        if stat.S_IMODE(st.st_mode) != 0o3770:
            have = f"{stat.S_IMODE(st.st_mode):o}"
            return (
                f"the backup folder {dest} must have mode 3770 (setgid and sticky) (it has {have}): nothing was copied"
            )
    elif st.st_uid != os.geteuid():
        return f"the backup folder {dest} is not owned by this user: nothing was copied"
    return None


def _sha256_of(fd: int) -> str:
    h = hashlib.sha256()
    with os.fdopen(os.dup(fd), "rb") as fh:
        fh.seek(0)
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def drop_strangers(drop: Path, record: Path) -> list[str]:
    """Names in the shared drop that are not exactly what this hub wrote: not in its record, not a regular file
    owned by this user, or different (size, inode, sha256) from the record. lstat / no-follow open: no link followed."""
    meta = _load_meta(record)
    try:
        names = sorted(os.listdir(drop))
    except OSError:
        return []
    bad = []
    for n in names:
        want = meta.get(n)
        if want is None:
            bad.append(n)
            continue
        try:
            st = os.lstat(drop / n)
            ok = stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid()
            ok = ok and st.st_size == want["size"] and st.st_ino == want["ino"]
            if ok:
                fd = os.open(drop / n, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    ok = _sha256_of(fd) == want["sha256"]
                finally:
                    os.close(fd)
        except OSError:
            ok = False
        if not ok:
            bad.append(n)
    return bad


def _write_private(dest: Path, name: str, src: Path, mode: int) -> dict[str, Any]:
    """The ONE way a file is written into the backup folder: a random O_EXCL temp name (never a predictable one, a link
    planted at any name is never followed), the content copied through file descriptors, the mode set on the descriptor,
    then a rename over the target name (a rename replaces a link, it never writes through it)."""
    fd_src = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fd, tmp = tempfile.mkstemp(dir=dest, prefix=".hub-", suffix=".part")
        try:
            os.fchmod(fd, mode)
            h = hashlib.sha256()
            size = 0
            with os.fdopen(fd, "wb") as out, os.fdopen(fd_src, "rb", closefd=False) as inp:
                for chunk in iter(lambda: inp.read(1 << 20), b""):
                    out.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
                out.flush()
                ino = os.fstat(out.fileno()).st_ino
            os.replace(tmp, dest / name)  # a rename keeps the inode
            return {"size": size, "ino": ino, "sha256": h.hexdigest()}
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    finally:
        os.close(fd_src)


_NAME = re.compile(r"hub-([0-9a-f]{8})-(\d{8}T\d{6}Z)\.orchbak")
RECORD_MAX = 400


def _stamp_ms(stamp: str) -> int | None:
    try:
        return int(calendar.timegm(time.strptime(stamp, "%Y%m%dT%H%M%SZ")) * 1000)
    except ValueError:
        return None


def _load_record(path: Path) -> list[str]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as fh:
            done = json.load(fh).get("done", [])
        return [n for n in done if isinstance(n, str) and _NAME.fullmatch(n)]
    except (OSError, ValueError, AttributeError):
        return []


def _load_meta(path: Path) -> dict[str, dict[str, Any]]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as fh:
            files = json.load(fh).get("files", {})
        return {
            n: m
            for n, m in files.items()
            if _NAME.fullmatch(n)
            and isinstance(m, dict)
            and type(m.get("size")) is int
            and type(m.get("ino")) is int
            and isinstance(m.get("sha256"), str)
        }
    except (OSError, ValueError, AttributeError):
        return {}


def _save_record(path: Path, done: list[str], meta: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rec-", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        os.fchmod(fd, 0o600)
        kept = done[-RECORD_MAX:]
        body = {"done": kept, "files": {n: meta[n] for n in kept if n in meta}}
        fh.write(json.dumps(body, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _prune(dest: Path, done: list[str], keep: int) -> None:
    """Delete only files THIS hub copied (named in its own record) beyond the newest ``keep``: a regular file owned by
    this user, found by lstat (a link, a directory or another user's file is never touched)."""
    if keep <= 0:
        return
    mine = sorted(set(done), key=lambda n: _NAME.fullmatch(n).group(2))  # type: ignore[union-attr]
    for name in mine[:-keep]:
        try:
            st = os.lstat(dest / name)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid():
            with contextlib.suppress(OSError):
                os.unlink(dest / name)


def backup_copy(
    src: Path,
    dest: Path,
    keep: int = BACKUP_KEEP,
    *,
    shared: bool = False,
    owner: str | None = None,
    record: Path | None = None,
    now: Callable[[], float] = time.time,
) -> tuple[list[str], list[str]]:
    """Copy the sealed ``hub-<id8>-<UTC>.orchbak`` files not yet copied; keep the newest ``keep`` of OUR copies there.
    ``(copied, problems)``. The files are sealed with the hub's backup key; the folder holds nothing a thief could read.

    ``shared``: ``dest`` is a drop folder an administrator made (owned by ``owner``, the ``orch-backup`` user, mode
    3770 (setgid, sticky), group-writable for the hub user). It is not created or re-moded here; files are 0640.
    The folder is checked first (a link, another owner or another mode: a problem is reported, nothing is copied).

    What was copied is remembered in ``record`` (hub-owned, 0600), never read back from names in ``dest``: another
    member of the shared group can plant any name there, which must change neither what is copied nor what is pruned."""
    if not shared:
        if not os.path.lexists(dest):
            dest.mkdir(parents=True, exist_ok=True, mode=0o700)
        bad = _check_dest(dest, False, None)
        if bad:
            return [], [bad]
        dest.chmod(0o700)  # on every run (not a link: checked above)
        record = record or dest / ".copied.json"
    else:
        bad = _check_dest(dest, True, owner)
        if bad:
            return [], [bad]
        if record is None:
            return [], ["backup copy in a shared folder needs --record FILE (in the hub's own state folder)"]
    horizon = (now() + 86400) * 1000
    cands = {}
    for q in src.glob("hub-*.orchbak") if src.is_dir() else []:
        m = _NAME.fullmatch(q.name)
        ms = _stamp_ms(m.group(2)) if m else None
        if m and ms is not None and ms <= horizon and q.is_file() and not q.is_symlink():
            cands[q.name] = m.group(1)
    if not cands:
        return [], ["there is no hub backup yet (set the backup key up in the hub console)"]
    ids = set(cands.values())
    if len(ids) != 1:
        return [], ["the hub's backup folder holds backups of more than one hub id: not copying"]
    done = _load_record(record)
    meta = _load_meta(record)
    copied, problems = [], []
    for name in sorted(cands):
        if name in done:
            continue
        try:
            cur = os.lstat(dest / name)  # lstat: a dangling link planted at the name counts as present
        except FileNotFoundError:
            cur = None
        if cur is not None:
            problems.append(f"{dest / name} is there but not from this hub's record: left alone, not overwritten")
            continue
        meta[name] = _write_private(dest, name, src / name, 0o640 if shared else 0o600)
        copied.append(name)
        done.append(name)
    _save_record(record, done, meta)
    _prune(dest, done, keep)
    return copied, problems


def main(argv: Sequence[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="orch.install.ops")
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("health")
    h.add_argument("--config", required=True)
    b = sub.add_parser("backup-copy")
    b.add_argument("--src", required=True)
    b.add_argument("--dest", required=True)
    b.add_argument("--keep", type=int, default=BACKUP_KEEP)
    b.add_argument("--shared", action="store_true")
    b.add_argument("--owner", default=None)
    b.add_argument("--record")
    a = p.parse_args(list(argv))
    if a.cmd == "health":
        cfg = json.loads(Path(a.config).read_text())
        report = run_health(cfg)
        _write_status(Path(cfg["status_file"]), report)
        for line in report["problems"]:
            print(f"orch health: WARNING: {line}", file=sys.stderr)
        return 0 if report["ok"] else 1
    copied, problems = backup_copy(
        Path(a.src), Path(a.dest), a.keep, shared=a.shared, owner=a.owner, record=Path(a.record) if a.record else None
    )
    for name in copied:
        print(f"orch backup: copied {name}")
    for line in problems:
        print(f"orch backup: WARNING: {line}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
