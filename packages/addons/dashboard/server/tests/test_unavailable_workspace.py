"""A workspace the host cannot serve is listed with its prefix and the host's own reason (S-3): a store from an older
version in the project root is named, with what to delete, not shown as "no such workspace"."""

from orch_dashboard import adapters
from orch_dashboard.errors import fail

OLD = (
    "the workspace cannot be served: validation.path: this folder has an orch store from an older version in the "
    "project root; orch now keeps its files in .orch/. Delete config.json, events/ and run orch setup again"
)


def test_the_reason_is_the_hosts_plain_words_without_codes_in_front():
    error = fail("workspace.unavailable", OLD).error
    text = adapters.unavailable_reason(error)
    assert text is not None and text.startswith("this folder has an orch store from an older version")
    assert "validation.path" not in text and "cannot be served" not in text and "Delete config.json" in text


def test_only_an_unavailable_workspace_carries_a_reason_and_hidden_characters_never_pass():
    assert adapters.unavailable_reason(fail("host.unavailable", "host.unavailable").error) is None
    assert adapters.unavailable_reason(fail("workspace.unavailable", "x" * 700).error) is None
    assert adapters.unavailable_reason(fail("workspace.unavailable", "delete it‮ now").error) is None
    assert adapters.unavailable_reason({"code": "workspace.unavailable"}) is None


def test_the_prefix_comes_from_the_registry_row_and_only_when_plain():
    assert adapters.pinned_prefix({"prefix": "DEMO"}) == "DEMO"
    for bad in ({}, {"prefix": "de mo"}, {"prefix": "A" * 40}, {"prefix": 5}, {"prefix": "D‮EMO"}):
        assert adapters.pinned_prefix(bad) == ""


def test_the_workspace_row_carries_the_message_and_the_prefix():
    row = adapters.Workspace(
        id="a" * 32,
        prefix=adapters.pinned_prefix({"prefix": "OLD"}),
        name="a" * 32,
        host_state="workspace.unavailable",
        host_message=adapters.unavailable_reason(fail("workspace.unavailable", OLD).error),
    )
    doc = row.model_dump()
    assert doc["prefix"] == "OLD" and doc["host_state"] == "workspace.unavailable"
    assert doc["host_message"].endswith("run orch setup again")
