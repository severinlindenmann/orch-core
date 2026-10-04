import json

import pytest

from orch.config.load import DEFAULTS, deep_merge, find_home, load_config, validate_schema
from orch.core.constants import STATUSES
from orch.errors import UsageError, ValidationError


def test_find_home_from_root_nested_and_home(ws_root):
    home = (ws_root / "orchestrator").resolve()
    nested = ws_root / "repo" / "src"
    nested.mkdir(parents=True)
    assert find_home(ws_root) == home
    assert find_home(nested) == home
    assert find_home(home) == home


def test_find_home_env(ws_root, tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_HOME", str(ws_root / "orchestrator"))
    assert find_home(tmp_path) == (ws_root / "orchestrator").resolve()


def test_find_home_missing(tmp_path):
    with pytest.raises(UsageError):
        find_home(tmp_path)


def test_load_config_merges_defaults(ws_root):
    cfg = load_config(ws_root / "orchestrator")
    assert cfg["customer"] == "acme"
    assert cfg["claims"]["ttl_hours"] == 4
    assert cfg["git"]["agent_may"] == {"commit": False, "push": False, "open_review": False}


def test_load_config_accepts_bom(ws_root):
    p = ws_root / "orchestrator" / "config.json"
    p.write_text("﻿" + p.read_text(encoding="utf-8"), encoding="utf-8")
    assert load_config(ws_root / "orchestrator")["customer"] == "acme"


@pytest.mark.parametrize("patch", [{"schema": 2}, {"id": {"prefix": "l-x", "pad": 4}}])
def test_load_config_rejects(ws_root, patch):
    p = ws_root / "orchestrator" / "config.json"
    p.write_text(json.dumps(deep_merge(json.loads(p.read_text(encoding="utf-8")), patch)), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(ws_root / "orchestrator")


def test_load_config_invalid_json(ws_root):
    (ws_root / "orchestrator" / "config.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(ws_root / "orchestrator")


def test_validate_schema_reports_type_errors():
    assert validate_schema(deep_merge(DEFAULTS, {"customer": "x"})) == []
    errors = validate_schema(deep_merge(DEFAULTS, {"customer": "x", "dashboard": {"port": "abc"}}))
    assert any("dashboard/port" in e for e in errors)


def test_workspace_layout_with_spaces(ws):
    assert " " in str(ws.root)
    for status in STATUSES:
        assert ws.status_dir(status).is_dir()
    for d in (ws.artifacts_dir, ws.state_dir, ws.temporary_dir, ws.static_dir):
        assert d.is_dir()
    with pytest.raises(ValueError):
        ws.status_dir("nope")
