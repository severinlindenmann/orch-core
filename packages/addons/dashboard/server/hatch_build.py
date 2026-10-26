"""Build the production UI and include the repository's canonical addon manifest."""

import os
import shutil
import subprocess
from pathlib import Path

UI_ERROR = (
    "the dashboard UI could not be built: install Node >= 24 or build the UI first with "
    "`cd packages/addons/dashboard && npm ci && npm run build`. "
    "Build a wheel from the full checkout; UI sources are not included in the server sdist. "
    "For development only, ORCH_DASHBOARD_SKIP_UI=1 permits a wheel without a UI."
)


PREBUILT_ERROR = (
    "ORCH_DASHBOARD_REQUIRE_PREBUILT=1 but the prebuilt dashboard UI is missing or incomplete ({why}) in {static}. "
    "Build it first with `cd packages/addons/dashboard && npm ci && npm run build`; this mode never runs npm or Node."
)


def require_prebuilt(static):
    """Release mode: use the already-built page as is; never run npm or Node, never skip the UI."""
    if not (static / "index.html").is_file():
        why = "index.html not found"
    elif not (static / "assets").is_dir() or not any(p.is_file() for p in (static / "assets").rglob("*")):
        why = "assets/ is missing or empty"
    else:
        return static
    raise RuntimeError(PREBUILT_ERROR.format(why=why, static=static))


def ensure_ui(server):
    """Reuse a prebuilt UI, or build it in the full dashboard source tree."""
    static = Path(server) / "src/orch_dashboard/static"
    if os.environ.get("ORCH_DASHBOARD_REQUIRE_PREBUILT") == "1":
        return require_prebuilt(static)
    if os.environ.get("ORCH_DASHBOARD_SKIP_UI") == "1" or (static / "index.html").is_file():
        return static
    dashboard = Path(server).parent
    required = ("package.json", "package-lock.json", "src/main.prod.tsx", "scripts/package-static.mjs")
    if not all((dashboard / name).is_file() for name in required):
        raise RuntimeError(UI_ERROR)
    if not shutil.which("node") or not shutil.which("npm"):
        raise RuntimeError(UI_ERROR)
    try:
        version = subprocess.run(["node", "--version"], check=True, capture_output=True, text=True).stdout
        if int(version.strip().removeprefix("v").split(".")[0]) < 24:
            raise ValueError("Node >= 24 required")
        subprocess.run(["npm", "ci"], cwd=dashboard, check=True)
        subprocess.run(["npm", "run", "build"], cwd=dashboard, check=True)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise RuntimeError(UI_ERROR) from exc
    if not (static / "index.html").is_file():
        raise RuntimeError(UI_ERROR)
    return static


def get_build_hook():
    # Keep the decision function importable without installing the build backend.
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface

    class DashboardBuildHook(BuildHookInterface):
        def initialize(self, version, build_data):
            server = Path(self.root)
            manifest = server.parent / "orch-addon.json"
            if not manifest.is_file():
                manifest = server / "orch-addon.json"  # carried by the server sdist
            if not manifest.is_file():
                raise RuntimeError("dashboard orch-addon.json is missing; build from the full checkout")
            packaged_manifest = server / "src/orch_dashboard/orch-addon.json"
            shutil.copyfile(manifest, packaged_manifest)
            includes = build_data.setdefault("force_include", {})
            if self.target_name == "wheel" and version == "editable":
                # `uv sync` (development, CI's Python jobs) installs editable: the package data stays in the source
                # tree, so only the manifest copy is needed and no UI is built (those jobs have no Node 24).
                return
            if self.target_name == "wheel":
                static = ensure_ui(server)
                includes[str(packaged_manifest)] = "orch_dashboard/orch-addon.json"
                if static.is_dir():
                    includes[str(static)] = "orch_dashboard/static"
            elif self.target_name == "sdist":
                includes[str(manifest)] = "orch-addon.json"
                static = server / "src/orch_dashboard/static"
                if (static / "index.html").is_file():
                    includes[str(static)] = "src/orch_dashboard/static"

    return DashboardBuildHook
