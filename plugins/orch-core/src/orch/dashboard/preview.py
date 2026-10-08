"""`orch addon preview` (#251): one addon slot drawn with Mission Control's own templates, CSS and widget code into a
self-contained HTML file, without a server and without the dashboard token, so an agent can look at what it built.

Read-only by construction: no action, decision or refresh runs, and the page carries no token. Providers run only
with --fetch (the addon's own ctx.run, same allowlist and scrubbed environment) or --fixtures (recorded output), and
either way they write into a temporary copy of the addon's state folder, never into the workspace's cache.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from orch.errors import UsageError

THEMES = ("light", "dark")
PANEL_SLOTS = ("ticket.code", "ticket.sync", "ticket.external", "ticket.pages", "board.external", "today.summary")
DECISIONS = "today.from_addons"
STATIC = Path(__file__).with_name("static")
CSS_FILES = ("tokens.css", "app.css", "tasks.css")


class _PreviewWorkspace:
    """The real workspace for reading (config, tickets, settings), with its own state folder and only one addon."""

    def __init__(self, ws, state_dir: Path):
        self.__dict__["_ws"] = ws
        self.__dict__["state_dir"] = state_dir
        self.__dict__["addons"] = None

    def __getattr__(self, name):
        return getattr(self.__dict__["_ws"], name)

    def __setattr__(self, name, value):
        self.__dict__[name] = value


def _refuse(argv, timeout, env=None):
    from orch.addons.runner import AddonRunError
    raise AddonRunError("a preview runs no commands; pass --fetch or --fixtures to fetch data first")


def _find(ws, target: str):
    """An addon folder, or the name of a known addon (as `orch addon check` takes it)."""
    from orch.addons.discovery import Found, find
    from orch.addons.manifest import load_manifest
    path = Path(target).expanduser()
    if path.is_dir():
        m = load_manifest(path)
        return Found(m.name, "custom", path.resolve(), m)
    found = find(target) if len(path.parts) == 1 else None
    if found is None:
        raise UsageError(f"no addon {target!r}: pass the folder that holds orch-addon.json or a known addon's name")
    return found


def _slots(manifest) -> list[str]:
    out = [f"page.{manifest.name}"] if manifest.has("page") else []
    out += [s for s in PANEL_SLOTS if s in (manifest.slots or ())]
    if manifest.has("decisions"):
        out.append(DECISIONS)
    return out


def _copy_state(ws, name: str, state: Path) -> None:
    src = Path(ws.state_dir) / "addons" / name
    dst = state / "addons" / name
    if src.is_dir():
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.lock"), dirs_exist_ok=True)
    dst.mkdir(parents=True, exist_ok=True)


def _css() -> str:
    """The dashboard's stylesheets, with their url()s pointing at the installed static folder (fonts, icons)."""
    out = []
    for name in CSS_FILES:
        text = (STATIC / name).read_text(encoding="utf-8")

        def absolute(m):
            ref = m.group(2).split("?")[0]
            if re.match(r"^(data:|https?:|#)", ref):
                return m.group(0)
            local = STATIC / ref.removeprefix("/static/") if ref.startswith("/static/") else STATIC / ref
            return f'url("{local.resolve().as_uri()}")' if not ref.startswith("/") or ref.startswith("/static/") \
                else m.group(0)
        out.append(re.sub(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", absolute, text))
    return "\n".join(out)


def render(ws, target: str, slot: str, *, ticket: str | None = None, params: dict | None = None,
           width: int | None = None, theme: str = "light", fetch: bool = False, fixtures: Path | None = None) -> str:
    """The HTML of one slot of one addon, as Mission Control draws it."""
    from orch.addons import cache
    from orch.addons.api import AddonContext
    from orch.addons.loader import AddonRegistry, LoadedAddon, import_entry
    from orch.addons.runtime import AddonRuntime, ticket_prefix
    from orch.addons.userfiles import folder_hash

    if theme not in THEMES:
        raise UsageError(f"--theme is light or dark, not {theme!r}")
    found = _find(ws, target)
    m = found.manifest
    known = _slots(m)
    if slot not in known:
        raise UsageError(f"{m.name} draws nothing in {slot!r}", hint="its slots: " + (", ".join(known) or "none"))
    if slot.startswith("ticket.") and not ticket:
        raise UsageError(f"{slot} is part of a ticket page: pass --ticket <id>")
    t = None
    if ticket:
        from orch.core import store
        try:
            t = store.read_ticket(store.resolve(ws, ticket).path)
        except Exception:
            raise UsageError(f"no ticket {ticket!r} in this workspace") from None
    width = width or (360 if slot.startswith("ticket.") else 1280)

    with tempfile.TemporaryDirectory(prefix="orch-preview-") as tmp:
        state = Path(tmp) / "state"
        _copy_state(ws, m.name, state)
        pws = _PreviewWorkspace(ws, state)
        if fixtures:
            from orch.testing import FakeRunner
            runner = FakeRunner.from_dir(Path(fixtures), strict=False)
        else:
            runner = None if fetch else _refuse
        ctx = AddonContext(pws, m.name, manifest=m, kind=found.kind, runner=runner)
        digest = "preview-" + folder_hash(found.folder)
        obj = import_entry(found, digest)(ctx)
        la = LoadedAddon(m.name, found.kind, m, Path(found.folder), obj, ctx, digest)
        pws.addons = AddonRegistry(pws, {m.name: la})
        if fetch or fixtures:
            pctx = ctx.provider_context()
            for provider in la.providers():
                for scope in provider.scopes(pctx):
                    cache.write_snapshot(pws, m.name, provider.fetch(pctx, scope, None))

        runtime = AddonRuntime(pws)
        group, groups, decisions = None, [], []
        if slot.startswith("page."):
            group = runtime.page(m.name, params=dict(params or {}))
        elif slot == DECISIONS:
            decisions = [(a, d) for a, d in runtime.decisions() if a == m.name]
            groups = runtime.slot(DECISIONS)
        else:
            groups = runtime.slot(slot, t, dict(params or {}) if slot == "board.external" else None,
                                  slot == "board.external")
        from markupsafe import Markup

        from orch.dashboard.views import TEMPLATES
        # css is core's own static CSS (never addon data), so it may skip escaping
        return TEMPLATES.get_template("preview.html").render(
            css=Markup(_css()), theme=theme, width=width, slot=slot, addon=m.name, title=m.title, version=m.version,
            ticket=t, group=group, groups=groups, decisions=decisions, can_refresh=False,
            key_prefix=ticket_prefix(ws), params=params or {})


def screenshot(html_file: Path, png: Path, width: int, theme: str) -> None:
    """PNG of a preview, with Playwright when it is installed; orch does not depend on it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise UsageError("a PNG needs Playwright, which orch does not install",
                         hint="uv pip install playwright && playwright install chromium; or write --out x.html") from None
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 800}, color_scheme=theme, device_scale_factor=2)
            page.goto(html_file.resolve().as_uri(), wait_until="load")
            page.screenshot(path=str(png), full_page=True)
        finally:
            browser.close()
