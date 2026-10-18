"""Read-only git for the File Explorer (issue #491): history of one file, one commit's patch, a file at a revision,
the difference of two versions, the files a commit changed, and a literal content search.

**This module adds no way around :mod:`orch.gitsafe`; it is a second CLOSED set of operations on top of it**, for
callers that need a log, a patch of one commit, a file's content at a revision, or a search (the set in
``gitsafe.OPS`` is the one the host's own observation uses and stays as it is). Every operation:

* runs :func:`orch.gitsafe.gate` first, every time (allow-listed repository config, config files owned by the person,
  no program a config key could name), then builds its argv from a FIXED template: :data:`orch.gitsafe.HARDEN` (hooks
  to ``/dev/null``, no fsmonitor, no network protocol, no submodule recursion, no attributes file, no commit graph),
  ``--no-pager``, ``--no-optional-locks``, ``-C <repo>``, and for ``log``/``show``/``diff`` ``--no-ext-diff
  --no-textconv`` plus the diff pins of ``gitsafe`` (every ``diff.*`` knob, every diff driver's ``binary``/
  ``xfuncname``/``funcname``);
* takes only validated operands, each in a slot of the template: ``<rev>`` (``HEAD``, a full commit id or
  ``refs/heads/<name>``, always after ``--end-of-options``), ``<file>`` (a path below the repository: no leading
  ``/``, no ``.``/``..``/empty component, no control characters; always behind ``:(literal)`` after ``--``, or in the
  object name ``<rev>:./<file>``), ``<num>`` (digits) and ``<pattern>`` (a literal search text, always the value of
  ``-e``). No caller can add a global option, a subcommand or an option of its own;
* runs with the person's system and global git configuration OFF (:func:`orch.gitsafe.isolated_env`), a scratch
  environment (no ``GIT_*`` of the caller), ``cwd="/"``, stdin closed, in its own process group that is killed after
  ``timeout`` seconds or when the output reaches ``max_bytes`` (:func:`run`);
* cannot reach the network (no fetch/pull/push/clone subcommand exists here), and ``grep`` never enters a submodule.

Residual (documented, same as ``gitsafe``): a ``filter`` driver the PERSON configured globally is never run here (the
global configuration is off and these operations never check a file out).
"""

from __future__ import annotations

import contextlib
import os
import re
import select
import signal
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass

from orch import gitsafe
from orch.gitsafe import Unsafe

__all__ = ["OPS", "Result", "command", "run"]

_SLOT = re.compile(r"<(rev|file|num|pattern)>")
#: pins on top of HARDEN for these operations (command scope beats every config file)
_EXTRA = (
    "-c", "i18n.logOutputEncoding=UTF-8", "-c", "core.quotePath=false", "-c", "log.showSignature=false",
    "-c", "log.mailmap=false", "-c", "log.follow=false", "-c", "log.showRoot=true", "-c", "log.decorate=no",
    "-c", "grep.patternType=fixed", "-c", "grep.extendedRegexp=false", "-c", "grep.fallbackToNoIndex=false",
)  # fmt: skip
#: the record format of ``log``/``show``: commit id, author name, author date (strict ISO 8601), subject, body
_COMMIT = "%H%x1f%aN%x1f%aI%x1f%s"
_BODY = _COMMIT + "%x1f%b"


@dataclass(frozen=True)
class Op:
    args: tuple[str, ...]

    @property
    def operands(self) -> tuple[str, ...]:
        return tuple(m for a in self.args for m in _SLOT.findall(a))


_SHOW = ("--no-color", "--no-renames", "--root", "-m", "--first-parent")
OPS: dict[str, Op] = {
    # the commits that changed one file, newest first; records start with \x1e
    "file_log": Op(("log", "--no-color", "--no-renames", f"--format=%x1e{_COMMIT}", "--max-count=<num>",
                    "--skip=<num>", "--end-of-options", "<rev>", "--", ":(literal)<file>")),
    # one commit's patch for one file (a merge: against its first parent), with the commit's record first
    "commit_file_patch": Op(("show", *_SHOW, f"--format=%x1e{_BODY}%x1e", "-U<num>", *gitsafe._DIFF_FIXED,
                             "--end-of-options", "<rev>", "--", ":(literal)<file>")),
    # the files one commit changed: `<status> NUL <path> NUL ...`
    "commit_names": Op(("show", *_SHOW, "--format=", "--name-status", "-z", *gitsafe._DIFF_FIXED, "--end-of-options",
                        "<rev>", "--")),
    # the difference of one file between two commits (old, new)
    "file_diff": Op(("diff", "--no-color", "--no-renames", "-U<num>", *gitsafe._DIFF_FIXED, "--end-of-options",
                     "<rev>", "<rev>", "--", ":(literal)<file>")),
    # a file as it was at a commit: its size, then its bytes
    "blob_size": Op(("cat-file", "-s", "<rev>:./<file>")),
    "blob": Op(("cat-file", "blob", "<rev>:./<file>")),
    # a literal search of the tracked files of the working tree (no submodule), `path NUL line NUL text LF`
    "grep": Op(("grep", "-I", "-n", "-z", "--no-color", "-F", "--no-recurse-submodules", "-e", "<pattern>", "--",
                ":(top)")),
}  # fmt: skip
_NUM = re.compile(r"[0-9]{1,6}")
_BAD_CHAR = re.compile(r"[\x00-\x1f\x7f]")


def _check(kind: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise Unsafe("invalid git operand")
    if kind == "rev":
        return gitsafe._check("rev", value)
    if kind == "num":
        if not _NUM.fullmatch(value):
            raise Unsafe("invalid number")
        return value
    if kind == "pattern":
        if len(value) > 200 or _BAD_CHAR.search(value):
            raise Unsafe("invalid search text")
        return value
    parts = value.split("/")  # file
    if (
        len(value) > 4096
        or value.startswith("/")
        or any(p in ("", ".", "..") for p in parts)
        or _BAD_CHAR.search(value)
    ):
        raise Unsafe("invalid path")
    return value


def _fill(spec: Op, operand: tuple[str, ...]) -> list[str]:
    if len(operand) != len(spec.operands):
        raise Unsafe("wrong number of git operands")
    it = iter(operand)
    return [_SLOT.sub(lambda m: _check(m.group(1), next(it)), a) for a in spec.args]


def command(
    repo: str, op: str, own: Mapping[str, str], *, operand: tuple[str, ...] = (), timeout: float = gitsafe.GATE_SECONDS
) -> tuple[list[str], dict[str, str]]:
    """The argv and environment of operation ``op`` of :data:`OPS` against ``repo`` (absolute). Runs
    :func:`orch.gitsafe.gate` first. Raises :class:`orch.gitsafe.Unsafe` for an unknown operation, an invalid operand
    or a refusal of the gate (git must not run)."""
    spec = OPS.get(op)
    if spec is None:
        raise Unsafe("unknown git operation")
    args = _fill(spec, operand)
    local = gitsafe.gate(repo, own, timeout=timeout)
    entries = gitsafe._driver_pins(local) if spec.args[0] in ("log", "show", "diff") else []
    argv = gitsafe._argv(repo, args)
    argv[1:1] = _EXTRA
    return argv, gitsafe.isolated_env(own, entries)


@dataclass(frozen=True)
class Result:
    code: int
    out: bytes
    cut: bool  # stopped because the output reached ``max_bytes`` or the time ran out
    timed_out: bool = False


def run(
    repo: str,
    op: str,
    own: Mapping[str, str],
    *,
    operand: tuple[str, ...] = (),
    max_bytes: int = 1 << 20,
    timeout: float = 15.0,
) -> Result:
    """Run one operation: at most ``max_bytes`` of output, at most ``timeout`` seconds (the whole gate included in the
    caller's budget is not counted: the gate has its own limit). The process group is killed when either is reached."""
    argv, env = command(repo, op, own, operand=operand)
    try:
        proc = subprocess.Popen(  # noqa: S603 - the argv is a fixed template with validated operands
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd="/",
            start_new_session=True, close_fds=True,
        )  # fmt: skip
    except OSError:
        raise Unsafe("git cannot be started") from None
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    deadline = time.monotonic() + timeout
    buf = bytearray()
    cut = timed_out = False
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                cut = timed_out = True
                break
            ready, _, _ = select.select([fd], [], [], min(left, 0.5))
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > max_bytes:
                cut = True
                del buf[max_bytes:]
                break
    finally:
        if cut or proc.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)  # the group this call created, nothing else
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
        proc.stdout.close()
    return Result(proc.returncode if proc.returncode is not None else -1, bytes(buf), cut, timed_out)
