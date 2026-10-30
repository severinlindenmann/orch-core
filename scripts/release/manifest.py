#!/usr/bin/env python3
"""Build the signed payloads of a release: the core manifest and the add-on catalog (CONTRACT.md §2).

    manifest.py core --channel develop --version 2.0.0.dev42 --seq 42 --commit <sha1> --core-wheel X.whl \
        --dashboard-wheel Y.whl --constraints constraints.txt --base-url https://…/download/v2.0.0.dev42 \
        --issued-at … --expires-at … --changelog-file CHANGELOG.txt --min-from 2.0.0.dev1 --out manifest.json
    manifest.py catalog --channel develop --seq 311 --issued-at … --expires-at … --addons addons.json --out catalog.json

The output is the canonical (RFC 8785 for this value domain) bytes that sign.py signs. No key is involved here.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

CHANNEL = re.compile(r"main|develop|test-[a-z0-9][a-z0-9-]{0,30}")
VERSION = re.compile(r"\d+\.\d+\.\d+(\.dev\d+)?")
TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
COMMIT = re.compile(r"[0-9a-f]{40}")
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
ROLES = ("core", "dashboard", "constraints")
MAX_INT = 2**53 - 1


def jcs(obj: Any) -> bytes:
    """RFC 8785 for the contract's value domain (ASCII keys, strings, ints, bools, lists, objects; no floats)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def need(cond: bool, what: str) -> None:
    if not cond:
        raise ValueError(what)


def check_common(channel: str, seq: int, issued_at: str, expires_at: str) -> None:
    need(CHANNEL.fullmatch(channel) is not None, f"bad channel: {channel!r}")
    need(type(seq) is int and 1 <= seq <= MAX_INT, f"bad seq: {seq!r}")
    for label, value in (("issued_at", issued_at), ("expires_at", expires_at)):
        need(TIME.fullmatch(value) is not None, f"bad {label}: {value!r}")
    need(issued_at < expires_at, "expires_at must be after issued_at")


def file_entry(role: str, path: Path, base_url: str) -> dict[str, Any]:
    name = path.name
    need(FILE_NAME.fullmatch(name) is not None, f"bad file name for {role}: {name!r}")
    data = path.read_bytes()
    need(len(data) >= 1, f"{role} file is empty: {name}")
    url = f"{base_url.rstrip('/')}/{name}"
    need(url.startswith("https://") and len(url) <= 512, f"bad url for {role}")
    need(all(c.isprintable() and c != " " for c in url), f"bad url for {role}: not printable or has spaces")
    return {"role": role, "name": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "url": url}


def build_manifest(
    *,
    channel: str,
    version: str,
    seq: int,
    commit: str,
    issued_at: str,
    expires_at: str,
    core_wheel: Path,
    dashboard_wheel: Path,
    constraints: Path,
    base_url: str,
    changelog: str,
    min_from: str,
) -> dict[str, Any]:
    check_common(channel, seq, issued_at, expires_at)
    need(VERSION.fullmatch(version) is not None, f"bad version: {version!r}")
    need(VERSION.fullmatch(min_from) is not None, f"bad min_from: {min_from!r}")
    need(COMMIT.fullmatch(commit) is not None, "commit must be 40 lowercase hex characters")
    need(len(changelog.encode("utf-8")) <= 4096, "changelog is longer than 4096 bytes")
    paths = {"core": core_wheel, "dashboard": dashboard_wheel, "constraints": constraints}
    return {
        "format": "orch.release/1",
        "channel": channel,
        "version": version,
        "seq": seq,
        "commit": commit,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "files": [file_entry(role, paths[role], base_url) for role in ROLES],
        "changelog": changelog,
        "min_from": min_from,
    }


def build_catalog(*, channel: str, seq: int, issued_at: str, expires_at: str, addons: dict[str, Any]) -> dict[str, Any]:
    check_common(channel, seq, issued_at, expires_at)
    need(isinstance(addons, dict), "addons must be an object")
    return {
        "format": "orch.addon-catalog/1",
        "channel": channel,
        "seq": seq,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "addons": addons,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="kind", required=True)
    for kind in ("core", "catalog"):
        p = sub.add_parser(kind)
        p.add_argument("--channel", required=True)
        p.add_argument("--seq", type=int, required=True)
        p.add_argument("--issued-at", required=True)
        p.add_argument("--expires-at", required=True)
        p.add_argument("--out", type=Path, required=True)
    core = sub.choices["core"]
    core.add_argument("--version", required=True)
    core.add_argument("--commit", required=True)
    core.add_argument("--core-wheel", type=Path, required=True)
    core.add_argument("--dashboard-wheel", type=Path, required=True)
    core.add_argument("--constraints", type=Path, required=True)
    core.add_argument("--base-url", required=True)
    core.add_argument("--changelog-file", type=Path, required=True)
    core.add_argument("--min-from", required=True)
    sub.choices["catalog"].add_argument("--addons", type=Path, required=True, help="JSON file: the addons object")
    a = ap.parse_args(argv)
    try:
        if a.kind == "core":
            doc = build_manifest(
                channel=a.channel,
                version=a.version,
                seq=a.seq,
                commit=a.commit,
                issued_at=a.issued_at,
                expires_at=a.expires_at,
                core_wheel=a.core_wheel,
                dashboard_wheel=a.dashboard_wheel,
                constraints=a.constraints,
                base_url=a.base_url,
                changelog=a.changelog_file.read_text(encoding="utf-8").strip(),
                min_from=a.min_from,
            )
        else:
            doc = build_catalog(
                channel=a.channel,
                seq=a.seq,
                issued_at=a.issued_at,
                expires_at=a.expires_at,
                addons=json.loads(a.addons.read_text(encoding="utf-8")),
            )
    except (ValueError, OSError) as exc:
        print(f"manifest: {exc}", file=sys.stderr)
        return 1
    a.out.write_bytes(jcs(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
