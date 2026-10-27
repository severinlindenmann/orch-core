"""The add-on set of a workspace as the instructions see it (design addon-skills D2, D3): replayed grants plus the
packages on disk, the registry's state per add-on, built-ins from the shipped manifests."""

from __future__ import annotations

import json

from orch.addons import builtin, install
from orch.instructions import (
    INSTRUCTIONS_REV,
    AddonRow,
    addon_fingerprint,
    collect_rows,
    render_agents_md,
    stale_findings,
    write_workspace_files,
)
from orch.model.types import Addon
from tests.addons.helpers import make_package, manifest


def grant(name: str, digest: str, version: str = "1.0.0", **kw) -> Addon:
    return Addon(name, version, digest, [], {}, [], [], **kw)


def package(tmp_path, name: str, **over):
    man = manifest(**{"schema": "orch.addon/2", "version": "1.0.0", "capabilities": [], **over})
    make_package(tmp_path, name, man=man | {"name": name}, script=False)
    return install.read_installed(tmp_path, name)[0].digest


def test_an_active_addon_carries_its_trigger_line(tmp_path):
    digest = package(tmp_path, "echo", agents_md="Use when the person says echo. Not for Y.")
    (row,) = collect_rows(tmp_path, {"echo": grant("echo", digest)})
    assert (row.name, row.state, row.active, row.speaks) == ("echo", "active", True, True)
    assert row.agents_md.startswith("Use when") and row.skill is None and row.digest == digest


def test_the_registry_states_become_the_off_reasons(tmp_path):
    digest = package(tmp_path, "echo", agents_md="Use when x.")
    package(tmp_path, "moved", agents_md="Use when y.")
    view = {
        "echo": grant("echo", digest, enabled=False),
        "moved": grant("moved", "sha256:" + "0" * 64),  # not the package that was granted
        "gone": grant("gone", "sha256:" + "1" * 64),  # no package on disk
        "dead": grant("dead", "sha256:" + "2" * 64, purged=True),  # purged: not listed at all
    }
    states = {r.name: r.state for r in collect_rows(tmp_path, view)}
    assert states == {"echo": "disabled", "moved": "changed", "gone": "missing"}


def test_an_incompatible_package_is_incompatible_not_disabled(tmp_path):
    man = manifest(**{"schema": "orch.addon/3", "api": ">=9.0,<10", "min_core": "1.0.0", "capabilities": []})
    make_package(tmp_path, "future", man=man | {"name": "future"}, script=False)
    digest = install.read_installed(tmp_path, "future")[0].digest
    (row,) = collect_rows(tmp_path, {"future": grant("future", digest)})
    assert row.state == "incompatible"


def test_a_pure_ui_addon_does_not_speak(tmp_path):
    digest = package(tmp_path, "echo")
    (row,) = collect_rows(tmp_path, {"echo": grant("echo", digest)})
    assert row.speaks is False


def test_a_builtin_without_a_package_folder_loads_its_shipped_manifest(tmp_path):
    from orch.addons.package import MANIFEST_FILE, package_digest

    digest = package_digest({MANIFEST_FILE: builtin.manifest_bytes("quick")})
    (row,) = collect_rows(
        tmp_path, {"quick": grant("quick", digest, version=json.loads(builtin.manifest_bytes("quick"))["version"])}
    )
    assert row.name == "quick" and row.state == "active"


def test_the_fingerprint_makes_the_stamp_stale_and_sync_fixes_it(tmp_path):
    write_workspace_files(tmp_path)
    assert stale_findings(tmp_path, None, []) == []
    rows = [AddonRow("echo", "active", "1.0.0", "sha256:a", "Use when x.")]
    found = stale_findings(tmp_path, None, rows)
    assert found == [{"where": "AGENTS.orch.md", "what": "the add-ons changed since it was written"}]
    assert stale_findings(tmp_path) == []  # the set is not known here: not checked
    write_workspace_files(tmp_path, addons=rows)
    assert stale_findings(tmp_path, None, rows) == []
    other = [AddonRow("echo", "disabled", "1.0.0", "sha256:a", "Use when x.")]
    assert stale_findings(tmp_path, None, other)[0]["what"].startswith("the add-ons changed")
    assert addon_fingerprint(rows) in (tmp_path / "AGENTS.orch.md").read_text().splitlines()[1]
    assert f"r{INSTRUCTIONS_REV}" in (tmp_path / "AGENTS.orch.md").read_text().splitlines()[1]
    assert "Add-ons here" in render_agents_md(rows)
