"""The hosted create with the shipped default: the host bootstraps the workspace into ``<project>/.orch``,
registers it with ``store: ".orch"`` and serves it from there."""

# ruff: noqa: F811
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from orch.host.registry import registry_path
from tests.host.hosted_create import create_hosted
from tests.host.lifecycle_helpers import Lab
from tests.host.test_bootstrap import ctx_for, fresh_root, ok, passphrases, recording, term  # noqa: F401  (fixtures)

pytestmark = [
    pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="Unix host"),
]


@pytest.fixture
def lab(tmp_path):
    value = Lab(tmp_path)
    try:
        yield value
    finally:
        value.close()


def test_init_through_the_host_bootstraps_into_dot_orch(
    lab,
    tmp_path,
    monkeypatch,
    term,
    passphrases,
    recording,  # noqa: F811
):
    root = fresh_root(tmp_path, "work")
    (root / "config.json").write_text('{"name": "my own project file"}')  # no collision in the new layout
    monkeypatch.chdir(root)
    with lab.host() as h:
        res = create_hosted(
            ctx_for(root, lab.state),
            {"prefix": "DEMO", "name": "Severin", "workspace_name": "Marketing Department", "host": True},
        )
        wid = res.data["workspace_id"]
        store = root / ".orch"
        assert stat.S_IMODE(store.stat().st_mode) == 0o700
        cfg = json.loads((store / "config.json").read_text())
        assert cfg["workspace"]["id"] == wid and cfg["workspace"]["name"] == "Marketing Department"
        assert (store / "events" / "workspace.jsonl").is_file() and (store / "AGENTS.orch.md").is_file()
        assert (root / "config.json").read_text() == '{"name": "my own project file"}'
        assert not (root / "events").exists() and not (root / "tickets").exists()
        assert ".orch/AGENTS.orch.md" in (root / "AGENTS.md").read_text()
        assert ".orch/" in (root / ".gitignore").read_text().split("\n")
        entry = h.host.registry.get(wid)
        assert entry is not None and entry.store_dir == Path(os.path.realpath(root)) / ".orch"
        assert '"store"' not in registry_path(lab.state).read_text()  # the store is always <root>/.orch
        service = h.host.services[wid]
        assert service.state == "ok", service.cause
        assert service.store is not None and service.store.layout.store.name == ".orch"
        with h.client() as c:
            rows = ok(c.call("host.list"))["data"]["workspaces"]
        assert [(r["workspace_id"], r["state"]) for r in rows] == [(wid, "ok")]
