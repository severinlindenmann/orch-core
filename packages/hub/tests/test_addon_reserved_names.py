"""FB15: the hub's reserved add-on names are core's (one list: core's schema file); a drift fails here."""

import json
from pathlib import Path

from orch_hub.contract import phase4 as P4


def test_the_reserved_names_equal_cores_schema_list():
    p = Path(__file__).resolve().parents[2] / "core/src/orch/schema/schemas/common.json"
    core = json.loads(p.read_text())["$defs"]["addonName"]["not"]["enum"]
    assert P4.RESERVED_ADDON_NAMES == frozenset(core)
