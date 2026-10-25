"""``orch share repo|release|remove``: the person's own sharing with the agent user (#1198 H1 L5, spec §13.2).

``repo PATH`` is the step that follows registering a repository (:func:`share_registered_repo` is the one function a
registration flow calls); ``release PATH...`` shares the release and the interpreter tree; ``remove PATH`` takes the
agent's entries away again (``--keep PATH`` names other shared paths whose ancestors stay). Run it as yourself, never as
root and never from an agent. Without a separate agent user (tier A) it says so and changes nothing.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from typing import TextIO

from orch.setup import shareacl

__all__ = ["main", "share_registered_repo"]

AGENT_ENV = ("ORCH_GRANT", "ORCH_SESSION")
EXIT_REFUSED = 3


def share_registered_repo(repo: str) -> shareacl.Report:
    """The registration hook: called with the absolute path of a repository that was just registered. A no-op report in
    tier A; otherwise the sharing of :func:`orch.setup.shareacl.share_repo`. The caller prints ``report.text()``."""
    return shareacl.share_repo(repo)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch share", add_help=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("repo").add_argument("path")
    r = sub.add_parser("release")
    r.add_argument("paths", nargs="+")
    d = sub.add_parser("remove")
    d.add_argument("path")
    d.add_argument("--release", action="store_true", help="the path is a release tree, not a repository")
    d.add_argument("--keep", action="append", default=[], help="another shared path whose ancestors stay")
    return p


def main(
    args: Sequence[str], *, env: Mapping[str, str] | None = None, out: TextIO | None = None, err: TextIO | None = None
) -> int:
    env = os.environ if env is None else env
    out, err = out or sys.stdout, err or sys.stderr
    if any(env.get(n) for n in AGENT_ENV):
        err.write("refused: sharing files with the agent user is a person's decision; an agent never does it\n")
        return EXIT_REFUSED
    try:
        a = _parser().parse_args(list(args))
    except SystemExit as exc:
        return 2 if exc.code else 0
    if a.cmd == "repo":
        reports = [share_registered_repo(a.path)]
    elif a.cmd == "release":
        reports = [shareacl.share_release(p) for p in a.paths]
    else:
        reports = [shareacl.unshare(a.path, is_repo=not a.release, keep=a.keep)]
    code = 0
    for rep in reports:
        out.write(rep.text() + "\n")
        if not rep.ok:
            code = EXIT_REFUSED
    return code
