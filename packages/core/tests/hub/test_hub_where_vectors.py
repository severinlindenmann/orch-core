"""M4 S0: core replays the ``hub_where`` cases of the shared ``contracts/hub/vectors_hub_backup.json`` against its own
validator ``orch.hubclient.where``. The other checks of that file (backup file, tar, restore floor) are the hub's;
they are named here so a new check kind cannot go unnoticed."""

from __future__ import annotations

import json

import pytest

from orch.hubclient import HubRefused
from orch.hubclient.where import check_hub_where

from .vec import CONTRACT_DIR

DOC = json.loads((CONTRACT_DIR / "vectors_hub_backup.json").read_text())
HUB_ONLY = {"bk_key", "open_backup", "tar", "file_list", "restore_floor"}
WHERE = [c for c in DOC["cases"] if c["check"] == "hub_where"]


def test_no_unknown_check_kind() -> None:
    assert {c["check"] for c in DOC["cases"]} == HUB_ONLY | {"hub_where"}
    assert len(WHERE) >= 35
    assert {"where_ok", "where_five_entries", "where_bad_addr_no_port", "where_unknown_kind", "where_name_61_long",
            "where_name_control_and_bidi_stripped",
            "where_name_surrogate_private_use_unassigned_stripped"} <= {c["name"] for c in WHERE}  # fmt: skip


@pytest.mark.parametrize("case", WHERE, ids=[c["name"] for c in WHERE])
def test_where_case(case) -> None:
    if "error" in case["expect"]:
        with pytest.raises(HubRefused) as e:
            check_hub_where(case["args"]["answer"])
        assert e.value.code == case["expect"]["error"]
    else:
        assert check_hub_where(case["args"]["answer"]) == case["expect"]["ok"]
