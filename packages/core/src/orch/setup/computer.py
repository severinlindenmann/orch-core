"""The computer's friendly name for the setup page (W35). Read-only, no shell, no network.

macOS: the Sharing "Computer Name" (``scutil --get ComputerName``, never the ``.local`` host name). Linux:
``PRETTY_HOSTNAME`` of ``/etc/machine-info``. Anything else, or any error: ``None`` and the page says "this computer".
The raw host name is a separate field the page keeps under Details."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

__all__ = ["friendly_name"]

_SCUTIL = "/usr/sbin/scutil"
_MACHINE_INFO = Path("/etc/machine-info")


def _run(argv: list[str], timeout: float) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, shell=False)  # noqa: S603
    return done.stdout if done.returncode == 0 else ""


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _clean(raw: str) -> str | None:
    name = "".join(ch for ch in raw if ch >= " " and ch != "\x7f").strip()[:80].strip()
    if not name or name.lower().endswith(".local"):
        return None
    return name


def _pretty(text: str) -> str | None:
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "PRETTY_HOSTNAME":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1].replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")
            return value
    return None


def friendly_name(
    platform: str,
    *,
    run: Callable[[list[str], float], str] = _run,
    read: Callable[[Path], str | None] = _read,
) -> str | None:
    try:
        if platform == "darwin":
            return _clean(run([_SCUTIL, "--get", "ComputerName"], 2.0))
        if platform == "linux":
            text = read(_MACHINE_INFO)
            pretty = _pretty(text) if text else None
            return _clean(pretty) if pretty else None
    except Exception:  # noqa: BLE001 - a missing or odd source is "no friendly name", never a failed setup page
        return None
    return None
