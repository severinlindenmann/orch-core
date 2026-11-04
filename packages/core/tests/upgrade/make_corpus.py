"""Make a frozen upgrade corpus of the core's data roots: the host state dir and a workspace store.

A script, not a test. From 2.0.0 on, data written by the previous release must still open with the code at HEAD
(``test_upgrade_corpus.py``), so every released format keeps ONE corpus directory, committed, forever:
``corpus/<kind>-f<format>/``. When a release changes a format, run this at that release's code (it writes the CURRENT
format into a NEW directory) and commit the new directory next to the old one. This script never overwrites or removes
an existing directory: it refuses, and ``--out`` names another place.

    uv run --project packages/core python packages/core/tests/upgrade/make_corpus.py workspace|host [--out DIR]

The data is made by the real code paths through the test helpers of the suite (``tests.store.helpers.Env``,
``tests.ops.helpers.Ws``: real signed events, a real store, a real host state dir) on a fixed clock. The keys are
THROWAWAY keys generated for the corpus: they protect nothing. Keys and event ids are random per run; the bytes of
a regenerated corpus differ, their shape does not.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # packages/core: `tests` and `orch` import when run as a script

from orch.host.custody import write_private  # noqa: E402
from orch.host.registry import Entry, group_name, registry_path  # noqa: E402
from orch.migrate import CURRENT, write_format  # noqa: E402

CORPUS = HERE / "corpus"
KINDS = ("host", "workspace")
EMPTY_DIRS = "empty-dirs.txt"
SKIP = ("lock", "index.sqlite")  # derived (rebuilt by version), never part of a corpus
ROOT = "/corpus/project"  # the registered root of the host corpus: nothing reads it back from disk


def commit() -> str:
    """The commit of the code that makes this corpus (``git rev-parse``), marked when the tree is not clean."""
    top = HERE.parents[3]
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=top, capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=top, capture_output=True, text=True
    )
    return sha + (" (plus uncommitted changes)" if dirty.stdout.strip() else "")


def copy_tree(src: Path, dst: Path) -> None:
    def skip(_dir: str, names: list[str]) -> list[str]:
        return [n for n in names if n in SKIP or n.startswith(tuple(f"{s}-" for s in SKIP)) or ".tmp" in n]

    shutil.copytree(src, dst, ignore=skip)


def make_workspace(out: Path) -> str:
    """A store with a genesis, one grant, a ticket created, edited and closed, and the checkpoints the store wrote."""
    from tests.store.helpers import Env

    with tempfile.TemporaryDirectory(prefix="orch-corpus-") as tmp:
        env = Env(Path(tmp))
        try:
            env.bootstrap()
            uid = env.new_ticket("Corpus ticket")
            env.tick()
            env.update(uid, {"ticket.title": "Corpus ticket, edited", "ticket.priority": "high"}, {"context": "why"})
            env.tick()
            env.close(uid)
            assert env.store is not None and env.store.chain_errors() == []
            env.store.close()
        finally:
            if env.store is not None:
                env.store.fs.close()
        copy_tree(env.store_dir, out)
    write_format(out, "workspace", CURRENT["workspace"])
    return (
        "a genesis, one grant, one ticket created, edited and closed, workspace + ticket checkpoints "
        f"(workspace {env.ws_id})"
    )


def make_host(out: Path) -> str:
    """A host state dir as ``orch setup`` leaves it: the workspace key and genesis pin, the heads database, and the
    registry with that workspace registered."""
    import os

    from tests.ops.helpers import Ws

    with tempfile.TemporaryDirectory(prefix="orch-corpus-") as tmp:
        ws = Ws(Path(tmp))
        try:
            ws.bootstrap()
            ws.store.close()
            copy_tree(ws.host_state, out)
        finally:
            if ws.store is not None:
                ws.store.fs.close()
    entry = Entry(
        ws.ws_id,
        "DEMO",
        ROOT,
        os.getuid(),
        group_name(ws.ws_id),
        {str(os.getuid()): {"class": "person", "person": ws.owner.ref}},
    )
    write_private(registry_path(out), {"v": 1, "workspaces": [entry.to_doc()]})
    write_format(out, "host", CURRENT["host"])
    return (
        "the state dir of a bootstrapped workspace (key, genesis pin, heads) and its registry entry "
        f"(workspace {ws.ws_id})"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("kind", choices=KINDS)
    ap.add_argument("--out", type=Path, help="a NEW directory (default: corpus/<kind>-f<current format>)")
    args = ap.parse_args(argv)
    fmt = CURRENT[args.kind]
    out = args.out or CORPUS / f"{args.kind}-f{fmt}"
    if out.exists():
        print(f"refused: {out} exists; a committed corpus is frozen. Name a new directory with --out.", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        what = (make_workspace if args.kind == "workspace" else make_host)(out)
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)  # a half-made corpus of OURS (the directory did not exist before)
        raise
    line = (
        f"{args.kind}-f{fmt}: made by tests/upgrade/make_corpus.py at commit {commit()} (format {fmt}): {what}. "
        "Throwaway test keys only. Frozen: never edit, never regenerate in place.\n"
    )
    (out / "CORPUS.md").write_text(line)
    # git keeps no empty folder: the ones the data has are listed, and the test makes them again
    empty = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_dir() and not any(p.iterdir()))
    (out / EMPTY_DIRS).write_text("".join(f"{rel}\n" for rel in empty))
    print(json.dumps({"corpus": str(out), "format": fmt}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
