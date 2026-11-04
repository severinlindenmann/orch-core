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
    # REPLACE whatever is embedded (the production root once the ceremony has run) with the public TEST root only
    out, n = re.subn(r"^ROOTS_B64 = \{.*\}$", f'ROOTS_B64 = {{"test-root-1": "{root_b64()}"}}', text, flags=re.M)
    assert n == 1
    # the shipped floor is the real, current one: the test releases are older, so the copy gets the test baseline floor
    out, n = re.subn(
        r'^REVOCATIONS_FLOOR_ISSUED_AT = ".*"$', 'REVOCATIONS_FLOOR_ISSUED_AT = "2026-10-20T00:00:00Z"', out, flags=re.M
    )
    assert n == 1
    out, n = re.subn(r"^REVOCATIONS_FLOOR_MIN_SERIAL = \d+$", "REVOCATIONS_FLOOR_MIN_SERIAL = 1", out, flags=re.M)
    assert n == 1
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
    revs_issued: str = ""

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
    revs_issued: datetime | None = None,
    revs_min_serial: int | None = None,
    constraints: str = "# no third-party dependencies in this test release\n",
    **over: Any,
) -> Release:
    """A release folder: channel.json (the signed envelope), two wheels and a constraints file."""
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    wheels = {
        "core": make_wheel(out, "orch_core", version, entry="orch"),
        "dashboard": make_wheel(out, "orch_dashboard", version),
    }
    (out / "constraints.txt").write_text(constraints, encoding="utf-8")
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
    revs_at = stamp(revs_issued or now - timedelta(days=1))
    revs_doc = {**GEN.REVOCATIONS, "issued_at": revs_at, "min_serial": revs_min_serial or GEN.REVOCATIONS["min_serial"]}
    env = GEN.envelope(
        GEN.jcs(m), key_id, cert=GEN.key_cert(key_id, raw=GEN.jcs(cert)), revs=GEN.revocations(doc=revs_doc)
    )
    (out / "channel.json").write_text(GEN.text(env), encoding="utf-8")
    return Release(out, version, seq, channel, m, revs_at)


def pinned_uv_version() -> str:
    m = re.search(r'^UV_VERSION="([^"]+)"', INSTALL_SH.read_text(encoding="utf-8"), re.M)
    assert m is not None
    return m.group(1)


def real_uv() -> str:
    uv = shutil.which("uv")
    assert uv, "uv is needed to run the install.sh tests"
    return uv


def fake_bin(tmp: Path, *, pinned_uv: bool = True, **stubs: str) -> Path:
    """A PATH folder with stub commands. With ``pinned_uv`` a wrapper `uv` that reports the pinned version and otherwise
    is the real uv is prepared in ``tmp/uvw``; ``run_install`` puts it where install.sh looks for its own uv."""
    b = tmp / "bin"
    b.mkdir(exist_ok=True)
    if pinned_uv:
        (tmp / "uvw").mkdir(exist_ok=True)
        version_line = f'if [ "$1" = "--version" ]; then echo "uv {pinned_uv_version()} (test)"; exit 0; fi'
        (tmp / "uvw" / "uv").write_text(f'#!/bin/sh\n{version_line}\nexec "{real_uv()}" "$@"\n')
        (tmp / "uvw" / "uv").chmod(0o755)
    for name, body in stubs.items():
        (b / name).write_text("#!/bin/sh\n" + body + "\n")
        (b / name).chmod(0o755)
    return b


def place_uv(tmp: Path, script: Path) -> None:
    """Install the wrapper uv as install.sh's own uv and pin its sha256 in this TEST copy of the script."""
    wrapper = tmp / "uvw" / "uv"
    dest = data(tmp) / "uv" / f"uv-{pinned_uv_version()}"
    if not dest.exists():
        dest.mkdir(parents=True, mode=0o700)
        shutil.copy(wrapper, dest / "uv")
    sha = hashlib.sha256((dest / "uv").read_bytes()).hexdigest()
    text = re.sub(r'^(UV_BIN_SHA256_\w+)="[0-9a-f]{64}"$', rf'\1="{sha}"', script.read_text(), flags=re.M)
    script.write_text(text)


def managed_python(tmp: Path) -> Path:
    """A uv "managed Python" folder holding a link to the Python running the tests (3.14), so no download is needed."""
    d = tmp / "pyinstall"
    d.mkdir(exist_ok=True)
    real = Path(sys.executable).resolve()
    info = json.loads(
        subprocess.run(
            [
                str(real),
                "-c",
                "import json,platform,sys;print(json.dumps([platform.system(),platform.machine(),"
                "sys.version.split()[0]]))",
            ],
            capture_output=True,
            text=True,
        ).stdout
    )
    system, machine, ver = info
    osname = {"Darwin": "macos", "Linux": "linux"}[system]
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64"}[machine]
    libc = "none" if osname == "macos" else "gnu"
    name = f"cpython-{ver}-{osname}-{arch}-{libc}"
    if not (d / name).exists():
        (d / name / "bin").mkdir(parents=True)
        for exe in ("python", "python3", f"python{ver.rsplit('.', 1)[0]}"):
            (d / name / "bin" / exe).symlink_to(real)
    return d


def run_install(
    tmp: Path,
    script: Path,
    *args: str,
    path_dirs: list[Path] | None = None,
    env_extra: dict[str, str] | None = None,
    system_dirs: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    if (tmp / "uvw" / "uv").exists():
        assert script.resolve() != INSTALL_SH.resolve(), "never rewrite the shipped installer"
        place_uv(tmp, script)
    (tmp / "tmp").mkdir(exist_ok=True)
    # only the Python binary, never its whole folder: on a CI runner Python lives in /usr/bin next to every system tool
    py_bin = tmp / "pybin"
    py_bin.mkdir(exist_ok=True)
    for name in ("python3", "python"):
        if not (py_bin / name).is_symlink():
            (py_bin / name).symlink_to(Path(sys.executable).resolve())
    py_dir = str(py_bin)
    system = ["/usr/bin", "/bin"] if system_dirs is None else system_dirs
    path = ":".join([*(str(p) for p in (path_dirs or [])), py_dir, *system])
    env = {
        "HOME": str(home),
        "PATH": path,
        "TMPDIR": str(tmp / "tmp"),
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_PYTHON_INSTALL_DIR": str(managed_python(tmp)),
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
