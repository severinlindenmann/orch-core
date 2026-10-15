from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

import orch.ops as ops
from orch.cli.main import main
from orch.cli.session import MemoryRecords

GOLDEN = Path(__file__).parent / "golden"
SESSION = "s_01J9ZK4Q7M3R8T2V6X0B5N1C9D"
GRANT = "gr_01J9ZK4Q7M3R8T2V6X0B5N1C9D." + "A" * 43


class Run:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err


def run_cli(*argv: str, env: dict[str, str] | None = None, records=None, now=None, hooks=None) -> Run:
    out, err = io.StringIO(), io.StringIO()
    kwargs = {} if now is None else {"now": now}
    if hooks is not None:
        kwargs["hooks"] = hooks
    code = main(
        list(argv),
        env=env or {},
        stdout=out,
        stderr=err,
        records=records if records is not None else MemoryRecords(),
        **kwargs,
    )
    return Run(code, out.getvalue(), err.getvalue())


@pytest.fixture(autouse=True)
def _declared_only():
    """The CLI contract tests (C5) run against declarations whose handlers are not written: every operation raises
    ``not_implemented`` here, whatever C6/C7 bound. The real handlers are tested in ``tests/ops``."""
    saved = {op.name: op.handler for op in ops.all() if op.name not in ("describe", "help")}  # C5's own handlers
    for name in saved:
        ops.bind(name, ops._unimplemented(name))
    yield
    for name, handler in saved.items():
        ops.bind(name, handler)


@pytest.fixture
def cli():
    return run_cli


@pytest.fixture
def bound():
    """Bind a handler to an operation for one test and put the original back."""
    saved: dict[str, object] = {}

    def bind(name: str, handler) -> None:
        saved.setdefault(name, ops.get(name).handler)
        ops.bind(name, handler)

    yield bind
    for name, handler in saved.items():
        ops.bind(name, handler)


@pytest.fixture
def golden(request):
    update = request.config.getoption("--update-golden")
    if update and os.environ.get("CI"):
        pytest.fail("--update-golden is refused when CI is set: golden files change in a reviewed commit")

    def check(rel: str, text: str) -> None:
        path = GOLDEN / rel
        if update:
            old = path.read_text(encoding="utf-8") if path.exists() else None
            if old != text:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                print(f"golden {'added' if old is None else 'changed'}: {rel}")
            return
        assert path.exists(), f"missing golden file {rel}; run pytest tests/cli/test_golden.py --update-golden -s"
        assert path.read_text(encoding="utf-8") == text, f"{rel} differs; run with --update-golden if intended"

    return check
