"""The host's dispatcher (H4a1): the registry ops on a workspace socket, with §0.5 to §0.8 applied to each request.

**The closed op list** (§0.4, §0.5). The host serves every registry op of :mod:`orch.ops` under its name at contract
version 1, plus ``whoami``, ``host.status``, ``outcome.get``, ``sub.open``, ``sub.close`` and the files ops
(``blob.put``, ``blob.drop``, ``artifact.get``: H4a2, :mod:`orch.host.blobs`, :mod:`orch.host.artifacts`), ``diff``
(the H3b1 follow-up, :mod:`orch.host.diff`), and nothing else (``unknown_command``). Left out:

* ``init`` and ``import.v1`` (D-P2-9: ``init`` is ``host.wsk_create`` + ``host.bootstrap``; ``import v1`` runs in
  direct mode before registration).

``task.done`` is served at contract 2 (H3b2): ``run`` is refused (``op.not_allowed``, the host never executes
``verify.cmd``), a client receipt comes in ``receipt`` and ``receipt_output`` (``files``) and is stored labelled
``attested_by: "agent"``.

**``wait`` on the bus (H6a).** Its P1 handler sleeps between looks at the log, which would hold the workspace's one
worker thread. Here each look is one short job on the worker thread (``Context.wait_step``: the handler looks once and
raises :class:`~orch.ops.commands.wait.Parked`); between looks the request parks on the event loop until the bus
delivers an entry of the ticket's log or its ``timeout`` passes (:meth:`RegistryBackend.wait`). Its phase is ``wait``:
a drain answers it ``host.restarting``, and ``opts.timeout_ms`` does not apply (§0.2).

**Subscriptions (H6a, §0.9).** ``sub.open`` runs its checks on the worker thread (the column, the topics, a
``ticket:<ref>`` through :meth:`Viewer.ticket`, the cursor against the journal) and fixes the live boundary there; the
event loop then registers it with the host's :class:`~orch.host.bus.Bus` before its ``res`` is sent.

Each op has a matrix category (:data:`orch.host.columns.OpKind`) from its registry ``who``: ``read`` (``doctor`` and
``instructions.sync`` are ``person_read``, ``instructions.hook`` is ``pg_read``), ``agent``, ``unattended``,
``human``. The gate
(:class:`orch.host.workspace.WorkspaceGate`) applies the column with it (§0.6 steps 3 to 7); this module does steps 8
to 11.

**One request** runs on the workspace worker thread in one piece (:meth:`RegistryBackend.serve`), submitted before the
coroutine awaits anything, so one connection's writes run in the order they were received (§0.2):

8. ``args`` against the op's schema (``invalid.input``), ``files`` (``blob.invalid``: an entry for an argument the
   registry does not mark ``x-file``, an ``x-file`` argument without its entry, inline bytes over 512 KiB, a ``{blob}``
   that is not a complete blob of this principal in this workspace, a ``{digest}`` without a record), ``dry_run``
   support (``invalid.input``);
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

**Files** (§0.3, §0.10, H4a2). The handler gets a file's bytes only through ``ctx.files`` (inline ``{b64}`` or a
``{blob}`` read here); the blob directory is never a handler's file root. The binding holds each file's digest (§0.7),
and a write that commits consumes the blobs it used (also when the answer is a replay); a refusal or a dry run keeps
them.

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
import contextlib
import logging
import time
import traceback
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from jsonschema import Draft202012Validator

from . import diff as git_diff
from . import phonehints, readerops, reads, sessions, terms
from . import quick as quick_list
from . import records as records_ops
from .agents import AGENT_OPS
from .artifacts import VALIDATOR as ARTIFACT_VALIDATOR
from .artifacts import artifact_get
from .auth import Redactor
from .blobs import INLINE_MAX, decoded_len
from .blobs import VALIDATORS as BLOB_VALIDATORS
from .bus import Subscription
from .columns import OpKind
from .errors import HostError
from .hubops import HUB_OPS
from .hubops import NETWORK_OPS as HUB_NETWORK_OPS
from .outcomes import BUILDERS, Planned, request_idem
from .peerops import NETWORK_OPS as PEER_NETWORK_OPS
from .peerops import PEER_OPS, PEER_READS
from .publishing import NETWORK_OPS as PUBLISH_NETWORK_OPS
from .publishing import PUBLISH_CAPS, PUBLISH_OPS
from .reads import CAPS as READ_CAPS
from .reads import READ_OPS
from .runnergrants import GRANT_OPS
from .runners import RUN_CAPS, RUN_OPS
from .shareops import NETWORK_OPS as SHARE_NETWORK_OPS
from .shareops import SHARE_OPS, SHARE_READS
from .spaceops import NETWORK_OPS as SPACE_NETWORK_OPS
from .spaceops import SPACE_OPS, SPACE_READS
from .terms import TERM_CAPS, TERM_OPS
from .visibility import Viewer
from .workdirs import CAPS as WORKDIR_CAPS
from .workdirs import WORKDIR_OPS
from .workdirs import WRITE_OPS as WORKDIR_WRITES
from .workspace import deny_append, ok_result
from .worktrees import WORKTREE_CAPS, WORKTREE_OPS

if TYPE_CHECKING:
    from orch.ops import Operation, Result
    from orch.ops.errors import OrchError

    from .workspace import Call, WorkspaceService

__all__ = [
    "HOST_OPS",
    "NOT_SERVED",
    "OP_VERSIONS",
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
}
PERSON_READS = frozenset({"doctor", "instructions.sync"})
#: registry reads for P and G: the session-start hook runs in the agent's own session (column G, a verified grant), so
#: it must not be a person-only read (run 6, #1067: ``instructions.hook is not allowed here`` at every agent start)
#: Column N (an agent with no verified grant) is excluded on purpose: the hook shows the person, claim and unread
#: decisions of a session, which an unverified caller has no right to see.
PERSON_GRANT_REGISTRY_READS = frozenset({"instructions.hook"})
#: arguments the host refuses (``op.not_allowed``) on an op it otherwise serves
HOST_REFUSED_ARGS: dict[str, dict[str, str]] = {
    "task.done": {
        "run": "the host never executes verify.cmd: run it yourself and send a receipt (task.done contract 2)"
    },
    "doctor": {
        "repair": "doctor --repair is a control action of the host's owner, not a workspace op (a control op later)"
    },
}
#: registry ops served above contract version 1 (§0.4): a client that does not list the version gets ``op.unsupported``
OP_VERSIONS: dict[str, int] = {"task.done": 2}
HOST_OPS: dict[str, int] = {"whoami": 1, "host.status": 1, "outcome.get": 1, "sub.open": 1, "sub.close": 1}
#: the files ops (H4a2, §0.10, §0.8): side-effect-free, columns P, G and N
FILE_OPS: dict[str, int] = {"blob.put": 1, "blob.drop": 1, "artifact.get": 1}
#: the git reads (§0.8, H3b1 follow-up): ``diff`` v1 (v1.4), columns P, G and N, through the host's reader
GIT_OPS: dict[str, int] = {"diff": 1}
#: ``quick/1`` (H11f, #308): ``quick.list``, a registry read that the host answers itself in the three phases of a git
#: read (:mod:`orch.host.quick`); columns P, G and N like ``ticket.query``
QUICK_OPS: dict[str, int] = {"quick.list": 1}
#: registry ops served in the ``wait`` phase (parked on the bus, answered ``host.restarting`` by a drain)
WAIT_OPS = frozenset({"wait"})
_TOPIC = r"^(?:workspace|tickets|needs|ticket:(?:(?:[A-Z][A-Z0-9]{0,15}-)?[0-9]{1,9}|[0-7][0-9A-HJKMNP-TV-Z]{25}))$"
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
    # detail (v1.5) is not here: without sub_ext/1 it is an unknown key, so invalid.input (§0.3, §0.9);
    # _SUB_OPEN_EXT is the schema of a host listing sub_ext/1
    "sub.open": {
        "type": "object",
        "properties": {
            "topics": {"type": "array", "items": {"type": "string", "pattern": _TOPIC}, "minItems": 1, "maxItems": 8},
            "after": {"type": "string", "minLength": 1, "maxLength": 256},
        },
        "required": ["topics"],
        "additionalProperties": False,
    },
    "sub.close": {
        "type": "object",
        "properties": {"sub": {"type": "string", "pattern": r"^sub_[0-9a-z]{1,16}$"}},
        "required": ["sub"],
        "additionalProperties": False,
    },
}
_HOST_VALIDATORS = {op: Draft202012Validator(schema) for op, schema in _HOST_SCHEMAS.items()}
_SUB_OPEN_EXT = {
    **_HOST_SCHEMAS["sub.open"],
    "properties": {**_HOST_SCHEMAS["sub.open"]["properties"], "detail": {"type": "boolean"}},
}
_SUB_OPEN_EXT_VALIDATOR = Draft202012Validator(_SUB_OPEN_EXT)
#: the host lists ``sub_ext/1`` and accepts ``sub.open {detail}`` (v1.5, §0.9); a test turns it off to show §0.3's
#: unknown-key refusal and the cap's absence from ``welcome.caps``
SUB_EXT = True


def served_ops() -> dict[str, int]:
    """Every op the workspace socket serves, with its contract version (``welcome.ops``)."""
    import orch.ops as ops

    from .human import HUMAN_OPS
    from .reads import READ_OPS
    from .review import HOST_PERSON_OPS

    out = {op.name: OP_VERSIONS.get(op.name, 1) for op in ops.all() if op.name not in NOT_SERVED}
    out.update(HOST_OPS)
    out.update(FILE_OPS)
    out.update(GIT_OPS)
    out.update(HUMAN_OPS)
    out.update(READ_OPS)
    out.update(dict.fromkeys(HOST_PERSON_OPS, 1))
    out.update(records_ops.RECORDS_OPS)
    out.update(phonehints.PHONE_WRITE_OPS)  # B10 Part B: phone.request, phone.cancel (P only)
    out.update(readerops.READER_HOST_OPS)  # H12d-2: reader.declare (reader.list is a read, cap reader/1)
    out.update(RUN_OPS)  # H11a: run.list, run.sub; H11b: run.stop
    out.update(TERM_OPS)  # H11b: term.preview, term.open, term.attach, attach.cancel
    out.update(WORKDIR_OPS)  # r3 part C: workdir.get, workdir.delete (P only)
    out.update(WORKTREE_OPS)  # H11d: worktree.list, worktree.create, worktree.remove
    out.update(PUBLISH_OPS)  # P5: publish.prepare, .send, .revoke, .list, .discard, .register
    out.update(HUB_OPS)  # V3 C2a: hub.connect, hub.unbind, hub.status
    out.update(PEER_OPS)  # V3 C12: peer.* (ws->ws through the hub)
    out.update(SPACE_OPS)  # V3 C13: space.* (shared spaces through the hub)
    out.update(SHARE_OPS)  # V3 C6: share.* (selective-mode share rules)
    out.update(GRANT_OPS)  # H11c: grant.deposit, grant.deposit_status, grant.forget (cap run/1)
    out.update(AGENT_OPS)  # H11c: agent.preview, agent.start (cap run/1)
    return out


def file_args(operation: Operation) -> frozenset[str]:
    """The arguments the registry marks ``x-file: true`` (§0.3): their bytes come only through ``files``."""
    props = operation.input.get("properties") or {}
    return frozenset(name for name, spec in props.items() if isinstance(spec, Mapping) and spec.get("x-file") is True)


def op_kind(name: str) -> tuple[OpKind, Phase] | None:
    """(matrix category, phase) of a served op, ``None`` for an op that is not served."""
    import orch.ops as ops

    from .human import HUMAN_OPS, PHASES
    from .reads import PERSON_GRANT_READS, READ_OPS
    from .review import HOST_PERSON_OPS

    if name in HOST_OPS or name in FILE_OPS or name in GIT_OPS:
        return ("read", "read")
    if name in phonehints.PHONE_WRITE_OPS:  # B10 Part B: column P only (G and N get op.not_allowed)
        return ("person_write", "write")
    if name in readerops.READER_HOST_OPS:  # H12d-2: column P (column R: device_signed, H12d-3)
        return ("person_write", "write")
    if name in records_ops.RECORDS_OPS:  # H11e: column P only; the role is checked by the op
        return ("person_write", "write") if name in records_ops.WRITE_OPS else ("person_read", "read")
    if name in WORKDIR_OPS:  # r3 part C: column P only; the role is checked by the op
        return ("person_write", "write") if name in WORKDIR_WRITES else ("person_read", "read")
    if name == "agent.preview":  # H11c: P only, no side effects
        return ("person_read", "read")
    if name == "agent.start":  # H11c: P only
        return ("person_write", "write")
    if name == "grant.deposit":  # H11c: a person with their own verified runner grant (checked by the op)
        return ("deposit", "write")
    if name == "grant.deposit_status":  # H11c: P only, own deposits
        return ("person_read", "read")
    if name == "grant.forget":  # H11c: P only, own grants
        return ("person_write", "write")
    if name == "term.preview":  # H11b: column P only
        return ("person_read", "read")
    if name in TERM_OPS or name == "run.stop":  # H11b: P only; G and N get op.not_allowed
        return ("person_write", "write")
    if name in RUN_OPS:  # H11a: P and G only (§0.5, D-P2-26)
        return ("pg_read", "read")
    if name in WORKTREE_OPS:  # H11d: P and G (G only on its claimed ticket, checked by the op), never N
        return ("pg_read", "read") if name == "worktree.list" else ("pg_write", "write")
    if name in PUBLISH_OPS:  # P5: prepare and discard P and G (G on its claimed ticket); send, revoke, register P only
        if name == "publish.list":
            return ("pg_read", "read")
        return ("pg_write", "write") if name in ("publish.prepare", "publish.discard") else ("person_write", "write")
    if name in HUB_OPS:  # V3 C2a: column P only (the op checks the owner role)
        return ("person_read", "read") if name in ("hub.status", "hub.handoff") else ("person_write", "write")
    if name in PEER_OPS:  # V3 C12: reads for P and G (an agent reads its inbox); writes P only, owner role
        return ("pg_read", "read") if name in PEER_READS else ("person_write", "write")
    if name in SHARE_OPS:  # V3 C6: column P only (the op checks the owner role)
        return ("person_read", "read") if name in SHARE_READS else ("person_write", "write")
    if name in SPACE_OPS:  # V3 C13: list and get for P and G (untrusted data), the rest P only (owner role)
        return ("pg_read", "read") if name in SPACE_READS else ("person_write", "write")
    if name in READ_OPS:  # H6b: session.list and grant.list are P and G only (§0.5)
        return ("pg_read" if name in PERSON_GRANT_READS else "read", "read")
    if name in HOST_PERSON_OPS:  # move, policy.set, settings.set: through human.* only; a plain req is human_only
        return ("human", "write")
    if name in WAIT_OPS:
        return ("read", "wait")
    if name in HUMAN_OPS:
        return ("human_api", PHASES[name])  # type: ignore[return-value]
    if name in NOT_SERVED:
        return None
    try:
        who = ops.get(name).who
    except KeyError:
        return None
    if who == "read":
        if name in PERSON_GRANT_REGISTRY_READS:
            return ("pg_read", "read")
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
    return Redactor().obj(out)  # logged text reaches the record: never a grant shape or key material


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
    detail = getattr(e, "detail", None)  # H12d-2: a reader refusal's closed detail (§0.3, closed per code)
    out = HostError(body["code"], body.get("message") or body["code"], hint=body.get("hint"), fix=fix, detail=detail)
    out.retryable = bool(body.get("retryable", out.retryable))
    return out


class PlanGuard:
    """One write's plan: ``plan`` adds an announced batch to the outcome record (fsynced) before it is appended, and
    ``check`` (the store's append check during ``apply``) refuses any event the record does not list."""

    def __init__(self, outcomes: Any, intent: Any) -> None:
        self.outcomes, self.intent = outcomes, intent
        self.listed = {(p.get("log"), p.get("id")) for p in intent.record.get("planned") or []}

    def plan(self, events: list[tuple[str, str, str, dict[str, Any]]], key: str | None = None) -> None:
        self.outcomes.plan(self.intent, [Planned(log, eid, typ, actor) for log, eid, typ, actor in events], key)
        self.listed |= {(log, eid) for log, eid, _typ, _actor in events}

    def check(self, log: str, event: dict[str, Any]) -> None:
        from orch.store import StoreError

        if (log, event.get("id")) not in self.listed:
            raise StoreError("internal", f"{event.get('type')}: an append its outcome record does not list")


class ScopedRecords:
    """Session records (stop rule, retry dedup) keyed by (principal, session) instead of the session label alone
    (§0.5, F4): a session id is public, so another principal naming it reaches none of these records."""

    def __init__(self, inner: Any, principal: str) -> None:
        self.inner, self.principal = inner, principal

    def _key(self, session: str) -> str:
        from orch.ops.runtime import scoped_key

        return scoped_key(self.principal, session)

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


# --- the backend ------------------------------------------------------------------------------------------------------


class RegistryBackend:
    """The :class:`~orch.host.workspace.OpBackend` of every workspace socket."""

    #: ``diff/1``: ``artifact.get`` (H4a2) and ``diff`` (H3b1 follow-up); ``diff_ext/1``: ``diff {head, base}`` (H6c;
    #: ``commit.list`` is not served yet, a client checks ``welcome.ops``); the H6b read caps; ``sub_ext/1`` while
    #: detail is served
    @property
    def caps(self) -> list[str]:
        base = ["ops/1", "outcome/1", "sub/1", "blob/1", "diff/1", "diff_ext/1", "quick/1", "human/1"]
        return [
            *base, *READ_CAPS, *records_ops.CAPS, *(["sub_ext/1"] if SUB_EXT else []), *RUN_CAPS, *TERM_CAPS,
            *WORKDIR_CAPS, *WORKTREE_CAPS, *PUBLISH_CAPS, *readerops.CAPS, *phonehints.CAPS,
        ]  # fmt: skip

    #: §0.6 step 10 is checked here, after the args (8) and the replay (9)
    state_checks = True

    def __init__(self, started: Callable[[], float] | None = None) -> None:
        self.started = started
        from .human import HumanOps

        self.ops = served_ops()
        self._kinds = {op: op_kind(op) for op in self.ops}
        self.human = HumanOps(self)
        register_builders()

    def kind(self, op: str) -> tuple[OpKind, Phase] | None:
        return self._kinds.get(op)

    async def run(self, call: Call) -> dict[str, Any]:
        op = call.frame["op"]
        if op in WAIT_OPS:
            return await self.wait(call)
        opts = call.frame.get("opts") or {}
        phase = self._kinds[op][1]  # type: ignore[index]
        timeout_ms = opts.get("timeout_ms", DEFAULT_TIMEOUT_MS)
        future = call.service.submit(self.serve, call)  # before any await: a connection's writes keep their order
        try:
            result = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout_ms / 1000)
        except TimeoutError:
            if phase == "write":  # never rolled back: it ends committed or failed, and outcome.get reports it
                raise HostError(
                    "outcome.unknown", f"{op} did not finish in time; ask outcome.get with its idem"
                ) from None
            raise HostError("timeout", f"{op} did not finish within {timeout_ms} ms") from None
        if op in GIT_OPS or op in QUICK_OPS:
            return await self._git_read(call, result)
        if op == "sub.open":
            return self._register(call, result)
        if op == "run.sub":
            return await self._register_runs(call, result)
        if op in TERM_OPS or op == "run.stop":
            return await terms.complete(call, result)
        if op in AGENT_OPS:  # H11c: the launch intent, the worktree, the spawn
            from . import agents

            return await agents.complete(call, result)
        if op in GRANT_OPS:  # H11c: the runner grant table and the runner, on the event loop
            from . import runnergrants

            return await runnergrants.complete(call, result)
        if op in WORKTREE_OPS and op != "worktree.list":  # H11d: the runner's git, then the registration
            from . import worktrees

            return await worktrees.execute(call, result)
        if op in PUBLISH_NETWORK_OPS or op == "publish.list":  # P5: the server calls, off the workspace worker
            from . import publishing

            return await publishing.execute(call, result)
        if op in HUB_NETWORK_OPS:  # V3 C2a: the hub link, off the workspace worker
            from . import hubops

            return await hubops.execute(call, result)
        if op in SHARE_NETWORK_OPS:  # V3 C6: the hub link, off the workspace worker
            from . import shareops

            return await shareops.execute(call, result)
        if op in SPACE_NETWORK_OPS:  # V3 C13: the hub link, off the workspace worker
            from . import spaceops

            return await spaceops.execute(call, result)
        if op in PEER_NETWORK_OPS:  # V3 C12: peer.send / peer.reply, the hub link off the worker
            from . import peerops

            return await peerops.execute(call, result)
        if op == "sub.close":
            bus = call.service.bus
            closed = bus is not None and bus.close(call.session, call.frame["args"]["sub"])
            hub = getattr(call.service, "runners", None)
            if not closed and hub is not None:
                hub.close_sub(call.session, call.frame["args"]["sub"])
        return result

    @staticmethod
    async def _git_read(call: Call, pending: Any) -> dict[str, Any]:
        """``diff``'s reader phase, off the workspace worker thread (the worker resolved visibility, links and
        capabilities): one in flight per principal, :data:`~orch.host.diff.BUDGET_S` for the request. ``quick.list``
        (H11f) goes through the same phases with its own reader loop and its own phase 3."""
        is_quick = call.frame["op"] == "quick.list"
        run_phase = quick_list.run if is_quick else git_diff.run
        budget = quick_list.BUDGET_S if is_quick else git_diff.BUDGET_S
        redactor = Redactor((call.frame.get("auth") or {}).get("grant"))
        principal = pending.principal

        def work() -> dict[str, Any]:
            # the slot is held until the reader work itself ends (also when the request stops waiting for it), and
            # given back on this thread, so it never outlives the work even when the event loop stops first
            try:
                return run_phase(pending)
            finally:
                git_diff.IN_FLIGHT.give(principal)

        try:
            git_diff.IN_FLIGHT.take(principal)
            try:
                future = asyncio.get_running_loop().run_in_executor(git_diff.EXECUTOR, work)
            except BaseException:  # never scheduled: work() will not give it back
                git_diff.IN_FLIGHT.give(principal)
                raise
            failure: HostError | None = None
            data: dict[str, Any] = {}
            try:  # the reader's deadlines end the work within the budget; this only bounds a stuck thread
                data = await asyncio.wait_for(asyncio.shield(future), budget + 5)
            except TimeoutError:
                failure = HostError("observe.unavailable", f"the read took longer than {budget:g} s")
            except HostError as exc:
                failure = exc
            # visibility again before the answer or the error: a ticket restricted while the reader ran is
            # not_found, never the reader's error (F6, F8)
            if is_quick:
                if failure is not None:
                    await asyncio.wrap_future(call.service.submit(call.authorize))
                    raise failure from None
                done = await asyncio.wrap_future(call.service.submit(quick_list.finish, call, data))
                return redactor.obj({**ok_result(done["data"]), "lines": done["lines"], "hints": done["hints"]})
            await asyncio.wrap_future(call.service.submit(git_diff.recheck, call, pending))
            if failure is not None:
                raise failure from None
            return redactor.obj(ok_result(data))
        except HostError as exc:
            raise redactor.error(exc) from None
        except Exception:  # the transport answers internal without a cause: keep it in the host's log, redacted
            LOG.error("%s failed: %s", call.frame.get("op"), redactor.text(traceback.format_exc()))
            raise

    def _register(self, call: Call, checked: dict[str, Any]) -> dict[str, Any]:
        """``sub.open`` on the event loop: register the subscription the worker checked, at the boundary it fixed."""
        bus = call.service.bus
        if bus is None:
            raise HostError("workspace.unavailable", "the workspace has no subscription bus")
        data = checked["data"]
        grant = (call.frame.get("auth") or {}).get("grant") if call.column.name == "G" else None
        sub: Subscription = bus.open(
            call.session,
            call.request,
            call.service.workspace_id,
            topics=frozenset(data["topics"]),
            tickets=frozenset(data["tickets"]),
            principal=call.principal,
            column=call.column.name,
            person=call.viewer,
            grant=grant,
            grant_id=call.verified.grant.id if call.verified.grant is not None else None,
            pos=int(data["pos"]),
            detail=bool(data.get("detail")),
        )
        return ok_result({"sub": sub.id, "cursor": bus.cursor(sub)})

    @staticmethod
    async def _register_runs(call: Call, checked: dict[str, Any]) -> dict[str, Any]:
        """``run.sub`` on the event loop (H11a): register it first (changes queue from now on), then take the runs it
        may see on the worker, then start delivery, so no change falls between the two (F6). The answer's ``rev`` is
        the host's run revision at registration; a client lists after it and keeps the higher per-run ``rev``."""
        from . import runners

        hub = getattr(call.service, "runners", None)
        if hub is None:
            raise HostError("runner.unavailable", "this host serves no runs", retry_after_ms=1000)
        sub = hub.open_sub(call, set(), start=False)
        rev = hub.store.rev
        try:
            now = await asyncio.wrap_future(call.service.submit(runners.serve, call))
        except BaseException:
            hub.end_sub(sub, "closed")
            raise
        sub.seen = set(now["data"]["seen"])
        hub.start_sub(sub)
        return ok_result({"sub": sub.id, "rev": rev})

    async def wait(self, call: Call) -> dict[str, Any]:
        """``wait`` on the bus: one look per worker job; between looks the request parks here until an entry of the
        ticket's log arrives or its ``timeout`` passes (then one last look gives the timeout answer)."""
        from orch.ops.commands.wait import Parked, Step

        service = call.service
        bus = service.bus
        if bus is None:
            raise HostError("workspace.unavailable", "the workspace has no subscription bus")
        loop = asyncio.get_running_loop()
        timeout = call.frame["args"].get("timeout", 540)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
            timeout = 540  # the first look refuses it (invalid.input)
        deadline = loop.time() + timeout
        step = Step()
        watch = bus.watch(service.workspace_id)  # before the first look: nothing committed after it is missed
        try:
            while True:
                try:
                    return await asyncio.wrap_future(service.submit(self.serve, call, step))
                except Parked:
                    pass
                watch.uid = step.log
                # look again only for an entry that can answer (or refuse) this wait, a rebuild, or the timeout
                while not watch.relevant():
                    watch.clear()
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        step.expired = True
                        break
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(watch.event.wait(), remaining)
                watch.clear()
        finally:
            bus.unwatch(watch)

    # -- on the worker thread
    def serve(self, call: Call, step: Any = None) -> dict[str, Any]:
        from orch.ops.commands.wait import Parked

        redactor = Redactor((call.frame.get("auth") or {}).get("grant"), call.frame.get("op"))
        try:
            call.authorize()  # the authoritative column, from the refreshed store, before anything else (F8)
            sessions.record(call)  # H6b: the host session records session.list and grant.list read (never raises)
            store = call.service.store
            if store is not None and store.append_check is None:
                store.append_check = deny_append  # outside a write's apply no caller event may land (§0.7)
            op = call.frame["op"]
            if op in HOST_OPS:
                result = self._host_op(call)
            elif op in READ_OPS:
                result = reads.serve(call)
            elif op in records_ops.RECORDS_OPS:
                result = records_ops.serve(call)
            elif op in WORKDIR_OPS:
                from . import workdirs

                result = workdirs.serve(call)
            elif op in readerops.READER_HOST_OPS:
                result = readerops.serve(call)
            elif op in phonehints.PHONE_WRITE_OPS:
                result = phonehints.serve(call)
            elif op in TERM_OPS or op == "run.stop":
                result = terms.serve(call)
            elif op in AGENT_OPS:
                from . import agents

                result = agents.serve(call)
            elif op in GRANT_OPS:
                from . import runnergrants

                result = runnergrants.serve(call)
            elif op in RUN_OPS:
                from . import runners

                result = runners.serve(call)
            elif op in WORKTREE_OPS:
                from . import worktrees

                result = worktrees.serve(call)
            elif op in PUBLISH_OPS:
                from . import publishing

                result = publishing.serve(call)
            elif op in HUB_OPS:
                from . import hubops

                result = hubops.serve(call)
            elif op in SHARE_OPS:
                from . import shareops

                result = shareops.serve(call)
            elif op in SPACE_OPS:
                from . import spaceops

                result = spaceops.serve(call)
            elif op in PEER_OPS:
                from . import peerops

                result = peerops.serve(call)
            elif op in FILE_OPS or op in GIT_OPS or op in QUICK_OPS:
                result = self._file_op(call)
            elif op in self.human.ops:
                result = self.human.serve(call, redactor)
            else:
                result = self._registry(call, redactor, step)
            return redactor.obj(result)
        except HostError as exc:
            raise redactor.error(exc) from None
        except Parked:
            raise
        except Exception:  # the transport answers internal without a cause: keep it in the host's log, redacted
            LOG.error("%s failed: %s", call.frame.get("op"), redactor.text(traceback.format_exc()))
            raise
        finally:
            try:  # the journal's new entries go to the bus before this answer is sent (and so before its evts)
                call.service.sync()
            except Exception:  # noqa: BLE001 - delivery bookkeeping never changes an op's answer
                LOG.error("journal sync failed: %s", redactor.text(traceback.format_exc()))

    @staticmethod
    def _state(service: WorkspaceService, write: bool) -> None:
        if service.state == "unavailable" or service.store is None:
            raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
        if write and service.state == "degraded":
            raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")

    def _host_op(self, call: Call) -> dict[str, Any]:
        op, frame = call.frame["op"], call.frame
        args = frame["args"]
        validator = _SUB_OPEN_EXT_VALIDATOR if op == "sub.open" and SUB_EXT else _HOST_VALIDATORS[op]
        errors = sorted(validator.iter_errors(args), key=lambda e: list(map(str, e.path)))
        if errors:
            where = ".".join(map(str, errors[0].absolute_path))
            raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")
        request_idem(op, frame.get("opts"))  # dry_run is invalid.input here; idem is ignored
        if op == "whoami":
            return ok_result(self.whoami(call))
        if op == "host.status":
            return ok_result(self.status(call))
        if op == "sub.close":
            return ok_result({})
        self._state(call.service, False)
        if op == "sub.open":
            return ok_result(self.sub_open(call, args))
        return ok_result(call.service.outcomes.lookup(call.principal, args["idem"]))

    def _file_op(self, call: Call) -> dict[str, Any]:
        """``blob.put``, ``blob.drop`` (§0.10), ``artifact.get`` and ``diff`` (§0.8): schema, no ``dry_run``, no
        ``files``, the workspace state, then the op."""
        op, frame = call.frame["op"], call.frame
        args = frame["args"]
        validator = (
            ARTIFACT_VALIDATOR
            if op == "artifact.get"
            else git_diff.VALIDATOR
            if op == "diff"
            else quick_list.VALIDATOR
            if op == "quick.list"
            else BLOB_VALIDATORS[op]
        )
        errors = sorted(validator.iter_errors(args), key=lambda e: list(map(str, e.path)))
        if errors:
            where = ".".join(map(str, errors[0].absolute_path))
            raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")
        if frame.get("files"):
            raise HostError("blob.invalid", f"{op} takes no files")
        request_idem(op, frame.get("opts"))  # dry_run is invalid.input here; idem is ignored
        service = call.service
        self._state(service, False)
        if op == "artifact.get":
            return ok_result(artifact_get(call, args))
        if op == "diff":
            return git_diff.prepare(call, args)  # type: ignore[return-value]  # the reader phase is run()'s
        if op == "quick.list":
            return quick_list.prepare(call, args)  # type: ignore[return-value]  # likewise
        if op == "blob.drop":
            service.blobs.drop(call.principal, args["blob"])
            return ok_result({})
        return ok_result(service.blobs.put(call.principal, call.peer.uid, call.peer.kind == "person", args))

    @staticmethod
    def sub_open(call: Call, args: dict[str, Any]) -> dict[str, Any]:
        """The worker's part of ``sub.open``: the topics (a ``ticket:<ref>`` the viewer may not see is ``not_found``),
        the cursor (``sub.reset`` unless it is this principal's, of this epoch and in the window) and the boundary."""
        service = call.service
        service.sync(check=True)  # a foreign append is found before the boundary is fixed
        journal = service.journal
        if journal is None or journal.stale:
            raise HostError("workspace.unavailable", "the subscription journal is not available")
        viewer = Viewer.of(call)
        topics, tickets = set(), set()
        for topic in args["topics"]:
            if topic.startswith("ticket:"):
                tickets.add(viewer.ticket(service.store, topic[len("ticket:") :]).uid)
            else:
                topics.add(topic)
        if "after" in args:
            pos = journal.position(args["after"], call.principal)
            if pos is None:
                raise HostError("sub.reset", "the cursor is too old, of another scope or epoch: take a snapshot")
        else:
            pos = journal.head
        return {"topics": sorted(topics), "tickets": sorted(tickets), "pos": pos, "detail": bool(args.get("detail"))}

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
            **({"runner": terms.runner_info(call.service, call.viewer)} if call.column.name in ("P", "G") else {}),
            **({"relay": self._relay_caps(call.service)} if call.column.name == "R" else {}),
        }

    @staticmethod
    def _relay_caps(service: Any) -> dict[str, Any]:
        """``whoami.relay`` (column R only): the person-event writes this host serves on R (the framing table's write
        kinds) and the live R6 gate, read now (the same source as ``host.status.relay.gate``). §11A.17.2: ``dev``
        only while :func:`orch.host.devmode.dev_gate` passes for this workspace now (fresh, never cached); absent
        everywhere else. Nothing else."""
        from orch.relay import worker as relay_worker
        from orch.relay.framing import WRITE_OP_VERSIONS

        from .devmode import dev_gate

        out: dict[str, Any] = {"ops": dict(WRITE_OP_VERSIONS), "r6_ready": relay_worker.r6_ready()}
        gate = dev_gate(service)
        if getattr(service, "dev", None) is not None:
            service.dev_ok = gate is not None  # the phone reads whoami before every review: P5's cache follows it
        if gate is not None:
            out["dev"] = {"sim_convenience": True, "sandbox": gate.sandbox_id[:8]}
        return out

    def status(self, call: Call) -> dict[str, Any]:
        from orch.cli.main import VERSION_LINE

        data: dict[str, Any] = {"version": VERSION_LINE[:64], "tier": call.service.entry.tier}
        if call.column.name in {"P", "G"}:  # class N gets only version and tier
            data["state"] = call.service.state
            if self.started is not None:
                data["uptime_s"] = int(time.monotonic() - self.started())
            from .relayhook import status as relay_status

            try:  # R5: host.status.relay (§11A.4), for a workspace with a relay directory; never fails the status
                relay = relay_status(call.service)
            except Exception as exc:  # noqa: BLE001 - reported, by class only
                relay = {"error": type(exc).__name__}
            if relay is not None:
                data["relay"] = relay
            dev = getattr(call.service, "dev", None)
            if dev is not None:  # §11A.17.2: only on a dev host (absent, not false, elsewhere)
                data["dev"] = {"sim_convenience": True, "sandbox": dev.id8}
        return data

    # -- registry ops
    def _registry(self, call: Call, redactor: Redactor, step: Any = None) -> dict[str, Any]:
        """§0.6 steps 8 to 11 of a registry op: a read (or dry run) runs directly, a write through the outcome store."""
        import orch.ops as ops

        operation = ops.get(call.frame["op"])
        args, session, specs = self._step8(call, operation, redactor)
        opts = call.frame.get("opts") or {}
        dry_run = bool(opts.get("dry_run"))
        idem_given = opts.get("idem") if operation.is_write and not dry_run else None
        digests, digest_args = self.bind_files(call, idem_given, specs)
        idem = request_idem(operation.name, opts)  # step 9 starts: idem.required (dry_run support was checked first)
        ctx = self._context(call, session, dry_run, self._file_bytes(call, specs, args), step)
        if idem is None:  # a read or a dry run: no outcome record, and the store's deny-all append check stays
            self._state(call.service, False)
            return self._run(operation, args, ctx, call)  # a dry run keeps its blobs
        result = self._write(call, operation, args, ctx, idem, digests, digest_args, redactor)
        # committed (or the replay of a commit): the blobs it used are consumed; a refusal raised above keeps them
        call.service.blobs.consume(call.principal, [spec["blob"] for spec in specs.values() if "blob" in spec])
        return result

    @staticmethod
    def bind_files(call: Call, idem: str | None, specs: Mapping[str, Any]) -> tuple[dict[str, str], frozenset[str]]:
        """The binding's file digests (§0.7): a ``{blob}`` must be a complete blob of this principal in this
        workspace (F9), else ``blob.invalid`` exactly as an unknown id."""
        service = call.service
        return service.outcomes.bind_files(
            call.principal, idem, specs, blob_digest=lambda blob: service.blobs.digest(call.principal, blob)
        )

    def _step8(self, call: Call, operation: Operation, redactor: Redactor) -> tuple[dict[str, Any], Any, dict]:
        """Step 8: the args against the schema, the host's own refusals, the session label, the ``files``."""
        from orch.cli.main import _SESSION
        from orch.cli.render import redact
        from orch.ops.errors import OrchError
        from orch.ops.inputs import check_input

        args = dict(call.frame["args"])
        try:
            check_input(operation, args, lambda text: redact(text, redactor.secrets)[:200])
        except OrchError as e:
            raise host_error(e, operation) from None
        for arg, why in HOST_REFUSED_ARGS.get(operation.name, {}).items():
            if args.get(arg):
                raise HostError("op.not_allowed", why)
        session = (call.frame.get("auth") or {}).get("session")
        if session is not None and not _SESSION.search(session):
            raise HostError("invalid.input", "auth.session is not a session id (s_<ULID>, then at most three .n)")
        return args, session, self._file_specs(operation, call.frame, args)

    def _write(
        self,
        call: Call,
        operation: Operation,
        args: dict[str, Any],
        ctx: Any,
        idem: str,
        digests: dict[str, str],
        digest_args: frozenset[str],
        redactor: Redactor,
    ) -> dict[str, Any]:
        """Steps 9 to 11 of a write: the outcome record (replay, conflict, expiry), the workspace state, the op. The
        handler announces its events (``ctx.plan``) and only those may be appended (:class:`PlanGuard`). The result,
        a refusal and a result rebuilt from the log are all redacted before the record stores them."""
        service = call.service

        def apply(intent: Any) -> dict[str, Any]:
            guard = PlanGuard(service.outcomes, intent)
            ctx.plan = guard.plan
            service.store.append_check = guard.check
            try:
                return redactor.obj(self._run(operation, args, ctx, call))
            except HostError as exc:
                raise redactor.error(exc) from None
            finally:
                service.store.append_check = deny_append

        return service.outcomes.execute(
            call.principal,
            idem,
            operation.name,
            self._normalise(operation, args, service.store),
            apply,
            planned=None,
            files=digests,
            session=(call.frame.get("auth") or {}).get("session"),
            digest_args=digest_args,
            before=lambda: self._state(service, True),
            redactor=redactor,
        )

    @staticmethod
    def _file_specs(operation: Operation, frame: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        """``files`` (§0.3): each entry names an argument the registry marks ``x-file`` that carries the file's
        logical name in ``args``; every ``x-file`` argument given comes with its entry (the host never reads a path a
        client names); inline bytes are at most 512 KiB per request (§0.10). Anything else is ``blob.invalid``."""
        specs = dict(frame.get("files") or {})
        marked = file_args(operation)
        for arg in specs:
            if arg not in marked or not isinstance(args.get(arg), str):
                raise HostError("blob.invalid", f"files.{arg} names no file argument of {operation.name}")
        for arg in sorted(marked):
            if args.get(arg) is not None and arg not in specs:
                raise HostError("blob.invalid", f"{arg} is a file: send its bytes in files.{arg}")
        names = [str(args[arg]) for arg in specs]
        if len(set(names)) != len(names):  # two arguments would share one byte string in ctx.files
            raise HostError("blob.invalid", "two files arguments name the same file: give each its own name")
        inline = sum(decoded_len(spec["b64"]) for spec in specs.values() if isinstance(spec, Mapping) and "b64" in spec)
        if inline > INLINE_MAX:
            raise HostError("blob.invalid", f"inline files are at most {INLINE_MAX} bytes per request: upload a blob")
        return specs

    @staticmethod
    def _file_bytes(call: Call, specs: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, bytes]:
        """The uploaded bytes by logical name (``-`` for stdin): what ``ctx.files`` gives the handler. A ``{digest}``
        has no bytes (M1): it only binds a replay, and an op that has to run again refuses it."""
        out = {}
        for arg, spec in specs.items():
            if "b64" in spec:
                try:
                    out[str(args[arg])] = base64.b64decode(spec["b64"], validate=True)
                except (binascii.Error, ValueError):  # bind_files refused it already
                    raise HostError("blob.invalid", f"files.{arg} is not valid base64") from None
            elif "blob" in spec:
                out[str(args[arg])] = call.service.blobs.read(call.principal, spec["blob"])
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

    def _context(
        self,
        call: Call,
        session: str | None,
        dry_run: bool,
        files: dict[str, bytes],
        step: Any = None,
        *,
        prepare: Any = None,
    ) -> Any:
        from orch.ops import Context
        from orch.ops.runtime import Workspace

        service = call.service
        caps = service.caps_for(call.principal)  # pins up to date; git reads run under the caller (F10)
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
            unserved=frozenset(NOT_SERVED),
            wait_step=step,
            prepare=prepare,
        )

    def _run(self, operation: Operation, args: dict[str, Any], ctx: Any, call: Call) -> dict[str, Any]:
        from orch.cli import main as cli
        from orch.cli.parser import Parsed
        from orch.cli.session import FileRecords
        from orch.cli.store_hooks import hooks_for
        from orch.ops.errors import OrchError

        store = call.service.store
        hooks = hooks_for(store)
        records = ScopedRecords(
            FileRecords(store.state_dir / "sessions", fs=store.fs), call.principal
        )  # the store's pinned root (#564)
        parsed = Parsed(operation, dict(args), dry_run=ctx.dry_run)
        try:
            res = cli.run(parsed, ctx, records, hooks)  # type: ignore[arg-type]
        except OrchError as e:
            e = cli.stop_rule(operation, parsed.args, ctx, ctx.session, records, hooks, e, ctx.now)  # type: ignore[arg-type]
            raise host_error(e, operation) from None
        if ctx.session and operation.is_write and not ctx.dry_run:
            records.succeeded_write(ctx.session)
        return result_dict(res)
