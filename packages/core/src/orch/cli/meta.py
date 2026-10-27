"""``orch describe`` and ``orch help``, and ``orch <cmd> --help``: all generated from the registry."""

from __future__ import annotations

import json
from typing import Any

import orch.ops as ops
from orch.cli.args import arg_specs, usage
from orch.ops import Context, Operation, Result
from orch.ops.errors import ERRORS, OrchError
from orch.ops.workflows import WORKFLOWS

__all__ = ["describe", "help", "op_help", "overview"]


def _public_input(schema: dict[str, Any]) -> dict[str, Any]:
    """The input schema without the ``x-host`` properties: arguments only a host client sends (``task.done``'s
    receipt) are not advertised to the person or agent typing the command."""
    props = schema.get("properties") or {}
    hidden = {n for n, p in props.items() if isinstance(p, dict) and p.get("x-host") is True}
    if not hidden:
        return schema
    return {
        **schema,
        "properties": {n: p for n, p in props.items() if n not in hidden},
        "required": [n for n in schema.get("required", []) if n not in hidden],
    }


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


ADDON_GROUP = "Add-on commands"


def _on(ctx: Context) -> Any:
    return ctx.addons_on


def visible(op: Operation, ctx: Context) -> bool:
    """Is ``op`` listed? A core operation always; an add-on's only while the add-on is on. Without a workspace
    (``ctx.addons_on`` is ``None``) everything is listed."""
    on = _on(ctx)
    return op.addon is None or on is None or op.addon in on


def _off(op: Operation) -> OrchError:
    from orch.ops import addon_gate

    return OrchError("addon.off", addon_gate.off_message(op.addon or ""))


def overview(ctx: Context | None = None) -> list[str]:
    """One line per group. Without a filter that is the registry's groups; with one (a workspace) the add-on operations
    that are on leave their groups and form the ``Add-on commands`` group."""
    if ctx is None or _on(ctx) is None:
        return [_group_line(g, m) for g, m in ops.iter_groups()]
    lines = [
        _group_line(g, [o for o in m if o.addon is None])
        for g, m in ops.iter_groups()
        if any(o.addon is None for o in m)
    ]
    addon_ops = [o for o in ops.all() if o.addon is not None and visible(o, ctx)]
    return [*lines, _group_line(ADDON_GROUP, addon_ops)] if addon_ops else lines


def _field_info(ctx: Context, name: str, fname: str) -> Result:
    """``orch describe <addon>.<field>``: type, limits, ``set_by`` and gate, from the grant."""
    on = _on(ctx)
    if on is None:
        raise OrchError("not_found", f"no command {name}.{fname!r}", hint="orch describe lists the commands")
    if name not in on:
        from orch.ops import addon_gate

        raise OrchError("addon.off", addon_gate.off_message(name))
    spec = on[name].get(fname)
    if spec is None:
        have = ", ".join(sorted(on[name])) or "none"
        raise OrchError("not_found", f"the {name} add-on declares no field {fname}", hint=f"fields of {name}: {have}")
    set_by = sorted(spec["set_by"])
    limits = {
        k: (list(v) if isinstance(v, tuple | list) else v)
        for k, v in spec.items()
        if k not in ("type", "set_by", "gate")
    }
    gate = sorted(spec["gate"]) if spec.get("gate") else None
    data: dict[str, Any] = {
        "target": f"{name}.{fname}",
        "addon": name,
        "field": fname,
        "type": spec["type"],
        "limits": limits,
        "set_by": set_by,
        "usage": f"orch set REF {name}.{fname}=VALUE",
    }
    if gate is not None:
        data["gate"] = gate
    lim = " ".join(f"{k}={json.dumps(v)}" for k, v in sorted(limits.items()))
    lines = [
        f"{name}.{fname}: a field of the {name} add-on",
        f"usage: orch set REF {name}.{fname}=VALUE  (none clears)",
        f"type: {spec['type']}" + (f" · {lim}" if lim else ""),
        f"set_by: {', '.join(set_by)}" + (f" · gate: {', '.join(gate)}" if gate else ""),
    ]
    return Result(data=data, lines=lines)


def describe(ctx: Context, args: dict[str, Any]) -> Result:
    words = [w for w in args.get("cmd", [])]
    if not words:
        commands = [
            {"name": o.name, "cli": o.cli, "group": o.group, "who": o.who, "summary": o.summary}
            for o in ops.all()
            if visible(o, ctx)
        ]
        return Result(
            data={"target": "all", "commands": commands},
            hints=["orch describe CMD"],
            lines=overview(ctx),
        )
    op = ops.resolve(words)
    if (
        op is None
        and len(words) == 1
        and "." in words[0]
        and not any(o.words[0] == words[0].split(".")[0] for o in ops.all())
    ):
        name, _, fname = words[0].partition(".")
        return _field_info(ctx, name, fname)
    if op is not None and not visible(op, ctx):
        raise _off(op)
    if op is None and len(words) == 1:
        members = [o for o in ops.all() if o.words[0] == words[0].replace("_", "-") and len(o.words) > 1]
        if members and not any(visible(o, ctx) for o in members):
            raise _off(members[0])
        members = [o for o in members if visible(o, ctx)]
        if members:
            return Result(
                data={
                    "target": words[0],
                    "commands": [{"name": o.name, "cli": o.cli, "summary": o.summary} for o in members],
                },
                hints=["orch describe CMD"],
                lines=[f"orch {o.cli}: {o.summary}" for o in members],
            )
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
        "input": _public_input(op.input),
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
        fix = " ".join(e["fix"])
        tail = "" if fix == e["hint"] else f" | fix: {fix}"
        lines.append(f"  {e['code']} (exit {e['exit']}{retry}): {e['hint']}{tail}")
    lines.append("out: " + op.output["text"].replace("\n", " / "))
    return Result(data=data, lines=lines)


def _steps(steps: list[tuple[str, str]], ctx: Context) -> list[tuple[str, str]]:
    return [(n, c) for n, c in steps if visible(ops.get(n), ctx)]


def help(ctx: Context, args: dict[str, Any]) -> Result:  # noqa: A001
    topic = args.get("workflow")
    if not topic:
        shown = {n: w for n, w in WORKFLOWS.items() if _steps(w[1], ctx)}
        lines = [f"{name}: {one}" for name, (one, _s) in shown.items()]
        if _on(ctx) is not None and (addon_ops := [o for o in ops.all() if o.addon is not None and visible(o, ctx)]):
            lines.append(_group_line(ADDON_GROUP, addon_ops))
        lines += [
            "commands: orch describe",
            "flags: --json (or ORCH_OUTPUT=json) on every command, --dry-run on every write",
            "REF left out means your current claim",
        ]
        return Result(
            data={"topic": "all", "workflows": list(shown)},
            hints=["orch help work"],
            lines=lines,
        )
    if topic not in WORKFLOWS:
        raise OrchError("not_found", f"no workflow {topic!r}", hint=f"workflows: {' '.join(WORKFLOWS)}")
    one, steps = WORKFLOWS[topic]
    shown_steps = _steps(steps, ctx)
    if not shown_steps:  # every step belongs to an add-on that is off
        raise _off(ops.get(steps[0][0]))
    lines = [one]
    rows = []
    for i, (name, call) in enumerate(shown_steps, 1):
        op = ops.get(name)
        lines.append(f"{i}. {call}  # {op.summary.split(':')[0].split(';')[0].rstrip('.')}")
        rows.append({"op": name, "call": call})
    return Result(data={"topic": topic, "steps": rows}, hints=["orch describe CMD"], lines=lines)
