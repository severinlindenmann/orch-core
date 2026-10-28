"""Run-12a FB16: setup lets Claude Code trust the new folder (only that folder, only when asked)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from orch.setup import claude_folder
from tests.setup.fakes import names
from tests.setup.test_create import AGENT, fake_signer, run_ok  # noqa: F401 - fake_signer: signing stand-ins


def _home(tmp_path: Path, doc) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"
    home.mkdir()
    if doc is not None:
        (home / ".claude.json").write_text(json.dumps(doc))
    exe = home / ".local" / "bin" / "claude"  # a claude in a usual install dir, so the tests do not need a real one
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return home, {"HOME": str(home), "PATH": os.environ.get("PATH", "")}


def test_trusts_only_the_new_folder_and_keeps_everything_else(tmp_path):
    home, env = _home(tmp_path, {"numStartups": 3, "projects": {"/other": {"hasTrustDialogAccepted": False, "x": 1}}})
    root = tmp_path / "work" / "ws"
    root.mkdir(parents=True)
    assert claude_folder.trust_folder(env, root) is True
    doc = json.loads((home / ".claude.json").read_text())
    assert doc["numStartups"] == 3 and doc["projects"]["/other"] == {"hasTrustDialogAccepted": False, "x": 1}
    assert doc["projects"][os.path.realpath(root)] == {"hasTrustDialogAccepted": True}


def test_no_config_file_or_a_broken_one_is_left_alone(tmp_path):
    root = tmp_path / "work" / "ws"
    root.mkdir(parents=True)
    _, env = _home(tmp_path, None)
    assert claude_folder.trust_folder(env, root) is False and not Path(env["HOME"], ".claude.json").exists()
    (Path(env["HOME"]) / ".claude.json").write_text("{not json")
    assert claude_folder.trust_folder(env, root) is False
    assert (Path(env["HOME"]) / ".claude.json").read_text() == "{not json"


def test_the_home_folder_itself_is_never_trusted(tmp_path):
    home, env = _home(tmp_path, {"projects": {}})
    assert claude_folder.trust_folder(env, home) is False


def test_installed_check_follows_path(tmp_path):
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert claude_folder.claude_installed({"PATH": str(exe.parent)}) is True
    assert claude_folder.claude_installed({"PATH": str(tmp_path)}) is False


def test_setup_runs_the_trust_step_only_when_the_profile_asks(tmp, monkeypatch):
    monkeypatch.setattr(claude_folder, "claude_installed", lambda env: True)
    f, ports, p, rv = run_ok(tmp, {**AGENT, "trust_folder": True})
    assert any(x["id"] == "trust" for x in rv["flags"]) and "trust_folder" in names(ports)
    assert {s["id"]: s["status"] for s in p["steps"]}["trust"] == "done"
    f, ports, p, rv = run_ok(tmp, AGENT)
    assert "trust_folder" not in names(ports)


def test_a_folder_with_foreign_project_config_is_not_offered_and_nothing_is_written(tmp_path):
    for name in (".claude", ".mcp.json", "CLAUDE.local.md", "CLAUDE.md"):
        (tmp_path / name.strip(".")).mkdir()
        home, env = _home(tmp_path / name.strip("."), {"projects": {}})
        root = tmp_path / name.strip(".") / "work" / "ws"
        root.mkdir(parents=True)
        (root / name).mkdir() if name == ".claude" else (root / name).write_text("{}")
        assert claude_folder.offered(env, root) is False
        if name != "CLAUDE.md":  # setup's own CLAUDE.md is allowed at write time; the offer already refused this one
            assert claude_folder.trust_folder(env, root) is False
        assert json.loads((home / ".claude.json").read_text()) == {"projects": {}}


def test_config_above_the_folder_is_named_and_trusted_only_on_the_explicit_choice(tmp_path):
    home, env = _home(tmp_path, {"projects": {}})
    docs = tmp_path / "work" / "Documents"
    (docs / ".claude").mkdir(parents=True)
    root = docs / "demo1"
    (root / ".orch").mkdir(parents=True)
    assert claude_folder.parent_config(root, env) == os.path.join(os.path.realpath(docs), ".claude")
    why = claude_folder.refusal(env, root)
    assert why and ".claude" in why and "apply to this folder too" in why
    assert claude_folder.trust_folder(env, root) is False  # default: Claude Code asks
    from orch.runner.claude_trust import _consented

    assert not _consented(os.path.realpath(root)) and json.loads((home / ".claude.json").read_text()) == {
        "projects": {}
    }
    assert claude_folder.trust_folder(env, root, allow_parent=True) is True  # "trust anyway"
    assert _consented(os.path.realpath(root))


def test_config_in_the_root_itself_is_never_trusted_even_with_trust_anyway(tmp_path):
    home, env = _home(tmp_path, {"projects": {}})
    root = tmp_path / "ws"
    (root / ".orch").mkdir(parents=True)
    (root / ".mcp.json").write_text("{}")
    assert claude_folder.offered(env, root, allow_parent=True) is False
    assert claude_folder.trust_folder(env, root, allow_parent=True) is False


def test_a_refusal_names_the_file_found_in_the_folder(tmp_path):
    home, env = _home(tmp_path, {"projects": {}})
    root = tmp_path / "ws"
    (root / ".claude").mkdir(parents=True)
    why = claude_folder.refusal(env, root)
    assert why and str(root.resolve() / ".claude") in why


def test_claude_is_found_in_the_usual_install_dirs_when_path_is_short(tmp_path):
    home = tmp_path / "h"
    exe = home / ".local" / "bin" / "claude"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    assert claude_folder.claude_path(env) == str(exe) and claude_folder.claude_installed(env)


def test_trust_step_writes_the_consent_marker_the_runner_needs(tmp_path):
    home, env = _home(tmp_path, {"projects": {}})
    root = tmp_path / "ws"
    (root / ".orch").mkdir(parents=True)
    assert claude_folder.trust_folder(env, root) is True
    from orch.runner.claude_trust import _consented

    assert _consented(os.path.realpath(root))


def test_settings_can_grant_the_consent_later(tmp_path):
    home, env = _home(tmp_path, {"projects": {}})
    root = tmp_path / "ws"
    (root / ".orch").mkdir(parents=True)
    assert claude_folder.grant_consent(env, root) is None
    from orch.runner.claude_trust import _consented

    assert _consented(os.path.realpath(root))


def test_orchs_own_claude_md_in_a_fresh_setup_folder_is_fine_after_setup_wrote_it(tmp_path):
    home, env = _home(tmp_path, {"projects": {}})
    root = tmp_path / "work" / "ws"
    root.mkdir(parents=True)
    assert claude_folder.foreign_config(root, env) is False  # fresh: offered (if claude is installed)
    (root / "CLAUDE.md").write_text("@AGENTS.md\n")  # what setup writes
    assert claude_folder.trust_folder(env, root) is True


def _setup_folder(tmp_path: Path, name: str) -> tuple[Path, Path, dict[str, str]]:
    """A fresh workspace folder with what setup writes into it: the instructions, orch's skill copies in .claude."""
    from orch.instructions.harness import write_workspace_files

    (tmp_path / name).mkdir()
    home, env = _home(tmp_path / name, {"projects": {}})
    root = tmp_path / name / "work" / "ws"
    (root / ".orch").mkdir(parents=True)
    write_workspace_files(root, pointers=True, store=root / ".orch")
    assert any((root / ".claude" / "skills").iterdir())
    return home, root, env


def test_orchs_own_claude_folder_from_setup_is_offered_and_trusted(tmp_path):
    home, root, env = _setup_folder(tmp_path, "a")
    assert claude_folder.foreign_config(root, env) is False and claude_folder.refusal(env, root) is None
    assert claude_folder.offered(env, root) is True
    assert claude_folder.trust_folder(env, root) is True
    doc = json.loads((home / ".claude.json").read_text())
    assert doc["projects"][os.path.realpath(root)] == {"hasTrustDialogAccepted": True}
    home, root, env = _setup_folder(tmp_path, "b")
    assert claude_folder.grant_consent(env, root) is None  # the Settings switch


def test_anything_else_in_orchs_claude_folder_is_still_refused(tmp_path):
    from orch.instructions.skills import builtin_skills

    skill = builtin_skills()[0].name
    cases = {
        "settings": lambda r: (r / ".claude" / "settings.json").write_text('{"hooks": {}}'),
        "local-settings": lambda r: (r / ".claude" / "settings.local.json").write_text("{}"),
        "extra-skill": lambda r: (
            (r / ".claude" / "skills" / "mine").mkdir(),
            (r / ".claude" / "skills" / "mine" / "SKILL.md").write_text("---\nname: mine\n---\n"),
        ),
        "extra-file-in-orch-skill": lambda r: (r / ".claude" / "skills" / skill / "run.sh").write_text("x"),
        "edited-skill": lambda r: (r / ".claude" / "skills" / skill / "SKILL.md").write_text("changed\n"),
        "linked-skill": lambda r: (
            (r / ".claude" / "skills" / skill / "SKILL.md").unlink(),
            (r / ".claude" / "skills" / skill / "SKILL.md").symlink_to(r / ".agents" / "skills" / skill / "SKILL.md"),
        ),
        "commands": lambda r: (r / ".claude" / "commands").mkdir(),
        "memory": lambda r: (r / ".claude" / "CLAUDE.md").write_text("hi\n"),
        "mcp": lambda r: (r / ".mcp.json").write_text("{}"),
        "local-md": lambda r: (r / "CLAUDE.local.md").write_text("hi\n"),
    }
    for name, add in cases.items():
        home, root, env = _setup_folder(tmp_path, name)
        add(root)
        assert claude_folder.offered(env, root) is False, name
        assert claude_folder.offered(env, root, allow_parent=True) is False, name
        assert claude_folder.trust_folder(env, root) is False, name
        assert claude_folder.grant_consent(env, root) is not None, name
        assert json.loads((home / ".claude.json").read_text()) == {"projects": {}}, name
    home, root, env = _setup_folder(tmp_path, "named")
    (root / ".claude" / "settings.json").write_text("{}")
    assert str(root.resolve() / ".claude" / "settings.json") in (claude_folder.refusal(env, root) or "")


def test_a_claude_md_that_is_not_setups_own_is_still_refused(tmp_path):
    home, root, env = _setup_folder(tmp_path, "a")
    (root / "CLAUDE.md").write_text("@AGENTS.md\n@~/secrets.md\n")
    assert claude_folder.offered(env, root) is False and "CLAUDE.md" in (claude_folder.refusal(env, root) or "")


def test_a_failed_row_is_never_ready_a_macos_block_is_named_and_try_again_works(tmp):
    from tests.setup.fakes import FakePorts
    from tests.setup.test_create import progress

    ports = FakePorts()
    ports.blocked = ["io.orch.host"]
    ports.fail_runner = True
    f, ports, p, rv = run_ok(tmp, AGENT, ports=ports)
    assert p["done"] is False and p["error"]["code"] == "background.blocked" and f.phase == "failed"
    assert "Login Items & Extensions" in p["error"]["message"] and "Try again" in p["error"]["message"]
    ports.fail_runner, ports.blocked = False, []
    out = f.dispatch("POST", "retry", {}, {})[1]
    f.thread.join(10)
    p2 = progress(f, out["run_id"])
    assert p2["error"] is None and p2["done"] is True and f.phase == "done"
