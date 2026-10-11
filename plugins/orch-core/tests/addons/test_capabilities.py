"""Capabilities are a closed list, granted exactly as declared, and widen nothing in P1 (ticket-format §8.1, §11.4)."""

from __future__ import annotations

import json

import pytest

from orch import schema
from orch.addons.install import grant_event
from orch.addons.manifest import CAPABILITIES, ManifestError, load_manifest, load_package_manifest
from orch.addons.package import read_package
from tests.addons.helpers import make_package, manifest


def load(caps):
    return load_manifest(json.dumps(manifest(capabilities=caps)).encode())


def test_the_list_is_closed_and_equals_the_schemas():
    assert set(CAPABILITIES) == set(schema.load("common")["$defs"]["capability"]["enum"])
    assert CAPABILITIES == ("serve_http", "spawn_agent", "pty", "network", "git_push")
    for bad in ("root", "fs_write", "NETWORK", "keys", "sign", "append", ""):
        with pytest.raises(ManifestError):
            load([bad])
    with pytest.raises(ManifestError):
        load(["pty", "pty"])
    with pytest.raises(ManifestError):
        load("pty")


def test_the_grant_carries_exactly_the_manifests_capabilities_sorted(tmp_path):
    d = make_package(tmp_path, man=manifest(capabilities=["pty", "network", "serve_http"]))
    pkg = read_package(d)
    ev = grant_event(load_package_manifest(pkg, "echo"), pkg)
    assert ev["capabilities"] == ["network", "pty", "serve_http"]
    assert ev["type"] == "addon.granted" and ev["package_sha256"] == pkg.digest and ev["version"] == "1.0.0"


def test_a_changed_capability_is_a_changed_package_so_it_needs_a_new_grant(tmp_path):
    a = read_package(make_package(tmp_path / "a", man=manifest(capabilities=["network"]))).digest
    b = read_package(make_package(tmp_path / "b", man=manifest(capabilities=["network", "pty"]))).digest
    assert a != b


def test_no_capability_names_a_key_or_a_core_event():
    """A capability is about the process, never about the log: there is none that signs, appends or reads keys."""
    assert not {"sign", "append", "keys", "grant", "approve", "ticket"} & set(CAPABILITIES)
