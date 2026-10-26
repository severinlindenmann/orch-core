"""Golden CLI outputs: the exact text and JSON of every operation's help and describe and of every error envelope.

``uv run pytest tests/cli/test_golden.py --update-golden`` rewrites the files; without the flag any diff, missing
file or stale file fails the test.
"""

from __future__ import annotations

import os

import pytest

from tests.cli.conftest import GOLDEN
from tests.cli.golden_cases import cases

ALL = dict(cases())


@pytest.mark.parametrize("name", sorted(ALL))
def test_golden(name, golden):
    golden(name, ALL[name])


def test_no_stale_golden_files(request):
    if request.config.getoption("--update-golden"):
        assert not os.environ.get("CI"), "--update-golden is refused when CI is set"
        for path in GOLDEN.rglob("*"):
            if path.is_file() and str(path.relative_to(GOLDEN)) not in ALL:
                print(f"golden removed: {path.relative_to(GOLDEN)}")
                path.unlink()
        return
    on_disk = {str(p.relative_to(GOLDEN)) for p in GOLDEN.rglob("*") if p.is_file()}
    assert on_disk == set(ALL), f"stale: {sorted(on_disk - set(ALL))}, missing: {sorted(set(ALL) - on_disk)}"


def test_golden_json_files_are_valid_envelopes():
    import json

    from orch import schema

    for name, text in ALL.items():
        if name.endswith(".json"):
            doc = json.loads(text)
            schema.validate("cli-error" if name.startswith("errors/") else "cli-result", doc)
