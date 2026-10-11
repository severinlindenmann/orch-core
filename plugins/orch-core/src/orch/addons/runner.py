"""The out-of-process addon runner: JSON-RPC 2.0 over stdio, one process per call (ticket-format §8.1).

Nothing in P1 calls this (A5: addons run from P2), but the boundary is built and tested now so that P2 only wires it.
P2 wires it as: ``call`` for a workspace-visible ticket, then ``Registry.check_proposal`` on the result, then the host
builds, validates (``Registry.check_write``) and signs the events itself.

What the host guarantees, each point pinned by ``tests/addons/test_runner.py``:

* **A private, verified copy.** The package is staged read-only in a fresh temporary directory and its digest is
  checked on the copy against the granted ``package_sha256``; the addon runs from the copy, never from the workspace.
* **A closed environment.** ``PATH``, ``LANG``, ``HOME``/``TMPDIR`` (the working directory),
  ``PYTHONDONTWRITEBYTECODE``, ``PYTHONPATH``, ``ORCH_ADDON``, ``ORCH_ADDON_CAPABILITIES`` and nothing else: no
  ``ORCH_GRANT``, no key, no token. Every file descriptor but 0, 1 and 2 is closed. The working directory is
  empty and its own.
* **Strict framing.** One request line in; exactly one response line out (at most 256 KiB), strict JSON, a fixed shape;
  stderr is capped and never interpreted. A crash, a timeout, too much output, a second line, malformed or extra
  keys, a wrong id: an error of the call, nothing applied.
* **A deadline.** The whole call, including startup, is bounded; the process group is killed at the end of every call.
  A process that leaves its group (``setsid``, a double fork) is **not** tracked and outlives the call; killing every
  process of the addon needs its own UID or a cgroup, which is a P2 requirement (ticket-format §8.1).
* **Bound to the grant.** The command, the capabilities and the manifest come from the package bytes that match the
  signed ``addon.granted`` (digest, name, version, capabilities), never from a separately passed manifest. The
  request carries only the ticket's key, type, status and this addon's own fields, and only for a ticket whose
  visibility is ``workspace``.
* **Proposals only.** The result is checked by :meth:`orch.addons.registry.Registry.check_proposal`; the addon holds
  no key and appends nothing.

Process limits (CPU time, file size, no core files) are defence in depth, **not a sandbox**: until the P2 host runs
addons under their own UID, an addon is trusted to the degree the owner trusts its package. In particular "no addon
holds a key" (core §4) is true only once the addon's UID cannot read the host's key store; in the same UID it is not
(ticket-format §8.1).
"""

from __future__ import annotations

import contextlib
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orch import canon
from orch.addons.manifest import ManifestError, load_manifest
from orch.addons.package import MANIFEST_FILE, Package, PackageError, check_path, package_digest, read_package
from orch.model.types import Addon

__all__ = ["Limits", "RunnerError", "call", "child_env"]

PATH = "/usr/local/bin:/usr/bin:/bin"
MAX_REQUEST = 64 << 10


class RunnerError(Exception):
    """A call failed. ``code`` is one of ``addon.digest_mismatch``, ``addon.start_failed``, ``addon.timeout``,
    ``addon.crashed``, ``addon.output_too_large``, ``addon.bad_response``, ``addon.error`` (the addon answered with a
    JSON-RPC error), ``addon.bad_request``; ``detail`` never contains output of the addon, ``stderr`` does (capped, for
    the log, never to be shown as instructions)."""

    def __init__(self, code: str, detail: str, stderr: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.stderr = stderr


@dataclass(frozen=True)
class Limits:
    timeout: float = 10.0  # seconds for the whole call
    max_response: int = 256 << 10
    max_stderr: int = 16 << 10

    def __post_init__(self) -> None:
        if not 0 < self.timeout <= 60 or self.max_response <= 0 or self.max_stderr < 0:
            raise ValueError("limits out of range (timeout 0..60 s)")


def child_env(name: str, capabilities: Sequence[str], work: str, pkg: str) -> dict[str, str]:
    """The whole environment of an addon process (§8.1). Built from nothing, never from the host's."""
    return {
        "PATH": PATH,
        "LANG": "C.UTF-8",
        "HOME": work,
        "TMPDIR": work,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": pkg,
        "ORCH_ADDON": name,
        "ORCH_ADDON_CAPABILITIES": ",".join(capabilities),
    }


def _stage(package: Package, root: str) -> str:
    """Write the package read-only under ``root/pkg`` and return its path."""
    pkg = os.path.join(root, "pkg")
    os.mkdir(pkg, 0o700)
    dirs = [pkg]
    for rel, data in package.files.items():
        check_path(rel)  # a hand-built Package is checked again: no ``..``, no absolute path, no odd name
        parts = rel.split("/")
        d = pkg
        for part in parts[:-1]:
            d = os.path.join(d, part)
            if not os.path.isdir(d):
                os.mkdir(d, 0o700)
                dirs.append(d)
        fd = os.open(
            os.path.join(d, parts[-1]), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o400
        )
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    for d in reversed(dirs):
        os.chmod(d, 0o500)
    return pkg


_TRAMPOLINE = """
import os, resource, sys
cpu, exe = int(sys.argv[1]), sys.argv[2]
for which, value in ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_FSIZE, 1 << 20), (resource.RLIMIT_CPU, cpu)):
    try:
        resource.setrlimit(which, (value, value))
    except (ValueError, OSError):
        pass
os.execv(exe, sys.argv[2:])
"""


def _kill(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)


def _rmtree(path: str) -> None:
    def fix(func: Any, p: str, _exc: Any) -> None:
        with contextlib.suppress(OSError):
            os.chmod(os.path.dirname(p), 0o700)
            os.chmod(p, 0o700)
            func(p)

    for dirpath, dirnames, _files in os.walk(path):
        with contextlib.suppress(OSError):
            os.chmod(dirpath, 0o700)
        for d in dirnames:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(dirpath, d), 0o700)
    shutil.rmtree(path, onerror=fix)


def _own_fields(ticket: Any, name: str) -> dict[str, Any]:
    f = ticket.fields.get("addons", {}).get(name, {}) if hasattr(ticket, "fields") else {}

    def plain(v: Any) -> Any:
        if isinstance(v, Mapping):
            return {k: plain(x) for k, x in v.items()}
        if isinstance(v, tuple | list):
            return [plain(x) for x in v]
        return v

    return plain(f)


def call(
    package: Package,
    granted: Addon,
    *,
    trigger: str,
    ticket: Any,
    limits: Limits | None = None,
) -> Any:
    """Run the addon once with method ``propose`` and return the ``result`` of its response (validate it with
    ``Registry.check_proposal``). ``granted`` is the replayed grant; ``ticket`` is a ``TicketView``, of which only the
    key, type, status and this addon's own fields are sent. Raises :class:`RunnerError`."""
    if not granted.enabled or granted.purged:
        raise RunnerError("addon.inactive", "the addon is disabled or purged")
    if ticket.visibility != "workspace":
        raise RunnerError("addon.not_visible", "an addon is run only for a ticket every member can see")
    try:
        digest = package_digest(package.files)  # recomputed: the digest field of a Package is never trusted
    except PackageError:
        raise RunnerError("addon.digest_mismatch", "the package is empty") from None
    if digest != granted.package_sha256 or MANIFEST_FILE not in package.files:
        raise RunnerError("addon.digest_mismatch", "the package is not the one that was granted")
    try:
        manifest = load_manifest(package.files[MANIFEST_FILE])
    except ManifestError:
        raise RunnerError("addon.digest_mismatch", "the granted package has no valid manifest") from None
    if (manifest.name, manifest.version, manifest.capabilities) != (
        granted.name,
        granted.version,
        sorted(granted.capabilities),
    ):
        raise RunnerError("addon.digest_mismatch", "the manifest is not the one that was granted")
    granted_digest = granted.package_sha256
    try:
        request = canon.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "propose",
                "params": {
                    "addon": granted.name,
                    "version": granted.version,
                    "capabilities": sorted(granted.capabilities),
                    "trigger": trigger,
                    "ticket": {
                        "key": ticket.key,
                        "type": ticket.type,
                        "status": ticket.status,
                        "fields": _own_fields(ticket, granted.name),
                    },
                },
            }
        )
    except (ValueError, TypeError, RecursionError):
        raise RunnerError("addon.bad_request", "the request is not strict JSON") from None
    request += b"\n"
    if len(request) > MAX_REQUEST:
        raise RunnerError("addon.bad_request", f"the request is larger than {MAX_REQUEST} bytes")

    limits = limits or Limits()
    deadline = time.monotonic() + limits.timeout
    root = tempfile.mkdtemp(prefix="orch-addon-")
    proc: subprocess.Popen[bytes] | None = None
    try:
        os.chmod(root, 0o700)
        try:
            pkg = _stage(package, root)
        except (PackageError, OSError) as e:
            raise RunnerError("addon.digest_mismatch", f"the package is not stageable: {type(e).__name__}") from None
        work = os.path.join(root, "work")
        os.mkdir(work, 0o700)
        try:
            if read_package(pkg).digest != granted_digest:  # the digest of the copy, not of what was read before
                raise RunnerError("addon.digest_mismatch", "the staged copy differs from the granted package")
        except PackageError:
            raise RunnerError("addon.digest_mismatch", "the staged copy is not a valid package") from None
        env = child_env(granted.name, sorted(granted.capabilities), work, pkg)
        argv = [a.replace("{pkg}", pkg) for a in manifest.entry_cmd]
        exe = argv[0] if os.sep in argv[0] else shutil.which(argv[0], path=PATH)
        if exe is None:
            raise RunnerError("addon.start_failed", "the command is not found")
        argv[0] = exe
        try:
            # a trampoline sets the resource limits and execs the command: no ``preexec_fn`` (unsafe in a threaded host)
            proc = subprocess.Popen(  # noqa: S603  (argv comes from the granted manifest, no shell)
                [sys.executable, "-I", "-S", "-c", _TRAMPOLINE, str(int(limits.timeout) + 2), *argv],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=work,
                env=env,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, ValueError):
            raise RunnerError("addon.start_failed", "the process did not start") from None
        out, err, overflow = _converse(proc, request, deadline, limits)
        _kill(proc)  # the group is killed; a process that left it (setsid) is not tracked in P1
        if overflow == "timeout":
            raise RunnerError("addon.timeout", f"no complete answer within {limits.timeout:g} s", _text(err))
        if overflow == "output":
            raise RunnerError("addon.output_too_large", f"more than {limits.max_response} bytes", _text(err))
        code = proc.returncode
        if code != 0:
            raise RunnerError("addon.crashed", f"exit status {code}", _text(err))
        return _parse(out, _text(err))
    finally:
        if proc is not None:
            _kill(proc)
            with contextlib.suppress(Exception):
                proc.wait(timeout=2)
            for f in (proc.stdin, proc.stdout, proc.stderr):
                if f is not None:
                    with contextlib.suppress(OSError):
                        f.close()
        _rmtree(root)


def _converse(
    proc: subprocess.Popen[bytes], request: bytes, deadline: float, limits: Limits
) -> tuple[bytes, bytes, str]:
    """Feed the request, collect stdout and stderr under the caps and the deadline, wait for the exit. The last item is
    ``""``, ``"timeout"`` or ``"output"``."""
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    sel = selectors.DefaultSelector()
    for f in (proc.stdin, proc.stdout, proc.stderr):
        os.set_blocking(f.fileno(), False)
    sel.register(proc.stdin, selectors.EVENT_WRITE, "in")
    sel.register(proc.stdout, selectors.EVENT_READ, "out")
    sel.register(proc.stderr, selectors.EVENT_READ, "err")
    out, err, sent = bytearray(), bytearray(), 0
    try:
        while sel.get_map():
            left = deadline - time.monotonic()
            if left <= 0:
                return bytes(out), bytes(err), "timeout"
            events = sel.select(timeout=min(left, 0.2))
            if not events and proc.poll() is not None:
                break  # exited and nothing is waiting: a grandchild that holds a pipe is not waited for
            for key, _ in events:
                which = key.data
                if which == "in":
                    try:
                        sent += os.write(proc.stdin.fileno(), request[sent : sent + 65536])
                    except BlockingIOError:
                        continue
                    except OSError:  # the addon closed its stdin: it will answer or not
                        sent = len(request)
                    if sent >= len(request):
                        sel.unregister(proc.stdin)
                        proc.stdin.close()
                    continue
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)  # type: ignore[union-attr]
                except BlockingIOError:
                    continue
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                if which == "out":
                    out += chunk
                    if len(out) > limits.max_response + 1:
                        return bytes(out), bytes(err), "output"
                elif len(err) < limits.max_stderr:
                    err += chunk[: limits.max_stderr - len(err)]
        try:
            proc.wait(timeout=max(deadline - time.monotonic(), 0.001))
        except subprocess.TimeoutExpired:
            return bytes(out), bytes(err), "timeout"
        return bytes(out), bytes(err), ""
    finally:
        sel.close()


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _parse(raw: bytes, stderr: str) -> Any:
    def bad(why: str) -> RunnerError:
        return RunnerError("addon.bad_response", why, stderr)

    if not raw.endswith(b"\n") or b"\n" in raw[:-1]:
        raise bad("the answer is exactly one line ending in LF")
    try:
        doc = canon.loads_strict(raw[:-1])
    except (canon.JcsError, ValueError, RecursionError, UnicodeError):
        raise bad("the answer is not strict JSON") from None
    if (
        type(doc) is not dict
        or doc.get("jsonrpc") != "2.0"
        or "id" not in doc
        or doc["id"] != 1
        or type(doc["id"]) is not int
    ):
        raise bad("not a JSON-RPC 2.0 response to request 1")
    keys = set(doc)
    if keys == {"jsonrpc", "id", "result"}:
        return doc["result"]
    if keys == {"jsonrpc", "id", "error"}:
        e = doc["error"]
        if type(e) is dict and set(e) == {"code", "message"} and type(e["code"]) is int and type(e["message"]) is str:
            raise RunnerError("addon.error", f"the addon answered with error {e['code']}", stderr)
        raise bad("the error object is {code, message}")
    raise bad("exactly one of result and error")
