# ruff: noqa: F811
"""Opus review G6 B2 (#1199), binding condition C6: the dev class may list the official ids, and ONLY the dev class.
A real workspace (even with the dev host object and the entry) and a normal host never reach it; a record made in the
sandbox does not carry the exception to a host without the class. Drop into tests/host/.
T0: uv run pytest tests/host/test_g6b2_review_c6_real_workspace.py -x -q"""

from __future__ import annotations

import dataclasses

import pytest

from orch.host import devmode
from orch.market import constants
from tests.host.dispatch_helpers import Served
from tests.host.lifecycle_helpers import Lab
from tests.host.market_helpers import err, ok, person, stage
from tests.host.test_market_dev import URL, fakes, sandbox  # noqa: F401

MOVED = ("model-routing", "github")


@pytest.mark.parametrize("name", MOVED)
def test_c6_the_sandbox_test_marketplace_stages_an_official_id(tmp_path, fakes, name):
    lab = sandbox(tmp_path)
    try:
        served = Served(lab, dev=lab.dev())
        try:
            fakes.add(name, "1.0.0")
            assert ok(person(served, "market.add", {"url": URL}))["data"]["class"] == "dev"
            st = ok(stage(served, name))["data"]
            assert st["market"]["class"] == "dev"
        finally:
            served.close()
    finally:
        lab.close()


@pytest.mark.parametrize("name", MOVED)
def test_c6_a_real_workspace_never_gets_the_exception(tmp_path, fakes, name):
    lab = sandbox(tmp_path)
    real = Lab(tmp_path / "real")
    try:
        served = Served(real)
        try:
            served.service.dev = dataclasses.replace(lab.dev())  # worst case: a dev host object on a real workspace
            assert devmode.test_market(served.service) is None
            fakes.add(name, "1.0.0")
            assert err(person(served, "market.add", {"url": URL})) == "market.community_unisolated"
            assert err(stage(served, name, url=URL)) in ("market.community_unisolated", "not_found")  # no record
            # and the official URL on a real workspace still has the official rule (id is official: fine there)
            assert name in constants.OFFICIAL_IDS
        finally:
            served.close()
    finally:
        real.close()
        lab.close()


@pytest.mark.parametrize("name", MOVED)
def test_c6_a_sandbox_record_does_not_carry_the_exception_to_a_normal_host(tmp_path, fakes, name):
    lab = sandbox(tmp_path)
    try:
        served = Served(lab, dev=lab.dev())
        try:
            fakes.add(name, "1.0.0")
            ok(person(served, "market.add", {"url": URL}))
            served.service.dev = None  # the same state directory, read by a normal host
            assert err(stage(served, name, url=URL)) == "market.community_unisolated"
        finally:
            served.close()
    finally:
        lab.close()
