"""How ``orch update`` runs uv: the SAME argv and the same environment policy as install.sh (P2-C).

* the environment starts EMPTY: only the names in :data:`KEEP_ENV` are copied from ours (never a ``UV_*`` or ``PIP_*``
  variable, which could point uv at another index, mirror or Python list, or switch hash checking off), then the
  pinned values of :data:`PINNED_ENV` are set;
* the venv and the install use ``--no-config`` (no uv.toml of the user's), and the install uses ``--require-hashes
  --no-build --no-deps``: only wheels, only hashed, no dependency's build code ever runs.

``tests/update_cli/test_uvenv.py`` reads install.sh and fails if the flags or the policy drift apart.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

__all__ = ["KEEP_ENV", "PINNED_ENV", "install_argv", "uv_env", "venv_argv"]

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
