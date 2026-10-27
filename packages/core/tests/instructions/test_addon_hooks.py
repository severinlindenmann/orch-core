# ruff: noqa: F811
"""The hooks carry the dynamic add-on set (design addon-skills D4): the line at session start and before compaction,
and the session-start half of the sync."""

from __future__ import annotations

from orch.instructions import PRE_COMPACT_MAX_LINES, SESSION_START_MAX_LINES, stamp_set, write_workspace_files
from orch.instructions.harness import AGENTS_FILE
from tests.addons.helpers import installed_package, manifest
from tests.ops.humans import agent, hws, me  # noqa: F401  (fixtures)

TEXT = "---\nname: orch-addon-echo\ndescription: Use when echo. Not for x.\n---\n# Echo\n\n1. Run `orch show`.\n"
LINE = "add-ons: echo · off: none · read a skill before using one"


def grant_echo(hws, me):
    man = manifest(
        schema="orch.addon/3", api=">=1.2,<2", min_core="2.0.0.dev0", skill="skills/main", capabilities=[],
        agents_md="Use when the person says echo. Not for notes.",
    )  # fmt: skip
    installed_package(hws, man=man, files={"skills/main/SKILL.md": TEXT.encode()})
    assert me("addon", "grant", "echo").code == 0


def hook(agent, event):
    r = agent("instructions", "hook", event)
    assert r.code == 0, r.err
    return r.out.splitlines()


def test_without_an_addon_there_is_no_addon_line(hws, agent):
    assert not any(x.startswith("add-ons:") for x in hook(agent, "session-start") + hook(agent, "pre-compact"))


def test_session_start_and_pre_compact_carry_the_line_before_the_next_line(hws, me, agent):
    grant_echo(hws, me)
    start, compact = hook(agent, "session-start"), hook(agent, "pre-compact")
    assert start[-2] == LINE and start[-1].startswith("next: ") and len(start) <= SESSION_START_MAX_LINES
    assert compact[-2] == LINE and compact[-1].startswith("next: ") and len(compact) <= PRE_COMPACT_MAX_LINES


def test_a_disabled_addon_moves_to_the_off_part_of_the_line(hws, me, agent):
    grant_echo(hws, me)
    assert me("addon", "disable", "echo").code == 0
    assert "add-ons: none · off: echo · read a skill before using one" in hook(agent, "session-start")


def test_session_start_syncs_the_addon_part_when_the_fingerprint_differs(hws, me, agent):
    # files written for the empty set, then the add-on arrives without the files following (a pull from another machine)
    write_workspace_files(hws.root, pointers=False, store=hws.store_dir)
    grant_echo(hws, me)
    path = hws.store_dir / AGENTS_FILE
    path.write_text(path.read_text().split("\n\n## Add-ons here")[0])  # back to the old stamp text
    stamp = path.read_text().splitlines()[1]
    path.write_text(path.read_text().replace(stamp, stamp.replace(f" add-ons {stamp_set(path.read_text())}", "")))
    skill = hws.root / ".agents/skills/orch-addon-echo/SKILL.md"
    skill.unlink()
    out = hook(agent, "session-start")
    assert "instructions stale: run orch instructions sync" not in out  # it was brought up to date, not reported
    assert skill.read_text() == TEXT and "Skill: orch-addon-echo." in path.read_text()
    assert stamp_set(path.read_text())


def test_an_unchanged_set_writes_nothing_at_session_start(hws, me, agent):
    grant_echo(hws, me)
    before = (hws.store_dir / AGENTS_FILE).stat().st_mtime_ns
    skill = hws.root / ".agents/skills/orch-addon-echo/SKILL.md"
    skill.write_text("my edit\n")
    hook(agent, "session-start")
    assert (hws.store_dir / AGENTS_FILE).stat().st_mtime_ns == before and skill.read_text() == "my edit\n"


def stale_agents(hws, me):
    """Files written for the empty set, then an add-on arrives without the files following."""
    write_workspace_files(hws.root, pointers=False, store=hws.store_dir)
    grant_echo(hws, me)
    skill = hws.root / ".agents/skills/orch-addon-echo"
    for p in list(skill.rglob("*"))[::-1]:
        p.unlink() if p.is_file() else p.rmdir()
    skill.rmdir()
    path = hws.store_dir / AGENTS_FILE
    head = path.read_text().split("\n\n## Add-ons here")[0].splitlines()
    head[1] = head[1].replace(f" add-ons {stamp_set(path.read_text())}", "")
    path.write_text("\n".join(head) + "\n")


def test_orch_worktree_in_the_env_is_never_a_place_to_write(hws, me, agent, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    stale_agents(hws, me)
    r = agent("instructions", "hook", "session-start", ORCH_WORKTREE=str(elsewhere))
    assert r.code == 0, r.err
    assert list(elsewhere.iterdir()) == []
    assert (hws.root / ".agents/skills/orch-addon-echo/SKILL.md").exists()  # the root was synced


def test_a_hosted_hook_never_runs_the_sync(hws, me, agent, monkeypatch):
    """The host syncs after add-on events; the hosted read hook has no side effect (the spy proves the guard: with
    ``hosted`` forced on, the handler still reaches the sync call site, because the direct store serves it)."""
    import dataclasses

    import orch.instructions as instructions
    from orch.ops.commands import instructions_hook as mod

    stale_agents(hws, me)
    calls = []
    monkeypatch.setattr(instructions, "sync_if_changed", lambda *a, **k: calls.append(1) or True)
    real = mod.handle
    reached = []

    def hosted(ctx, args):
        reached.append(1)
        return real(dataclasses.replace(ctx, hosted=True), args)

    original = mod.OP.handler
    object.__setattr__(mod.OP, "handler", hosted)  # the operation is frozen
    try:
        hook(agent, "session-start")
    finally:
        object.__setattr__(mod.OP, "handler", original)
    assert reached and calls == [] and (hws.store_dir / AGENTS_FILE).read_text().count("Add-ons here") == 0
