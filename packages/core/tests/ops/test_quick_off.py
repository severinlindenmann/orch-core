"""Quick tasks is a built-in add-on: without its grant no NEW quick task can be made (CLI, agents), existing stay."""

from __future__ import annotations


def test_quick_add_and_the_label_are_refused_without_the_grant(ws, cli):
    r = cli("quick", "add", "small thing", "--json")
    assert r.code != 0 and "addon.off" in (r.out + r.err)
    assert cli("new", "x", "--label", "quick").code != 0
    assert cli("new", "plain").code == 0
    assert cli("set", "1", "labels=quick").code != 0
    assert cli("set", "1", "labels=other").code == 0
    assert cli("quick", "list").code != 0  # `quick list` is a command of the add-on: off with it
    assert cli("list").code == 0  # the tickets themselves stay readable
