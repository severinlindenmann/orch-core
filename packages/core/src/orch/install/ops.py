"""Server ops (S9): the health check and the nightly backup copy, run by two systemd user timers.

``python -m orch.install.ops health --config FILE`` and ``... backup-copy --src DIR --dest DIR [--keep N]``.
Problems go to stderr (the journal) and to a status file; the exit code is 1 when anything is wrong, so the unit
shows as failed. The hub has no admin-warning path of its own (its notifications are phone pushes to a person), so
the journal and the status file are the warning. The unit generators are pure and tested; nothing here needs root."""

from __future__ import annotations

import contextlib
import json
import os
import pwd
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


def backup_unit_files(python: str, src: Path, dest: Path, shared: bool = False) -> dict[str, str]:
    """``orch-hub-backup.service`` + ``.timer`` (every night 03:40): copy the sealed hub backups to the pull folder."""
    argv = [python, "-P", "-m", "orch.install.ops", "backup-copy", "--src", str(src), "--dest", str(dest)]
    argv += ["--shared", "--owner", "orch-backup"] if shared else []
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
        if stat.S_IMODE(st.st_mode) != 0o2770:
            have = f"{stat.S_IMODE(st.st_mode):o}"
            return f"the backup folder {dest} must have mode 2770 (it has {have}): nothing was copied"
    elif st.st_uid != os.geteuid():
        return f"the backup folder {dest} is not owned by this user: nothing was copied"
    return None


def _write_private(dest: Path, name: str, src: Path, mode: int) -> None:
    """The ONE way a file is written into the backup folder: a random O_EXCL temp name (never a predictable one, a link
    planted at any name is never followed), the content copied through file descriptors, the mode set on the descriptor,
    then a rename over the target name (a rename replaces a link, it never writes through it)."""
    fd_src = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fd, tmp = tempfile.mkstemp(dir=dest, prefix=".hub-", suffix=".part")
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as out, os.fdopen(fd_src, "rb", closefd=False) as inp:
                shutil.copyfileobj(inp, out)
            os.replace(tmp, dest / name)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    finally:
        os.close(fd_src)


def backup_copy(
    src: Path, dest: Path, keep: int = BACKUP_KEEP, *, shared: bool = False, owner: str | None = None
) -> tuple[list[str], list[str]]:
    """Copy the sealed ``*.orchbak`` files not yet in ``dest``; keep the newest ``keep`` there. ``(copied, problems)``.
    The files are sealed with the hub's backup key; this folder holds nothing a thief could read.

    ``shared``: ``dest`` is a drop folder an administrator made (owned by ``owner``, the ``orch-backup`` user, mode
    2770, group-writable for the hub user). It is not created or re-moded here and files are group-readable (0640).
    The folder is checked first (a link, another owner or another mode: a problem is reported, nothing is copied)."""
    if not shared:
        if not os.path.lexists(dest):
            dest.mkdir(parents=True, exist_ok=True, mode=0o700)
        bad = _check_dest(dest, False, None)
        if bad:
            return [], [bad]
        dest.chmod(0o700)  # on every run (not a link: checked above)
    else:
        bad = _check_dest(dest, True, owner)
        if bad:
            return [], [bad]
    files = sorted(src.glob("hub-*.orchbak")) if src.is_dir() else []
    if not files:
        return [], ["there is no hub backup yet (set the backup key up in the hub console)"]
    copied, problems = [], []
    stamp = lambda n: n.rsplit("-", 1)[-1]  # noqa: E731 - the UTC part of hub-<hub8>-<utc>.orchbak
    have = [stamp(q.name) for q in dest.glob("hub-*.orchbak")]
    newest = max(have, default="")
    for f in files:
        target = dest / f.name
        if not f.is_file() or f.is_symlink() or stamp(f.name) < newest:
            continue  # older than what was pruned: not copied back
        try:
            cur = os.lstat(target)  # lstat: a dangling link planted at the name counts as present
        except FileNotFoundError:
            cur = None
        if cur is not None:
            if stat.S_ISLNK(cur.st_mode):
                problems.append(f"{target} is a link, not a backup: left alone, not overwritten")
            continue
        _write_private(dest, f.name, f, 0o640 if shared else 0o600)
        copied.append(f.name)
    for old in sorted(dest.glob("hub-*.orchbak"))[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
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
    a = p.parse_args(list(argv))
    if a.cmd == "health":
        cfg = json.loads(Path(a.config).read_text())
        report = run_health(cfg)
        _write_status(Path(cfg["status_file"]), report)
        for line in report["problems"]:
            print(f"orch health: WARNING: {line}", file=sys.stderr)
        return 0 if report["ok"] else 1
    copied, problems = backup_copy(Path(a.src), Path(a.dest), a.keep, shared=a.shared, owner=a.owner)
    for name in copied:
        print(f"orch backup: copied {name}")
    for line in problems:
        print(f"orch backup: WARNING: {line}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
