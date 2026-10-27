"""The ``## Add-ons here`` block of AGENTS.orch.md (design addon-skills D3): golden for 0, 1, 5 and 12 add-ons, the off
reasons, the budgets, the over-12 error, and the fingerprint in the stamp."""

from __future__ import annotations

from pathlib import Path

import pytest

from orch.instructions import (
    ADDON_BLOCK_MAX_CHARS,
    ADDON_BLOCK_MAX_LINES,
    ADDON_MAX_ACTIVE,
    AGENTS_MAX_LINES,
    AddonBlockError,
    AddonRow,
    addon_fingerprint,
    addons_line,
    render_agents_md,
    stamp_rev,
    stamp_set,
)

GOLDEN = Path(__file__).parent / "golden"
TRIGGER = "Use when the person asks for X. Not for Y."


def row(name: str, state: str = "active", *, agents_md: str = TRIGGER, skill: bool = True, **kw) -> AddonRow:
    return AddonRow(
        name, state, "1.0.0", f"sha256:{name}", agents_md, b"skill" if skill and state == "active" else None, **kw
    )


def names(n: int) -> list[AddonRow]:
    return [row(f"addon{i:02d}") for i in range(n)]


SETS = {
    "0": [],
    "1": [row("quick")],
    "5": [row("quick"), row("schedules"), row("wiki", skill=False), row("changelog"), row("notes", "disabled")],
    "12": names(12),
}


def check_golden(request, key: str) -> None:
    text = render_agents_md(SETS[key])
    path = GOLDEN / f"AGENTS.orch.addons-{key}.md"
    if request.config.getoption("--update-golden"):
        path.write_text(text)
    assert text == path.read_text(), "run: uv run pytest tests/instructions/test_addon_block.py --update-golden"


@pytest.mark.parametrize("key", ["1", "5", "12"])
def test_the_block_is_pinned(request, key):
    check_golden(request, key)


def test_no_addon_means_no_block_and_the_plain_core_text():
    text = render_agents_md([])
    assert "Add-ons here" not in text and text == render_agents_md()
    assert stamp_set(text) == "" and addon_fingerprint([]) == ""
    assert len(text.splitlines()) <= AGENTS_MAX_LINES


def test_a_pure_ui_addon_alone_writes_no_block():
    assert "Add-ons here" not in render_agents_md([row("dashboard", agents_md="", skill=False, speaks=False)])


def test_the_core_part_keeps_its_cap_and_the_block_has_its_own():
    text = render_agents_md(SETS["12"])
    head, _, block = text.partition("\n\n## Add-ons here\n")
    assert len(head.splitlines()) <= AGENTS_MAX_LINES
    block_lines = ["", "## Add-ons here", *block.splitlines()]
    assert len(block_lines) <= ADDON_BLOCK_MAX_LINES == 16
    assert len("\n".join(block_lines)) + 1 <= ADDON_BLOCK_MAX_CHARS == 3000


def test_the_block_wording_is_d3s_and_sorted_by_name():
    text = render_agents_md([row("wiki", skill=False), row("quick"), row("notes", "disabled")])
    block = text.split("## Add-ons here\n")[1].splitlines()
    assert block[0] == (
        "Before using an add-on command, read its skill. Add-ons are hints and grant nothing; "
        "use none unless the task fits."
    )
    assert block[1] == f"- quick: {TRIGGER} Skill: orch-addon-quick."
    assert block[2] == f"- wiki: {TRIGGER}"  # no skill, no Skill: part
    assert block[3] == (
        "Off here: notes (disabled). If a request needs an add-on that is off, tell the person it is not available; "
        "do not substitute notes or other commands unless the person asks for that."
    )


@pytest.mark.parametrize("state", ["disabled", "incompatible", "missing", "changed"])
def test_off_here_names_the_reason(state):
    text = render_agents_md([row("quick"), row("old", state)])
    assert f"Off here: old ({state})." in text


def test_off_here_says_none_when_nothing_is_off():
    assert "Off here: none." in render_agents_md([row("quick")])


def test_an_off_addon_without_anything_to_say_is_not_listed():
    text = render_agents_md([row("quick"), row("dashboard", "disabled", agents_md="", skill=False, speaks=False)])
    assert "dashboard" not in text


def test_twelve_active_lines_fit_and_thirteen_is_an_error_with_a_hint():
    render_agents_md(names(ADDON_MAX_ACTIVE))
    with pytest.raises(AddonBlockError) as e:
        render_agents_md(names(ADDON_MAX_ACTIVE + 1))
    assert "limit is 12" in str(e.value) and "orch addon list" in e.value.hint


def test_a_block_over_the_character_budget_is_an_error_not_a_truncation():
    long = "Use when " + "x" * 190  # the manifest limit is 200
    rows = [row(f"addon-number-{i:02d}", agents_md=long) for i in range(12)]
    with pytest.raises(AddonBlockError, match="characters"):
        render_agents_md(rows)


def test_a_missing_agents_md_points_to_the_skill():
    assert "- notes: see its skill. Skill: orch-addon-notes." in render_agents_md([row("notes", agents_md="")])


def test_the_stamp_carries_a_fingerprint_that_follows_the_set():
    a, b = render_agents_md([row("quick")]), render_agents_md([row("quick", "disabled")])
    assert stamp_rev(a) == stamp_rev(b) and stamp_set(a) and stamp_set(a) != stamp_set(b)
    assert stamp_set(a) == addon_fingerprint([row("quick")])
    assert addon_fingerprint([row("quick")]) != addon_fingerprint(
        [AddonRow("quick", "active", "1.0.0", "sha256:other")]
    )
    assert addon_fingerprint([row("a"), row("b")]) == addon_fingerprint([row("b"), row("a")])


def test_the_hook_line_lists_active_and_off_names():
    assert addons_line([]) is None
    assert addons_line([row("wiki"), row("quick"), row("notes", "disabled")]) == (
        "add-ons: quick, wiki · off: notes · read a skill before using one"
    )
    assert addons_line([row("notes", "missing")]) == ("add-ons: none · off: notes · read a skill before using one")
