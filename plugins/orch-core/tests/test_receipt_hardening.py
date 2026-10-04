"""Review fixes for receipts: a receipt or an artifact's author cannot be forged by editing the ticket file; the
`checks` guard holds across invalid JSON; a killed run takes its command with it; only an explicit command runs."""
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from orch.core import store, tasks as tk
from orch.errors import UsageError
from orch.hooks.guard import evaluate


def _write(path, text):
    return {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": text}}


# -- 1. receipts and authors are orch's to write ----------------------------------------------------------------

FORGED = ("artifacts:\n- name: fake.log\n  kind: receipt\n  by: human:you\n  task: T1\n"
          "  run:\n    exit: 0\n    timed_out: false\n")


def test_a_hand_written_receipt_is_refused(ws, put):
    tid = put("in-progress")
    path = store.resolve(ws, tid).path
    text = path.read_text(encoding="utf-8")
    d = evaluate(ws, _write(path, text.replace("\n---\n", "\n" + FORGED + "---\n", 1)))
    assert not d.allow and "receipt" in d.reason


def test_rewriting_who_added_an_artifact_is_refused(ws, aops, working, tmp_path):
    f = tmp_path / "shot.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    aops.artifact_add(working, f)
    path = store.resolve(ws, working).path
    text = path.read_text(encoding="utf-8")
    assert "by: agent:claude-code:7f3c9a21" in text
    assert not evaluate(ws, _write(path, text.replace("by: agent:claude-code:7f3c9a21", "by: human:you"))).allow


def test_a_plain_artifact_and_a_label_stay_editable(ws, aops, working, tmp_path):
    f = tmp_path / "shot.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    aops.artifact_add(working, f, label="before")
    path = store.resolve(ws, working).path
    text = path.read_text(encoding="utf-8")
    assert evaluate(ws, _write(path, text.replace("label: before", "label: after"))).allow


# -- 2. the checks guard across invalid JSON ---------------------------------------------------------------------

def test_checks_cannot_be_rewritten_through_a_broken_file(configure):
    ws = configure(checks={"verify": {"steps": [{"name": "test", "run": "pytest -q"}]}})
    cfg = json.loads((ws.home / "config.json").read_text())
    changed = json.dumps({**cfg, "checks": {"verify": {"steps": [{"name": "test", "run": "true"}]}}})
    assert not evaluate(ws, _write(ws.home / "config.json", changed + "}")).allow     # step 1: broken on purpose
    (ws.home / "config.json").write_text(changed + "}")                                # say it got there anyway
    assert not evaluate(ws, _write(ws.home / "config.json", changed)).allow           # step 2: still the old checks


def test_a_broken_config_without_checks_is_not_this_rules_business(ws):
    assert evaluate(ws, _write(ws.home / "config.json", "{not json")).allow


# -- 3. a killed run takes its command with it --------------------------------------------------------------------

def test_a_terminated_run_kills_its_process_group(tmp_path):
    marker = tmp_path / "pid"
    script = (f"import sys; sys.path.insert(0, {repr(sys.path[0])}); from pathlib import Path; "
              "from orch.core.receipts import run_steps; "
              f"run_steps([{{'name': 'v', 'run': 'echo $$ > {marker}; exec sleep 300'}}], Path({str(tmp_path)!r}), "
              "timeout=600, max_bytes=1000)")
    proc = subprocess.Popen([sys.executable, "-c", script])
    for _ in range(100):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.05)
    child = int(marker.read_text())
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_a_check_may_set_its_own_timeout(configure):
    from orch.config.load import check_timeout
    ws = configure(checks={"slow": {"steps": [{"name": "e2e", "run": "true"}], "timeout": 1200}})
    assert check_timeout(ws.config, "slow") == 1200 and check_timeout(ws.config, None) is None


# -- 4. only an explicit command runs ------------------------------------------------------------------------------

@pytest.fixture
def ticket(working, plan_approved):
    plan_approved(working)
    return working


@pytest.mark.parametrize("line", ["databricks bundle deploy -t dev and one green run per job",
                                  "`databricks bundle deploy -t dev` and one green run per job",
                                  "cost query saved as artifact cost-compare.csv"])
def test_a_prose_verify_line_is_not_run(aops, ticket, tmp_path, line):
    _, ids = aops.task_add(ticket, [{"text": "Prove it", "verify": line}])
    aops.task_start(ticket, ids[0])
    with pytest.raises(UsageError, match="cmd:"):
        aops.task_done_run(ticket, ids[0], cwd=tmp_path)


def test_cmd_marks_a_command(ws, aops, ticket, tmp_path):
    _, ids = aops.task_add(ticket, [{"text": "Prove it", "verify": f"cmd: touch {tmp_path / 'ran'}"}])
    aops.task_start(ticket, ids[0])
    aops.task_done_run(ticket, ids[0], cwd=tmp_path)
    assert (tmp_path / "ran").exists()
    assert tk.find(tk.ticket_tasks(store.load(ws, ticket)[1]), ids[0]).state == "done"
