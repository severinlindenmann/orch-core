"""V3-D38 (owner, 24 Oct): installing a marketplace add-on is ONE click in the browser. The click view carries the whole
review (every line the terminal shows, the marketplace block labelled "per host, not signed"), the confirmation is a
plain click even for an add-on with capabilities, and nothing is installed before the signature."""

# ruff: noqa: F811
from __future__ import annotations

import pytest

from tests.host import human_helpers
from tests.host import market_helpers as M
from tests.host.market_helpers import add_market, addon_dir, mfiles, ok, stage
from tests.host.test_click import Click, click  # noqa: F401

net = M.net
lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


@pytest.fixture
def ready(served, net):
    net.add("standup", "1.0.0", caps=("network", "spawn_agent"))
    add_market(served, net)
    return net


def test_a_market_install_is_one_click_with_the_whole_review(click, served, ready):
    st = ok(stage(served))["data"]
    p = click.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    v = click.view(p["request_id"])
    assert v["view"]["confirm"] == {"kind": "click"}  # no typed word, though it asks for network and spawn_agent
    text = "\n".join(v["view"]["lines"])
    lines = v["view"]["lines"]
    text = "\n".join(lines)
    assert f"package_sha256: {st['package_sha256']}" in lines  # the signed event, every field
    assert "network" in text and "spawn_agent" in text
    for label in ("marketplace", "marketplace commit", "source", "publisher", "index version"):
        assert any(x.startswith(f"{label} (per host, not signed): ") for x in lines), label  # the market block
    assert not addon_dir(served).exists() and mfiles(served).installs()["addons"] == {}  # nothing before the click
    click.mono.t += 2
    out = click.sign(p["request_id"], v["view_digest"])
    assert click.signatures == 1 and out["duplicate"] is False
