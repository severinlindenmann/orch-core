"""Shared test data for the addon platform tests (imported as `addon_fixtures`)."""
GOOD = {
    "name": "hello-status", "title": "Hello status", "version": "0.1.0", "requires_api": "2",
    "kind": "in-process", "description": "Example", "capabilities": ["provider", "page", "settings"], "slots": [],
    "binaries": ["git"], "env": [], "entry": "hello_status:create",
    "menu": {"title": "Hello status", "icon": "status"},
    "settings_schema": [{"key": "greeting", "label": "Greeting", "type": "text", "default": "Hello"}],
    "actions": [],
}


def loaded(ws, obj, name="demo", **over):
    """A LoadedAddon around `obj`, bypassing discovery and trust (tests only)."""
    from orch.addons.api import AddonContext
    from orch.addons.loader import LoadedAddon
    from orch.addons.manifest import parse_manifest
    m = parse_manifest({**GOOD, "name": name, **over})
    return LoadedAddon(name, "custom", m, ws.root, obj, AddonContext(ws, name, manifest=m), "test")


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


import json as _json
from pathlib import Path as _Path

INIT = "from .provider import create  # noqa: F401\n"
PROVIDER = '''from orch.addons.api import Snapshot
from orch.addons.widgets import Card, Text


class P:
    id = "fake"
    kind = "status"
    interval_s = 60

    def scopes(self, ctx):
        return ["default"]

    def fetch(self, ctx, scope, previous):
        r = ctx.run(["git", "status", "--porcelain"])
        return Snapshot(self.id, scope, ctx.now(), items=({"id": "c", "label": "Changed", "role": "ok", "text": str(len(r.stdout.splitlines()))},))


class Addon:
    def __init__(self, ctx):
        self.providers = [P()]

    def widgets(self, slot, view):
        return [Card("Hello", (Text(view.workspace_name),))]


def create(ctx):
    return Addon(ctx)
'''


def make_addon(base, *, provider=PROVIDER, extra=None, manifest=None):
    """Write a hello-status addon folder under `base` and return it."""
    folder = _Path(base) / "hello-status"
    (folder / "hello_status").mkdir(parents=True)
    (folder / "orch-addon.json").write_text(_json.dumps(manifest or GOOD), encoding="utf-8")
    (folder / "README.md").write_text("# Hello status\n", encoding="utf-8")
    (folder / "hello_status" / "__init__.py").write_text(INIT, encoding="utf-8")
    (folder / "hello_status" / "provider.py").write_text(provider, encoding="utf-8")
    for rel, text in (extra or {}).items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text, encoding="utf-8")
    return folder
