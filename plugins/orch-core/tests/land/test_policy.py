"""Signed owner policy and the UI target normalization contract."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from orch.land.policy import QueueError, check_owner_flag, is_owner_target, norm_target

INVALID = [
    "main/",
    "/main",
    "main.",
    "HEAD",
    " main",
    "main.lock",
    "ｍain",
    "mаin",
    "main\u200b",
    "refs/heads//main",
    "refs/tags/main",
    "tags/main",
    "a//b",
    "a..b",
    "a@{b",
    "a b",
    "a~b",
    "a^b",
    "a:b",
    "a?b",
    "a*b",
    "a[b",
    "a\\b",
    "-main",
    ".main",
    "a/.b",
    "a.lock/b",
    "a\n",
]


@given(
    st.lists(st.sampled_from(["refs/heads/", "heads/", "remotes/company/", "origin/", "upstream/"]), max_size=8),
    st.sampled_from(["main", "MAIN", "Main", "mAiN"]),
)
def test_owner_prefix_case(prefixes: list[str], name: str) -> None:
    raw = "".join(prefixes) + name
    assert norm_target(raw) == "main"
    assert is_owner_target({}, raw)
    assert is_owner_target({"owner_target": False, "remote_names": ["main"]}, raw)


@given(st.text(), st.lists(st.text()), st.booleans())
def test_flag_mismatch_always_refused(target: str, owners: list[str], noise: bool) -> None:
    settings = {"owner_targets": owners, "owner_target": noise}
    derived = is_owner_target(settings, target)
    check_owner_flag(settings, target, derived)
    with pytest.raises(QueueError, match="land.owner_required"):
        check_owner_flag(settings, target, not derived)
    assert derived == is_owner_target({"owner_targets": owners}, target)


@pytest.mark.parametrize("raw", INVALID)
def test_invalid_fails_closed(raw: str) -> None:
    assert norm_target(raw) is None
    assert is_owner_target({"owner_targets": []}, raw)


@pytest.mark.parametrize("value", [None, "release", ("release",), 12, ["release", None], {}])
def test_malformed_uses_default(value: Any) -> None:
    for name in ("main", "master"):
        assert is_owner_target({"owner_targets": value}, name)
    assert not is_owner_target({"owner_targets": value}, "release")


def test_configured_policy_and_remote_prefix() -> None:
    assert norm_target("company/refs/remotes/company/MAIN", ["company"]) == "main"
    assert norm_target("feature/new-api") == "feature/new-api"
    assert is_owner_target({"owner_targets": ["origin/RELEASE"]}, "refs/heads/release")
    assert is_owner_target({"owner_targets": []}, "main")  # main/master stay owner targets always
    assert is_owner_target({"owner_targets": ["release"]}, "main")
