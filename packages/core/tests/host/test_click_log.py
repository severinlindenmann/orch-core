"""H5c: the click signer's private log (orch-v2-host §0.8.6 item 5)."""

from __future__ import annotations

import json
import os

from orch.click import log as click_log
from orch.click.log import ClickLog

WS = "0123456789abcdef0123456789abcdef"
REQ = "hr_0" + "A" * 25


def _lines(state):
    return [json.loads(x) for x in (state / "person" / "click.jsonl").read_text().splitlines()]


def test_only_validated_identifiers_and_codes_are_written(tmp_path):
    state = tmp_path / "state"
    ClickLog(state).write("refused", workspace=WS, request=REQ, op="log", code="human.click_refused.word")
    ClickLog(state).write("refused", workspace="not an id", request="text with spaces", op="Bad Op", code="X", text="t")
    a, b = _lines(state)
    assert a == {
        "at": a["at"],
        "what": "refused",
        "workspace": WS,
        "request": REQ,
        "op": "log",
        "code": "human.click_refused.word",
    }
    assert set(b) == {"at", "what"}  # nothing unvalidated, never a payload text
    path = state / "person" / "click.jsonl"
    assert path.stat().st_mode & 0o777 == 0o600
    assert (state / "person").stat().st_mode & 0o777 == 0o700


def test_a_symlinked_log_is_never_written_through(tmp_path):
    state = tmp_path / "state"
    ClickLog(state).write("review", workspace=WS)
    target = tmp_path / "elsewhere"
    target.write_text("")
    path = state / "person" / "click.jsonl"
    path.unlink()
    os.symlink(target, path)
    ClickLog(state).write("review", workspace=WS)
    assert target.read_text() == ""
    assert not path.is_symlink()


def test_rotation_keeps_four_files_and_the_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(click_log, "MAX_BYTES", 200)
    state = tmp_path / "state"
    for _ in range(60):
        ClickLog(state).write("signed", workspace=WS, request=REQ, op="log")
    names = sorted(p.name for p in (state / "person").iterdir())
    assert names == ["click.jsonl", "click.jsonl.1", "click.jsonl.2", "click.jsonl.3"]
    for n in names:
        assert (state / "person" / n).stat().st_mode & 0o777 == 0o600
