"""Make a frozen upgrade corpus of the relay's state dir.

A script, not a test. From 2.0.0 on, data written by the previous release must still open with the code at HEAD
(``test_upgrade_corpus.py``), so every released format keeps ONE corpus directory, committed, forever:
``corpus/relay-f<format>/``. When a release changes the format, run this at that release's code and commit the new
directory next to the old one. This script never overwrites or removes an existing directory: it refuses, and
``--out`` names another place.

    uv run --project packages/relay python packages/relay/tests/upgrade/make_corpus.py [--out DIR]

The format marker is what ``orch install relay`` leaves (``format_helpers``). The database uses the real schema,
with one hub account inserted by plain SQL. All values are fixed, obviously fake, THROWAWAY test data only.
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
    str(HERE.parents[1] / "src"),
]  # tests/ (format_helpers) and src/ (orch_relay)

from format_helpers import FORMAT, write_format

from orch_relay import db

CORPUS = HERE / "corpus"
KIND = "relay"
HUB_ID = "0" * 32
STAMP = 1700000000000


def commit() -> str:
    """The commit of the code that makes this corpus (``git rev-parse``), marked when the tree is not clean."""
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
    path = state / db.DB_NAME
    db.init_schema(path)
    con = db.connect(path)
    try:
        con.execute(
            """INSERT INTO hub_accounts
               (hub_id, hk_root_pub, op_cert_id, op_seq, expires_ms, card_seq, dev, card, registered_ms, updated_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                HUB_ID,
                "FAKE corpus public key",
                "1" * 32,
                1,
                STAMP,
                1,
                1,
                '{"fake":true}',
                STAMP,
                STAMP,
            ),
        )
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    finally:
        con.close()
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, help=f"a NEW directory (default: corpus/{KIND}-f{FORMAT})")
    args = ap.parse_args(argv)
    out = args.out or CORPUS / f"{KIND}-f{FORMAT}"
    if out.exists():
        print(
            f"refused: {out} exists; a committed corpus is frozen. Name a new directory with --out.",
            file=sys.stderr,
        )
        return 2
    out.mkdir(parents=True, mode=0o700)
    make(out)
    line = (
        f"{KIND}-f{FORMAT}: made by tests/upgrade/make_corpus.py at commit {commit()} (format {FORMAT}): one hub account. "
        "Throwaway test data only. Frozen: never edit, never regenerate in place.\n"
    )
    (out / "CORPUS.md").write_text(line)
    print(json.dumps({"corpus": str(out), "format": FORMAT}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
