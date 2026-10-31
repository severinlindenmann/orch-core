"""Make a frozen upgrade corpus of publish's data dir.

A script, not a test. Keep one committed corpus/publish-f<format>/ per released format, forever.
Run this at the release's code and commit the new directory next to the old ones. Existing
directories are never overwritten; --out names another place. All data is THROWAWAY test data.

    uv run --project packages/publish python packages/publish/tests/upgrade/make_corpus.py [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [
    str(HERE.parents[0]),
    str(HERE.parents[1]),
]  # tests/ (format_helpers) and publish/ (pubsrv)

from format_helpers import FORMAT, write_format

from pubsrv import db

CORPUS = HERE / "corpus"
KIND = "publish"
WORKSPACE_ID, SHARE_ID = "0" * 32, "1" * 32
STAMP = 1700000000000


def commit() -> str:
    """The commit that makes this corpus, marked when the tree is not clean."""
    top = HERE.parents[3]
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=top, capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=top,
        capture_output=True,
        check=False,
        text=True,
    )
    return sha + (" (plus uncommitted changes)" if dirty.stdout.strip() else "")


def make(state: Path) -> None:
    write_format(state)
    path = db.prepare_dir(state)
    db.init(path)
    conn = db.connect(path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO workspaces "
                "(workspace_id, wsk_pub, owner_person_id, owner_pk, delegation_json, status, created_ms, updated_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    WORKSPACE_ID,
                    "throwaway-wsk",
                    SHARE_ID,
                    "throwaway-owner-key",
                    "{}",
                    "active",
                    STAMP,
                    STAMP,
                ),
            )
            conn.execute(
                "INSERT INTO shares "
                "(id, owner_ws, ticket, kind, token_hash, expires_ms, view_limit, views, state, bytes, "
                "created_ms, label, by_kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    SHARE_ID,
                    WORKSPACE_ID,
                    "throwaway-ticket",
                    "secret",
                    "0" * 64,
                    STAMP + 3600000,
                    0,
                    0,
                    "staged",
                    0,
                    STAMP,
                    "Corpus share",
                    "person",
                ),
            )
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        Path(path + suffix).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, help=f"a NEW directory (default: corpus/{KIND}-f{FORMAT})")
    args = ap.parse_args(argv)
    out = args.out or CORPUS / f"{KIND}-f{FORMAT}"
    if out.exists() or out.is_symlink():
        print(
            f"refused: {out} exists; a committed corpus is frozen. Name a new directory with --out.",
            file=sys.stderr,
        )
        return 2
    out.mkdir(parents=True, mode=0o700)
    make(out)
    line = (
        f"{KIND}-f{FORMAT}: made by tests/upgrade/make_corpus.py at commit {commit()} (format {FORMAT}): "
        "one workspace and one share. Throwaway test data only. "
        "Frozen: never edit, never regenerate in place.\n"
    )
    (out / "CORPUS.md").write_text(line)
    print(json.dumps({"corpus": str(out), "format": FORMAT}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
