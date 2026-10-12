"""The thin CLI (H4b, §8): ``orch`` as a client of the host.

**Mode by registration.** :func:`locate` decides, for the workspace the CLI call found, whether the host serves it: the
workspace is in the person's own registry (``<state>/host/workspaces.json``) or in the root-owned pin of this agent UID.
Then the call is a request on the workspace socket and **never** a direct write; if the host is down the answer is
``host.unavailable`` (exit 9, with the fix), not a silent fallback. A workspace the host does not serve runs in direct
mode as before. ``help``/``describe`` are answered locally in both modes, and so are ``init`` and ``import v1`` (the
host does not serve them; the direct-mode guard still answers ``host.required`` for a registered workspace).

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

The run directory is ``ORCH_RUN_DIR`` when set (tests, dev), else the service-managed directory when a host serves
there, else ``<state>/run`` (as ``orch host`` and ``orch sign``).
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orch.ops import Operation, Result
from orch.ops.errors import OrchError

__all__ = ["HostFailure", "Target", "dry_text", "execute", "locate", "result_from", "workspace_socket"]

#: registry ops the host does not serve (``dispatch.NOT_SERVED``): they stay local, the direct-mode guard decides
LOCAL_OPS = frozenset({"help", "describe", "init", "import.v1"})
FilesFn = Callable[[Any, bool], dict[str, Any] | None]
WAIT_MARGIN = 30.0
RETRIES = 3


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
    from orch.host.listener import default_run_directory

    if env.get("ORCH_RUN_DIR"):
        return Path(env["ORCH_RUN_DIR"]) / workspace_id / "host.sock"
    try:
        service = default_run_directory(host_uid) / workspace_id / "host.sock"
    except Exception:  # noqa: BLE001 - an unsupported platform has no service run directory
        service = None
    if service is not None and service.exists():
        return service
    return Path(state) / "run" / workspace_id / "host.sock"


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
    from orch.ops.runtime import _config, state_dir

    root = workspace.root
    if root is None:
        return None
    cfg = _config(root, workspace.files)
    wid = ((cfg or {}).get("workspace") or {}).get("id")
    if not isinstance(wid, str):
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
    host_uid = entry.get("host_uid")
    if type(host_uid) is not int:
        raise _unavailable("the host registry entry has no host UID")
    return Target(wid, host_uid, workspace_socket(env, sd, host_uid, wid), "person", sd, entry)


# --- connection ----------------------------------------------------------------------------------------------------


def _connect(target: Target) -> Any:
    from orch.host import frames
    from orch.host._unix import UnsafePath
    from orch.host.client import Connection, Pin
    from orch.host.errors import HostError

    try:
        return Connection(Pin(target.host_uid, target.workspace_id, target.socket), client="orch cli")
    except UnsafePath as exc:
        if str(exc).startswith("Server"):  # the process behind the socket is not the registry's host: a hard refusal
            raise HostFailure(
                HostError("host.untrusted", f"{target.socket}: {exc}; refusing to talk to it"), 3
            ) from None
        raise _unavailable(f"the host socket {target.socket} is not usable: {exc}") from None
    except (FileNotFoundError, ConnectionRefusedError):
        raise _unavailable(f"the orch host is not serving this workspace (no one listens on {target.socket})") from None
    except (OSError, ConnectionError, TimeoutError, frames.FrameError) as exc:
        raise _unavailable(f"cannot talk to the orch host: {exc}") from None


def _failure(exc: Any) -> HostFailure:
    return HostFailure(exc)


def _sleep(i: int) -> None:
    time.sleep(0.2 * (i + 1))


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
    conn: Any = None,
) -> dict[str, Any]:
    """One request, its ``result``. With an ``idem`` a lost answer is retried with the same key on a new connection.
    ``files(conn, retry)`` builds the ``files`` member on the live connection (``retry``: a blob that may be consumed
    is named by its digest)."""
    from orch.host import frames
    from orch.host.errors import HostError

    attempts = RETRIES if idem else 1
    last: BaseException | None = None
    for i in range(attempts):
        retry = i > 0
        own = conn is None
        try:
            c = conn if conn is not None else _connect(target)
        except HostFailure:
            if i == 0:
                raise
            _sleep(i)
            continue
        try:
            spec = files(c, retry) if files is not None else None
            try:
                return c.call(op, args, idem=idem, files=spec, auth=auth, dry_run=dry_run, timeout=timeout)
            except HostError as exc:
                if retry and exc.code == "blob.invalid" and files is not None:
                    return c.call(
                        op, args, idem=idem, files=files(c, False), auth=auth, dry_run=dry_run, timeout=timeout
                    )
                raise
        except HostError as exc:
            last = exc
            if idem and exc.code == "outcome.unknown" and i + 1 < attempts:
                _sleep(i)
                continue
            raise _failure(exc) from None
        except (OSError, ConnectionError, TimeoutError, frames.FrameError) as exc:
            last = exc
            if not idem or conn is not None or i + 1 >= attempts:
                break
            _sleep(i)
        finally:
            if own:
                c.close()
    if isinstance(last, HostError):  # outcome.unknown after every retry
        raise _failure(last) from None
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


def files_builder(
    data: Mapping[str, bytes], auth: dict[str, str] | None
) -> Callable[[Any, bool], dict[str, Any] | None]:
    """``files`` member of a request: inline up to 512 KiB in all, else a blob per file. A blob is uploaded once; on a
    retry it is named by its digest (the first call may have consumed it)."""
    from orch.host.blobs import INLINE_MAX

    blobs: dict[str, str] = {}
    inline = sum(len(b) for b in data.values()) <= INLINE_MAX

    def build(conn: Any, retry: bool) -> dict[str, Any] | None:
        if not data:
            return None
        if inline:
            return {n: {"b64": base64.b64encode(b).decode()} for n, b in data.items()}
        out: dict[str, Any] = {}
        for name, b in data.items():
            if retry and name in blobs:
                out[name] = {"digest": _sha(b)}
                continue
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
    nothing. The prepared request is not the op's result, so this names what would be signed."""
    types = [str(e.get("type")) for e in prepared.get("events") or []]
    return Result(
        data={"dry_run": True, "op": op.name, "events": types, "fingerprint": prepared.get("fingerprint")},
        lines=["dry-run: nothing was written", "would sign: " + ", ".join(types)],
        head=f"ok {op.name} dry-run",
    )


# --- the call ------------------------------------------------------------------------------------------------------


def _auth(ctx: Any) -> dict[str, str] | None:
    auth = {k: v for k, v in (("grant", ctx.grant), ("session", ctx.session)) if v}
    return auth or None


def execute(parsed: Any, ctx: Any, target: Target) -> tuple[Result, bool]:
    """Send ``parsed`` to the host and return ``(result, text_ok)``; ``text_ok`` is False for a host-mode dry run of a
    person op, which :func:`dry_text` shapes. Raises :class:`~orch.ops.errors.OrchError` for every refusal."""
    op: Operation = parsed.op
    args = dict(parsed.args)
    if op.who == "human":
        return _human(op, args, ctx, target, parsed.dry_run)
    files = read_files(op, args, ctx)
    auth = _auth(ctx)
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
    return result_from(doc), True


def _human(op: Operation, args: dict[str, Any], ctx: Any, target: Target, dry_run: bool) -> tuple[Result, bool]:
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
    directory = human.keys_dir(target.state, wid)
    if not (directory / human.KEY_FILE).is_file():
        raise OrchError("custody.no_key", "this machine holds no device key of yours for this workspace")
    name = Path(str(entry.get("root") or wid)).name
    conn = _connect(target)
    try:
        backend = human.open_backend(directory)
        device = device_ref(backend.public_key(human.KEY_ID))
        if dry_run:
            doc = conn.call(
                "human.prepare",
                {"op": op.name, "args": args, "signer": {"device": device, "auth": backend.auth}},
                dry_run=True,
                files=wire,
            )
            return dry_text(op, doc["data"]), False
        res = signer.run(
            conn.call,
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
            idem=new_idem(),
            files=wire,
        )
        return result_from(res), True
    except OrchError:
        raise
    except signer.SignerRefused as exc:
        text = "not confirmed: nothing was signed" if str(exc) == "not confirmed" else f"{exc}; nothing was signed"
        raise OrchError("invalid.input", text[:400]) from None
    except HostError as exc:
        raise _failure(exc) from None
    except CustodyError as exc:
        declared = {e["code"] for e in op.errors}
        if exc.code in ("custody.no_prompt", "custody.no_key", "custody.wrong_passphrase") and exc.code in declared:
            raise OrchError(exc.code, str(exc)) from None
        raise OrchError("internal", f"the key custody: {exc}") from None
    except (OSError, ConnectionError, TimeoutError) as exc:
        raise _unavailable(f"lost the connection to the orch host: {exc}") from None
    finally:
        conn.close()
