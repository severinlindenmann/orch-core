import json
import re

import pytest


def _write_workspaces(tmp_path, top_level):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "workspaces.json").write_text(json.dumps(top_level))


def test_brand_mission_control_uses_colour_icon(ws, configure):
    from fastapi.testclient import TestClient
    from orch.core.workspace import Workspace
    from orch.dashboard.app import create_app
    configure(dashboard={"brand": "mission-control"})
    c = TestClient(create_app(Workspace.open(ws.root), "tok"))
    html = c.get("/?token=tok").text
    assert re.search(r'<link rel="icon" type="image/svg\+xml" href="/static/brand/mc-favicon\.svg\?v=[0-9a-f]+">', html)
    assert c.get("/static/brand/mc-favicon.svg").status_code == 200
    assert "/static/brand/mc-icon.svg" in html


def test_brand_none_uses_plain_icon(ws, configure):
    from fastapi.testclient import TestClient
    from orch.core.workspace import Workspace
    from orch.dashboard.app import create_app
    configure(dashboard={"brand": "none"})
    c = TestClient(create_app(Workspace.open(ws.root), "tok"))
    html = c.get("/?token=tok").text
    assert "mc-icon-plain.svg" in html and "/static/brand/mc-icon.svg" not in html


def test_switcher_lists_other_workspaces(ws, tmp_path, monkeypatch):
    from orch.dashboard import switcher
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    switcher.register(ws, 8765)
    other = tmp_path / "other"
    (other / ".state").mkdir(parents=True)
    (tmp_path / "state").mkdir(exist_ok=True)
    data = json.loads((tmp_path / "state" / "workspaces.json").read_text())
    data[str(other)] = {"path": str(other), "name": "northwind", "last_port": 8766, "state_dir": str(other / ".state")}
    (tmp_path / "state" / "workspaces.json").write_text(json.dumps(data))
    (other / ".state" / "needs-count").write_text("1")
    assert switcher.others(ws) == [{"name": "northwind", "url": "http://127.0.0.1:8766/", "needs": 1}]


# -- Fix round 1: a malformed workspaces.json must never crash others() or build a spoofed URL ---

@pytest.mark.parametrize("bad_port", [
    "8766@evil.example.com",  # userinfo-style string: must never land in the built URL
    "8766",                   # a numeric string is still not an int
    True,                     # bool is an int subclass; must not pass as a port
    False,
    0,                        # out of range
    -1,
    65536,                    # out of range
    3.5,                      # float, not int
    None,
])
def test_switcher_skips_invalid_port(ws, tmp_path, monkeypatch, bad_port):
    from orch.dashboard import switcher
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    switcher.register(ws, 8765)
    other = tmp_path / "other"
    (other / ".state").mkdir(parents=True)
    data = json.loads((tmp_path / "state" / "workspaces.json").read_text())
    data[str(other)] = {"path": str(other), "name": "northwind", "last_port": bad_port, "state_dir": str(other / ".state")}
    _write_workspaces(tmp_path, data)
    assert switcher.others(ws) == []


def _valid_other_entry(other):
    return {"path": str(other), "name": "northwind", "last_port": 8766, "state_dir": str(other / ".state")}


@pytest.mark.parametrize("override", [
    {"path": 123},
    {"path": None},
    {"name": 123},
    {"name": ""},
    {"name": "   "},
])
def test_switcher_skips_entry_with_bad_field_types(ws, tmp_path, monkeypatch, override):
    """A non-str/empty `path` or `name` is not enough to trust and show the entry: it is dropped."""
    from orch.dashboard import switcher
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    switcher.register(ws, 8765)
    other = tmp_path / "other"
    (other / ".state").mkdir(parents=True)
    entry = {**_valid_other_entry(other), **override}
    data = json.loads((tmp_path / "state" / "workspaces.json").read_text())
    data[str(other)] = entry
    _write_workspaces(tmp_path, data)
    assert switcher.others(ws) == []


def test_switcher_shows_entry_with_bad_state_dir_without_needs_count(ws, tmp_path, monkeypatch):
    """A non-str `state_dir` must never crash `Path(...)`; the workspace still shows, just with
    no needs-you count (the one field that was unusable), not a 500 for the whole switcher."""
    from orch.dashboard import switcher
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    switcher.register(ws, 8765)
    other = tmp_path / "other"
    (other / ".state").mkdir(parents=True)
    entry = {**_valid_other_entry(other), "state_dir": 123}
    data = json.loads((tmp_path / "state" / "workspaces.json").read_text())
    data[str(other)] = entry
    _write_workspaces(tmp_path, data)
    assert switcher.others(ws) == [{"name": "northwind", "url": "http://127.0.0.1:8766/", "needs": None}]


@pytest.mark.parametrize("top_level", [
    ["not", "a", "dict"],
    "also not a dict",
    42,
    None,
])
def test_switcher_treats_non_dict_top_level_as_empty(ws, tmp_path, monkeypatch, top_level):
    from orch.dashboard import switcher
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    _write_workspaces(tmp_path, top_level)
    assert switcher.others(ws) == []


def test_switcher_skips_non_dict_entry(ws, tmp_path, monkeypatch):
    from orch.dashboard import switcher
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    _write_workspaces(tmp_path, {"other": ["not", "a", "dict"]})
    assert switcher.others(ws) == []


def test_switcher_tolerates_invalid_json(ws, tmp_path, monkeypatch):
    from orch.dashboard import switcher
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "workspaces.json").write_text("{not json")
    assert switcher.others(ws) == []


def test_page_renders_with_malformed_workspaces_file(dash, tmp_path, monkeypatch):
    """others() runs on every page render; a malformed file must give a normal page, not a 500."""
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "workspaces.json").write_text("{not json")
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    resp = dash.get("/")
    assert resp.status_code == 200
    assert "menu-switcher" not in resp.text


def test_register_recovers_from_corrupt_file(ws, tmp_path, monkeypatch):
    from orch.dashboard import switcher
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "workspaces.json").write_text("{not json")
    switcher.register(ws, 8765)
    data = json.loads((state / "workspaces.json").read_text())
    key = str(ws.root.resolve())
    assert data == {key: data[key]}
    assert data[key]["last_port"] == 8765


def test_register_oserror_does_not_stop_startup(ws, monkeypatch, caplog):
    """A read-only config directory makes `register()` raise OSError; `orch serve` must still
    start (the switcher only loses this workspace's entry), and the failure is logged."""
    import logging

    from fastapi.testclient import TestClient
    from orch.core.workspace import Workspace
    from orch.dashboard import switcher
    from orch.dashboard.app import create_app

    def read_only(ws, port):
        raise PermissionError(13, "Permission denied", "workspaces.json")

    monkeypatch.setattr(switcher, "register", read_only)
    with caplog.at_level(logging.WARNING, logger="orch.dashboard"):
        app = create_app(Workspace.open(ws.root), "tok", port=8765)
    assert TestClient(app).get("/?token=tok").status_code == 200
    assert any("workspaces.json" in r.getMessage() for r in caplog.records)


# -- Final review: the needs-count file must never 500 a page, block on a FIFO or read a huge file --

def test_write_needs_oserror_never_breaks_a_page(dash, ws, put, caplog):
    from orch.dashboard import switcher
    put("testing", sections={"Verification": "ok"})
    (ws.state_dir / "needs-count").unlink(missing_ok=True)
    (ws.state_dir / "needs-count").mkdir(parents=True)  # the write now fails with an OSError
    switcher.write_needs(ws, 3)  # swallowed
    r = dash.get("/")
    assert r.status_code == 200 and "<title>(1) Today" in r.text
    assert "needs-count" in caplog.text


def test_needs_count_written_at_startup_without_a_page(ws, put):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    put("testing", sections={"Verification": "ok"})
    with TestClient(create_app(ws, "tok")):
        assert (ws.state_dir / "needs-count").read_text().strip() == "1"


def test_needs_for_reads_only_small_regular_files(tmp_path):
    from orch.dashboard import switcher
    (tmp_path / "needs-count").write_text("7\n")
    assert switcher._needs_for(str(tmp_path)) == 7
    (tmp_path / "needs-count").write_text("5" + " " * 1_000_000)  # huge: never read, ignored
    assert switcher._needs_for(str(tmp_path)) is None
    (tmp_path / "needs-count").unlink()
    (tmp_path / "needs-count").mkdir()
    assert switcher._needs_for(str(tmp_path)) is None


@pytest.mark.skipif(not hasattr(__import__("os"), "mkfifo"), reason="no FIFOs on this platform")
def test_needs_for_never_blocks_on_a_fifo(tmp_path):
    import os
    import threading
    from orch.dashboard import switcher
    os.mkfifo(tmp_path / "needs-count")
    result = []
    t = threading.Thread(target=lambda: result.append(switcher._needs_for(str(tmp_path))), daemon=True)
    t.start()
    t.join(2)
    if t.is_alive():  # unblock the reader so the test process can exit
        fd = os.open(tmp_path / "needs-count", os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        pytest.fail("_needs_for blocked on a FIFO")
    assert result == [None]
