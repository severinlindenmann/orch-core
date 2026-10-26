"""Add-on API compatibility: the ``incompatible`` registry state (addon-api.md, design addon-platform §4)."""

from __future__ import annotations

import json

import pytest

from orch.addons.manifest import ADDON_API, declarations, load_manifest
from orch.addons.registry import ACTIVE, INCOMPATIBLE, STATES, Registry, state_of
from orch.model.types import Addon
from tests.addons.helpers import manifest

DIGEST = "sha256:" + "1" * 64


def load(**over):
    return load_manifest(json.dumps(manifest(**over)).encode())


def reg(m, core="2.0.0.dev60", api=ADDON_API, **flags):
    a = Addon(m.name, m.version, DIGEST, m.capabilities, **declarations(m))
    for k, v in flags.items():
        setattr(a, k, v)
    return Registry({m.name: a}, {m.name: (m, DIGEST)}, core, api), a


def test_incompatible_is_a_named_state():
    assert INCOMPATIBLE == "incompatible" and INCOMPATIBLE in STATES


def test_a_v3_manifest_that_fits_is_active():
    r, _ = reg(load(schema="orch.addon/3", api=">=1.0,<2", min_core="2.0.0.dev60"))
    assert r.state("echo") == ACTIVE and r.active() == ["echo"] and r.incompatible_reason("echo") is None


def test_a_legacy_v2_manifest_is_read_as_api_1():
    r, _ = reg(load())
    assert r.state("echo") == ACTIVE
    r2, _ = reg(load(), api="2.0")  # a core with API 2 no longer fits a /2 manifest
    assert r2.state("echo") == INCOMPATIBLE


@pytest.mark.parametrize(
    ("api", "core_api"), [(">=1.1,<2", "1.0"), (">=2.0,<3", "1.5"), ("<1.0", "1.0"), ("==1.2", "1.3"), (">1.0", "1.0")]
)
def test_an_api_range_that_does_not_fit_is_incompatible_not_active(api, core_api):
    r, _ = reg(load(schema="orch.addon/3", api=api), api=core_api)
    assert r.state("echo") == INCOMPATIBLE and r.active() == [] and r.manifest("echo") is None
    assert "API" in r.incompatible_reason("echo")


@pytest.mark.parametrize(
    ("api", "core_api"), [(">=1.1,<2", "1.1"), (">=1.0,<2", "1.9"), ("==1.2", "1.2"), ("<=1.0", "1.0")]
)
def test_api_ranges_that_fit(api, core_api):
    r, _ = reg(load(schema="orch.addon/3", api=api), api=core_api)
    assert r.state("echo") == ACTIVE


@pytest.mark.parametrize(
    ("min_core", "core", "ok"),
    [
        ("2.0.0.dev60", "2.0.0.dev59", False),
        ("2.0.0.dev60", "2.0.0.dev60", True),
        ("2.0.0.dev60", "2.0.0.dev100", True),
        ("2.0.0.dev60", "2.0.0", True),  # a release sorts after its dev builds
        ("2.0.0", "2.0.0.dev999", False),
        ("2.1", "2.0.9", False),
        ("2.1", "2.1.0", True),
        ("2", "3.0.0", True),
    ],
)
def test_min_core(min_core, core, ok):
    r, _ = reg(load(schema="orch.addon/3", api=">=1.0,<2", min_core=min_core), core=core)
    assert r.state("echo") == (ACTIVE if ok else INCOMPATIBLE)


def test_an_unparseable_core_version_fails_closed_when_min_core_is_set():
    r, _ = reg(load(schema="orch.addon/3", api=">=1.0,<2", min_core="2.0.0"), core="banana")
    assert r.state("echo") == INCOMPATIBLE


def test_an_unparseable_core_api_fails_closed():
    r, _ = reg(load(), api="banana")
    assert r.state("echo") == INCOMPATIBLE


def test_other_states_win_over_incompatible():
    m = load(schema="orch.addon/3", api=">=2.0,<3")
    assert reg(m, enabled=False)[0].state("echo") == "disabled"
    assert reg(m, purged=True)[0].state("echo") == "purged"
    a = Addon(m.name, m.version, DIGEST, m.capabilities, **declarations(m))
    assert state_of(a, None) == "missing"
    assert state_of(a, (m, "sha256:" + "2" * 64)) == "changed"  # the package is not the granted one
    assert state_of(a, (m, DIGEST)) == INCOMPATIBLE


def test_an_incompatible_addon_accepts_no_writes_and_no_registration():
    r, _ = reg(load(schema="orch.addon/3", api=">=2.0,<3"))
    assert r.field_spec("echo", "points") is None and r.sections_for("feature") == []
    assert r.needs_rules() == [] and r.artifact_kinds("echo") == []
