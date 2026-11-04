"""The feature registry (webapi/features.py): a ``*_api.py`` module that is not listed would be silently unwired."""

from __future__ import annotations

import importlib
from pathlib import Path

import orch_hub.webapi as webapi_pkg
from orch_hub.webapi.api import ROUTES, WebApi
from orch_hub.webapi.features import FEATURES


def test_every_api_module_is_registered() -> None:
    root = Path(webapi_pkg.__file__).parent
    listed = {f.mixin.__module__.rsplit(".", 1)[-1] for f in FEATURES}
    on_disk = {p.stem for p in root.glob("*_api.py")}
    assert on_disk - listed == set(), f"add a Feature line in webapi/features.py for: {sorted(on_disk - listed)}"
    for name in listed:
        importlib.import_module(f"orch_hub.webapi.{name}")


def test_mixins_are_bases_of_webapi() -> None:
    assert all(issubclass(WebApi, f.mixin) for f in FEATURES)


def test_route_handlers_exist_and_method_path_unique() -> None:
    handlers = [r.handler for r in ROUTES]
    keys = [(r.method, r.path) for r in ROUTES]
    assert len(keys) == len(set(keys)), "two routes share a method and path"
    assert all(hasattr(WebApi, "_h_" + h) for h in handlers)
