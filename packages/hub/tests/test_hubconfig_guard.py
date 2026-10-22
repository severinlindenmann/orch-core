"""Structural guard (orch-hub#30): no function in src/orch_hub may give a ``dev`` / ``dev_mode`` parameter a default.
A construction that forgets the argument must fail with a TypeError, never run in dev mode."""

from __future__ import annotations

import ast
from pathlib import Path

import orch_hub

SRC = Path(orch_hub.__file__).parent
NAMES = {"dev", "dev_mode"}


def _defaulted(fn: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> list[str]:
    a = fn.args
    pos = a.posonlyargs + a.args
    out = [p.arg for p, d in zip(pos[len(pos) - len(a.defaults) :], a.defaults, strict=True)]
    out += [p.arg for p, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is not None]
    return [n for n in out if n in NAMES]


def test_no_dev_parameter_has_a_default():
    bad = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                for n in _defaulted(node):
                    bad.append(f"{path.relative_to(SRC)}:{node.lineno} {getattr(node, 'name', '<lambda>')}({n}=...)")
    assert not bad, "dev/dev_mode must be a required argument:\n" + "\n".join(bad)


def test_guard_catches_a_default():
    fn = ast.parse("def f(a, *, dev_mode: bool = True): ...").body[0]
    assert _defaulted(fn) == ["dev_mode"]
    fn = ast.parse("def f(dev=False, b=1): ...").body[0]
    assert _defaulted(fn) == ["dev"]
    fn = ast.parse("def f(*, dev: bool): ...").body[0]
    assert _defaulted(fn) == []
