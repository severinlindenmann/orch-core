"""M4 S0: core replays the ``hub_where`` cases of the shared ``contracts/hub/vectors_hub_backup.json`` against its own
validator ``orch.hubclient.where``. The other checks of that file (backup file, tar, restore floor) are the hub's;
they are named here so a new check kind cannot go unnoticed."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import orch
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


def _core_labels() -> set[str]:
    """Every ``orch/v2/...`` string literal in core's source (kdf labels have no ``|``, signature/AAD/hash labels end in it)."""
    found: set[str] = set()
    for f in Path(orch.__file__).parent.rglob("*.py"):
        found |= set(re.findall(r"""["'](orch/v2/[A-Za-z0-9/_.-]+\|?)["']""", f.read_text()))
    return found


def _clashes(mine: str, others: set[str]) -> list[str]:
    return sorted(o for o in others if o != mine and (o.startswith(mine) or mine.startswith(o)))


def test_backup_labels_are_prefix_free_with_every_core_label() -> None:
    """S0 review (info): the two hub-backup labels are new and prefix-free with every label core uses (the hub test
    checks them against the hub's own table)."""
    core = _core_labels()
    assert len(core) > 100 and {"orch/v2/seal-aad|", "orch/v2/sig/device-cert|", "orch/v2/seal"} <= core
    mine = [DOC["labels"]["sign_header"], DOC["labels"]["aad"]]
    for m in mine:
        assert m.startswith("orch/v2/") and m.endswith("|") and m not in core
        assert not _clashes(m, core), m
    assert _clashes("orch/v2/seal", core)  # the check itself bites: a label that is a prefix of another is found
