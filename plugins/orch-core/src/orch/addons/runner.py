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
* **Methods are closed and bound to a capability** (addon-api 1.1, ``METHODS``): ``propose`` needs none,
  ``launch.model`` needs ``choose_model`` in the active grant. :func:`call` is the one generic call; :func:`propose`
  builds the ticket params. The caller (core) builds the params and checks the answer per method.
* **``{python}``** is expanded only when ``cmd[0]`` is exactly that token, to core's own interpreter without
  site-packages (``PYTHON_ARGV``); never ``python3`` from ``PATH``. A later argument is never expanded for it.
* **A credential** is read through the caller's provider just before the call, only while the grant holds ``credential``
  and ``network``, and travels only in ``params.credential`` on stdin: never in the environment, argv, the logs or an
  error. stderr is redacted, and an answer that holds it is ``addon.bad_response``.

Process limits (CPU time, file size, no core files) are defence in depth, **not a sandbox**: until the P2 host runs
addons under their own UID, an addon is trusted to the degree the owner trusts its package. In particular "no addon
holds a key" (core §4) is true only once the addon's UID cannot read the host's key store; in the same UID it is not
(ticket-format §8.1).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema import exceptions as jsonschema_exceptions

from orch import __version__ as CORE_VERSION
from orch import canon
from orch.addons import envtree
from orch.addons.manifest import ManifestError, incompatibility, load_manifest
from orch.addons.package import MANIFEST_FILE, Package, PackageError, check_path, package_digest, read_package
from orch.market import MarketError
from orch.model.types import Addon

__all__ = ["METHODS", "PYTHON_ARGV", "Limits", "RunnerError", "call", "child_env", "propose"]

PATH = "/usr/local/bin:/usr/bin:/bin"
#: where a Node add-on's ``node`` is looked for (V3 G4): fixed system directories, never a path the add-on or the
#: dependency environment names
NODE_DIRS = "/opt/homebrew/bin:/usr/local/bin:/usr/bin"
MAX_REQUEST = 64 << 10
PYTHON_TOKEN = "{python}"
_BARE_PYTHON = re.compile(r"python[0-9.]*")  # a bare interpreter name would be looked up on PATH
#: what ``{python}`` expands to: core's interpreter, no bytecode, no PYTHON* environment, no user or system
#: site-packages (so an add-on cannot import orch). Not ``-I``: it would drop the script directory.
PYTHON_ARGV = (sys.executable, "-B", "-E", "-s", "-S")
#: the methods an add-on is called with, and the capability each needs in the active grant (``None``: none)
METHODS: Mapping[str, str | None] = {"propose": None, "launch.model": "choose_model"}


class RunnerError(Exception):
    """A call failed. ``code`` is one of ``addon.digest_mismatch``, ``addon.incompatible``, ``addon.start_failed``,
    ``addon.timeout``, ``addon.crashed``, ``addon.output_too_large``, ``addon.bad_response``, ``addon.error`` (the
    addon answered with a JSON-RPC error), ``addon.bad_request``; ``detail`` never contains output of the addon,
    ``stderr`` does (capped, for the log, never to be shown as instructions)."""

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


def child_env(
    name: str, capabilities: Sequence[str], work: str, pkg: str, env_path: str | None = None
) -> dict[str, str]:
    """The whole environment of an addon process (§8.1). Built from nothing, never from the host's. ``env_path`` (V3 G4)
    is the verified dependency environment of a Python add-on: it follows the package on ``PYTHONPATH``."""
    return {
        "PATH": PATH,
        "LANG": "C.UTF-8",
        "HOME": work,
        "TMPDIR": work,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": pkg if env_path is None else f"{pkg}:{env_path}",
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


def _link_node_modules(pkg: str, env_path: str) -> None:
    """``<pkg>/node_modules`` -> ``<env>/node_modules``, made by orch after the digest check of the copy, so Node
    (CommonJS and ESM both walk up from the file) finds exactly the verified packages. The package directory is the
    runner's own."""
    os.chmod(pkg, 0o700)
    try:
        os.symlink(os.path.join(env_path, "node_modules"), os.path.join(pkg, "node_modules"))
    except OSError:
        raise RunnerError("addon.start_failed", "the node_modules link could not be made") from None
    finally:
        os.chmod(pkg, 0o500)


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


def propose(
    package: Package,
    granted: Addon,
    *,
    trigger: str,
    ticket: Any,
    limits: Limits | None = None,
    env: envtree.EnvLocation | None = None,
) -> Any:
    """:func:`call` with method ``propose``: ``ticket`` is a ``TicketView``, of which only the key, type, status and
    this addon's own fields are sent, and only for a ticket whose visibility is ``workspace``.
    ``env`` is :func:`call`'s."""
    if not granted.enabled or granted.purged:
        raise RunnerError("addon.inactive", "the addon is disabled or purged")
    if ticket.visibility != "workspace":
        raise RunnerError("addon.not_visible", "an addon is run only for a ticket every member can see")
    params = {
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
    }
    return call(package, granted, "propose", params, limits, env=env)


def _check_settings(manifest: Any, value: Any) -> None:
    """``params.settings`` is this add-on's own value, valid against the granted schema: a stale value (the schema
    changed with a new package) or a value for an add-on without settings is refused, never sent."""
    if value is None:
        return
    declared = manifest.settings
    if declared is None:
        raise RunnerError("addon.bad_request", "the add-on declares no settings")
    try:
        Draft202012Validator(declared["schema"]).validate(value)
    except jsonschema_exceptions.ValidationError:
        raise RunnerError("addon.bad_request", "the settings do not fit the granted schema") from None


def _forms(secret: str | None) -> tuple[str, ...]:
    return () if not secret else (secret, json.dumps(secret)[1:-1])


def _strip_partial(raw: bytes, forms: Sequence[bytes]) -> bytes:
    """Drop a trailing run of bytes that is a prefix of any form (a copy cut short, whatever cut it)."""
    longest = max((k for f in forms for k in range(len(f) - 1, 0, -1) if raw.endswith(f[:k])), default=0)
    return raw[: len(raw) - longest]


def _cover(raw: bytes, forms: Sequence[bytes]) -> bytes:
    """Every byte inside ANY occurrence of ANY form (overlapping, at every start) is covered; each covered run becomes
    one ``[redacted]``. Sequential ``replace`` per form leaves bytes when one form sits inside another (a key holding
    a backslash or a quote) or copies overlap."""
    hit = bytearray(len(raw))
    for f in forms:
        i = raw.find(f)
        while i != -1:
            hit[i : i + len(f)] = b"\x01" * len(f)
            i = raw.find(f, i + 1)
        for k in range(len(f) - 1, 0, -1):  # the trailing partial copy is covered too, before the runs collapse
            if raw.endswith(f[:k]):
                hit[len(raw) - k :] = b"\x01" * k
                break
    out, i = bytearray(), 0
    while i < len(raw):
        if hit[i]:
            while i < len(raw) and hit[i]:
                i += 1
            out += b"[redacted]"
        else:
            out.append(raw[i])
            i += 1
    return bytes(out)


def _safe_text(raw: bytes, secret: str | None, cap: int) -> str:
    """The ONE way child stderr becomes text: from raw bytes, with the secret's forms (raw and JSON-escaped) and nothing
    else. Whole copies become ``[redacted]``; a trailing partial copy is always dropped (the pipe may have ended early,
    been cut, killed or timed out); then the text is cut to ``cap`` bytes and a partial copy dropped again; decoding
    last, so a split UTF-8 character can only become U+FFFD."""
    forms = [f.encode() for f in _forms(secret)]
    raw = _cover(raw, forms)
    raw = _strip_partial(raw, forms)
    raw = _strip_partial(raw[:cap], forms)
    return raw.decode("utf-8", errors="replace")


def _holds(doc: Any, secret: str) -> bool:
    """Whether ``secret`` is in any string (key or value) of a parsed answer."""
    if isinstance(doc, str):
        return secret in doc
    if isinstance(doc, Mapping):
        return any(_holds(k, secret) or _holds(v, secret) for k, v in doc.items())
    if isinstance(doc, list | tuple):
        return any(_holds(x, secret) for x in doc)
    return False


def call(
    package: Package,
    granted: Addon,
    method: str,
    params: Mapping[str, Any],
    limits: Limits | None = None,
    *,
    credential: Callable[[str], str | None] | None = None,
    env: envtree.EnvLocation | None = None,
) -> Any:
    """Run the addon once with ``method`` and ``params`` (built by core) and return the ``result`` of its response; the
    caller checks it per method. ``granted`` is the replayed grant (the ACTIVE one: disabled or purged is refused) and
    must hold the capability ``METHODS[method]`` names. ``credential`` is a provider, called as
    ``credential(granted.name)`` just before the call and only when the grant holds ``credential`` and ``network``:
    the runner names the add-on, so a provider built for another add-on must refuse the name (it raises; the call
    fails and nothing is sent). Its string goes in ``params.credential`` and nowhere else. ``params`` must not itself
    contain ``credential``. ``env`` (V3 G4, C1) says where the host keeps dependency environments: an add-on whose
    manifest declares ``runtime`` runs only if :func:`orch.addons.envtree.verify` passes HERE, inside this call
    (``market.env_changed`` otherwise, never a rebuild); one without ``runtime`` is refused an ``env``
    (``addon.bad_request``). Raises :class:`RunnerError`."""
    if method not in METHODS:
        raise RunnerError("addon.bad_request", "unknown method")
    if "credential" in params:
        raise RunnerError("addon.bad_request", "the caller does not pass a credential: the runner injects it")
    if not granted.enabled or granted.purged:
        raise RunnerError("addon.inactive", "the addon is disabled or purged")
    needed = METHODS[method]
    if needed is not None and needed not in granted.capabilities:
        raise RunnerError("addon.not_granted", f"the grant does not hold {needed}")
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
    why = incompatibility(manifest, CORE_VERSION)  # the registry state ``incompatible``: never run, never hidden
    if why is not None:
        raise RunnerError("addon.incompatible", why)
    granted_digest = granted.package_sha256
    rt = manifest.runtime
    env_path: str | None = None
    if rt is None:
        if env is not None:
            raise RunnerError("addon.bad_request", "an environment was given to an add-on without dependencies")
    else:
        if env is None or granted.runtime is None or {k: granted.runtime.get(k) for k in ("kind", "lock")} != rt:
            raise RunnerError("market.env_changed", "the granted dependencies are not the manifest's")
        try:  # before the call deadline: the hashing is not part of the add-on's time
            env_path = str(envtree.verify(env.state, env.ws, granted.name, granted_digest, rt["kind"]))
        except MarketError as e:
            raise RunnerError("market.env_changed", e.reason or "changed") from None
    if "settings" in params:
        _check_settings(manifest, params["settings"])
    full = dict(params)
    secret: str | None = None
    if "credential" in manifest.capabilities and "network" in manifest.capabilities and credential is not None:
        try:
            value = credential(granted.name)  # read just before the call, by the caller's single reader
        except Exception:
            raise RunnerError("addon.bad_request", "the credential is not available for this add-on") from None
        if value is not None:
            if type(value) is not str or not value:
                raise RunnerError("addon.bad_request", "the credential is not a non-empty string")
            secret = value
            full["credential"] = secret
    try:
        request = canon.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": full})
    except (ValueError, TypeError, RecursionError):
        raise RunnerError("addon.bad_request", "the request is not strict JSON") from None
    request += b"\n"
    if len(request) > MAX_REQUEST:
        raise RunnerError("addon.bad_request", f"the request is larger than {MAX_REQUEST} bytes")
    del full

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
        py_env = env_path if rt is not None and rt["kind"] == "python-requirements" else None
        child = child_env(granted.name, sorted(granted.capabilities), work, pkg, py_env)
        cmd = manifest.entry_cmd
        if any(PYTHON_TOKEN in a for a in cmd[1:]) or (PYTHON_TOKEN in cmd[0] and cmd[0] != PYTHON_TOKEN):
            raise RunnerError("addon.start_failed", "the python placeholder is only the whole of the command")
        if cmd[0] != PYTHON_TOKEN and _BARE_PYTHON.fullmatch(cmd[0]):
            raise RunnerError("addon.start_failed", "python is never taken from PATH: the command is {python}")
        if py_env is not None:
            # dependencies arrive through PYTHONPATH (from-nothing env), so ``-E`` is dropped; ``-B -s -S`` stay
            head = [a for a in PYTHON_ARGV if a != "-E"]
        else:
            head = list(PYTHON_ARGV) if cmd[0] == PYTHON_TOKEN else [cmd[0].replace("{pkg}", pkg)]
        argv = head + [a.replace("{pkg}", pkg) for a in cmd[1:]]  # {python} is never re-scanned: one pass, cmd[0] only
        if rt is not None and rt["kind"] == "node-package-lock":
            if any(p.split("/")[0] == "node_modules" for p in package.files):
                raise RunnerError("addon.start_failed", "a Node add-on ships its own node_modules")
            _link_node_modules(pkg, env_path or "")
            exe = shutil.which("node", path=NODE_DIRS)
        else:
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
                env=child,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, ValueError):
            raise RunnerError("addon.start_failed", "the process did not start") from None
        slack = max((len(f.encode()) for f in _forms(secret)), default=0)  # a cut must not leave a prefix of it
        out, err, overflow = _converse(proc, request, deadline, limits, slack)
        _kill(proc)  # the group is killed; a process that left it (setsid) is not tracked in P1
        errtext = _safe_text(err, secret, limits.max_stderr)
        if overflow == "timeout":
            raise RunnerError("addon.timeout", f"no complete answer within {limits.timeout:g} s", errtext)
        if overflow == "output":
            raise RunnerError("addon.output_too_large", f"more than {limits.max_response} bytes", errtext)
        code = proc.returncode
        if code != 0:
            raise RunnerError("addon.crashed", f"exit status {code}", errtext)
        if secret is not None and any(f.encode() in out for f in _forms(secret)):
            raise RunnerError("addon.bad_response", "the answer holds the credential", errtext)
        result = _parse(out, errtext)
        if secret is not None and _holds(result, secret):
            raise RunnerError("addon.bad_response", "the answer holds the credential", errtext)
        return result
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
    proc: subprocess.Popen[bytes], request: bytes, deadline: float, limits: Limits, stderr_slack: int = 0
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
                elif len(err) < limits.max_stderr + stderr_slack:
                    err += chunk[: limits.max_stderr + stderr_slack - len(err)]
        try:
            proc.wait(timeout=max(deadline - time.monotonic(), 0.001))
        except subprocess.TimeoutExpired:
            return bytes(out), bytes(err), "timeout"
        return bytes(out), bytes(err), ""
    finally:
        sel.close()


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
