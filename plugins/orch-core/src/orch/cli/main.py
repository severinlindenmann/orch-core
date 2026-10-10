"""Entry point for the `orch` command: parse, check, run, render, and exit with the contract's code."""

from __future__ import annotations

import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any, TextIO

from jsonschema import Draft202012Validator

import orch.ops as ops
from orch import __version__, schema
from orch.cli import meta, parser, render
from orch.cli.errors import UsageError
from orch.cli.session import STOP_AFTER, MemoryRecords
from orch.ops import Context, NotImplementedYet, Operation, Result
from orch.ops.errors import ERRORS, GLOBAL_ERRORS, OrchError

__all__ = ["main", "run"]

VERSION_LINE = "orch v2 (in development)"
_SESSION = re.compile(schema.load("common")["$defs"]["sessionId"]["pattern"])
_GRANT = re.compile(r"^gr_[0-7][0-9A-HJKMNP-TV-Z]{25}\.[A-Za-z0-9_-]{43}$")


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
        what = e.message
    else:
        what = e.message[:100]
    return f"{where}: {what}" if where else what


def _check_input(op: Operation, args: dict[str, Any]) -> None:
    errors = sorted(Draft202012Validator(op.input).iter_errors(args), key=lambda e: (list(map(str, e.path)), e.message))
    if errors:
        raise OrchError("invalid.input", f"{op.cli}: {_short_error(errors[0])}")


def _check_who(op: Operation, ctx: Context) -> None:
    if op.who == "human" and not ctx.human_presence:
        raise OrchError("human_only", f"{op.cli}: human only")
    if op.who == "agent":
        if not ctx.grant:
            raise OrchError("grant.required", f"{op.cli} needs a grant")
        if not _GRANT.match(ctx.grant):
            raise OrchError("grant.required", "ORCH_GRANT is malformed")


def _call(op: Operation, ctx: Context, args: dict[str, Any]) -> Result:
    try:
        return op.handler(ctx, args)
    except NotImplementedYet as e:
        raise OrchError("not_implemented", f"{op.cli}: not implemented yet") from e


def run(
    parsed: parser.Parsed,
    ctx: Context,
    records: MemoryRecords,
) -> Result:
    """Check and run one parsed call. Raises :class:`OrchError` for every refusal."""
    op, args = parsed.op, parsed.args
    _check_input(op, args)
    _check_who(op, ctx)
    session = ctx.session
    if session and records.stopped(session, op.name, args):
        raise OrchError("stop")
    caching = bool(session) and op.is_write and not ctx.dry_run
    if caching:
        hit = records.recall(session, op.name, args, ctx.now())  # type: ignore[arg-type]
        if hit is not None:
            return Result(**{**hit, "duplicate": True})
    try:
        res = _call(op, ctx, args)
    except OrchError as e:
        if e.code not in GLOBAL_ERRORS and not any(d["code"] == e.code for d in op.errors):
            raise OrchError("internal", f"{op.cli} returned undeclared error {e.code}") from e
        raise
    if caching:
        records.remember(session, op.name, args, ctx.now(), _plain(res))  # type: ignore[arg-type]
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
    return bool(cat) and cat.exit in (2, 3, 4, 5, 6) and e.code != "stop" and not cat.retryable


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    records: MemoryRecords | None = None,
    now: Callable[[], float] = time.time,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    records = records if records is not None else _DEFAULT_RECORDS

    if args == ["--version"]:
        out.write(VERSION_LINE + "\n")
        return 0
    tokens, flag_json = parser.split_globals(args)
    as_json = flag_json or env.get("ORCH_OUTPUT") == "json"
    secrets = [env.get("ORCH_GRANT", "")]
    secrets.append(secrets[0].partition(".")[2])

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
    try:
        if not tokens:
            tokens = ["help"]
        if any(t in ("--help", "-h") for t in tokens):
            tokens = [t for t in tokens if t not in ("--help", "-h")]
            if tokens:  # orch <cmd> --help; a bare orch --help is orch help
                emit(meta.op_help(parser.resolve_command(tokens)[0]), out)
                return 0
            tokens = ["help"]
        parsed = parser.parse(tokens)
        op = parsed.op
        session = env.get("ORCH_SESSION") or None
        if session and not _SESSION.search(session):
            raise OrchError("invalid.input", "ORCH_SESSION is not a session id (s_<ULID>, then at most three .n)")
        ctx = Context(
            session=session,
            grant=env.get("ORCH_GRANT") or None,
            human_presence=False,
            dry_run=parsed.dry_run,
            now=now,
            env=env,
        )
        res = run(parsed, ctx, records)
        if session:
            records.succeeded(session)
        text = render.dumps(render.result_envelope(res)) if as_json else render.result_text(op, res)
        emit(text, out)
        return res.exit
    except OrchError as e:
        session = env.get("ORCH_SESSION") or None
        if op is not None and parsed is not None and session and e.code != "stop" and _is_refusal(e):
            if records.refused(session, op.name, parsed.args, e.code) >= STOP_AFTER:
                e = OrchError("stop")
        return fail(e, op)
    except render.TemplateError as e:
        return fail(OrchError("internal", f"output template: {e}"), op)
    except Exception as e:  # the safety net: an unexpected failure is still an envelope
        return fail(OrchError("internal", f"{type(e).__name__}: {e}"[:200]), op)


_DEFAULT_RECORDS = MemoryRecords()

__all__ += ["UsageError", "__version__", "ops"]
