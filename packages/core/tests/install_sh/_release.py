"""Helpers for the install.sh tests: a TEST release folder (signed with the public TEST keys), the install.sh copy
that trusts the TEST root, and an `env -i` runner. Nothing here is a real key or a real wheel."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
INSTALL_SH = REPO / "install.sh"
VEC = HERE.parent / "vectors" / "release"

_spec = importlib.util.spec_from_file_location("gen_release_vectors", VEC / "gen_release_vectors.py")
assert _spec is not None and _spec.loader is not None
GEN = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(GEN)

VERIFIER_RX = re.compile(r"<<'PYEOF'\n(.*?)\nPYEOF\n", re.S)


def stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def verifier_source() -> str:
    m = VERIFIER_RX.search(INSTALL_SH.read_text(encoding="utf-8"))
    assert m is not None
    return m.group(1)


def root_b64() -> str:
    return base64.b64encode(GEN.ROOT.public).decode()


def trusting_test_root(text: str, *, allow_test_channels: bool = False) -> str:
    """install.sh (or its verifier) with the public TEST root embedded instead of the production roots."""
    out = text.replace("ROOTS_B64 = {}", f'ROOTS_B64 = {{"root-1": "{root_b64()}"}}')
    assert out != text
    if allow_test_channels:
        out2 = out.replace("ALLOW_TEST_CHANNELS = False", "ALLOW_TEST_CHANNELS = True")
        assert out2 != out
        out = out2
    return out


def write_test_script(dest: Path, **kw: Any) -> Path:
    dest.write_text(trusting_test_root(INSTALL_SH.read_text(encoding="utf-8"), **kw), encoding="utf-8")
    return dest


def make_wheel(dest: Path, dist: str, version: str, *, entry: str | None = None) -> Path:
    """A tiny real wheel. With ``entry`` it has a console script of that name that prints ``<dist> <version>``."""
    files: dict[str, bytes] = {
        f"{dist}/__init__.py": f"def main():\n    print('{dist} {version}')\n".encode(),
        f"{dist}-{version}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n".encode(),
        f"{dist}-{version}.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    if entry:
        files[f"{dist}-{version}.dist-info/entry_points.txt"] = f"[console_scripts]\n{entry} = {dist}:main\n".encode()
    rec = []
    for n, d in files.items():
        h = base64.urlsafe_b64encode(hashlib.sha256(d).digest()).rstrip(b"=").decode()
        rec.append(f"{n},sha256={h},{len(d)}")
    rec.append(f"{dist}-{version}.dist-info/RECORD,,")
    files[f"{dist}-{version}.dist-info/RECORD"] = ("\n".join(rec) + "\n").encode()
    path = dest / f"{dist}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return path


@dataclass
class Release:
    dir: Path
    version: str
    seq: int
    channel: str
    manifest: dict[str, Any]

    def path(self, role: str) -> Path:
        name = next(f["name"] for f in self.manifest["files"] if f["role"] == role)
        return self.dir / name


def build_release(
    out: Path,
    *,
    channel: str = "main",
    seq: int = 5,
    version: str = "2.0.0",
    key_id: str = "rk-3",
    cert_channels: list[str] | None = None,
    cert_serial: int | None = None,
    issued: datetime | None = None,
    expires: datetime | None = None,
    **over: Any,
) -> Release:
    """A release folder: channel.json (the signed envelope), two wheels and a constraints file."""
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    wheels = {
        "core": make_wheel(out, "orch_core", version, entry="orch"),
        "dashboard": make_wheel(out, "orch_dashboard", version),
    }
    (out / "constraints.txt").write_text("# no third-party dependencies in this test release\n", encoding="utf-8")
    files = [
        *(
            {
                "role": role,
                "name": p.name,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                "size": p.stat().st_size,
                "url": f"https://example.invalid/releases/download/v{version}/{p.name}",
            }
            for role, p in wheels.items()
        ),
        {
            "role": "constraints",
            "name": "constraints.txt",
            "sha256": hashlib.sha256((out / "constraints.txt").read_bytes()).hexdigest(),
            "size": (out / "constraints.txt").stat().st_size,
            "url": f"https://example.invalid/releases/download/v{version}/constraints.txt",
        },
    ]
    m = GEN.manifest(
        channel,
        seq,
        version=version,
        files=files,
        issued_at=stamp(issued or now - timedelta(hours=1)),
        expires_at=stamp(expires or now + timedelta(days=14)),
        **over,
    )
    signer = GEN.SIGNER[key_id]
    serial = cert_serial or {"rk-3": 3, "rk-4": 4}[key_id]
    cert = GEN.cert_doc(
        signer,
        serial,
        "core",
        cert_channels or ["develop", "main"],
        stamp(now - timedelta(days=1)),
        stamp(now + timedelta(days=90)),
    )
    env = GEN.envelope(GEN.jcs(m), key_id, cert=GEN.key_cert(key_id, raw=GEN.jcs(cert)))
    (out / "channel.json").write_text(GEN.text(env), encoding="utf-8")
    return Release(out, version, seq, channel, m)


def pinned_uv_version() -> str:
    m = re.search(r'^UV_VERSION="([^"]+)"', INSTALL_SH.read_text(encoding="utf-8"), re.M)
    assert m is not None
    return m.group(1)


def real_uv() -> str:
    uv = shutil.which("uv")
    assert uv, "uv is needed to run the install.sh tests"
    return uv


def fake_bin(tmp: Path, *, pinned_uv: bool = True, **stubs: str) -> Path:
    """A PATH folder: a `uv` that reports the pinned version and otherwise is the real uv, plus stub commands."""
    b = tmp / "bin"
    b.mkdir(exist_ok=True)
    if pinned_uv:
        version_line = f'if [ "$1" = "--version" ]; then echo "uv {pinned_uv_version()} (test)"; exit 0; fi'
        script = f'#!/bin/sh\n{version_line}\nexec "{real_uv()}" "$@"\n'
        (b / "uv").write_text(script)
        (b / "uv").chmod(0o755)
    for name, body in stubs.items():
        (b / name).write_text("#!/bin/sh\n" + body + "\n")
        (b / name).chmod(0o755)
    return b


def run_install(
    tmp: Path, script: Path, *args: str, path_dirs: list[Path] | None = None, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    (tmp / "tmp").mkdir(exist_ok=True)
    py_dir = str(Path(sys.executable).resolve().parent)
    path = ":".join([*(str(p) for p in (path_dirs or [])), py_dir, "/usr/bin", "/bin"])
    env = {
        "HOME": str(home),
        "PATH": path,
        "TMPDIR": str(tmp / "tmp"),
        "UV_PYTHON_PREFERENCE": "system",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_PYTHON_INSTALL_DIR": subprocess.run(
            [real_uv(), "python", "dir"], capture_output=True, text=True
        ).stdout.strip(),
        "LC_ALL": "C",
    }
    env.update(env_extra or {})
    return subprocess.run(
        ["env", "-i", *(f"{k}={v}" for k, v in env.items()), "sh", str(script), *args],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=tmp,
    )


def state(tmp: Path) -> Path:
    return tmp / "home" / ".config" / "orch"


def data(tmp: Path) -> Path:
    return tmp / "home" / ".local" / "share" / "orch"


def installed_anything(tmp: Path) -> bool:
    """True when orch left anything behind: a version folder, the current link, the shim or the record."""
    s = state(tmp)
    return (
        bool(list((s / "install" / "tools").glob("orch-*")) if (s / "install" / "tools").exists() else [])
        or (s / "install" / "current").is_symlink()
        or (tmp / "home" / ".local" / "bin" / "orch").is_symlink()
        or (data(tmp) / "install.json").exists()
    )


def mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def jsonload(p: Path) -> Any:
    return json.loads(p.read_text())
