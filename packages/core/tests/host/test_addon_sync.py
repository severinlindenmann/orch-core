# ruff: noqa: F811
"""The host brings the instructions in line with the add-on set after a grant, a disable and a remove (design
addon-skills D2): AGENTS.orch.md gets the add-on block, the stamp the fingerprint."""

from __future__ import annotations

from pathlib import Path

import pytest

from orch import layout as layout_mod
from orch.instructions import stamp_set
from tests.host import human_helpers
from tests.host.market_helpers import add_market, files, net, ok, person, stage  # noqa: F401
from tests.host.market_helpers import install as do_install

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human
TRIGGER = "Use when someone asks for a standup. Not for notes."


def agents_text(served) -> str:
    root = Path(served.service.entry.root)
    return (layout_mod.store_of(root) / "AGENTS.orch.md").read_text()


@pytest.fixture
def installed(served, human, net):
    net.add("standup", "1.0.0", pkg=files("standup", "1.0.0", agents_md=TRIGGER))
    add_market(served, net)
    do_install(served, human)


def test_a_grant_writes_the_block_and_a_disable_moves_it_to_off_here(served, human, installed):
    text = agents_text(served)
    assert f"- standup: {TRIGGER}" in text and "Off here: none." in text and stamp_set(text)
    before = stamp_set(text)
    prepared = human.prepare("addon.disable", {"name": "standup"})
    ok(human.submit_res(prepared["request"]["id"], human.sign(prepared).events))
    text = agents_text(served)
    assert "- standup:" not in text and "Off here: standup (disabled)." in text
    assert stamp_set(text) != before


def test_a_remove_takes_it_out_of_the_instructions(served, human, installed):
    prepared = human.prepare("addon.disable", {"name": "standup"})
    ok(human.submit_res(prepared["request"]["id"], human.sign(prepared).events))
    ok(person(served, "market.uninstall", {"name": "standup"}))
    # the package is gone: the add-on is still in the log, disabled, with nothing to say about it any more
    text = agents_text(served)
    assert "- standup:" not in text


SKILL = (
    "---\nname: orch-addon-standup\ndescription: Use when a standup is asked for. Not for notes.\n---\n"
    "# Standup add-on (active)\n\n## Steps\n1. Run `orch show`.\n"
)


def skill_pkg(text: str = SKILL, version: str = "1.0.0") -> dict[str, bytes]:
    pkg = files("standup", version, agents_md=TRIGGER, api=">=1.2,<2", skill="skills/main")
    return {**pkg, "skills/main/SKILL.md": text.encode()}


def skill_file(served, base=".agents/skills") -> Path:
    return Path(served.service.entry.root) / base / "orch-addon-standup" / "SKILL.md"


def test_a_grant_writes_the_skill_and_a_disable_removes_it(served, human, net):
    net.add("standup", "1.0.0", pkg=skill_pkg())
    add_market(served, net)
    do_install(served, human)
    assert skill_file(served).read_text() == SKILL and skill_file(served, ".claude/skills").exists()
    assert "Skill: orch-addon-standup." in agents_text(served)
    root = Path(served.service.entry.root)
    assert ".agents/skills/orch-addon-*/" in (root / ".gitignore").read_text().splitlines()
    prepared = human.prepare("addon.disable", {"name": "standup"})
    ok(human.submit_res(prepared["request"]["id"], human.sign(prepared).events))
    assert not skill_file(served).exists() and not skill_file(served, ".claude/skills").exists()
    assert "Off here: standup (disabled)." in agents_text(served)


def test_an_update_rewrites_the_skill_and_the_review_shows_the_diff(served, human, net):
    net.add("standup", "1.0.0", pkg=skill_pkg())
    add_market(served, net)
    do_install(served, human)
    new = SKILL.replace("Run `orch show`.", "Run `orch show`, then `orch status`.")
    net.add("standup", "1.1.0", pkg=skill_pkg(new, "1.1.0"))
    net.version = 2
    ok(person(served, "market.refresh"))
    st = ok(stage(served, update=True))["data"]
    review = "\n".join(st["review"])
    assert "skill text changed" in review and "widened: yes" in review
    assert "skill diff: +1. Run `orch show`, then `orch status`." in review
    assert "skill orch-addon-standup" in review  # the install review shows the new text
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    ok(human.submit_res(prepared["request"]["id"], human.sign(prepared).events))
    assert skill_file(served).read_text() == new


def test_the_files_are_written_from_the_state_after_the_append(served, human, installed):
    """The stamp is the fingerprint of the set the log has now, not of the one before the event."""
    from orch.instructions import addon_fingerprint, collect_rows

    store = served.service.store

    def now_rows():
        return collect_rows(store.layout.store, store.state.workspace.addons)

    assert stamp_set(agents_text(served)) == addon_fingerprint(now_rows()) != ""
    prepared = human.prepare("addon.disable", {"name": "standup"})
    ok(human.submit_res(prepared["request"]["id"], human.sign(prepared).events))
    assert stamp_set(agents_text(served)) == addon_fingerprint(now_rows())
    assert [r.state for r in now_rows()] == ["disabled"]


def test_a_failing_refresh_never_fails_the_signed_request(served, human, net, monkeypatch):
    from orch.host import addonsync

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(addonsync, "refresh", boom)
    net.add("standup", "1.0.0", pkg=files("standup", "1.0.0", agents_md=TRIGGER))
    add_market(served, net)
    _st, _prepared, res = do_install(served, human)
    assert any(x.startswith("could not refresh the agent instructions") for x in res["lines"])
    assert "standup" in served.service.store.state.workspace.addons
