"""The adapters against what a REAL host (core H6b) answered, recorded and scrubbed (src/test/fixtures/real-host).

Offline: no host is started. Re-record with `UPDATE_REALHOST_GOLDEN=1 uv run pytest -m realhost -k record`.
"""

import json

import pytest
from realhost_goldens import DIRECTORY, adapt

pytestmark = pytest.mark.skipif(not (DIRECTORY / "raw.json").exists(), reason="no recorded real-host answers")


def test_the_adapters_still_make_the_recorded_documents_from_the_recorded_host_answers():
    raw = json.loads((DIRECTORY / "raw.json").read_text())
    assert json.loads(json.dumps(adapt(raw), sort_keys=True)) == json.loads((DIRECTORY / "adapted.json").read_text())


def test_the_recording_holds_no_real_identifier():
    text = (DIRECTORY / "raw.json").read_text()
    assert "orch-rh" not in text and "/Users/" not in text and "severin" not in text.lower()
    assert '"welcome"' in text and '"human.prepare"' in text


def test_what_the_real_host_listed_and_did_not():
    welcome = json.loads((DIRECTORY / "raw.json").read_text())["welcome"]
    assert "human/1" not in welcome["caps"] and "human.prepare" in welcome["ops"]  # stream A RH-1
    assert "diff/1" in welcome["caps"] and "diff" not in welcome["ops"]  # stream A RH-2
    assert welcome["ops"]["move"] == 1
