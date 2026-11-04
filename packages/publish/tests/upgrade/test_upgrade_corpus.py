"""Frozen release data must still open and retain its workspace and share."""

import json
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[0]), str(HERE.parents[1])]

from format_helpers import write_format

from pubsrv import dataformat, db

pytestmark = pytest.mark.slow
CORPORA = sorted((HERE / "corpus").glob("publish-f*"))


@pytest.mark.parametrize("corpus", [p for p in CORPORA if p.is_dir()], ids=lambda p: p.name)
def test_upgrade_corpus(corpus, tmp_path):
    state = tmp_path / corpus.name
    shutil.copytree(corpus, state)
    marker = json.loads((state / "format.json").read_text())
    # Once a step exists, run orch migrate through core.
    assert marker["format"] == 1
    handle = dataformat.open_data(state, "publish", 1, env={})
    try:
        path = state / "publish.db"
        db.init(path)  # user_version must match SCHEMA_VERSION
        with closing(sqlite3.connect(path)) as conn:
            workspaces = conn.execute(
                "SELECT workspace_id, wsk_pub, owner_person_id, owner_pk, delegation_json, status, "
                "created_ms, updated_ms FROM workspaces"
            ).fetchall()
            assert workspaces == [
                (
                    "0" * 32,
                    "throwaway-wsk",
                    "1" * 32,
                    "throwaway-owner-key",
                    "{}",
                    "active",
                    1700000000000,
                    1700000000000,
                )
            ]
            shares = conn.execute(
                "SELECT id, owner_ws, ticket, kind, token_hash, expires_ms, view_limit, views, state, "
                "manifest_hash, bytes, created_ms, ended_ms, label, by_kind, by_name, pw FROM shares"
            ).fetchall()
            assert shares == [
                (
                    "1" * 32,
                    "0" * 32,
                    "throwaway-ticket",
                    "secret",
                    "0" * 64,
                    1700003600000,
                    0,
                    0,
                    "staged",
                    None,
                    0,
                    1700000000000,
                    None,
                    "Corpus share",
                    "person",
                    None,
                    None,
                )
            ]
    finally:
        handle.close()


def test_newer_format_refused(tmp_path):
    state = tmp_path / "publish"
    write_format(state, fmt=2)
    with pytest.raises(dataformat.DataFormatError) as exc:
        handle = dataformat.open_data(state, "publish", 1, env={})
        handle.close()
    assert getattr(exc.value, "code", None) == "format.newer" or "format.newer" in str(exc.value)
