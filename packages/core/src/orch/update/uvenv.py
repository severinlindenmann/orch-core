"""How ``orch update`` runs uv: the SAME argv and the same environment policy as install.sh (P2-C).

* the environment starts EMPTY: only the names in :data:`KEEP_ENV` are copied from ours (never a ``UV_*`` or ``PIP_*``
  variable, which could point uv at another index, mirror or Python list, or switch hash checking off), then the
  pinned values of :data:`PINNED_ENV` are set;
* the venv and the install use ``--no-config`` (no uv.toml of the user's), and the install uses ``--require-hashes
  --no-build --no-deps``: only wheels, only hashed, no dependency's build code ever runs.

``tests/update_cli/test_uvenv.py`` reads install.sh and fails if the flags or the policy drift apart.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
from collections.abc import Mapping
from pathlib import Path

from orch.ops.errors import OrchError

__all__ = ["KEEP_ENV", "PINNED_ENV", "UV_BIN_SHA256", "UV_VERSION", "check_uv", "install_argv", "uv_env", "venv_argv"]

KEEP_ENV = (
    "HOME", "PATH", "TMPDIR", "USER", "LOGNAME", "LANG", "LC_ALL", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
)  # fmt: skip
PINNED_ENV = {"UV_PYTHON_PREFERENCE": "only-managed"}


def uv_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    src = os.environ if source is None else source
    env = {k: src[k] for k in KEEP_ENV if k in src}
    env.update(PINNED_ENV)
    return env


def venv_argv(uv: str, python: str, venv: Path) -> list[str]:
    return [uv, "venv", "--no-config", "--quiet", "--python", python, str(venv)]


def install_argv(uv: str, venv_python: Path, requirements: Path) -> list[str]:
    return [
        uv, "pip", "install", "--no-config", "--quiet", "--require-hashes", "--no-build", "--no-deps",
        "--python", str(venv_python), "-r", str(requirements),
    ]  # fmt: skip


#: the pinned uv and the sha256 of its BINARY per platform: the same constants as install.sh (UV_VERSION,
#: UV_BIN_SHA256_*;
#: ``tests/update_cli/test_uvenv.py`` compares them)
UV_VERSION = "0.13.3"
UV_BIN_SHA256 = {
    "aarch64-apple-darwin": "9e1c4b0585ee3042fa8624229fb2897231edf8194fe64577be31087b210e367d",
    "x86_64-apple-darwin": "5c0532bffe173c7f21d45b8e89fc6604e5143348ee447e5d6df4ce75bd7013e1",
    "x86_64-unknown-linux-gnu": "90c2ff84aa2a76a1ae70454ede3c6a9a4a40c786ccbf777148341a05cb583018",
    "aarch64-unknown-linux-gnu": "0bbbc387a140ee29a80ef14a556368f8bcdacfb4320a4aca9ce48eb239760511",
}


def triple() -> str:
    m = platform.machine().lower()
    arch = "aarch64" if m in ("arm64", "aarch64") else "x86_64" if m in ("x86_64", "amd64") else m
    return f"{arch}-apple-darwin" if platform.system() == "Darwin" else f"{arch}-unknown-linux-gnu"


def check_uv(data: Path, recorded: str, *, version_of=None) -> str:
    """The uv to use: ONLY ``<data>/uv/uv-<pinned>/uv`` (the record's path must be exactly that), a regular file, not a
    link, whose bytes hash to the pinned value and which reports the pinned version. Anything else: ``update.no_record``
    (run the installer again)."""
    want = Path(data) / "uv" / f"uv-{UV_VERSION}" / "uv"
    fail = OrchError("update.no_record", "the uv that install.sh pinned is missing or changed: run the installer again")
    if recorded != str(want) or want.is_symlink() or not want.is_file() or not os.access(want, os.X_OK):
        raise fail
    pinned = UV_BIN_SHA256.get(triple())
    if pinned is None or hashlib.sha256(want.read_bytes()).hexdigest() != pinned:
        raise fail
    if version_of is None:
        try:
            r = subprocess.run([str(want), "--version"], capture_output=True, text=True, timeout=30, env=uv_env())
        except (OSError, subprocess.SubprocessError):
            raise fail from None
        out = r.stdout
    else:
        out = version_of(want)
    if out.split()[1:2] != [UV_VERSION]:
        raise fail
    return str(want)
