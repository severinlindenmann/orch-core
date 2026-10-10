"""The golden outputs: every case is (relative file name, exact text)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import orch.ops as ops
from orch.cli import render
from orch.ops.errors import ERRORS, EXIT_CODES, OrchError
from orch.ops.workflows import WORKFLOWS
from tests.cli.conftest import run_cli


def _pretty(text: str) -> str:
    """JSON goldens are pretty-printed so a one-field change is a one-line diff."""
    return json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n"


def cases() -> Iterator[tuple[str, str]]:
    for name, text in _raw_cases():
        yield name, _pretty(text) if name.endswith(".json") else text


def _raw_cases() -> Iterator[tuple[str, str]]:
    for op in ops.all():
        yield f"help/{op.name}.txt", run_cli(*op.words, "--help").out
        yield f"describe/{op.name}.txt", run_cli("describe", op.name).out
        yield f"describe/{op.name}.json", run_cli("describe", op.name, "--json").out
    yield "describe/_all.txt", run_cli("describe").out
    yield "describe/_all.json", run_cli("describe", "--json").out
    yield "help/_all.txt", run_cli("help").out
    yield "help/_all.json", run_cli("help", "--json").out
    for name in WORKFLOWS:
        yield f"help/workflow.{name}.txt", run_cli("help", name).out
        yield f"help/workflow.{name}.json", run_cli("help", name, "--json").out
    for code in ERRORS:
        env = render.error_envelope(OrchError(code))
        yield f"errors/{code}.txt", render.error_text(env) + "\n"
        yield f"errors/{code}.json", render.dumps(env) + "\n"
    table = "\n".join(f"{n} {w}" for n, w in EXIT_CODES.items())
    codes = "\n".join(f"{c} {s.exit} retry:{str(s.retryable).lower()}" for c, s in ERRORS.items())
    yield "errors/_exit-codes.txt", table + "\n\n" + codes + "\n"
    yield "errors/_human-only.json", run_cli("approve", "plan", "--json").out
    yield "errors/_usage.txt", run_cli("task", "done").err
    yield "errors/_unknown.txt", run_cli("frobnicate").err
    yield "errors/_not-implemented.txt", run_cli("doctor").err
