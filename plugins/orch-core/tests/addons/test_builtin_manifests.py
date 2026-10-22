"""B5b: the start-agent, terminals and worktrees manifests ship in the package and install without a clone."""

import io
import json
from pathlib import Path

import pytest

from orch.addons import builtin
from orch.addons.manifest import load_manifest
from orch.cli.main import main

REPO_ADDONS = Path(__file__).resolve().parents[4] / "addons"


@pytest.mark.parametrize("name", builtin.NAMES)
def test_manifest_is_valid_and_equals_the_repo_copy(name):
    data = builtin.manifest_bytes(name)
    assert load_manifest(data).name == name
    assert json.loads(data)["name"] == name
    repo = REPO_ADDONS / name / "orch-addon.json"
    if repo.exists():
        assert repo.read_bytes() == data


def test_worktrees_manifest_names_the_addon_the_host_gates_on():
    from orch.host.worktrees import ADDON

    assert ADDON == "worktrees" and json.loads(builtin.manifest_bytes("worktrees"))["name"] == ADDON


def test_cli_prints_and_writes(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    assert main(["addon", "manifest", "terminals"], env={}, stdout=out, stderr=err) == 0
    assert json.loads(out.getvalue())["name"] == "terminals"
    out = io.StringIO()
    (tmp_path / ".orch").mkdir()  # --write names the project; the manifest goes into its store
    assert main(["addon", "manifest", "worktrees", "--write", str(tmp_path)], env={}, stdout=out, stderr=err) == 0
    written = tmp_path / ".orch" / "addons" / "worktrees" / "orch-addon.json"
    assert written.read_bytes() == builtin.manifest_bytes("worktrees") and out.getvalue().strip() == str(written)
    # idempotent; a differing file needs --force
    assert (
        main(["addon", "manifest", "worktrees", "--write", str(tmp_path)], env={}, stdout=io.StringIO(), stderr=err)
        == 0
    )
    written.write_text("{}")
    assert (
        main(["addon", "manifest", "worktrees", "--write", str(tmp_path)], env={}, stdout=io.StringIO(), stderr=err)
        == 2
    )
    assert (
        main(
            ["addon", "manifest", "worktrees", "--write", str(tmp_path), "--force"],
            env={},
            stdout=io.StringIO(),
            stderr=err,
        )
        == 0
    )
    assert written.read_bytes() == builtin.manifest_bytes("worktrees")


def test_unknown_name_is_refused():
    err = io.StringIO()
    assert main(["addon", "manifest", "nope"], env={}, stdout=io.StringIO(), stderr=err) == 2


@pytest.mark.parametrize("name", builtin.NAMES)
def test_every_bundled_manifest_is_compatible_with_this_core(name):
    from orch import __version__
    from orch.addons.manifest import incompatibility

    assert incompatibility(load_manifest(builtin.manifest_bytes(name)), __version__) is None


def test_every_repo_addon_manifest_is_compatible_with_this_core():
    from orch import __version__
    from orch.addons.manifest import incompatibility

    found = sorted(REPO_ADDONS.glob("*/orch-addon.json"))
    assert found
    for f in found:
        assert incompatibility(load_manifest(f.read_bytes()), __version__) is None, f
