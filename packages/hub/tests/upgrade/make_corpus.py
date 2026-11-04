"""Make a frozen upgrade corpus of the hub's state dir.

A script, not a test. From 2.0.0 on, data written by the previous release must still open with the code at HEAD
(``test_upgrade_corpus.py``), so every released format keeps ONE corpus directory, committed, forever:
``corpus/hub-f<format>/``. When a release changes the format, run this at that release's code (it writes the CURRENT
format into a NEW directory) and commit the new directory next to the old one. This script never overwrites or removes
an existing directory: it refuses, and ``--out`` names another place.

    uv run --project packages/hub python packages/hub/tests/upgrade/make_corpus.py [--out DIR]

The data is made by the real code paths: the format marker ``orch install hub`` leaves (``format_helpers``), then the
``orch-hub`` commands ``keys init``, ``users add``, ``groups add`` and ``groups add-member`` (which also write the
audit chain). The keys are THROWAWAY keys made for the corpus: they protect nothing. Keys and ids are random per run:
the bytes of a regenerated corpus differ, their shape does not.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[0]), str(HERE.parents[1] / "src")]  # tests/ (format_helpers) and src/ (orch_hub)

from format_helpers import FORMAT, write_format

from orch_hub import cli

CORPUS = HERE / "corpus"
KIND = "hub"
EMPTY_DIRS = "empty-dirs.txt"
USER, GROUP = "Corpus User", "corpus-group"


def commit() -> str:
    """The commit of the code that makes this corpus (``git rev-parse``), marked when the tree is not clean."""
    top = HERE.parents[3]
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=top, capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=top, capture_output=True, check=False, text=True
    )
    return sha + (" (plus uncommitted changes)" if dirty.stdout.strip() else "")


def hub(*argv: str) -> str:
    """One ``orch-hub`` command; its stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(list(argv))
    if rc != 0:
        raise SystemExit(f"orch-hub {' '.join(argv)} failed ({rc})")
    return buf.getvalue()


def make(state: Path) -> str:
    write_format(state)  # what `orch install hub` leaves before the hub makes its keys
    hub("keys", "--state", str(state), "init")
    user = json.loads(hub("users", "--state", str(state), "add", "--name", USER).strip().splitlines()[-1])
    hub("groups", "--state", str(state), "add", GROUP)
    hub("groups", "--state", str(state), "add-member", GROUP, user["user_id"])
    return f"keys init, one user ({USER!r}), one group ({GROUP!r}) with that user as its member, the audit chain of those changes"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, help=f"a NEW directory (default: corpus/{KIND}-f{FORMAT})")
    args = ap.parse_args(argv)
    out = args.out or CORPUS / f"{KIND}-f{FORMAT}"
    if out.exists():
        print(f"refused: {out} exists; a committed corpus is frozen. Name a new directory with --out.", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="orch-corpus-") as tmp:
        state = Path(tmp) / "hub"
        what = make(state)
        # the lock files are made by taking a lock: no data
        shutil.copytree(state, out, ignore=lambda _d, names: [n for n in names if n == "format.lock"])
    line = (
        f"{KIND}-f{FORMAT}: made by tests/upgrade/make_corpus.py at commit {commit()} (format {FORMAT}): {what}. "
        "Throwaway test keys only. Frozen: never edit, never regenerate in place.\n"
    )
    (out / "CORPUS.md").write_text(line)
    # git keeps no empty folder: the ones the data has are listed, and the test makes them again
    empty = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_dir() and not any(p.iterdir()))
    (out / EMPTY_DIRS).write_text("".join(f"{rel}\n" for rel in empty))
    print(json.dumps({"corpus": str(out), "format": FORMAT}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
