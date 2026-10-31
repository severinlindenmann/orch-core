"""Frozen relay data from released formats must still open."""

import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [
    str(HERE.parents[0]),
    str(HERE.parents[1] / "src"),
]  # tests/ (format_helpers) and src/ (orch_relay)

from format_helpers import write_format

from orch_relay import db
from orch_relay.dataformat import open_data

pytestmark = pytest.mark.slow
CORPORA = sorted((HERE / "corpus").glob("relay-f*"))


@pytest.mark.parametrize(
    "corpus", [path for path in CORPORA if path.is_dir()], ids=lambda path: path.name
)
def test_upgrade_corpus(corpus, tmp_path):
    state = tmp_path / corpus.name
    shutil.copytree(corpus, state)
    marker = json.loads((state / "format.json").read_text())
    assert marker["format"] == 1
    # Once a step exists, run orch migrate through core before opening the copied corpus.
    opened = open_data(state, "relay", 1, env={})
    try:
        assert db.init_schema(state / db.DB_NAME) is False
        con = sqlite3.connect(state / db.DB_NAME)
        try:
            rows = con.execute("SELECT hub_id FROM hub_accounts").fetchall()
            assert rows == [("0" * 32,)]
        finally:
            con.close()
    finally:
        opened.close()


def test_newer_format_refused(tmp_path):
    write_format(tmp_path, fmt=2)
    with pytest.raises(Exception) as exc:
        open_data(tmp_path, "relay", 1, env={})
    assert exc.value.code == "format.newer"
