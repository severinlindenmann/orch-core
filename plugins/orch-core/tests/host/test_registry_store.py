"""Registry ``store`` key (exact key set, read side) and the host's store/project split (design B11-P2 step 3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch.host.auth import log_stamp
from orch.host.errors import HostError
from orch.host.registry import Entry

DOC = {
    "workspace_id": "a" * 32,
    "prefix": "T",
    "root": "/tmp/proj",
    "host_uid": 501,
    "group": "orch-aaaaaaaaaaaa",
    "peers": {},
    "repos": {},
}


def test_an_entry_without_store_is_legacy_and_round_trips_without_the_key() -> None:
    e = Entry.from_doc(dict(DOC))
    assert e.store is None and e.store_dir == Path("/tmp/proj")
    assert "store" not in e.to_doc()


def test_store_dot_orch_is_read_and_written_back() -> None:
    e = Entry.from_doc({**DOC, "store": ".orch"})
    assert e.store == ".orch" and e.store_dir == Path("/tmp/proj/.orch")
    assert e.to_doc()["store"] == ".orch"


@pytest.mark.parametrize("bad", ["", "orch", ".orch/", "../x", None, 1, [".orch"], "/tmp/proj"])
def test_any_other_store_value_is_refused(bad: object) -> None:
    with pytest.raises(HostError):
        Entry.from_doc({**DOC, "store": bad})


def test_other_extra_keys_are_still_refused() -> None:
    with pytest.raises(HostError):
        Entry.from_doc({**DOC, "stores": ".orch"})
    with pytest.raises(HostError):
        Entry.from_doc({**DOC, "store": ".orch", "x": 1})


def test_the_auth_stamp_reads_the_logs_of_the_store_dir(tmp_path: Path) -> None:
    store = tmp_path / "proj" / ".orch"
    (store / "events").mkdir(parents=True)
    (store / "events" / "workspace.jsonl").write_text(json.dumps({"x": 1}) + "\n")
    assert log_stamp(store) is not None
    assert log_stamp(tmp_path / "proj") is None  # the project root has no events/: the old wrong place
