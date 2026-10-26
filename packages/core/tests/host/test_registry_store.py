"""Registry entries: the store is always ``<root>/.orch``; an earlier host's ``store`` key is tolerated on read."""

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


def test_an_entry_has_its_store_in_dot_orch_and_never_writes_the_key() -> None:
    e = Entry.from_doc(dict(DOC))
    assert e.store_dir == Path("/tmp/proj/.orch")
    assert "store" not in e.to_doc()


def test_an_earlier_hosts_store_key_is_tolerated_and_dropped_on_write() -> None:
    e = Entry.from_doc({**DOC, "store": ".orch"})
    assert e.store_dir == Path("/tmp/proj/.orch")
    assert "store" not in e.to_doc()


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
