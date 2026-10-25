"""``orch share status|repair``: the backup of what the dashboard does for the agent user's share (#1198 H1 L5,
spec 13.2 item 5). ``status`` says what is shared and what the doctor finds; ``repair`` makes the folders, the mirrors
and the release copy right again, as the person. Both are the person's own terminal: refused under ``ORCH_GRANT`` or
``ORCH_SESSION`` (an agent never changes what it can read). Without a separate agent user (tier A) they say so and
change nothing."""

from __future__ import annotations

import argparse
import os
import pwd
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from orch.host import sharemirror

__all__ = ["main"]

AGENT_ENV = ("ORCH_GRANT", "ORCH_SESSION")
EXIT_REFUSED = 3


def _share(env: Mapping[str, str]) -> sharemirror.Share | None:
    from orch.ops.runtime import state_dir

    try:
        person = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return None
    return sharemirror.share_for(person, Path(state_dir(env)))


def main(
    args: Sequence[str], *, env: Mapping[str, str] | None = None, out: TextIO | None = None, err: TextIO | None = None
) -> int:
    env = os.environ if env is None else env
    out, err = out or sys.stdout, err or sys.stderr
    if any(env.get(name) for name in AGENT_ENV):
        err.write("refused: what an agent can read is a person's decision; an agent never changes it\n")
        return EXIT_REFUSED
    parser = argparse.ArgumentParser(prog="orch share", add_help=True)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="what is shared with the agent user, and whether it is as orch set it up")
    sub.add_parser("repair", help="make the shared folders, mirrors and release right again")
    try:
        ns = parser.parse_args(list(args))
    except SystemExit as exc:
        return 2 if exc.code else 0
    if os.geteuid() == 0:
        err.write("refused: run this as yourself, not as root\n")
        return EXIT_REFUSED
    share = _share(env)
    report = sharemirror.status_text(share) if ns.cmd == "status" else sharemirror.repair(share)
    out.write(report.text() + "\n")
    return 0 if report.ok else EXIT_REFUSED
