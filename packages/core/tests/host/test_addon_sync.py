# ruff: noqa: F811
"""The host brings the instructions in line with the add-on set after a grant, a disable and a remove (design
addon-skills D2): AGENTS.orch.md gets the add-on block, the stamp the fingerprint."""

from __future__ import annotations

from pathlib import Path

import pytest

from orch import layout as layout_mod
from orch.instructions import stamp_set
from tests.host import human_helpers
from tests.host.market_helpers import add_market, files, net, ok, person  # noqa: F401
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
