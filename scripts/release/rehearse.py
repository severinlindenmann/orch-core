#!/usr/bin/env python3
"""Download and verify a test release, or prepare a private rehearsal installer.

Run with python3 -I. The stdlib installer verifier is the production verifier's
standalone counterpart; only its test-channel allowance changes here.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wheel_hashes

REPO = Path(__file__).resolve().parents[2]
NOTICE = "# REHEARSAL COPY: accepts test-* channels; never distribute\n"
ALLOW = "ALLOW_TEST_CHANNELS = False"


def rehearsal_source() -> str:
    source = (REPO / "install.sh").read_text(encoding="utf-8")
    if (
        source.count(ALLOW) != 1
        or len(
            re.findall(
                r"^ALLOW_TEST_CHANNELS = False(?:\s*#.*)?$", source, re.MULTILINE
            )
        )
        != 1
    ):
        raise ValueError("expected exactly one ALLOW_TEST_CHANNELS = False line")
    return source.replace(ALLOW, "ALLOW_TEST_CHANNELS = True", 1)


def fetch(tag: str, outdir: Path) -> None:
    if re.fullmatch(r"test-v[0-9]+\.[0-9]+\.[0-9]+\.dev[0-9]+", tag) is None:
        raise ValueError("expected test-v<base>.dev<N> tag")
    subprocess.run(["gh", "release", "download", tag, "-D", str(outdir)], check=True)
    version = tag.removeprefix("test-v")
    paths = list(outdir.iterdir())
    if any(p.is_symlink() or not p.is_file() for p in paths):
        raise ValueError("release must contain only regular files")
    if not (outdir / "release.json").is_file():
        raise ValueError("missing release.json")
    with tempfile.TemporaryDirectory(prefix="orch-rehearse-") as temp:
        stage = Path(temp) / "assets"
        stage.mkdir()
        # SHA256SUMS covers the pre-signing build artifact, not release.json.
        for path in paths:
            if path.name != "release.json":
                shutil.copyfile(path, stage / path.name)
        wheel_hashes.verify(stage, version)
        print("SHA256SUMS: ok", flush=True)
        raw = (outdir / "release.json").read_bytes()
        doc = json.loads(base64.b64decode(json.loads(raw)["payload"], validate=True))
        if (
            not isinstance(doc["channel"], str)
            or not doc["channel"].startswith("test-")
            or doc["version"] != version
            or doc["seq"] != int(version.rsplit("dev", 1)[1])
        ):
            raise ValueError(
                "release channel, version or seq does not match a test channel and requested tag"
            )
        sources = re.findall(
            r"<<'PYEOF'\n(.*?)\nPYEOF\n", rehearsal_source(), re.DOTALL
        )
        if len(sources) != 1:
            raise ValueError("expected exactly one embedded installer verifier")
        verifier = Path(temp) / "verify.py"
        verifier.write_text(sources[0], encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(verifier),
                "check",
                "--channel",
                doc["channel"],
                "--envelope",
                str(outdir / "release.json"),
                "--files",
                str(stage),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise ValueError(f"release.json: {result.stderr.strip()}")
        print("release.json: ok")


def installer(outdir: Path) -> None:
    source = rehearsal_source()
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / "install-rehearsal.sh"
    with target.open("x", encoding="utf-8") as stream:
        target.chmod(0o700)
        stream.write(NOTICE + source)
    for server in (True, False):
        args = ["sh", str(target.resolve()), "--channel", "test-cut"]
        if server:
            args.append("--server")
        args.extend(["--from", str(outdir.resolve())])
        print(shlex.join(args))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser("fetch")
    download.add_argument("tag")
    download.add_argument("outdir", type=Path)
    commands.add_parser("installer").add_argument("outdir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch":
            fetch(args.tag, args.outdir)
        else:
            installer(args.outdir)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"rehearse: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
