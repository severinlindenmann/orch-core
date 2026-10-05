"""`checks` (what `orch task done --run` runs for check:<name>) is the human's setting: the guard refuses an agent
edit that changes it, by file tool or by shell, and lets every other config edit through."""
import json

from orch.hooks.guard import evaluate

CHECKS = {"verify": {"steps": [{"name": "test", "run": "pytest -q"}]}}


def _write(ws, cfg):
    return evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": str(ws.home / "config.json"),
                                                               "content": json.dumps(cfg)}})


def _cfg(ws):
    return json.loads((ws.home / "config.json").read_text())


def test_adding_a_check_is_refused(ws):
    d = _write(ws, {**_cfg(ws), "checks": CHECKS})
    assert not d.allow and "checks" in d.reason


def test_changing_what_a_check_runs_is_refused(ws, configure):
    ws = configure(checks=CHECKS)
    changed = {"verify": {"steps": [{"name": "test", "run": "true"}]}}
    assert not _write(ws, {**_cfg(ws), "checks": changed}).allow


def test_other_config_edits_pass(ws, configure):
    ws = configure(checks=CHECKS)
    assert _write(ws, {**_cfg(ws), "customer": "renamed"}).allow


def test_a_shell_write_naming_checks_is_refused(ws):
    d = evaluate(ws, {"tool_name": "Bash", "tool_input": {
        "command": "python3 -c \"import json;c=json.load(open('orchestrator/config.json'));c['checks']={};"
                   "json.dump(c,open('orchestrator/config.json','w'))\""}})
    assert not d.allow and "checks" in d.reason


def test_reading_checks_is_fine(ws):
    assert evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": "grep checks orchestrator/config.json"}}).allow
