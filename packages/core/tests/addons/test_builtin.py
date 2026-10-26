"""G6 B2 (#924): ``github`` and ``model-routing`` are no longer built in. Their ids stay reserved for the official
marketplace; nothing in core, setup or the CLI offers them. T0: ``uv run pytest tests/addons/test_builtin.py -x -q``"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from orch.addons import builtin
from orch.cli.main import main
from orch.host import marketinstall
from orch.market import MarketError
from orch.market import constants as c
from orch.market import install as mi
from orch.setup import facts

MOVED = ("github", "model-routing")
REPO = Path(__file__).resolve().parents[4]


def test_the_built_in_names_are_the_tech_agnostic_ones():
    assert builtin.NAMES == (
        "start-agent", "terminals", "worktrees", "dashboard", "publish", "usage", "estimate", "explorer", "wiki",
        "quick",
    )  # fmt: skip
    for name in MOVED:
        with pytest.raises(ValueError):
            builtin.manifest_bytes(name)
        assert not (Path(builtin.__file__).parent / "manifests" / f"{name}.json").exists()
        assert not (REPO / "addons" / name).exists()


@pytest.mark.parametrize("name", MOVED)
def test_the_cli_cannot_print_or_write_a_moved_manifest(name):
    err = io.StringIO()
    assert main(["addon", "manifest", name], env={}, stdout=io.StringIO(), stderr=err) == 2


def test_a_fresh_setup_offers_neither_of_them():
    names = {a.id for a in facts.catalog(optional=True)}
    assert not names & set(MOVED)
    assert not facts.OPTIONAL & set(MOVED)


def test_the_ids_stay_reserved_for_the_official_marketplace():
    assert set(MOVED) <= set(c.OFFICIAL_IDS) and c.MOVED_IDS == ()
    doc = lambda i: {  # noqa: E731
        "addons": [{"id": i, "version": "1.0.0", "source": {"git": "https://github.com/acme/market"}}]
    }

    def ctx(cls):
        return mi.Ctx(
            url="https://github.com/acme/market", cls=cls, keys=(), core_version="2.0.0.dev0",
            git_hosts=frozenset(c.DEFAULT_GIT_HOSTS), installed={}, known_keys={}, seen={},
            official_ids=frozenset(c.OFFICIAL_IDS),
        )  # fmt: skip

    for name in MOVED:
        with pytest.raises(MarketError) as e:  # a community marketplace never claims them
            mi.check_entry(ctx("community"), doc(name), name)
        assert e.value.code == "market.reserved_id"
    with pytest.raises(MarketError) as e:  # the dev class does not lift the built-in ids
        mi.check_entry(ctx("dev"), doc("wiki"), "wiki")
    assert e.value.code == "market.reserved_id"


def test_the_sandbox_test_marketplace_may_carry_the_official_ids():
    """Only the dev class (a dev host's workspace that passes S1..S6; ``policy.gate`` hands out no other)."""
    doc = lambda i: {  # noqa: E731
        "addons": [
            {
                "id": i,
                "version": "1.0.0",
                "source": {"git": "https://github.com/acme/market"},
                "min_orch": "0.0.1",
                "package_sha256": "sha256:" + "0" * 64,
            }
        ]
    }
    ctx = mi.Ctx(
        url="https://github.com/acme/market", cls="dev", keys=(), core_version="2.0.0.dev0",
        git_hosts=frozenset(c.DEFAULT_GIT_HOSTS), installed={}, known_keys={}, seen={},
        official_ids=frozenset(c.OFFICIAL_IDS),
    )  # fmt: skip
    for name in MOVED:
        try:
            mi.check_entry(ctx, doc(name), name)
        except MarketError as e:  # a later step may refuse this bare entry, never the reserved-id rule
            assert e.code != "market.reserved_id", name


def test_a_plain_grant_of_a_moved_id_is_not_installed():
    class Files:
        def installs(self):
            return {marketinstall.ADDONS: {}}

    for name in MOVED:
        ev = {"name": name, "version": "1.0.0", "package_sha256": "sha256:" + "1" * 64}
        with pytest.raises(MarketError) as e:
            marketinstall.gate_plain(Files(), ev)
        assert e.value.code == "market.not_installed"
