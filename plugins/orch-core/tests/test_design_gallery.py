"""The /design gallery (design-system spec §5.2): every widget in its variants, light and dark, in page, aside and
phone frames; its fixtures are clean under widget_problems and the design lint, so it is the lint's reference too."""
import re

import pytest

pytest.importorskip("fastapi")

from orch.addons.design_lint import widget_warnings  # noqa: E402
from orch.addons.manifest import parse_manifest  # noqa: E402
from orch.addons.widgets import widget_problems  # noqa: E402
from orch.dashboard.design import gallery  # noqa: E402

M = parse_manifest({"name": gallery.ADDON, "title": "Design gallery", "version": "0.1.0", "requires_api": "2",
                    "kind": "in-process", "capabilities": ["page"], "entry": "x:create",
                    "menu": {"title": "Design gallery", "icon": "box"},
                    "actions": [{"id": a, "label": label} for a, label in gallery.ACTIONS]})


def _specimens():
    return [sp for s in gallery.sections() for sp in s.specimens]


@pytest.mark.parametrize("sp", _specimens(), ids=lambda sp: sp.title)
def test_every_specimen_is_a_valid_and_lint_clean_widget(sp):
    slot = "page.design-gallery" if sp.slot.startswith("page.") else sp.slot
    for w in sp.widgets:
        assert widget_problems(w, slot=slot, manifest=M) == [], sp.title
    assert sorted({x.split(" ", 1)[0] for x in widget_warnings(list(sp.widgets), slot)}) == sorted(sp.warns), sp.title


def test_the_gallery_covers_every_widget_kind():
    kinds = set()

    def walk(w):
        kinds.add(type(w).__name__)
        for child in getattr(w, "body", ()) or ():
            walk(child)
        for row in getattr(w, "rows", ()) or ():
            for c in (row if isinstance(row, tuple) else ()):
                if hasattr(c, "kind"):
                    kinds.add(type(c).__name__)
        for item in getattr(w, "items", ()) or ():
            walk(item)
    for sp in _specimens():
        for w in sp.widgets:
            walk(w)
    assert {"Text", "Badge", "Link", "Copy", "Action", "Callout", "KV", "Table", "Card", "Search", "Chips", "QR",
            "Tabs", "Time", "Tile", "Chart"} <= kinds


def test_design_page_renders_both_themes_in_three_frames(dash):
    r = dash.get("/design")
    assert r.status_code == 200
    html = r.text
    assert "<h1>Design system</h1>" in html
    for theme in ("light", "dark"):
        assert f'class="ds-frame" data-theme="{theme}" style="width: 960px"' in html
        assert f'class="ds-frame" data-theme="{theme}" style="width: 320px"' in html
        assert f'class="ds-frame" data-theme="{theme}" style="width: 375px"' in html
    for marker in ('class="callout callout-warn"', 'class="table wtable cols-m"', 'class="table wtable cols-xl"',
                   'class="widget-tabs"', "<time datetime=", 'class="kv-stats"', 'class="widget-chips"',
                   'class="decision-card"', 'class="confirm-dialog confirm-sample overlay"', 'class="dialog-sheet overlay"', 'class="receipt"',
                   'class="btn btn-primary" disabled', 'aria-busy="true"', 'class="addon-banner', 'class="sum sum-err"',
                   '<svg viewBox'):
        assert marker in html, marker
    assert "could not render" not in html and "invalid widget" not in html
    # specimen forms post to no real addon: every frame is inert (looked at, not used)
    assert html.count('<div class="addon-slot" inert>') == html.count('class="ds-frame"')


def test_design_page_theme_and_density_parameters(dash):
    html = dash.get("/design?theme=dark&density=compact").text
    assert 'data-theme="light"' not in html.split('<div class="ds-gallery"', 1)[1]
    assert '<div class="ds-gallery" data-density="compact">' in html
    assert '<div class="ds-gallery" data-density="comfortable">' in dash.get("/design?density=weird").text


def test_design_page_needs_the_token(ws):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    assert TestClient(create_app(ws, "tok")).get("/design").status_code == 401


def test_design_page_runs_no_addon_code(dash, monkeypatch):
    from orch.addons import runtime

    def boom(*a, **k):
        raise AssertionError("the gallery must not ask addons for widgets")
    monkeypatch.setattr(runtime.AddonRuntime, "slot", boom)
    monkeypatch.setattr(runtime.AddonRuntime, "page", boom)
    assert dash.get("/design").status_code == 200


def test_design_page_is_not_in_the_menu(dash):
    assert not re.search(r'<a [^>]*href="/design"', dash.get("/").text)


def test_design_page_without_an_id_prefix(dash, ws):
    """A hand-edited config without id.prefix must not break the gallery; keys are then simply not linked."""
    ws.config.pop("id", None)
    r = dash.get("/design")
    assert r.status_code == 200 and "<h1>Design system</h1>" in r.text
