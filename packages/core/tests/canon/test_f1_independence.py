"""The F1 oracle is independent of ``orch``: its modules import only the standard library, ``cryptography`` and each
other, so a vector file is a known answer that two separate implementations agree on."""

import ast
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
ORACLES = sorted(HERE.glob("oracle_f1*.py"))
ALLOWED_THIRD_PARTY = {"cryptography"}


def _imports(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from ((a.name, 0) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield (node.module or ""), node.level


def test_the_oracle_modules_exist():
    assert {p.stem for p in ORACLES} >= {"oracle_f1", "oracle_f1_gate", "oracle_f1_world", "oracle_f1_signed"}


@pytest.mark.parametrize("path", ORACLES, ids=lambda p: p.stem)
def test_the_oracle_does_not_import_orch(path):
    for module, level in _imports(path):
        top = module.split(".")[0]
        if level:  # `from . import oracle_f1_gate`, `from .oracle_f1_world import ...`
            assert module == "" or module.startswith("oracle_f1"), (path.name, module)
            continue
        assert top != "orch", (path.name, module)
        assert top in sys.stdlib_module_names or top in ALLOWED_THIRD_PARTY, (path.name, module)
