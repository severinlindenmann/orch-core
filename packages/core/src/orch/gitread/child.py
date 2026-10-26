"""The reader child (``python -I -m orch.gitread``), started only by :func:`orch.host.launcher.spawn_gitread`.

Protocol, on stdin and stdout, one JSON object per line:

1. ``{"profile": "gitdir"|"worktree", "pins": RepoPins}`` or ``{"profile": "pin", "roots": [path, ...]}``;
2. ``{"op": str, "args": {...}}`` (:mod:`orch.gitread.ops`);
3. the answer: ``{"ok": result}`` or ``{"error": {"code", "message"}}``; then the child exits.

The child sets its limits before anything else, reads both requests, imports everything it will use, installs its
audit hook for the profile and only then opens the repository.
"""

from __future__ import annotations

import json
import sys
from typing import Any, BinaryIO

from . import limits, sandbox
from .errors import Unavailable


def _line(stream: BinaryIO) -> dict[str, Any]:
    raw = stream.readline(limits.REQUEST_MAX + 1)
    if not raw.endswith(b"\n") or len(raw) > limits.REQUEST_MAX:
        raise Unavailable("bad request framing")
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise Unavailable("bad request")
    return doc


def serve(inp: BinaryIO, out: BinaryIO) -> int:
    try:
        hello = _line(inp)
        req = _line(inp)
        profile = hello.get("profile")
        from . import ops  # every module the request needs, before the hook

        if profile not in ops.PROFILES:
            raise Unavailable("bad profile")
        sandbox.install(ops.allowed_paths(profile, hello))
        resp: dict[str, Any] = {"ok": ops.run(profile, hello, req)}
    except Unavailable as e:
        resp = {"error": {"code": e.code, "message": e.reason}}
    except PermissionError:
        resp = {"error": {"code": "observe.unavailable", "message": "the reader's profile refused an access"}}
    except MemoryError:
        resp = {"error": {"code": "observe.unavailable", "message": "the reader ran out of its memory limit"}}
    except (ValueError, RecursionError):
        resp = {"error": {"code": "observe.unavailable", "message": "the reader could not parse the request"}}
    except Exception as e:  # never a traceback: it would open source files the hook refuses
        resp = {"error": {"code": "observe.unavailable", "message": f"reader error ({type(e).__name__})"}}
    out.write(json.dumps(resp, separators=(",", ":")).encode("utf-8") + b"\n")
    out.flush()
    return 0


def main() -> int:
    sandbox.apply_limits()  # first: before a request byte, let alone a repository byte, is read
    return serve(sys.stdin.buffer, sys.stdout.buffer)
