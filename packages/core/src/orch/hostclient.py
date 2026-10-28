"""The thin CLI (H4b, §8): ``orch`` as a client of the host.

**Mode by registration.** :func:`locate` decides, for the workspace the CLI call found, whether the host serves it: the
workspace is in the person's own registry (``<state>/host/workspaces.json``) or in the root-owned pin of this agent UID.
Then the call is a request on the workspace socket and **never** a direct write; if the host is down the answer is
``host.unavailable`` (exit 9, with the fix), not a silent fallback. A workspace the host does not serve runs in direct
mode as before. ``help``/``describe`` are answered locally in both modes, and so is ``import v1`` (the
host does not serve it; the direct-mode guard still answers ``host.required`` for a registered workspace).

**Rendering.** A registry op is sent with its parsed arguments and the answer's ``result`` is rendered by the same
renderer as direct mode (:func:`result_from` rebuilds ``orch.ops.Result``), so text and ``--json`` are byte-identical.
A refusal becomes an :class:`~orch.ops.errors.OrchError` carrying the exit the socket sent (§0.6: the CLI keeps the
catalog's exit codes; ``lock.busy`` is 9 on the socket).

**Writes** carry ``opts.idem`` (a fresh ``a_<ULID>`` per call, §0.7). A write whose answer is lost (timeout, dropped
connection, ``outcome.unknown``) is sent again with the same key on a new connection: the host replays the stored
result or waits for the first call, so it is applied once. The grant and the session go in ``auth``, never in
``args``; ``--dry-run`` is ``opts.dry_run``. Files (``x-file`` arguments) are read here and go inline (at most
512 KiB) or as a blob; the argument keeps the name the user typed as the logical name (§0.3).

**Person commands** (``approve``, ``verdict``, ``answer``, ``grant``, ...) run the H5a composite
(:func:`orch.host.signer.run`) with the terminal signer: ``human.prepare``, the review on ``/dev/tty``, the device key,
``human.submit``. Only the person's own registry entry and device key are used; an agent UID or an ``ORCH_GRANT``
never gets here (``human_only``).

**``wait``** is one request: the host parks it on its bus (H6a). Its socket timeout is the ``--timeout`` plus a margin.

The run directory is ``ORCH_HOST_RUN_DIR`` when set (tests, dev), else ``<state>/run`` (tier A, as ``orch host`` and
the CLI signer; H7). ``ORCH_HOST_RUN_DIR`` is the one environment name for it in core (``ORCH_RUN_DIR`` is never
read). It only says where to look: it is **not** a trust
input. Whom the socket may belong to comes from the registry or the root-owned pin, checked by the handshake.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orch.ops import Operation, Result
from orch.ops.errors import OrchError

__all__ = ["HostFailure", "Target", "dry_text", "execute", "locate", "result_from", "workspace_socket"]

#: registry ops the host does not serve (``dispatch.NOT_SERVED``): they stay local, the direct-mode guard decides
LOCAL_OPS = frozenset({"help", "describe", "import.v1"})
FilesFn = Callable[[Any, str], dict[str, Any] | None]  # (connection, "first" | "digest" | "fresh")
WAIT_MARGIN = 30.0
#: attempts of a write (the first call and two retries with the same idem)
RETRIES = 3
WS_ID = re.compile(r"[0-9a-f]{32}")


class HostFailure(OrchError):
    """A refusal from the host (or the client's ``host.unavailable``): ``wire_exit`` is the exit the socket sent."""

    def __init__(self, error: Any, exit_: int | None = None) -> None:
        super().__init__(error.code, error.message, hint=error.hint, fix=error.fix, retryable=error.retryable)
        self.wire_exit = exit_ if exit_ is not None else error.exit


@dataclass(frozen=True)
class Target:
    """Where the workspace is served and who the CLI is there."""

    workspace_id: str
    host_uid: int
    socket: Path
    role: str  # "person" | "agent"
    state: Path
    entry: Mapping[str, Any] | None = None  # the person's registry entry (None for an agent pin)


def workspace_socket(env: Mapping[str, str], state: Path, host_uid: int, workspace_id: str) -> Path:
    """The workspace socket in the tier-A run directory (``ORCH_HOST_RUN_DIR``, else ``<state>/run``; H7). A path that
    merely exists under the tier-B ``/Users/Shared/orch/run`` never wins: any local account can create it."""
    from orch.host.enable import run_dir_for

    return run_dir_for(Path(state), None, env) / workspace_id / "host.sock"


def _unavailable(message: str) -> HostFailure:
    from orch.host.errors import HostError

    return HostFailure(HostError("host.unavailable", message[:400]), 9)


def locate(
    env: Mapping[str, str], workspace: Any, *, uid: int | None = None, pins: Any = None, state: Path | None = None
) -> Target | None:
    """The :class:`Target` when the host serves the workspace ``workspace`` (an ``orch.ops.runtime.Workspace``) found,
    else ``None`` (direct mode). An unreadable registry or pin fails closed: the workspace counts as registered and the
    call is ``host.unavailable``, as the direct-mode guard would refuse it."""
    from orch.host.custody import read_json
    from orch.host.errors import HostError
    from orch.host.pins import AgentPins
    from orch.host.registry import registry_path
    from orch.ops.runtime import state_dir

    root = workspace.root
    if root is None:
        return None
    cfg = workspace.config  # as discovery read it, never again by path
    wid = ((cfg or {}).get("workspace") or {}).get("id")
    if not isinstance(wid, str) or not WS_ID.fullmatch(wid):
        return None
    uid = os.getuid() if uid is None else uid
    sd = Path(state) if state is not None else state_dir(env, workspace.files)
    pins = pins or AgentPins()
    pin = pins.get(uid)
    if pin is not None and pins.names_workspace(uid, wid):
        host_uid = pin.get("host_uid")
        if type(host_uid) is not int:
            raise _unavailable("the agent pin for this workspace is unreadable; refusing direct mode")
        return Target(wid, host_uid, workspace_socket(env, sd, host_uid, wid), "agent", sd)
    try:
        doc = read_json(registry_path(sd), uid)
    except HostError as exc:
        raise _unavailable(f"the host registry is unreadable ({exc.message}); refusing direct mode") from None
    if doc is None:
        return None
    try:
        entry = next((i for i in doc["workspaces"] if i.get("workspace_id") == wid), None)
    except (KeyError, TypeError, AttributeError):
        raise _unavailable("the host registry has an unexpected shape; refusing direct mode") from None
    if entry is None:
        return None
    if not _same_root(str(entry.get("root")), root):
        raise _unavailable(
            "this directory's config.json names a registered workspace that lives elsewhere "
            f"({entry.get('root')}); refusing to send this call to it. Run orch from that workspace, or fix config.json"
        )
    host_uid = entry.get("host_uid")
    if type(host_uid) is not int:
        raise _unavailable("the host registry entry has no host UID")
    return Target(wid, host_uid, workspace_socket(env, sd, host_uid, wid), "person", sd, entry)


def _same_root(registered: str, found: Path) -> bool:
    """The registry's root (the project) and the project directory the CLI found are one directory."""
    try:
        want = Path(registered).resolve()
        have = Path(found).resolve()
    except (OSError, RuntimeError):
        return False
    return have == want


# --- connection ----------------------------------------------------------------------------------------------------


def _connect(target: Target) -> Any:
    from orch.host import frames
    from orch.host._unix import UnsafePath
    from orch.host.client import Connection, Pin, PinMismatch
    from orch.host.errors import HostError

    try:
        conn = Connection(Pin(target.host_uid, target.workspace_id, target.socket), client="orch cli")
        _SEEN["release"] = (conn.welcome or {}).get("release") if isinstance(conn.welcome, dict) else None
        _SEEN["connected"] = True
        return conn
    except PinMismatch as exc:  # the process behind the socket is not the registry's host: a hard refusal
        raise HostFailure(HostError("host.untrusted", f"{target.socket}: {exc}; refusing to talk to it")) from None
    except UnsafePath as exc:
        raise _unavailable(f"the host socket {target.socket} is not usable: {exc}") from None
    except (FileNotFoundError, ConnectionRefusedError):
        raise _unavailable(f"the orch host is not serving this workspace (no one listens on {target.socket})") from None
    except (OSError, ConnectionError, TimeoutError, frames.FrameError) as exc:
        raise _unavailable(f"cannot talk to the orch host: {exc}") from None


_SEEN: dict[str, Any] = {"connected": False, "release": None}


def forget_host() -> None:
    _SEEN.update(connected=False, release=None)


def stale_host_warning() -> str | None:
    """B5b: a one-line warning when the host that answered runs another release than this CLI. A host that predates
    the ``release`` field in its welcome is older by definition. ``None`` when no host answered or they match."""
    if not _SEEN["connected"]:
        return None
    from orch.host.release import code_digest

    try:
        mine = code_digest()
    except OSError:
        return None
    theirs = _SEEN["release"]
    if theirs == mine:
        return None
    return (
        f"the orch host runs an older release ({theirs or 'unknown'}) than this CLI ({mine}): "
        "run `orch host enable` (it restarts the host on the new release)"
    )


def _sleep(i: int) -> None:
    time.sleep(0.2 * (i + 1))


def _send_once(target: Target, op: str, args: dict[str, Any], mode: str, **kw: Any) -> dict[str, Any]:
    """One attempt on a new pinned connection. ``mode``: how files go (``first`` upload, ``digest`` for a retry,
    ``fresh`` upload again after the host had no record of the first attempt)."""
    from orch.host.errors import HostError

    files = kw.pop("files", None)
    with _connect(target) as c:
        try:
            return c.call(op, args, files=files(c, mode) if files else None, **kw)
        except HostError as exc:
            if files and mode == "digest" and exc.code == "blob.invalid":  # no record: the blob may be consumed or gone
                return c.call(op, args, files=files(c, "fresh"), **kw)
            raise


def _request(
    target: Target,
    op: str,
    args: dict[str, Any],
    *,
    auth: dict[str, str] | None,
    idem: str | None = None,
    files: FilesFn | None = None,
    dry_run: bool = False,
    timeout: float | None = None,
) -> dict[str, Any]:
    """One request, its ``result``. With an ``idem`` a lost answer (or ``outcome.unknown``) is sent again with the same
    key on a new connection, at most :data:`RETRIES` attempts in all."""
    from orch.host import frames
    from orch.host.errors import HostError

    attempts = RETRIES if idem else 1
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return _send_once(
                target, op, args, "first" if i == 0 else "digest",
                files=files, idem=idem, auth=auth, dry_run=dry_run, timeout=timeout,
            )  # fmt: skip
        except HostFailure:
            if i == 0:
                raise
            last = None  # the host is not reachable now: say so below
        except HostError as exc:
            if not (idem and exc.code == "outcome.unknown"):
                raise HostFailure(exc) from None
            last = exc
        except (OSError, ConnectionError, TimeoutError, frames.FrameError) as exc:
            last = exc
            if not idem:
                break
        if i + 1 < attempts:
            _sleep(i)
    if isinstance(last, HostError):  # outcome.unknown after every attempt
        raise HostFailure(last) from None
    extra = f" (idem {idem}: ask `outcome.get` before repeating a write)" if idem else ""
    raise _unavailable(f"lost the connection to the orch host: {last}{extra}")


# --- files ---------------------------------------------------------------------------------------------------------


def file_args(op: Operation) -> list[str]:
    props = op.input.get("properties") or {}
    return [n for n, spec in props.items() if isinstance(spec, Mapping) and spec.get("x-file") is True]


def read_files(op: Operation, args: Mapping[str, Any], ctx: Any) -> dict[str, bytes]:
    """The bytes of every ``x-file`` argument given: a path (read here, a regular file) or ``-`` (standard input)."""
    from orch.ops.caps import CapabilityDenied, NotARegularFile
    from orch.ops.runtime import ARTIFACT_LIMIT, FILE_LIMIT, read_stdin

    out: dict[str, bytes] = {}
    for name in file_args(op):
        source = args.get(name)
        if not isinstance(source, str):
            continue
        try:
            if source == "-":
                data = read_stdin(ctx)
                if len(data) > FILE_LIMIT:
                    raise OrchError("invalid.input", f"the text is longer than {FILE_LIMIT} bytes")
            else:
                data, size = ctx.caps.files.read_client(source, ARTIFACT_LIMIT)
                if size > ARTIFACT_LIMIT:
                    raise OrchError("invalid.input", f"{source[:80]} is larger than {ARTIFACT_LIMIT} bytes")
        except NotARegularFile:
            raise OrchError("invalid.input", f"{source[:80]} is not a regular file") from None
        except CapabilityDenied as exc:
            raise OrchError("invalid.input", f"cannot read {source[:80]}: {exc}") from None
        except OSError as exc:
            raise OrchError("invalid.input", f"cannot read {source[:80]}: {exc.strerror}") from None
        out[name] = data
    return out


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def upload(conn: Any, data: bytes, auth: dict[str, str] | None) -> str:
    """``data`` as one blob in chunks (§0.10); returns its id."""
    from orch.host.blobs import CHUNK_MAX
    from orch.host.control_client import new_idem

    def b64(b: bytes) -> str:
        return base64.b64encode(b).decode()

    digest = _sha(data)
    first = data[:CHUNK_MAX]
    done = len(first) == len(data)
    res = conn.call(
        "blob.put",
        {
            "offset": 0,
            "b64": b64(first),
            "size": len(data),
            "upload_key": new_idem(),
            "last": done,
            **({"sha256": digest} if done else {}),
        },
        auth=auth,
    )
    blob, pos = res["data"]["blob"], len(first)
    while pos < len(data):
        part = data[pos : pos + CHUNK_MAX]
        last = pos + len(part) == len(data)
        conn.call(
            "blob.put",
            {"blob": blob, "offset": pos, "b64": b64(part), "last": last, **({"sha256": digest} if last else {})},
            auth=auth,
        )
        pos += len(part)
    return blob


def files_builder(data: Mapping[str, bytes], auth: dict[str, str] | None) -> FilesFn:
    """``files`` member of a request: inline up to 512 KiB in all, else a blob per file. A blob is uploaded once; a
    retry names it by its digest (the first call may have consumed it); ``fresh`` drops the unused first blob and
    uploads again (the host had no record of the first call)."""
    from orch.host.blobs import INLINE_MAX

    blobs: dict[str, str] = {}
    inline = sum(len(b) for b in data.values()) <= INLINE_MAX

    def build(conn: Any, mode: str) -> dict[str, Any] | None:
        if not data:
            return None
        if inline:
            return {n: {"b64": base64.b64encode(b).decode()} for n, b in data.items()}
        out: dict[str, Any] = {}
        for name, b in data.items():
            if mode == "digest" and name in blobs:
                out[name] = {"digest": _sha(b)}
                continue
            if mode == "fresh" and name in blobs:
                try:
                    conn.call("blob.drop", {"blob": blobs[name]}, auth=auth)
                except Exception:  # noqa: BLE001 - best effort: an unused blob expires in 10 minutes anyway
                    pass
            blobs[name] = upload(conn, b, auth)
            out[name] = {"blob": blobs[name]}
        return out

    return build


# --- results -------------------------------------------------------------------------------------------------------


def result_from(doc: Mapping[str, Any]) -> Result:
    """``orch.ops.Result`` from the wire ``Result`` (§0.3)."""
    return Result(
        data=doc.get("data"),
        key=doc.get("key"),
        seq=int(doc.get("seq") or 0),
        cursor=int(doc.get("cursor") or 0),
        hints=list(doc.get("hints") or []),
        lines=list(doc.get("lines") or []),
        duplicate=bool(doc.get("duplicate")),
        exit=int(doc.get("exit") or 0),
        head=doc.get("head"),
    )


def dry_text(op: Operation, prepared: Mapping[str, Any]) -> Result:
    """A person op under ``--dry-run`` in host mode: the host judged it (``human.prepare`` with ``dry_run``) and signs
    nothing. A host since H7 sends the inner op's would-be ``result``, rendered as direct mode renders its dry run; an
    older host sends only the prepared request, so this names what would be signed."""
    would = prepared.get("result")
    if isinstance(would, Mapping):
        return in_schema_order(op, result_from(would))
    types = [str(e.get("type")) for e in prepared.get("events") or []]
    return Result(
        data={"dry_run": True, "op": op.name, "events": types, "fingerprint": prepared.get("fingerprint")},
        lines=["dry-run: nothing was written", "would sign: " + ", ".join(types)],
        head=f"ok {op.name} dry-run",
    )


def in_schema_order(op: Operation, res: Result) -> Result:
    """``data``'s keys in the order of the op's output schema, as the direct-mode handler builds them. A person op's
    result comes back from the request record the host stored as canonical JSON (sorted keys); the text and JSON
    renderers keep key order, so this is what makes the two modes byte-identical (H7)."""
    props = ((op.declaration.get("output") or {}).get("json") or {}).get("properties")
    if isinstance(res.data, dict) and isinstance(props, Mapping):
        first = {k: res.data[k] for k in props if k in res.data}
        res.data = {**first, **{k: v for k, v in res.data.items() if k not in first}}
    return res


# --- the call ------------------------------------------------------------------------------------------------------


def _auth(ctx: Any) -> dict[str, str] | None:
    auth = {k: v for k, v in (("grant", ctx.grant), ("session", ctx.session)) if v}
    return auth or None


def _task_ref(task: str) -> tuple[str | None, str]:
    ref, _, tid = task.rpartition("/")
    return (ref or None), tid


_KEY = re.compile(r"[A-Z][A-Z0-9]{0,15}-[0-9]{1,9}")
_TASK = re.compile(r"T[1-9][0-9]*")
VERIFY_MAX = 4096  # F1: verify.cmd is a plain string; the receipt's cmd is at most this long


def _bound_key(key: Any, ref: str | None) -> str:
    """The ticket key the host answered with, which must be the ticket asked for (a key or its number)."""
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise OrchError("invalid.input", "the host's answer names no ticket: nothing was run")
    if ref is not None:
        prefix, _, number = key.rpartition("-")
        asked_prefix, dash, asked_number = ref.rpartition("-")
        same = (
            dash == "-"
            and asked_prefix.upper() == prefix
            and asked_number.isdigit()
            and int(asked_number) == int(number)
        )
        if not (same or (ref.isdigit() and int(ref) == int(number))):
            raise OrchError("invalid.input", "the host answered for another ticket than the one asked: nothing was run")
    return key


def _checked_cmd(cmd: Any, tid: str) -> str:
    """``verify.cmd`` as the host's view of the signed ticket gives it, in the shape F1 allows (a non-empty string of
    at most 4096 UTF-8 bytes) and without a NUL, which no argv holds. Anything else: nothing is run."""
    if cmd is None:
        raise OrchError(
            "invalid.input",
            f"{tid} has no verify command, so --run has nothing to run; close it without --run",
            hint=f"orch task done {tid}",
            retryable=True,
        )
    if not isinstance(cmd, str) or not cmd.strip() or "\x00" in cmd or len(cmd.encode("utf-8", "replace")) > VERIFY_MAX:
        raise OrchError("invalid.input", f"the host sent a verify command for {tid} that is not valid: nothing was run")
    return cmd


def _local_repo(ctx: Any, linked: list[str]) -> tuple[str | None, Path | None]:
    """The linked repository a verify command runs in (F1 section 6): the only linked one; with several, the one whose
    working copy the shell is in, found by its directory name. ``(None, None)`` when the ticket links none."""
    from orch.store import observe

    if not linked:
        return None, None
    here = Path(ctx.caps.files.getcwd())
    top = observe.toplevel(here)
    if top is None:
        why = observe.refusal(here)
        if why is not None:  # a working copy orch's reader refuses: say why, not "not a linked repo"
            raise OrchError("verify.failed", f"orch cannot read the repository here: {why}"[:400])
        raise OrchError(
            "invalid.input",
            f"run this in the working copy of a linked repo ({', '.join(linked)}): the host takes a receipt for a "
            "linked repo only with its commit",
        )
    path = Path(top).resolve()
    if len(linked) == 1:
        return linked[0], path
    named = [n for n in linked if n == path.name]
    if len(named) != 1:
        raise OrchError(
            "invalid.input",
            f"this ticket links several repos ({', '.join(linked)}): run it in a working copy whose directory is named "
            "after one of them",
        )
    return named[0], path


def _workdir_or_here(ctx: Any, key: str | None = None) -> Path:
    """No codebase: the check runs in the ticket's work folder (``ORCH_WORKDIR``, set by the host at launch) when it is
    a real directory, else where the shell is."""
    import os

    here = Path(ctx.caps.files.getcwd())
    named = (ctx.env or {}).get("ORCH_WORKDIR")
    expect = None
    root = (ctx.env or {}).get("ORCH_WORKSPACE")
    if key is not None and root and os.path.isabs(root):
        from orch.layout import Layout, LayoutError, workdir_stat

        try:  # the only place a work folder can be: <project>/.orch-work/<KEY>; the variable is a hint, not a path
            lay = Layout.new_at(root)
            expect = os.fspath(lay.workdir(key)) if workdir_stat(lay, key) is not None else None
        except (LayoutError, OSError):
            expect = None
    if named and expect is not None and named == expect and os.path.isdir(named) and not os.path.islink(named):
        return Path(named)
    return here


def _preflight(
    task: dict[str, Any], args: dict[str, Any], key: str, tid: str, target: Target, auth: dict[str, str] | None
) -> None:
    """Nothing runs before the host said it would take the receipt (F1 "Receipts", §0.8 "Trust of the command"):
    ``verify_runnable`` of the ``task.list`` answer (an approved plan's command, or one the caller's own person wrote;
    absent from an older host: refused), then a dry-run ``task.done`` for the same task, which checks the claim, the
    lease and the task's state exactly as the real one will. The host checks the trust again when the receipt comes."""
    if task.get("verify_runnable") is not True:
        who = task.get("verify_author")
        who = who if isinstance(who, str) and who else "someone else"
        from orch.canon.text import clean

        raise OrchError(
            "verify.unapproved",
            f"{tid}'s verify command was written by {clean(who)} and is not approved in the current plan, or the plan "
            f"gate does not apply: nothing was run. Approve the plan (a person: orch approve {key} plan) or add your "
            "own verify command (orch task add TEXT --verify CMD)",
        )
    dry = {"task": f"{key}/{tid}", **{k: args[k] for k in ("message",) if args.get(k)}}
    _request(target, "task.done", dry, auth=auth, dry_run=True)


def _client_receipt(
    op: Operation, args: dict[str, Any], ctx: Any, target: Target, auth: dict[str, str] | None
) -> tuple[dict[str, Any], bytes, list[str]]:
    """``task done --run`` against a host (H3b2, contract 2): the host never runs ``verify.cmd``. Read the task's exact
    command and the linked repos, run the command here (the same runner as direct mode: allow-listed environment,
    timeout, output capped at 1 MiB), and return the ``receipt``, its output and the notes. Nothing is sent when the
    command fails."""
    from orch.ops import runner

    ref, tid = _task_ref(str(args["task"]))
    doc = _request(target, "task.list", {"ref": ref} if ref else {}, auth=auth)
    key = _bound_key(doc.get("key"), ref)
    data = doc.get("data") or {}
    # only the entry of the task asked for, by its exact id: nothing else the host sent is read as a command
    task = next((t for t in data.get("tasks") or [] if isinstance(t, dict) and t.get("id") == tid), None)
    if task is None:
        raise OrchError("not_found", f"no task {tid} on this ticket")
    cmd = _checked_cmd(task.get("verify"), tid)
    _preflight(task, args, key, tid, target, auth)
    if ctx.progress is not None:  # the host's answer decides what runs on this machine: show it first, escaped
        from orch.canon.text import clean

        ctx.progress(f"running the verify command the host gave for {key}/{tid}: {clean(cmd)}")
    repo, path = _local_repo(ctx, list(data.get("repos") or []))
    if path is not None:
        from orch.store import observe

        state = observe.dirty(path)
        if state is None:  # unknown is never clean: refuse before the verify command runs (Opus delta on #561)
            why = observe.dirty_refusal(path)
            raise OrchError(
                "verify.failed",
                f"orch cannot tell whether the working tree of {repo} is clean"
                + (f": orch cannot read the repository there: {why}" if why else ": it cannot be read"),
            )
        if state:
            raise OrchError(
                "verify.failed",
                f"the working tree of {repo} is not clean: commit first, the receipt must name the commit it ran at",
            )
    env = ctx.caps.exec.child_env(ctx.env)
    secret = (ctx.grant or "").partition(".")[2]
    ran = runner.run_verify(
        cmd,
        repo=repo,
        path=path,
        cwd=path if path is not None else _workdir_or_here(ctx, key),
        env=env,
        caps=ctx.caps,
        grant_secret=secret,
    )
    r = ran.receipt
    if repo is not None and r["commit"] is None:
        raise OrchError("verify.failed", f"{repo} has no clean commit to name: commit first, then run it again")
    args["task"] = f"{key}/{tid}"  # the task.done goes to the ticket the command was read from, not to a later claim
    receipt = {"cmd": cmd, "exit": r["exit"], "duration_ms": r["ms"], "commit": r["commit"], "repo": repo}
    notes = [*ran.notes, "note: attested by you (the agent): this ran on your machine, the host does not verify it"]
    return receipt, ran.output[: runner.OUTPUT_LIMIT], notes


def execute(parsed: Any, ctx: Any, target: Target) -> Result:
    """Send ``parsed`` to the host and return its result. Raises :class:`~orch.ops.errors.OrchError` on a refusal."""
    from orch.host.dispatch import NOT_SERVED

    op: Operation = parsed.op
    args = dict(parsed.args)
    if op.name in NOT_SERVED:
        raise OrchError(
            "not_implemented",
            f"{op.cli}: not served by the host yet ({NOT_SERVED[op.name]}); "
            "direct mode is off for a registered workspace",
        )
    if op.who == "human":
        return _human(op, args, ctx, target, parsed.dry_run)
    auth = _auth(ctx)
    notes: list[str] = []
    receipt_output: bytes | None = None
    if op.name == "task.done" and args.get("run"):
        args.pop("run")  # never forwarded: the host refuses it
        if parsed.dry_run:
            notes.append("dry-run: would run the task's verify command here and send its receipt")
        else:
            args["receipt"], receipt_output, notes = _client_receipt(op, args, ctx, target, auth)
            args["receipt_output"] = "-"
    files = (
        read_files(op, args, ctx) if receipt_output is None else read_files(op, {**args, "receipt_output": None}, ctx)
    )
    if receipt_output is not None:
        files["receipt_output"] = receipt_output
    idem = None
    if op.is_write and not parsed.dry_run:
        from orch.host.control_client import new_idem

        idem = new_idem()
    timeout = None
    if op.name == "wait":
        t = args.get("timeout", 540)
        timeout = (t if isinstance(t, int) and not isinstance(t, bool) else 540) + WAIT_MARGIN
    doc = _request(
        target,
        op.name,
        args,
        auth=auth,
        idem=idem,
        files=files_builder(files, auth) if files else None,
        dry_run=parsed.dry_run,
        timeout=timeout,
    )
    res = result_from(doc)
    if notes:
        res.lines[:0] = notes
    return res


def _durable(target: Target, link: list[Any], idem: str) -> Callable[..., Any]:
    """``call`` for the signer composite. Every call goes on the current connection; ``human.submit`` (the one write)
    that loses its answer is sent again with the same idem and the same signed events on a new pinned connection, so it
    is applied once. If it stays uncertain the person is told the idem: a new request is never made silently."""
    from orch.host import frames
    from orch.host.errors import HostError

    def call(op: str, args: dict[str, Any], **kw: Any) -> Any:
        if op != "human.submit":
            return link[0].call(op, args, **kw)
        last: BaseException | None = None
        for i in range(RETRIES):
            try:
                if i:
                    link[0].close()
                    link[0] = _connect(target)
                return link[0].call(op, args, **kw)
            except HostError as exc:
                if exc.code != "outcome.unknown":
                    raise
                last = exc
            except (OSError, ConnectionError, TimeoutError, frames.FrameError, HostFailure) as exc:
                last = exc
            if i + 1 < RETRIES:
                _sleep(i)
        raise _unavailable(
            f"lost the answer to human.submit ({last}); the signed request may have been applied (idem {idem}). "
            "Check with `orch show` or human.pending before signing again; a grant's secret cannot be shown again "
            "(revoke the grant and issue a new one)"
        ) from None

    return call


def _human(op: Operation, args: dict[str, Any], ctx: Any, target: Target, dry_run: bool) -> Result:
    from orch.custody import CustodyError
    from orch.host import signer
    from orch.host.control_client import new_idem
    from orch.host.errors import HostError
    from orch.identity import device_ref
    from orch.ops import human

    if ctx.grant is not None or target.role != "person" or target.entry is None:
        raise OrchError("human_only", f"{op.cli}: human only")
    entry = target.entry
    uid = os.getuid()
    person = ((entry.get("peers") or {}).get(str(uid)) or {}).get("person")
    if not person:
        raise OrchError("human_only", "this UID is not mapped to a person in this workspace")
    files = read_files(op, args, ctx)
    from orch.host.blobs import INLINE_MAX

    if sum(len(b) for b in files.values()) > INLINE_MAX:
        raise OrchError("invalid.input", f"a file you sign is at most {INLINE_MAX} bytes")
    wire = {n: {"b64": base64.b64encode(b).decode()} for n, b in files.items()} or None
    wid = target.workspace_id
    backend = human.device_backend(target.state, wid, convenience=True)  # the one key lookup; before connecting
    name = Path(str(entry.get("root") or wid)).name
    conn = _connect(target)
    idem = new_idem()
    link = [conn]
    try:
        device = device_ref(backend.public_key(human.KEY_ID))
        if dry_run:
            doc = conn.call(
                "human.prepare",
                {"op": op.name, "args": args, "signer": {"device": device, "auth": backend.auth}},
                dry_run=True,
                files=wire,
            )
            return dry_text(op, doc["data"])
        on_secret = None
        if op.name == "grant" and args.get("runner"):  # H11c: the secret goes to the orch runner, never on screen
            from orch.identity import new_ulid

            deposit_id = new_ulid()

            def on_secret(secret: str) -> list[str]:
                out = signer.hand_over(lambda o, a, **kw: link[0].call(o, a, **kw), secret, deposit_id)
                return signer.deposit_lines(out)

        res = signer.run(
            _durable(target, link, idem),
            op.name,
            args,
            backend=backend,
            key_id=human.KEY_ID,
            workspace_id=wid,
            person=person,
            device=device,
            workspace=lambda w: (name, str(entry.get("prefix"))) if w == wid else None,
            confirm=human.review_prompt,
            show_secret=human.show_secret,
            idem=idem,
            files=wire,
            on_secret=on_secret,
        )
        return in_schema_order(op, result_from(res))
    except OrchError:
        raise
    except signer.SignerRefused as exc:
        text = "not confirmed: nothing was signed" if str(exc) == "not confirmed" else f"{exc}; nothing was signed"
        raise OrchError("invalid.input", text[:400]) from None
    except HostError as exc:
        raise HostFailure(exc) from None
    except CustodyError as exc:
        declared = {e["code"] for e in op.errors}
        if exc.code in ("custody.no_prompt", "custody.no_key", "custody.wrong_passphrase") and exc.code in declared:
            raise OrchError(exc.code, str(exc)) from None
        raise OrchError("internal", f"the key custody: {exc}") from None
    except (OSError, ConnectionError, TimeoutError) as exc:
        raise _unavailable(f"lost the connection to the orch host: {exc} (idem {idem})") from None
    finally:
        link[0].close()
