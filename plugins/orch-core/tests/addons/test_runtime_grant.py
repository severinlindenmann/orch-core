"""V3 G4 (D6): the signed ``addon.granted`` names the dependencies the add-on installs after the signature."""

from __future__ import annotations

import json

import jsonschema
import pytest

from orch import schema
from orch.addons import install
from orch.addons.manifest import load_manifest
from orch.addons.package import from_files
from orch.market import MarketError


def dep_package(lock: bytes | None, with_runtime: bool = True):
    m = {
        "schema": "orch.addon/3", "name": "dep", "version": "1.0.0", "title": "Dep", "api": ">=1.0,<2",
        "entry": {"cmd": ["python3", "{pkg}/main.py"]},
    }  # fmt: skip
    if with_runtime:
        m["runtime"] = {"kind": "python-requirements", "lock": "requirements.txt"}
    files = {"orch-addon.json": json.dumps(m).encode(), "main.py": b"print(1)\n"}
    if lock is not None:
        files["requirements.txt"] = lock
    return from_files(files)


def test_grant_event_carries_runtime_and_the_locked_package_count():
    lock = ("idna==3.10 --hash=sha256:" + "a" * 64 + "\nsix==1.16.0 --hash=sha256:" + "b" * 64 + "\n").encode()
    pkg = dep_package(lock)
    ev = install.grant_event(load_manifest(pkg.manifest_bytes), pkg)
    assert ev["runtime"] == {"kind": "python-requirements", "lock": "requirements.txt", "packages": 2}
    sub = schema.load("event.addon.granted")["properties"]["runtime"]
    jsonschema.validate(ev["runtime"], sub)
    for bad in ({**ev["runtime"], "packages": 0}, {**ev["runtime"], "x": 1}, {"kind": "python-requirements"}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, sub)


def test_without_runtime_the_event_has_no_runtime_key():
    pkg = dep_package(None, with_runtime=False)
    assert "runtime" not in install.grant_event(load_manifest(pkg.manifest_bytes), pkg)


@pytest.mark.parametrize(
    ("lock", "code"),
    [(None, "market.lock_missing"), (b"x==1.0\n", "market.lock_invalid"), (b"", "market.lock_invalid")],
)
def test_a_missing_or_bad_lock_is_a_market_refusal_never_a_grant(lock, code):
    pkg = dep_package(lock)
    with pytest.raises(MarketError) as e:
        install.grant_event(load_manifest(pkg.manifest_bytes), pkg)
    assert e.value.code == code


def test_the_replayed_addon_keeps_runtime():
    from orch.model.types import Addon

    view = {
        "version": "1.0.0", "package_sha256": "sha256:" + "0" * 64, "capabilities": [], "fields": {}, "sections": [],
        "artifact_kinds": [], "enabled": True, "purged": False,
        "runtime": {"kind": "node-package-lock", "lock": "package-lock.json", "packages": 4},
    }  # fmt: skip
    got = install.as_addons({"dep": view})["dep"]
    assert isinstance(got, Addon) and got.runtime == view["runtime"]
    assert install.as_addons({"dep": {k: v for k, v in view.items() if k != "runtime"}})["dep"].runtime is None
