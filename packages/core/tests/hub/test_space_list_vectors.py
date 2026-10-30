"""The ``space_list`` reply vectors (contracts/hub/vectors_space_list.json): core's reply check refuses an extra key,
an unknown space id and a non-string value in ``meta``."""

from __future__ import annotations

import json

import pytest

from orch.hubclient.tlslink.client import _REPLY

from .vec import CONTRACT_DIR

CASES = json.loads((CONTRACT_DIR / "vectors_space_list.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_space_list_case(case) -> None:
    assert _REPLY["space_list"](case["reply"]) is (case["expect"] == "ok")
