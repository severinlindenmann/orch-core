"""V3 G3 (#921, C5): the sandbox's test marketplace. It exists only for a sandbox workspace on a dev host whose
``dev.json`` names it; a real workspace, a normal host and a dev host without the entry get ``community_unisolated``.
No address rule is relaxed anywhere (the fetch tests in tests/market cover the address checks)."""

# ruff: noqa: F811
from __future__ import annotations

import dataclasses
import json

import pytest

from orch import devsandbox
from orch.host import devmode
from orch.market import address, constants, fetch
from tests.host.devgate_helpers import SbxLab
from tests.host.dispatch_helpers import Served
from tests.host.lifecycle_helpers import Lab
from tests.host.market_helpers import Net, addon_dir, err, ok, person, stage
from tests.market import helpers as H

URL = "https://github.com/acme/orch-marketplace-test"


@pytest.fixture
def fakes(monkeypatch):
    n = Net()
    n.url = URL
    monkeypatch.setattr(fetch, "fetch_index", n.fetch_index)
    monkeypatch.setattr(fetch, "fetch_package", n.fetch_package)
    monkeypatch.setattr(address, "resolve", n.resolve)
    monkeypatch.setattr(constants, "OFFICIAL_KEYS", ())  # the dev class uses its own keys, never the official ones
    return n


def sandbox(tmp_path, market=True):
    lab = SbxLab(tmp_path, dev_json=False)
    devsandbox.write_dev_json(lab.sroot, lab.sandbox_id, "https://relay.dev.example",
                              {"url": URL, "keys": [H.KEY_B64]} if market else None)  # fmt: skip
    return lab


def test_a_sandbox_workspace_on_a_dev_host_can_add_install_and_run_the_test_marketplace(tmp_path, fakes):
    lab = sandbox(tmp_path)
    try:
        served = Served(lab, dev=lab.dev())
        try:
            assert lab.dev().market == (URL, (H.KEY_B64,))
            fakes.add("hello", "1.0.0")
            assert ok(person(served, "market.add", {"url": URL}))["data"]["class"] == "dev"
            row = ok(person(served, "market.list", {"entries": True}))["data"]["markets"][0]
            assert row["class"] == "dev" and row["cache"] == "ok" and row["verified"]
            st = ok(stage(served, "hello"))["data"]
            assert st["market"]["class"] == "dev" and st["market"]["publisher"]["tofu"] == "new"
        finally:
            served.close()
    finally:
        lab.close()


def test_the_dev_class_wants_the_dev_keys_not_just_any_signature(tmp_path, fakes):
    lab = sandbox(tmp_path)
    try:
        served = Served(lab, dev=lab.dev())
        try:
            from cryptography.hazmat.primitives.asymmetric import ec

            other = H.index(1, url=URL, key=ec.generate_private_key(ec.SECP256R1()))
            fakes.fetch_index = lambda url, commit: (H.COMMIT_A, H.raw(other))
            import orch.market.fetch as f

            f.fetch_index = fakes.fetch_index
            assert err(person(served, "market.add", {"url": URL})) == "market.index_unsigned"
        finally:
            served.close()
    finally:
        lab.close()


def test_a_dev_host_without_the_entry_refuses_the_test_url(tmp_path, fakes):
    lab = sandbox(tmp_path, market=False)
    try:
        served = Served(lab, dev=lab.dev())
        try:
            assert lab.dev().market is None
            assert err(person(served, "market.add", {"url": URL})) == "market.community_unisolated"
        finally:
            served.close()
    finally:
        lab.close()


def test_a_real_workspace_refuses_it_even_when_the_host_carries_the_entry(tmp_path, fakes):
    """The dev host object names the market, but the workspace is not a sandbox workspace (S1 to S6 fail): the class
    code returns community_unisolated, so the test marketplace is never reachable for a real workspace."""
    lab = sandbox(tmp_path)
    real = Lab(tmp_path / "real")
    try:
        dev = dataclasses.replace(lab.dev())
        served = Served(real)
        try:
            served.service.dev = dev  # the worst case: a dev host object on a real workspace
            assert devmode.test_market(served.service) is None
            assert err(person(served, "market.add", {"url": URL})) == "market.community_unisolated"
            assert err(person(served, "market.refresh", {"url": URL})) in ("market.community_unisolated", "not_found")
        finally:
            served.close()
    finally:
        real.close()
        lab.close()


def test_the_picker_offers_the_test_marketplace_until_it_is_added_and_a_normal_host_offers_nothing(tmp_path, fakes):
    lab = sandbox(tmp_path)
    try:
        served = Served(lab, dev=lab.dev())
        try:
            assert ok(person(served, "market.list", {}))["data"]["offered"] == [{"url": URL, "class": "dev"}]
            fakes.add("hello", "1.0.0")
            ok(person(served, "market.add", {"url": URL}))
            assert ok(person(served, "market.list", {}))["data"]["offered"] == []
        finally:
            served.close()
    finally:
        lab.close()
    real = Lab(tmp_path / "real")
    try:
        served = Served(real)
        try:
            assert ok(person(served, "market.list", {}))["data"]["offered"] == []
        finally:
            served.close()
    finally:
        real.close()


def test_a_normal_host_has_no_dev_class(tmp_path, fakes):
    real = Lab(tmp_path)
    try:
        served = Served(real)
        try:
            assert served.service.dev is None
            assert err(person(served, "market.add", {"url": URL})) == "market.community_unisolated"
        finally:
            served.close()
    finally:
        real.close()


def test_a_record_of_the_dev_class_is_not_trusted_when_the_class_is_gone(tmp_path, fakes):
    lab = sandbox(tmp_path)
    try:
        served = Served(lab, dev=lab.dev())
        try:
            fakes.add("hello", "1.0.0")
            ok(person(served, "market.add", {"url": URL}))
            served.service.dev = None  # the same state directory read by a normal host
            row = ok(person(served, "market.list", {"entries": True}))["data"]["markets"][0]
            assert row["cache"] == "invalid" and "addons" not in row
            assert err(stage(served, "hello", url=URL)) == "market.community_unisolated"
        finally:
            served.close()
    finally:
        lab.close()


@pytest.mark.parametrize(
    "market",
    [
        {"url": "http://github.com/a/b", "keys": [H.KEY_B64]},
        {"url": "https://127.0.0.1/a/b", "keys": [H.KEY_B64]},
        {"url": URL, "keys": []},
        {"url": URL, "keys": ["not a key"]},
        {"url": URL, "keys": [H.KEY_B64], "extra": 1},
        {"url": URL},
        "string",
    ],
)
def test_dev_json_market_is_strict(tmp_path, market):
    lab = SbxLab(tmp_path, dev_json=False)
    try:
        doc = {"v": 1, "sim_convenience": True, "sandbox_id": lab.sandbox_id, "root": {"st_dev": 1, "st_ino": 2},
               "relay_origin": "https://r.example", "market": market}  # fmt: skip
        with pytest.raises(devsandbox.SandboxRefused):
            devsandbox.parse_dev_json(json.dumps(doc).encode())
        with pytest.raises(devsandbox.SandboxRefused):
            devsandbox.write_dev_json(lab.sroot, lab.sandbox_id, "https://r.example", market)
    finally:
        lab.close()


# Delta: a dev-class stage whose class vanishes before submit (a normal host reads the same state) is refused.
def test_a_dev_stage_cannot_be_granted_once_the_dev_class_is_gone(tmp_path, monkeypatch):
    from tests.host.dispatch_helpers import Served
    from tests.host.human_helpers import Human

    n = Net()
    n.url = URL
    monkeypatch.setattr(fetch, "fetch_index", n.fetch_index)
    monkeypatch.setattr(fetch, "fetch_package", n.fetch_package)
    monkeypatch.setattr(address, "resolve", n.resolve)
    lab = sandbox(tmp_path)
    try:
        served = Served(lab, dev=lab.dev())
        try:
            n.add("hello", "1.0.0")
            ok(person(served, "market.add", {"url": URL}))
            st = ok(stage(served, "hello"))["data"]
            h = Human(served)
            prepared = h.prepare("addon.grant", {"name": "hello", "staged": st["stage"]})
            served.service.dev = None  # the class is gone between stage and submit
            res = h.submit_res(prepared["request"]["id"], h.sign(prepared).events)
            assert err(res) == "market.community_unisolated", res
            assert not (addon_dir(served, "hello")).exists()
        finally:
            served.close()
    finally:
        lab.close()
