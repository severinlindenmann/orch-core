"""Verify the exact build artifact and print hash-pinned local wheel requirements."""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path


def verify(directory: Path, version: str) -> str:
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:\.dev[0-9]+)?", version) is None:
        raise ValueError("invalid version")
    wheels = {f"orch_{name}-{version}-py3-none-any.whl" for name in ("core", "dashboard", "hub", "relay", "publish")}
    expected = wheels | {"SHA256SUMS", "constraints.txt", "changelog.txt"}
    if {p.name for p in directory.iterdir()} != expected:
        raise ValueError("artifact file set differs from expected")
    if any(p.is_symlink() or not p.is_file() for p in directory.iterdir()):
        raise ValueError("artifact must contain only regular files")
    recorded = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        if match is None or match[2] in recorded:
            raise ValueError("invalid or duplicate SHA256SUMS entry")
        recorded[match[2]] = match[1]
    if set(recorded) != expected - {"SHA256SUMS"}:
        raise ValueError("SHA256SUMS file set differs from expected")
    requirements = []
    for name in sorted(recorded):
        wheel = directory / name
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != recorded[name]:
            raise ValueError(f"sha256 mismatch: {name}")
        if name in wheels:
            requirements.append(f"{wheel.resolve().as_uri()} --hash=sha256:{recorded[name]}")
    return "\n".join(requirements) + "\n"


if __name__ == "__main__":
    try:
        print(verify(Path(sys.argv[1]), sys.argv[2]), end="")
    except (OSError, ValueError) as exc:
        sys.exit(f"wheel hashes: {exc}")
