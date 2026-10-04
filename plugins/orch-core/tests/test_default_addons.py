import json
from pathlib import Path

import pytest

from orch.addons.check import static_problems
from orch.testing import FakeRunner, run_addon_contract

ADDONS = Path(__file__).resolve().parents[1] / "addons"


def recordings(folder: Path) -> list[dict]:
    out = []
    for path in sorted((folder / "tests" / "fixtures").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.extend(data if isinstance(data, list) else [data])
    return out


@pytest.mark.parametrize("name", ["github-reviews", "github-issues"])
def test_default_addon_passes_orch_addon_check(name):
    folder = ADDONS / name
    assert static_problems(folder) == []
    assert run_addon_contract(folder, runner=FakeRunner(recordings(folder), strict=False)) == []
