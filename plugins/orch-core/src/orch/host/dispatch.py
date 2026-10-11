"""The host's dispatcher (H4a1): the registry ops on a workspace socket, with §0.5 to §0.8 applied to each request.

**The closed op list** (§0.4, §0.5). The host serves every registry op of :mod:`orch.ops` under its name at contract
version 1, plus ``whoami``, ``host.status`` and ``outcome.get``, and nothing else (``unknown_command``). Left out:

* ``init`` and ``import.v1`` (D-P2-9: ``init`` is ``host.wsk_create`` + ``host.bootstrap``; ``import v1`` runs in
  direct mode before registration);
* ``task.done``, until H3b2 serves it at contract 2 (client receipts: the host never runs ``verify.cmd``);
* ``wait``, until H6a serves it on the bus (its P1 handler sleeps, which would hold the workspace's one worker thread).

Each op has a matrix category (:data:`orch.host.columns.OpKind`) from its registry ``who``: ``read`` (``doctor``,
``instructions.sync`` and ``instructions.hook`` are ``person_read``), ``agent``, ``unattended``, ``human``. The gate
(:class:`orch.host.workspace.WorkspaceGate`) applies the column with it (§0.6 steps 3 to 7); this module does steps 8
to 11.

**One request** runs on the workspace worker thread in one piece (:meth:`RegistryBackend.serve`), submitted before the
coroutine awaits anything, so one connection's writes run in the order they were received (§0.2):

8. ``args`` against the op's schema (``invalid.input``), ``files`` (``blob.invalid``: an argument the op does not
   have, a ``{blob}`` until H4a2, a ``{digest}`` without a record), ``dry_run`` support (``invalid.input``);
9. idempotency for a write (H2b's :class:`~orch.host.outcomes.OutcomeStore`): ``idem.required``, then a replay,
   ``idem.conflict`` or ``idem.expired``;
10. the workspace state: ``workspace.unavailable``, then ``workspace.degraded`` for a write;
11. the op: the P1 check-and-run core (:func:`orch.cli.main.run`: input, who-check, grant check, stop rule, session
    dedup, declared errors) with ``Capabilities.host`` (D-P2-9), the host's own store injected
    (:meth:`orch.ops.runtime.Workspace.hosted`: no root discovery, no ``config.json``, no direct-mode guard), and the
    viewer the gate derived.

**Writes and their events.** A registry handler plans its events itself, so the outcome record starts empty (a
deferred plan) and the handler announces each batch through ``ctx.plan`` before appending it
(:meth:`orch.ops.runtime.Projection.commit`, ``new``). ``Store.append_check`` refuses any append the record does not
list, so no event can land that reconciliation could not find (§0.7).

**Identity** (§0.5, F4). The principal is ``g:<grant id>@<uid>`` only for a grant verified on the worker thread, else
``uid:<n>``. The handler gets the grant only in column G: an unverified grant is no identity, so a read with one sees
``workspace`` tickets only (F1 §10.7). Session notes, dedup records and the stop rule are keyed by (principal,
session), outcome records by principal.

**Redaction** (D-P2-10). Every result is passed through the request's :class:`~orch.host.auth.Redactor` before it is
stored or sent, and so is every error.

**Visibility** (F6): :class:`Viewer` holds the hooks the later reads use (``ticket.query``, ``needs``,
``session.list``, ``diff``, ``artifact.get``, ``sub.open``: H6a, H6b, H3b1, H4a2). The registry reads already filter
through ``Call.sees`` with the viewer this module passes in.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import re
import time
import traceback
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from jsonschema import Draft202012Validator

from .auth import Redactor
from .columns import OpKind
from .errors import HostError
from .outcomes import BUILDERS, Planned, request_idem
from .workspace import ok_result

if TYPE_CHECKING:
    from orch.ops import Operation, Result
    from orch.ops.errors import OrchError

    from .workspace import Call, WorkspaceService

__all__ = [
    "HOST_OPS",
    "NOT_SERVED",
    "PERSON_READS",
    "RegistryBackend",
    "ScopedRecords",
    "Viewer",
    "op_kind",
    "result_dict",
    "served_ops",
]

Phase = Literal["read", "write", "wait"]

#: registry ops the host does not serve, and why
NOT_SERVED: dict[str, str] = {
    "init": "init is host.wsk_create and host.bootstrap (D-P2-9)",
    "import.v1": "import v1 runs in direct mode before registration (D-P2-9)",
    "task.done": "served at contract 2 from H3b2 (client receipts)",
    "wait": "served on the bus from H6a",
}
PERSON_READS = frozenset({"doctor", "instructions.sync", "instructions.hook"})
HOST_OPS: dict[str, int] = {"whoami": 1, "host.status": 1, "outcome.get": 1}
DEFAULT_TIMEOUT_MS = 30_000
LOG = logging.getLogger(__name__)

_IDEM = {"type": "string", "pattern": r"^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$"}
_HOST_SCHEMAS = {
    "whoami": {"type": "object", "additionalProperties": False},
    "host.status": {"type": "object", "additionalProperties": False},
    "outcome.get": {
        "type": "object",
        "properties": {"idem": _IDEM},
        "required": ["idem"],
        "additionalProperties": False,
    },
}
_HOST_VALIDATORS = {op: Draft202012Validator(schema) for op, schema in _HOST_SCHEMAS.items()}


def served_ops() -> dict[str, int]:
    """Every op the workspace socket serves, with its contract version (``welcome.ops``)."""
    import orch.ops as ops

    out = {op.name: 1 for op in ops.all() if op.name not in NOT_SERVED}
    out.update(HOST_OPS)
    return out


def op_kind(name: str) -> tuple[OpKind, Phase] | None:
    """(matrix category, phase) of a served op, ``None`` for an op that is not served."""
    import orch.ops as ops

    if name in HOST_OPS:
        return ("read", "read")
    if name in NOT_SERVED:
        return None
    try:
        who = ops.get(name).who
    except KeyError:
        return None
    if who == "read":
        return ("person_read" if name in PERSON_READS else "read", "read")
    return (who, "write")  # type: ignore[return-value]


def result_dict(res: Result) -> dict[str, Any]:
    """``orch.ops.Result`` as the §0.3 ``Result`` object."""
    return {
        "data": res.data,
        "key": res.key,
        "seq": int(res.seq),
        "cursor": int(res.cursor),
        "hints": [str(h) for h in res.hints][:8],
        "lines": [str(line) for line in res.lines][:10000],
        "duplicate": bool(res.duplicate),
        "exit": int(res.exit),
        "head": res.head,
    }


def rebuild(record: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """The result of a registry write rebuilt from its logged events (§0.7 reconciliation): the ticket key, the last
    ``seq`` and, for ``new``, its data. Hints and lines are not rebuilt (they were advice, not the outcome)."""
    created = next((e for e in events if e.get("type") == "ticket.created"), None)
    data: dict[str, Any] = {}
    if record.get("op") == "new" and created is not None:
        data = {"title": created.get("title"), "ticket_type": created.get("ticket_type")}
    out = ok_result(data, seq=max(int(e["seq"]) for e in events))
    out["key"] = record.get("key") or (created or {}).get("key")
    return out


def register_builders() -> None:
    """Every served registry write gets :func:`rebuild` as its result builder, unless one is registered already."""
    import orch.ops as ops

    for op in ops.all():
        if op.is_write and op.name not in NOT_SERVED:
            BUILDERS.setdefault(op.name, rebuild)


def host_error(e: OrchError, op: Operation | None) -> HostError:
    """A P1 refusal as a host error: the code keeps its catalog disposition; hint, fix and retryability are resolved as
    the CLI resolves them (the op's declaration, then the catalog)."""
    from orch.cli import render

    body = render.error_envelope(e, op)["error"]
    fix = (body.get("fix") or {}).get("argv")
    out = HostError(body["code"], body.get("message") or body["code"], hint=body.get("hint"), fix=fix)
    out.retryable = bool(body.get("retryable", out.retryable))
    return out


class ScopedRecords:
    """Session records (stop rule, retry dedup) keyed by (principal, session) instead of the session label alone
    (§0.5, F4): a session id is public, so another principal naming it reaches none of these records."""

    def __init__(self, inner: Any, principal: str) -> None:
        self.inner, self.principal = inner, principal

    def _key(self, session: str) -> str:
        return "h_" + hashlib.sha256(f"{self.principal}\0{session}".encode()).hexdigest()[:40]

    def refused(self, session: str, *a: Any, **k: Any) -> int:
        return self.inner.refused(self._key(session), *a, **k)

    def stopped(self, session: str, *a: Any, **k: Any) -> bool:
        return self.inner.stopped(self._key(session), *a, **k)

    def succeeded_write(self, session: str) -> None:
        self.inner.succeeded_write(self._key(session))

    def recall(self, session: str, *a: Any, **k: Any) -> dict[str, Any] | None:
        return self.inner.recall(self._key(session), *a, **k)

    def attempt(self, session: str, *a: Any, **k: Any) -> str:
        return self.inner.attempt(self._key(session), *a, **k)

    def remember(self, session: str, *a: Any, **k: Any) -> None:
        self.inner.remember(self._key(session), *a, **k)


# --- visibility (§0.5, F6) ----------------------------------------------------------------------------------------

_ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")  # the F1 §6 name rule


@dataclass(frozen=True)
class Viewer:
    """The viewer of one read (§0.5): P the mapped person, G the grant's person, N ``no-person`` (``person`` None),
    which sees ``workspace`` tickets only. Every row, count, item and subscription item is filtered with
    :meth:`sees` **before** it is counted or returned, and a ref the viewer may not see is ``not_found`` exactly as a
    ref that does not exist."""

    column: str
    person: str | None
    #: a person-class owner (column P): may name another person in ``needs`` and sees every session and grant
    owner: bool = False

    @classmethod
    def of(cls, call: Call) -> Viewer:
        auth = call.service.auth
        owner = call.column.name == "P" and auth is not None and auth.owner(call.viewer)
        return cls(call.column.name, call.viewer, owner)

    def sees(self, view: Any) -> bool:
        vis = view.visibility
        if vis == "workspace":
            return True
        return self.person is not None and isinstance(vis, Mapping) and self.person in vis.get("restricted", ())

    def visible(self, views: Iterable[Any]) -> list[Any]:
        """The views this viewer may see, in order: filter first, count after (``total``, ``next_page``)."""
        return [v for v in views if self.sees(v)]

    def ticket(self, store: Any, ref: str) -> Any:
        """The ticket ``ref`` names, or ``not_found`` (also for a ticket the viewer may not see: N5, F6)."""

        head = ref.partition("/")[0] if isinstance(ref, str) else ""
        try:
            view = store.ticket(store.normalise_ref(head)) if head else None
        except Exception:  # noqa: BLE001 - a malformed ref is a ref that does not exist
            view = None
        if view is None or not self.sees(view):
            raise HostError("not_found", f"no ticket {' '.join(str(ref).split())[:60]}")
        return view

    def needs_person(self, requested: str | None) -> str | None:
        """``needs {person}``: G always its grant's person; P its own person, or another one only as an owner; N only
        ``workspace`` items (``None``). Naming another person otherwise is ``op.not_allowed``."""
        if self.column == "G":
            if requested is not None and requested != self.person:
                raise HostError("op.not_allowed", "a grant reads only its own person's needs")
            return self.person
        if self.column == "P":
            if requested is None or requested == self.person:
                return self.person
            if self.owner:
                return requested
            raise HostError("op.not_allowed", "only an owner reads another person's needs")
        if requested is not None:
            raise HostError("op.not_allowed", "no-person reads workspace items only")
        return None

    def session_visible(self, entry_person: str | None) -> bool:
        """``session.list`` and ``grant.list``: an owner (P) sees every entry, any other P its own, G its own
        person's; N is outside the matrix (``op.not_allowed``, the gate's)."""
        if self.column == "P" and self.owner:
            return True
        return self.column in {"P", "G"} and entry_person is not None and entry_person == self.person

    def claims(self, store: Any, keys: Iterable[str]) -> list[str]:
        """A session's claims with every ticket the viewer may not see left out."""
        out = []
        for key in keys:
            view = store.ticket(key)
            if view is not None and self.sees(view):
                out.append(key)
        return out

    @staticmethod
    def linked_repo(view: Any, repo: str | None) -> None:
        """``diff {repo}``: only a repo named in the ticket's ``links.repos``; any other is ``not_found``."""
        if repo is None:
            return
        repos = ((view.fields.get("links") or {}).get("repos")) or []
        if repo not in repos:
            raise HostError("not_found", "no such repo on this ticket")

    @staticmethod
    def artifact(view: Any, name: str) -> Any:
        """``artifact.get {name}``: the F1 §6 name rule, then the ticket's replayed manifest (read by digest after);
        a client string is never joined to a path, so ``../x`` is simply a name that does not exist."""
        if not isinstance(name, str) or not _ARTIFACT_NAME.fullmatch(name):
            raise HostError("not_found", "no such artifact on this ticket")
        for artifact in view.artifacts:
            if artifact.name == name:
                return artifact
        raise HostError("not_found", "no such artifact on this ticket")


# --- the backend ------------------------------------------------------------------------------------------------------


class RegistryBackend:
    """The :class:`~orch.host.workspace.OpBackend` of every workspace socket."""

    caps = ["ops/1", "outcome/1"]
    #: §0.6 step 10 is checked here, after the args (8) and the replay (9)
    state_checks = True

    def __init__(self, started: Callable[[], float] | None = None) -> None:
        self.started = started
        self.ops = served_ops()
        self._kinds = {op: op_kind(op) for op in self.ops}
        register_builders()

    def kind(self, op: str) -> tuple[OpKind, Phase] | None:
        return self._kinds.get(op)

    async def run(self, call: Call) -> dict[str, Any]:
        op = call.frame["op"]
        opts = call.frame.get("opts") or {}
        phase = self._kinds[op][1]  # type: ignore[index]
        timeout_ms = opts.get("timeout_ms", DEFAULT_TIMEOUT_MS)
        future = call.service.submit(self.serve, call)  # before any await: a connection's writes keep their order
        try:
            return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout_ms / 1000)
        except TimeoutError:
            if phase == "write":  # never rolled back: it ends committed or failed, and outcome.get reports it
                raise HostError(
                    "outcome.unknown", f"{op} did not finish in time; ask outcome.get with its idem"
                ) from None
            raise HostError("timeout", f"{op} did not finish within {timeout_ms} ms") from None

    # -- on the worker thread
    def serve(self, call: Call) -> dict[str, Any]:
        redactor = Redactor((call.frame.get("auth") or {}).get("grant"))
        try:
            call.authorize()  # the authoritative column, from the refreshed store, before anything else (F8)
            op = call.frame["op"]
            result = self._host_op(call) if op in HOST_OPS else self._registry(call, redactor)
            return redactor.obj(result)
        except HostError as exc:
            raise redactor.error(exc) from None
        except Exception:  # the transport answers internal without a cause: keep it in the host's log, redacted
            LOG.error("%s failed: %s", call.frame.get("op"), redactor.text(traceback.format_exc()))
            raise

    @staticmethod
    def _state(service: WorkspaceService, write: bool) -> None:
        if service.state == "unavailable" or service.store is None:
            raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
        if write and service.state == "degraded":
            raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")

    def _host_op(self, call: Call) -> dict[str, Any]:
        op, frame = call.frame["op"], call.frame
        args = frame["args"]
        errors = sorted(_HOST_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
        if errors:
            where = ".".join(map(str, errors[0].absolute_path))
            raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")
        request_idem(op, frame.get("opts"))  # dry_run is invalid.input here; idem is ignored
        if op == "whoami":
            return ok_result(self.whoami(call))
        if op == "host.status":
            return ok_result(self.status(call))
        self._state(call.service, False)
        return ok_result(call.service.outcomes.lookup(call.principal, args["idem"]))

    def whoami(self, call: Call) -> dict[str, Any]:
        grant = call.verified.grant
        session = (call.frame.get("auth") or {}).get("session")
        return {
            "peer": {"uid": call.peer.uid, "class": call.peer.kind},
            "person": call.viewer,
            "tier": call.service.entry.tier,
            "grant": None
            if grant is None
            else {
                "id": grant.id,
                "until": grant.expires_at,
                "verbs": grant.verbs if isinstance(grant.verbs, str) else list(grant.verbs),
                "scope": grant.scope,
            },
            "session": session,
        }

    def status(self, call: Call) -> dict[str, Any]:
        from orch.cli.main import VERSION_LINE

        data: dict[str, Any] = {"version": VERSION_LINE[:64], "tier": call.service.entry.tier}
        if call.column.name in {"P", "G"}:  # class N gets only version and tier
            data["state"] = call.service.state
            if self.started is not None:
                data["uptime_s"] = int(time.monotonic() - self.started())
        return data

    # -- registry ops
    def _registry(self, call: Call, redactor: Redactor) -> dict[str, Any]:
        import orch.ops as ops
        from orch.cli.main import _SESSION, _check_input
        from orch.ops.errors import OrchError

        op, frame, service = call.frame["op"], call.frame, call.service
        operation = ops.get(op)
        args = dict(frame["args"])
        opts = frame.get("opts") or {}
        session = (frame.get("auth") or {}).get("session")
        # step 8: the args, the files, dry_run support
        try:
            _check_input(operation, args, redactor.secrets)
        except OrchError as e:
            raise host_error(e, operation) from None
        if session is not None and not _SESSION.search(session):
            raise HostError("invalid.input", "auth.session is not a session id (s_<ULID>, then at most three .n)")
        write, dry_run = operation.is_write, bool(opts.get("dry_run"))
        specs = self._file_specs(operation, frame, args)
        idem_given = opts.get("idem") if write and not dry_run else None
        digests, digest_args = service.outcomes.bind_files(call.principal, idem_given, specs)
        # step 9 starts here: idem.required (after dry_run support, which request_idem checks first)
        idem = request_idem(op, opts)
        ctx = self._context(call, session, dry_run, self._file_bytes(specs, args))
        if idem is None:  # a read or a dry run: no outcome record
            self._state(service, False)
            return self._run(operation, args, ctx, call)
        normalised = self._normalise(operation, args, service.store)

        def apply(intent: Any) -> dict[str, Any]:
            from orch.store import StoreError

            def plan(events: list[tuple[str, str, str, dict[str, Any]]], key: str | None = None) -> None:
                service.outcomes.plan(intent, [Planned(*e) for e in events], key)

            def check(log: str, event: dict[str, Any]) -> None:
                listed = {(p.get("log"), p.get("id")) for p in intent.record.get("planned") or []}
                if (log, event.get("id")) not in listed:
                    raise StoreError("internal", f"{event.get('type')}: an append its outcome record does not list")

            ctx.plan = plan
            service.store.append_check = check
            try:
                return redactor.obj(self._run(operation, args, ctx, call))
            finally:
                service.store.append_check = None

        return service.outcomes.execute(
            call.principal,
            idem,
            op,
            normalised,
            apply,
            planned=None,
            files=digests,
            session=session,
            digest_args=digest_args,
            before=lambda: self._state(service, True),
        )

    @staticmethod
    def _file_specs(operation: Operation, frame: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        """``files`` (§0.3): each entry names an argument of the op that carries the file's logical name in
        ``args``; anything else is ``blob.invalid``."""
        specs = dict(frame.get("files") or {})
        props = operation.input.get("properties", {})
        for arg in specs:
            if arg not in props or not isinstance(args.get(arg), str):
                raise HostError("blob.invalid", f"files.{arg} names no file argument of {operation.name}")
        return specs

    @staticmethod
    def _file_bytes(specs: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, bytes]:
        """The uploaded bytes by logical name (``-`` for stdin): what ``ctx.files`` gives the handler."""
        out = {}
        for arg, spec in specs.items():
            if "b64" in spec:
                try:
                    out[str(args[arg])] = base64.b64decode(spec["b64"], validate=True)
                except (binascii.Error, ValueError):  # bind_files refused it already
                    raise HostError("blob.invalid", f"files.{arg} is not valid base64") from None
        return out

    @staticmethod
    def _normalise(operation: Operation, args: dict[str, Any], store: Any) -> dict[str, Any]:
        """``args_normalised`` of the binding (§0.7): the schema's defaults filled in and the REF resolved to the
        ticket uid (a task suffix kept)."""
        out = dict(args)
        for name, spec in (operation.input.get("properties") or {}).items():
            if name not in out and isinstance(spec, Mapping) and "default" in spec:
                out[name] = spec["default"]
        ref = out.get("ref")
        if isinstance(ref, str) and store is not None:
            head, sep, tail = ref.partition("/")
            try:
                uid = store.uid_of(head) if head else None
            except Exception:  # noqa: BLE001 - a ref that does not resolve stays as given
                uid = None
            if uid:
                out["ref"] = uid + sep + tail
        return out

    def _context(self, call: Call, session: str | None, dry_run: bool, files: dict[str, bytes]) -> Any:
        from orch.ops import Context
        from orch.ops.runtime import Workspace

        service = call.service
        caps = service.capabilities()
        workspace = Workspace.hosted(
            Path(service.entry.root), service.store, env=caps.env, now=service.clock, files=caps.files
        )
        grant = (call.frame.get("auth") or {}).get("grant") if call.column.name == "G" else None
        return Context(
            session=session,
            grant=grant,
            human_presence=False,  # never a person at a terminal: human ops go through human.* (H5a)
            dry_run=dry_run,
            now=service.clock,
            workspace=workspace,
            files=files,
            caps=caps,
            hosted=True,
            viewer=call.viewer,
            principal=call.principal,
        )

    def _run(self, operation: Operation, args: dict[str, Any], ctx: Any, call: Call) -> dict[str, Any]:
        from orch.cli import main as cli
        from orch.cli.parser import Parsed
        from orch.cli.session import FileRecords
        from orch.cli.store_hooks import hooks_for
        from orch.ops.errors import OrchError
        from orch.ops.runtime import records_dir

        store = call.service.store
        hooks = hooks_for(store)
        records = ScopedRecords(FileRecords(records_dir(Path(call.service.entry.root))), call.principal)
        parsed = Parsed(operation, dict(args), dry_run=ctx.dry_run)
        try:
            res = cli.run(parsed, ctx, records, hooks)  # type: ignore[arg-type]
        except OrchError as e:
            e = cli.stop_rule(operation, parsed.args, ctx, ctx.session, records, hooks, e, ctx.now)  # type: ignore[arg-type]
            raise host_error(e, operation) from None
        if ctx.session and operation.is_write and not ctx.dry_run:
            records.succeeded_write(ctx.session)
        return result_dict(res)
