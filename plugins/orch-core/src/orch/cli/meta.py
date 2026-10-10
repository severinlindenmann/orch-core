"""``orch describe`` and ``orch help``, and ``orch <cmd> --help``: all generated from the registry."""

from __future__ import annotations

from typing import Any

import orch.ops as ops
from orch.cli.args import arg_specs, usage
from orch.ops import Context, Operation, Result
from orch.ops.errors import ERRORS, OrchError
from orch.ops.workflows import WORKFLOWS

__all__ = ["describe", "help", "op_help", "overview"]


def _arg_lines(op: Operation) -> list[str]:
    lines = []
    for a in arg_specs(op):
        label = a.metavar if a.positional else (f"{a.flag} {a.metavar}" if a.kind != "bool" else str(a.flag))
        if a.short and not a.positional:
            label = f"-{a.short}, {label}"
        extra = ""
        if a.choices:
            extra += f" ({'|'.join(a.choices)})"
        if a.default is not None:
            extra += f" [default {a.default}]"
        if a.required and not a.positional:
            extra += " (required)"
        lines.append(f"  {label}  {a.help}{extra}")
    if op.is_write:
        lines.append("  --dry-run  say what would happen, append nothing")
    return lines


def op_help(op: Operation) -> str:
    """``orch <cmd> --help``: the summary, the usage line and the arguments."""
    return "\n".join([op.summary, f"usage: {usage(op)}", *_arg_lines(op), "more: orch describe " + op.name])


def _error_rows(op: Operation) -> list[dict[str, Any]]:
    return [
        {
            "code": e["code"],
            "exit": ERRORS[e["code"]].exit,
            "retryable": e["retryable"],
            "hint": e["hint"],
            "fix": e["fix"]["argv"],
        }
        for e in op.errors
    ]


def _group_line(group: str, members: list[Operation]) -> str:
    seen: dict[str, list[str]] = {}
    for op in members:
        seen.setdefault(op.words[0], []).append(" ".join(op.words[1:]))
    parts = []
    for head, subs in seen.items():
        if "" in subs:
            parts.append(head)
        subs = [x for x in subs if x]
        if len(subs) == 1:
            parts.append(f"{head} {subs[0]}")
        elif subs:
            parts.append(f"{head} {'|'.join(subs)}")
    return f"{group}: {', '.join(parts)}"


def overview() -> list[str]:
    return [_group_line(g, m) for g, m in ops.iter_groups()]


def describe(ctx: Context, args: dict[str, Any]) -> Result:
    words = [w for w in args.get("cmd", [])]
    if not words:
        commands = [
            {"name": o.name, "cli": o.cli, "group": o.group, "who": o.who, "summary": o.summary} for o in ops.all()
        ]
        return Result(
            data={"target": "all", "commands": commands},
            hints=["orch describe CMD"],
            lines=overview(),
        )
    op = ops.resolve(words)
    if op is None:
        raise OrchError("not_found", f"no command {' '.join(words)!r}", hint="orch describe lists the commands")
    errors = _error_rows(op)
    data = {
        "target": op.name,
        "name": op.name,
        "cli": f"orch {op.cli}",
        "summary": op.summary,
        "who": op.who,
        "usage": usage(op),
        "pre": op.pre,
        "emits": op.emits,
        "input": op.input,
        "output": op.output,
        "errors": errors,
    }
    lines = [op.summary, f"usage: {usage(op)}", f"who: {op.who} · emits: {','.join(op.emits) or '-'}"]
    if op.pre:
        lines.append(f"pre: {' '.join(op.pre)}")
    arg_lines = _arg_lines(op)
    if arg_lines:
        lines += ["args:", *arg_lines]
    lines.append("errors:")
    for e in errors:
        retry = " retry" if e["retryable"] else ""
        lines.append(f"  {e['code']} (exit {e['exit']}{retry}): {e['hint']} | fix: {' '.join(e['fix'])}")
    lines.append("out: " + op.output["text"].replace("\n", " / "))
    return Result(data=data, lines=lines)


def help(ctx: Context, args: dict[str, Any]) -> Result:  # noqa: A001
    topic = args.get("workflow")
    if not topic:
        lines = [f"{name}: {one}" for name, (one, _steps) in WORKFLOWS.items()]
        lines += [
            "commands: orch describe",
            "flags: --json (or ORCH_OUTPUT=json) on every command, --dry-run on every write",
            "REF left out means your current claim",
        ]
        return Result(
            data={"topic": "all", "workflows": list(WORKFLOWS)},
            hints=["orch help work"],
            lines=lines,
        )
    if topic not in WORKFLOWS:
        raise OrchError("not_found", f"no workflow {topic!r}", hint=f"workflows: {' '.join(WORKFLOWS)}")
    one, steps = WORKFLOWS[topic]
    lines = [one]
    rows = []
    for i, (name, call) in enumerate(steps, 1):
        op = ops.get(name)
        lines.append(f"{i}. {call}  # {op.summary.split(':')[0].split(';')[0].rstrip('.')}")
        rows.append({"op": name, "call": call})
    return Result(data={"topic": topic, "steps": rows}, hints=["orch describe CMD"], lines=lines)
