import re
import shlex
from pathlib import Path

import typer.main

from orch.cli import app
from orch.core.model import yaml_load
from orch.instructions import sync

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILL = PLUGIN_ROOT / "skills" / "orch-setup" / "SKILL.md"
COMMAND = PLUGIN_ROOT / "commands" / "setup.md"
ROOT = typer.main.get_command(app)


def _command_names():
    return set(ROOT.commands)


def _resolve_command(tokens):
    """Walk tokens through the click command tree (groups then leaf commands).

    Returns (command, consumed) where `consumed` is how many leading tokens
    were used up resolving sub-commands (e.g. "instructions sync" -> 2).
    """
    cmd = ROOT
    consumed = 0
    for tok in tokens:
        sub = getattr(cmd, "commands", None)
        if sub and tok in sub:
            cmd = sub[tok]
            consumed += 1
            continue
        break
    return cmd, consumed


def _orch_spans(text):
    """Every backtick-quoted span that starts with the literal `orch `."""
    return [m.group(1) for m in re.finditer(r"`(orch [^`]*)`", text)]


def test_setup_skill_frontmatter_and_registration():
    text = SKILL.read_text(encoding="utf-8")
    meta = yaml_load(text.split("---\n")[1])
    assert meta["name"] == "orch-setup" and len(meta["description"]) > 80
    assert "orch-setup" in sync.SKILL_NAMES


def test_setup_skill_and_command_commands_and_flags_resolve_against_help():
    """Every `orch <cmd> [<subcmd>] --flag` mention, anywhere in the skill or the
    command file, must name a real (sub)command and only its real flags — checked
    against the exact (sub)command, not just `init`."""
    saw_a_command = False
    saw_a_flag = False
    for path in (SKILL, COMMAND):
        text = path.read_text(encoding="utf-8")
        for span in _orch_spans(text):
            tokens = shlex.split(span)[1:]  # drop the leading "orch"
            if not tokens:
                continue
            saw_a_command = True
            assert tokens[0] in _command_names(), (path.name, span, "unknown top-level command")
            cmd, consumed = _resolve_command(tokens)
            flags = {t for t in tokens[consumed:] if t.startswith("--")}
            if flags:
                saw_a_flag = True
            allowed = {opt for p in cmd.params for opt in p.opts}
            assert flags <= allowed, (path.name, span, flags - allowed)
    assert saw_a_command, "no `orch ...` mention found to check"
    assert saw_a_flag, "no flag mention found to check — this test would be vacuous"


def test_setup_skill_flag_resolution_actually_rejects_bad_flags():
    """Negative self-check: the resolution helper above must be able to fail,
    otherwise the positive test could pass vacuously."""
    cmd, _consumed = _resolve_command(["init", "--not-a-real-flag"])
    flags = {t for t in ["--not-a-real-flag"] if t.startswith("--")}
    allowed = {opt for p in cmd.params for opt in p.opts}
    assert not (flags <= allowed)

    cmd, _consumed = _resolve_command(["instructions", "sync", "--also-not-real"])
    flags = {t for t in ["--also-not-real"] if t.startswith("--")}
    allowed = {opt for p in cmd.params for opt in p.opts}
    assert not (flags <= allowed)


def test_setup_skill_asks_before_side_effects():
    text = SKILL.read_text(encoding="utf-8").lower()
    for phrase in ("uv tool install", "--adopt", "orch hooks install"):
        assert phrase in text
    assert "only after the user says yes" in text


def test_setup_skill_narrow_trigger_and_guard():
    text = SKILL.read_text(encoding="utf-8")
    meta = yaml_load(text.split("---\n")[1])
    description = meta["description"].lower()
    assert "directly ask" in description or "directly asks" in description
    assert "not trigger just because" in description
    body = text.split("---\n", 2)[2]
    assert "offer once" in body.lower()
    assert "orch setup --dismiss" in body


def test_setup_skill_covers_declined_steps_and_exact_tokens():
    text = SKILL.read_text(encoding="utf-8").lower()
    assert "don't promise the dashboard" in text or "do not promise the dashboard" in text
    assert "`commit`, `push`, `review`" in text
    assert "--harness claude-plugin" in text
    assert "duplicat" in text  # duplicate/duplicating the plugin's skills
    assert "--repo name[=path]" in text


def test_setup_skill_example_uses_different_prefixes():
    text = SKILL.read_text(encoding="utf-8")
    m = re.search(r"orch init[^`]*--prefix (\S+)[^`]*--tracker \"(\S+?)=", text)
    assert m, "no `orch init` example with --prefix and --tracker found"
    assert m.group(1) != m.group(2)


def test_setup_command_is_thin():
    text = COMMAND.read_text(encoding="utf-8")
    body = text.split("---\n", 2)[2] if text.startswith("---\n") else text
    prose = [line for line in body.splitlines() if line.strip()]
    assert len(prose) <= 15 and "orch-setup" in body


def test_skills_use_neutral_example_names():
    tickets = (PLUGIN_ROOT / "skills" / "orch-tickets" / "SKILL.md").read_text(encoding="utf-8")
    assert "--external ABC-123" in tickets
    setup = SKILL.read_text(encoding="utf-8")
    assert "--customer acme --prefix ACM" in setup and '--tracker "ABC=ABC-' in setup


def test_setup_skill_branches_on_harness_and_missing_cli():
    text = SKILL.read_text(encoding="utf-8")
    assert "--harness copilot" in text and "GitHub Copilot" in text
    assert "Do not pass `--harness claude-plugin` there" in text
    assert "`uv` is installed" in text and "Neither `orch` nor `uv` works" in text


def test_setup_skill_existing_workspace_and_doctor_items():
    text = SKILL.read_text(encoding="utf-8")
    assert "**Existing workspace:**" in text and "do not run `orch init` again" in text
    step5 = text.split("## 5.", 1)[1].split("## 6.", 1)[0]
    for code in ("`adopt`", "`repos`", "`hooks`", "`plugin`", "`harness`", "`skill-copies`"):
        assert f"- {code}:" in step5, code
    assert "`tickets`, `refine-ticket`, `work-on-ticket`" in step5


def test_setup_skill_checks_the_cli_version_before_init():
    text = SKILL.read_text(encoding="utf-8")
    before_init = text.split("## 4.", 1)[0]
    assert "**Version first:**" in before_init and "uv tool install --force" in before_init
    step5 = text.split("## 5.", 1)[1].split("## 6.", 1)[0]
    assert "- `legacy-plugin`:" in step5 and "orch-ticket-workflow@ai-convenience-store" in step5
