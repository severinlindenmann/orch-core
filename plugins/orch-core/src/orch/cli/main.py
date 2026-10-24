"""Entry point for the `orch` command: parse, check, run, render, and exit with the contract's code."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TextIO

from orch import schema
from orch._build import build_info, version_line
from orch.cli import meta, parser, render
from orch.cli.args import arg_specs
from orch.cli.errors import UsageError
from orch.cli.session import STOP_AFTER, FileRecords, MemoryRecords, fingerprint
from orch.ops import Context, NotImplementedYet, Operation, Result
from orch.ops.errors import ERRORS, GLOBAL_ERRORS, OrchError

__all__ = ["Hooks", "main", "run", "stop_rule"]

VERSION_LINE = version_line()
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


def _check_input(op: Operation, args: dict[str, Any], secrets: list[str]) -> None:
    from orch.ops.inputs import check_input

    check_input(op, args, lambda text: _cap(text, secrets, 200))


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


def run_hosted(parsed: parser.Parsed, ctx: Context, target: Any) -> Result:
    """The same call as a request to the host (H4b, :mod:`orch.hostclient`). The checks that need no store stay here
    (input, who, grant shape), so a bad call fails the same way in both modes and without a round trip; the stop rule,
    the session dedup and the declared-error check are the host's (it runs :func:`run`)."""
    from orch import hostclient

    op, args = parsed.op, parsed.args
    _check_input(op, args, _secrets(ctx.grant))
    if op.who == "human" and not ctx.human_presence:
        raise OrchError("human_only", f"{op.cli}: human only")
    if op.who != "human":
        if ctx.grant is not None and not _GRANT.fullmatch(ctx.grant):
            raise OrchError("grant.required", "ORCH_GRANT is malformed")
        if op.who == "agent" and not ctx.grant:
            raise OrchError("grant.required", f"{op.cli} needs a grant")
    return hostclient.execute(parsed, ctx, target)


def stop_rule(
    op: Operation,
    args: dict[str, Any],
    ctx: Context | None,
    session: str | None,
    records: MemoryRecords,
    hooks: Hooks,
    e: OrchError,
    now: Callable[[], float],
) -> OrchError:
    """Count a refusal for the stop rule (F1 10.4 item 6); the third identical one within 15 minutes is ``stop``.
    Shared by the CLI and the host (H4a1), which passes records keyed by its principal."""
    if not session or not _is_refusal(e):
        return e
    from orch.ops.runtime import keyed

    kargs = _with_ticket(op, ctx, keyed(ctx, args), hooks) if ctx is not None else args
    norm = _normalised(op, kargs, hooks)
    try:
        if records.refused(session, op.name, norm, e.code, now()) >= STOP_AFTER:
            return OrchError("stop")
    except OrchError as broken:  # unreadable records fail closed: say so instead of the refusal
        return broken
    return e


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


def _progress(line: str, err: TextIO) -> None:
    """A progress line on stderr (stdout carries only the result, F1 10.4 item 1)."""
    from orch.canon import clean_line

    try:
        err.write(clean_line(line)[:200] + "\n")
        err.flush()
    except (OSError, ValueError):
        pass


def entry() -> int:
    """The ``orch`` command (console script and ``python -m orch``). Ctrl-C (a passphrase prompt, a wait) ends calmly:
    one line, exit 130, no traceback. ``main`` itself still lets KeyboardInterrupt through (tests simulate a kill)."""
    try:
        return main()
    except KeyboardInterrupt:
        try:
            sys.stderr.write("Cancelled.\n")
        except (OSError, ValueError):
            pass
        return 130


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
    if args[:1] == ["host"]:  # the host service and its control socket (orch.host.cli), not a registry op
        from orch.host.cli import main as host_main

        return host_main(args[1:], env=env, out=out)
    if args[:1] == ["dashboard"]:  # the dashboard addon's entry point (orch.cli.dashboard): not a registry op
        from orch.cli.dashboard import main as dashboard_main

        return dashboard_main(args[1:], env=env, err=err)
    if args[:2] == [
        "addon",
        "manifest",
    ]:  # the manifests shipped in the package (orch.addons.builtin): offline, no store
        from orch.addons.builtin import main as manifest_main

        return manifest_main(args[2:], out=out, err=err)
    if args[:1] == ["setup"]:  # one command to a working workspace and its onboarding page (B9): not a registry op
        from orch.setup.flow import main as setup_main

        return setup_main(args[1:], env=env, out=out, err=err)
    if args[:1] == ["runner"]:  # dashboard execution and the runner service (orch.runner.cli, H11a): not a registry op
        from orch.runner.cli import main as runner_main

        return runner_main(args[1:], env=env, out=out)
    if args[:2] == ["keys", "rotate"]:  # C8 break-glass: a new workspace key epoch, owner at the terminal (host op)
        from orch.cli.hub import main as hub_main

        return hub_main(["rotate", *args[2:]], env=env, out=out, err=err, now=now)
    if args[:1] == ["keys"]:  # custody of the person's device key (H5c, orch-v2-host §0.8.6): not a registry op
        from orch.custody.cli import main as keys_main

        return keys_main(args[1:], env=env, out=out, err=err)
    if args[:1] == ["records"]:  # records commit and push (H11e): the person-side tool, not a registry op
        from orch.records.cli import main as records_main

        return records_main(args[1:], env=env, out=out)
    if args[:1] == ["worktree"]:  # direct-mode worktrees (H11d, D-P2-38): the person-side tool, not a registry op
        from orch.worktree_cli import main as worktree_main

        return worktree_main(args[1:], env=env, out=out, err=err, now=now)
    if args[:1] == ["hub"]:  # join and leave a hub, selective mode (V3 C2a): a host client, not a registry op
        from orch.cli.hub import main as hub_main

        return hub_main(args[1:], env=env, out=out, err=err, now=now)
    if args[:1] == ["addon"] and args[1:2] in (["login"], ["logout"]):
        # the key an add-on may hold (V3 G6): the person's own terminal only, not a registry op and not a host op
        from orch.cli.addon_credential import main as addon_credential_main

        return addon_credential_main(args[1:], env=env, out=out, err=err, now=now)
    if args[:1] == ["addon"] and args[1:2] in (
        ["install"], ["update"], ["remove"], ["setup"], ["new"], ["pack"], ["dev"],
    ):  # fmt: skip
        # add-ons from a marketplace (V3 G3) and the author tools (G5): host clients that sign at the person's terminal
        from orch.cli.addon_market import main as addon_market_main

        return addon_market_main(args[1:], env=env, out=out, err=err, now=now)
    if args[:1] == ["market"]:  # the add-on marketplace (V3 G2): a host client, not a registry op
        from orch.cli.market import main as market_main

        return market_main(args[1:], env=env, out=out, err=err, now=now)
    if args[:1] == ["space"]:  # files shared through a hub space (V3 C13): a host client, not a registry op
        from orch.cli.space import main as space_main

        return space_main(args[1:], env=env, out=out, err=err, now=now)
    if args[:1] == ["peer"]:  # ws->ws through the hub (V3 C12): a host client, not a registry op
        from orch.cli.peer import main as peer_main

        return peer_main(args[1:], env=env, out=out, err=err, now=now)
    if args[:1] == ["publish"]:  # the Publish add-on (P5): a host client, not a registry op
        from orch.cli.publish import main as publish_main

        return publish_main(args[1:], env=env, out=out, err=err, now=now)
    if args[:1] == ["sign"]:  # a hand-off signed at the person's terminal (H5b): a host client, not a registry op
        from orch.host.handoff import main as sign_main

        return sign_main(args[1:], env=env, out=out, err=err)
    if args[:2] == ["quick", "start"]:  # B11 step (ii): a person's quick task on one review (a host client, like sign)
        from orch.host.quickstart import main as start_main

        return start_main(args[2:], env=env, out=out, err=err)
    if args[:2] == ["quick", "add"]:
        # H11f: the composite (new, then set labels=quick); `quick list` is a registry read like any other
        def step(argv: list[str], step_out: TextIO, step_err: TextIO) -> int:
            return main(argv, env=env, stdout=step_out, stderr=step_err, records=records, now=now, hooks=hooks)

        from orch.cli.quick import main as quick_main

        return quick_main(args[1:], env=env, out=out, err=err, run=step, now=now)
    from orch.ops.caps import Files
    from orch.ops.runtime import Workspace  # the workspace is found and opened only when something asks for it

    render.new_nonce()
    workspace = Workspace(env, now, files=Files.direct())
    own_records = records is None  # opened here, so closed here (its pinned root fd)
    try:
        if hooks is None or records is None:
            from orch.cli.store_hooks import workspace_hooks, workspace_records
        hooks = hooks or workspace_hooks(workspace)
        records = records if records is not None else workspace_records(workspace, _DEFAULT_RECORDS)

        if args in (["--version"], ["--version", "--json"], ["--json", "--version"]):
            out.write((json.dumps(build_info()) if "--json" in args else VERSION_LINE) + "\n")
            return 0
        grant_env = env.get("ORCH_GRANT") or None
        secrets = _secrets(grant_env)
        head_args = args[: args.index("--")] if "--" in args else args
        as_json = "--json" in head_args or env.get("ORCH_OUTPUT") == "json"

        def emit(text: str, stream: TextIO) -> None:
            stream.write(render.redact(text, secrets) + "\n")

        hosted = False

        def fail(e: OrchError, op: Operation | None) -> int:
            envelope = render.error_envelope(e, op)
            if as_json:
                emit(render.dumps(envelope), out)
            else:
                emit(render.error_text(envelope, render.use_color(err, as_json, env)), err)
            wire = getattr(e, "wire_exit", None)  # a refusal from the host keeps the exit the socket sent (§0.6)
            return wire if wire is not None else render.error_exit(e.code)

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
                progress=lambda line: _progress(line, err),
            )
            from orch import hostclient

            target = None if op.name in hostclient.LOCAL_OPS else hostclient.locate(env, workspace)
            if target is not None:
                hosted = True
                hostclient.forget_host()
                try:
                    res = run_hosted(parsed, ctx, target)
                finally:
                    if (stale := hostclient.stale_host_warning()) is not None:
                        err.write(stale + "\n")
            else:
                res = run(parsed, ctx, records, hooks)
            # a hook outside a workspace is silent
            if not (isinstance(res.data, dict) and res.data.get("silent") is True):
                emit(render.dumps(render.result_envelope(res)) if as_json else render.result_text(op, res), out)
            if session and op.is_write and not parsed.dry_run and not hosted:
                records.succeeded_write(session)
            return res.exit
        except OrchError as e:
            session = env.get("ORCH_SESSION") or None
            if op is not None and parsed is not None and not hosted:
                e = stop_rule(op, parsed.args, ctx, session, records, hooks, e, now)
            return fail(e, op)
        except render.TemplateError as e:
            return fail(OrchError("internal", _cap(f"output template: {e}", secrets, 200)), op)
        except Exception as e:  # the safety net: an unexpected failure is still an envelope
            return fail(OrchError("internal", _cap(f"{type(e).__name__}: {e}", secrets, 200)), op)
    finally:
        # Every fd this call opened (the store's root and cached directory fds, the index and pin databases, the
        # records' pinned root) is released here, deterministically: left to the cyclic GC they pile up in a
        # long-lived process that runs many calls (the host, a test worker) past FD_SETSIZE.
        workspace.close()
        if own_records and isinstance(records, FileRecords):
            records.close()


_DEFAULT_RECORDS = MemoryRecords()

__all__ += ["UsageError"]
