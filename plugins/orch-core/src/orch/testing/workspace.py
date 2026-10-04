from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FakeWorkspace:
    root: Path
    ws: object
    tickets: list

    @property
    def state_dir(self) -> Path:
        return self.ws.state_dir

    def enable(self, name: str, config: dict | None = None) -> None:
        from orch.addons import userfiles
        userfiles.set_enabled(self.root, name, True)
        if config is not None:
            userfiles.save_addon_config(self.root, name, config)

    def context(self, manifest, *, runner=None):
        from orch.addons.api import AddonContext
        return AddonContext(self.ws, manifest.name, manifest=manifest, runner=runner)

    def provider_context(self, manifest, *, runner=None):
        return self.context(manifest, runner=runner).provider_context()

    def load(self, folder, *, runner=None):
        """Import an addon folder without trust and wrap it (tests only)."""
        from orch.addons.discovery import Found
        from orch.addons.loader import LoadedAddon, import_entry
        from orch.addons.manifest import load_manifest
        from orch.addons.userfiles import folder_hash
        m = load_manifest(folder)
        digest = "test-" + folder_hash(folder)
        ctx = self.context(m, runner=runner)
        obj = import_entry(Found(m.name, "custom", Path(folder), m), digest)(ctx)
        return LoadedAddon(m.name, "custom", m, Path(folder), obj, ctx, digest)

    def cache(self, addon: str, snapshot) -> bool:
        from orch.addons.cache import write_snapshot
        return write_snapshot(self.ws, addon, snapshot)


def fake_workspace(path, *, customer: str = "acme", prefix: str = "DEMO", tickets=(), repos=None,
                   suggested_addons=(), harnesses=("claude",), trackers=()) -> FakeWorkspace:
    """A workspace in `path` with these tickets: each spec is {title, status="backlog", type="feature", size="m",
    priority="normal", external=[keys], sections={name: text}, meta={...}} (meta is merged last). `trackers` are
    external_trackers entries {prefix, pattern, url}."""
    from orch.clock import stamp
    from orch.core import store
    from orch.core.ids import next_id
    from orch.core.model import new_ticket
    from orch.core.workspace import Workspace

    root = Path(path)
    home = root / "orchestrator"
    home.mkdir(parents=True, exist_ok=True)
    cfg = {"schema": 1, "customer": customer, "id": {"prefix": prefix, "pad": 4}, "harnesses": list(harnesses),
           "git": {"repos": dict(repos or {})}, "suggested_addons": list(suggested_addons),
           "external_trackers": [dict(t) for t in trackers]}
    (home / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    ws = Workspace.open(root)
    ids = []
    for spec in tickets:
        t = new_ticket(next_id(ws), spec["title"], type=spec.get("type", "feature"), priority=spec.get("priority", "normal"),
                       size=spec.get("size", "m"), created=stamp())
        t.meta["status"] = spec.get("status", "backlog")
        t.meta["external"] = [{"key": k, "url": None} for k in spec.get("external", [])]
        t.meta.update(spec.get("meta") or {})
        for name, text in (spec.get("sections") or {}).items():
            t.set_section(name, text)
        store.save(ws, t)
        ids.append(t.id)
    return FakeWorkspace(root, ws, ids)
