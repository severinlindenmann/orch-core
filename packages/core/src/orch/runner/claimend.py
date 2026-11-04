"""W31: at an agent run's end the runner lets go of that run's claim, as the agent.

A claim only lapses by inactivity, grant end or member removal, so an agent that stopped (the person pressed Stop, it
exited, it was killed) kept its ticket for ``claim_ttl_min`` and a new start was refused. The runner holds the run's
``ORCH_GRANT`` and ``ORCH_SESSION`` (:mod:`orch.runner.grants`), so it does what the agent would have done: ``orch
release <ticket>`` with exactly that run's environment.

Guards: only the run's own ticket (``ORCH_TICKET``), only with the run's own grant and session, and only while that
session still holds a live claim on it (``orch show`` first; a claim already released, lapsed or taken over is left
alone). No text of the agent or its output is ever passed on: the argv is fixed. A run whose grant was dropped or has
expired has no secret any more and is skipped (the release would fail anyway). The child's environment goes through
:func:`orch.childenv.sanitize`.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from collections.abc import Callable, Mapping

from orch import childenv

__all__ = ["CliFn", "release_claim", "run_cli"]

LOG = logging.getLogger(__name__)
TIMEOUT = 20.0
_TICKET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
#: ``(orch arguments, environment) -> (exit code, stdout)``
CliFn = Callable[[list[str], Mapping[str, str]], tuple[int, str]]


def run_cli(argv: list[str], env: Mapping[str, str]) -> tuple[int, str]:
    """Run ``orch <argv>`` as a child with a cleaned copy of ``env``. Never raises."""
    try:
        done = subprocess.run(
            # Opus W31: -P keeps the current folder off sys.path and cwd="/" is a folder no agent writes, so a planted
            # orch/__main__.py (e.g. in $HOME, a service's default folder) is never what runs with the grant; the
            # workspace comes from ORCH_WORKSPACE in the run's own environment
            [sys.executable, "-P", "-m", "orch", *argv],
            cwd="/",
            env=childenv.sanitize(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return done.returncode, done.stdout


def release_claim(run: str, base: Mapping[str, str], secret: str | None, cli: CliFn = run_cli) -> bool:
    """Release ``run``'s claim. ``base`` is the run's environment without its secret. True only when this call
    released it; every other outcome is a quiet no-op (logged without any output of the child)."""
    ticket, session = base.get("ORCH_TICKET", ""), base.get("ORCH_SESSION", "")
    if secret is None:
        LOG.info("run %s: no grant to release its claim with (revoked or expired); skipped", run)
        return False
    if not (_TICKET.fullmatch(ticket) and session):
        return False
    env = {**base, "ORCH_GRANT": secret}
    code, out = cli(["show", ticket, "--json"], env)
    if code != 0:
        LOG.info("run %s: claim check failed (exit %s); skipped", run, code)
        return False
    try:
        claim = json.loads(out)["data"]["ticket"]["claim"]
    except (ValueError, KeyError, TypeError):
        return False
    if not (isinstance(claim, dict) and claim.get("live") is True and claim.get("session") == session):
        return False  # already released, lapsed, or held by another session: not ours to touch
    code, _ = cli(["release", ticket], env)
    if code != 0:
        LOG.info("run %s: claim release refused (exit %s)", run, code)
        return False
    LOG.info("run %s: claim released", run)
    return True
