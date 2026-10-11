"""Entry point for the `orch` command: parse, check, run, render, and exit with the contract's code."""

from __future__ import annotations

import dataclasses
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TextIO

from jsonschema import Draft202012Validator

from orch import schema
from orch.cli import meta, parser, render
from orch.cli.args import arg_specs
from orch.cli.errors import UsageError
from orch.cli.session import STOP_AFTER, MemoryRecords, fingerprint
from orch.ops import Context, NotImplementedYet, Operation, Result
from orch.ops.errors import ERRORS, GLOBAL_ERRORS, OrchError

__all__ = ["Hooks", "main", "run"]

VERSION_LINE = "orch v2 (in development)"
_SESSION = re.compile(schema.load("common")["$defs"]["sessionId"]["pattern"])
_GRANT = re.compile(r"gr_[0-7][0-9A-HJKMNP-TV-Z]{25}\.[A-Za-z0-9_-]{43}")
# Not counted by the stop rule: they are about the command line, not about a refusal of the action.
_NOT_COUNTED = {"usage", "unknown_command", "grant.secret_in_args", "stop", "not_implemented", "internal"}


@dataclass
class Hooks:
    """What the CLI cannot know without the store (C6 wires them); the defaults are neutral."""

    #: The head ``seq`` of the ticket a write touches, part of the dedup key; ``None`` when unknown.
    head_seq: Callable[[Operation, dict[str, Any]], int | None] = field(default=lambda op, args: None)
    #: A ``REF`` as the ticket key (``43`` and ``DEMO-0043`` are the same ticket); used for stop rule and dedup.
    normalise_ref: Callable[[str], str] = field(default=lambda ref: ref)
    #: The key of the ticket a ref-less call resolves to (the session's single claim), or ``None``: it is part of the
    #: stop-rule fingerprint and the dedup key, so the same words on two claimed tickets are two calls (F1 10.4).
    session_ticket: Callable[[Operation, Context, dict[str, Any]], str | None] = field(
        default=lambda op, ctx, args: None
    )
    #: Raises ``grant.expired`` when the (well-formed) grant is expired, revoked or out of scope.
    grant_valid: Callable[[Context], None] = field(default=lambda ctx: None)
    #: The grant's ``verbs``: ``"agent"`` (every agent operation) or the list of operation names it allows.
    grant_verbs: Callable[[Context], Any] = field(default=lambda ctx: "agent")


def _secrets(grant: str | None) -> list[str]:
    return [grant, grant.partition(".")[2]] if grant else []


def _cap(text: str, secrets: list[str], n: int) -> str:
    """Redact first, then truncate: a cut can never leave a secret too short to match."""
    return render.redact(text, secrets)[:n]


def _short_error(e: Any) -> str:
    where = ".".join(str(p) for p in e.absolute_path)
    kind = e.validator
    if kind == "enum":
        what = "must be one of " + ", ".join(str(v) for v in e.validator_value)
    elif kind == "type":
        what = f"must be {e.validator_value}"
    elif kind == "pattern":
        what = "has the wrong form"
    elif kind in ("minimum", "maximum", "maxLength", "minLength"):
        what = f"fails {kind} {e.validator_value}"
    elif kind == "required":
        what = e.message.split(" is a required")[0] + " is required" if "is a required" in e.message else "is required"
    elif kind == "oneOf":
        names = [n for sub in e.validator_value for n in sub.get("required", [])]
        what = "give exactly one of " + ", ".join(names)
    else:
        what = "is not valid"
    return f"{where}: {what}" if where else what


def _check_input(op: Operation, args: dict[str, Any], secrets: list[str]) -> None:
    errors = sorted(Draft202012Validator(op.input).iter_errors(args), key=lambda e: (list(map(str, e.path)), e.message))
    if errors:
        raise OrchError("invalid.input", _cap(f"{op.cli}: {_short_error(errors[0])}", secrets, 200))


def _check_who(op: Operation, ctx: Context, args: dict[str, Any], hooks: Hooks) -> None:
    if op.who == "human":
        if not ctx.human_presence:
            raise OrchError("human_only", f"{op.cli}: human only")
        return
    if op.who == "read":
        return
    if ctx.grant is not None and not _GRANT.fullmatch(ctx.grant):
        raise OrchError("grant.required", "ORCH_GRANT is malformed")
    if op.who == "agent" and not ctx.grant:
        raise OrchError("grant.required", f"{op.cli} needs a grant")
    if op.who == "unattended" and not ctx.grant:
        for a in arg_specs(op):
            if a.needs_grant and a.name in args:
                raise OrchError("grant.required", f"{op.cli} {a.flag} needs a grant")
    if ctx.grant:
        hooks.grant_valid(ctx)
        verbs = hooks.grant_verbs(ctx)
        if verbs != "agent" and op.name not in verbs:  # F1 10.1: verbs are operation names, matched exactly
            raise OrchError("grant.verb", f"the grant does not cover {op.cli}")


def _call(op: Operation, ctx: Context, args: dict[str, Any]) -> Result:
    try:
        return op.handler(ctx, args)
    except NotImplementedYet as e:
        raise OrchError("not_implemented", f"{op.cli}: not implemented yet") from e


def _normalised(op: Operation, args: dict[str, Any], hooks: Hooks) -> dict[str, Any]:
    out = dict(args)
    if isinstance(out.get("ref"), str):
        out["ref"] = hooks.normalise_ref(out["ref"])
    return out


def _dedup_key(op: Operation, ctx: Context, args: dict[str, Any], hooks: Hooks, *, with_head: bool = True) -> str:
    grant_id = ctx.grant.partition(".")[0] if ctx.grant else "none"
    mode = "attended" if ctx.grant else "unattended"
    head = hooks.head_seq(op, args) if with_head and "ticket_exists" in op.pre else None
    return fingerprint(ctx.session, grant_id, mode, op.name, _normalised(op, args, hooks), head)


def _redact_obj(obj: Any, secrets: list[str]) -> Any:
    if isinstance(obj, str):
        return render.redact(obj, secrets)
    if isinstance(obj, list):
        return [_redact_obj(v, secrets) for v in obj]
    if isinstance(obj, dict):
        return {k: _redact_obj(v, secrets) for k, v in obj.items()}
    return obj


def _with_ticket(op: Operation, ctx: Context, args: dict[str, Any], hooks: Hooks) -> dict[str, Any]:
    """``args`` for stop rule and dedup key: a ticket-scoped call without a REF gets the ticket it resolves to."""
    if "ticket_exists" not in op.pre or isinstance(args.get("ref"), str):
        return args
    key = hooks.session_ticket(op, ctx, args)
    # marked: a retry of the REF-less call is a duplicate, the same call with the REF spelled out is a different one
    return {**args, "ref": key, "ref_from_claim": True} if key else args


def run(parsed: parser.Parsed, ctx: Context, records: MemoryRecords, hooks: Hooks | None = None) -> Result:
    """Check and run one parsed call. Raises :class:`OrchError` for every refusal."""
    hooks = hooks or Hooks()
    op, args = parsed.op, parsed.args
    secrets = _secrets(ctx.grant)
    _check_input(op, args, secrets)
    _check_who(op, ctx, args, hooks)
    session = ctx.session
    if session:  # a file argument is its content for the stop rule and the dedup key, not its name
        from orch.ops.runtime import keyed

        res_args = parsed.args
        args = _with_ticket(op, ctx, keyed(ctx, args), hooks)
    else:
        res_args = args
    if session and records.stopped(session, op.name, _normalised(op, args, hooks), ctx.now()):
        raise OrchError("stop")
    # a person's operation is never replayed from a record: each one is a fresh signature, never a cached result
    caching = bool(session) and op.is_write and op.who != "human" and not ctx.dry_run
    key = _dedup_key(op, ctx, args, hooks) if caching else ""
    if caching:
        hit = records.recall(session, key, ctx.now())  # type: ignore[arg-type]
        if hit is not None:
            return Result(**{**hit, "duplicate": True})
    base = _dedup_key(op, ctx, args, hooks, with_head=False) if caching else ""
    idem = records.attempt(session, base, ctx.now()) if caching else None  # type: ignore[arg-type]
    try:
        res = _call(op, dataclasses.replace(ctx, idem=idem) if caching else ctx, res_args)
    except OrchError as e:
        if e.code not in GLOBAL_ERRORS and not any(d["code"] == e.code for d in op.errors):
            raise OrchError("internal", f"{op.cli} returned undeclared error {e.code}") from e
        raise
    if caching:
        # recorded under the head *after* this call: a client retry sees that head, anything that happened since differs
        key = _dedup_key(op, ctx, args, hooks)
        records.remember(session, key, ctx.now(), _redact_obj(_plain(res), secrets), base)  # type: ignore[arg-type]
    return res


def _plain(res: Result) -> dict[str, Any]:
    return {
        "data": res.data,
        "key": res.key,
        "seq": res.seq,
        "cursor": res.cursor,
        "hints": list(res.hints),
        "lines": list(res.lines),
        "exit": res.exit,
    }


def _is_refusal(e: OrchError) -> bool:
    cat = ERRORS.get(e.code)
    return bool(cat) and e.code not in _NOT_COUNTED and cat.exit in (2, 3, 4, 5, 6) and not cat.retryable


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    records: MemoryRecords | None = None,
    now: Callable[[], float] = time.time,
    hooks: Hooks | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    from orch.ops.runtime import Workspace  # the workspace is found and opened only when something asks for it

    render.new_nonce()
    workspace = Workspace(env, now)
    if hooks is None or records is None:
        from orch.cli.store_hooks import workspace_hooks, workspace_records
    hooks = hooks or workspace_hooks(workspace)
    records = records if records is not None else workspace_records(workspace, _DEFAULT_RECORDS)

    if args == ["--version"]:
        out.write(VERSION_LINE + "\n")
        return 0
    grant_env = env.get("ORCH_GRANT") or None
    secrets = _secrets(grant_env)
    head_args = args[: args.index("--")] if "--" in args else args
    as_json = "--json" in head_args or env.get("ORCH_OUTPUT") == "json"

    def emit(text: str, stream: TextIO) -> None:
        stream.write(render.redact(text, secrets) + "\n")

    def fail(e: OrchError, op: Operation | None) -> int:
        envelope = render.error_envelope(e, op)
        if as_json:
            emit(render.dumps(envelope), out)
        else:
            emit(render.error_text(envelope, render.use_color(err, as_json, env)), err)
        return render.error_exit(e.code)

    op: Operation | None = None
    parsed: parser.Parsed | None = None
    ctx: Context | None = None
    try:
        for t in args:
            if _GRANT.search(t) or any(len(s) >= 8 and s in t for s in secrets):
                raise OrchError("grant.secret_in_args", "an argument contains a grant secret")
        sc = parser.scan(args)
        as_json = as_json or sc.json
        if sc.help:
            if sc.op is not None:
                emit(meta.op_help(sc.op), out)
                return 0
            args = ["help"]
        elif sc.op is None and not sc.rest:
            args = ["help"]
        parsed = parser.parse(args)
        op = parsed.op
        session = env.get("ORCH_SESSION") or None
        if session and not _SESSION.search(session):
            raise OrchError("invalid.input", "ORCH_SESSION is not a session id (s_<ULID>, then at most three .n)")
        ctx = Context(
            session=session,
            grant=grant_env,
            # a call that carries an agent's grant is never a person's; whether there is a person at a terminal is
            # the custody prompt's business (/dev/tty, fail closed), not an environment variable's
            human_presence="ORCH_GRANT" not in env,  # an empty value counts as set
            dry_run=parsed.dry_run,
            now=now,
            env={k: v for k, v in env.items() if k != "ORCH_GRANT"},
            workspace=workspace,
        )
        res = run(parsed, ctx, records, hooks)
        if not (isinstance(res.data, dict) and res.data.get("silent") is True):  # a hook outside a workspace is silent
            emit(render.dumps(render.result_envelope(res)) if as_json else render.result_text(op, res), out)
        if session and op.is_write and not parsed.dry_run:
            records.succeeded_write(session)
        return res.exit
    except OrchError as e:
        session = env.get("ORCH_SESSION") or None
        if op is not None and parsed is not None and session and _is_refusal(e):
            from orch.ops.runtime import keyed

            kargs = _with_ticket(op, ctx, keyed(ctx, parsed.args), hooks) if ctx is not None else parsed.args
            norm = _normalised(op, kargs, hooks)
            try:
                if records.refused(session, op.name, norm, e.code, now()) >= STOP_AFTER:
                    e = OrchError("stop")
            except OrchError as broken:  # unreadable records fail closed: say so instead of the refusal
                e = broken
        return fail(e, op)
    except render.TemplateError as e:
        return fail(OrchError("internal", _cap(f"output template: {e}", secrets, 200)), op)
    except Exception as e:  # the safety net: an unexpected failure is still an envelope
        return fail(OrchError("internal", _cap(f"{type(e).__name__}: {e}", secrets, 200)), op)


_DEFAULT_RECORDS = MemoryRecords()

__all__ += ["UsageError"]
