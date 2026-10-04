"""GET /design: the living reference of the design system (design-system spec §5.2). Every addon widget in its
variants, light and dark, in page, aside and phone container frames, plus the core primitives. Human-only like
every page (behind the dashboard token), not in the menu, and no addon code runs: the specimens are fixtures."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, Request

from orch.addons.runtime import ticket_prefix
from orch.dashboard.design import gallery
from orch.dashboard.views import page

router = APIRouter()


@router.get("/design")
def design_page(request: Request):
    ws = request.app.state.ws
    theme = request.query_params.get("theme")
    themes = (theme,) if theme in ("light", "dark") else ("light", "dark")
    density = "compact" if request.query_params.get("density") == "compact" else "comfortable"
    prefix = ticket_prefix(ws)
    group = SimpleNamespace(addon=gallery.ADDON, title="Design gallery", confirms=gallery.confirms(), uploads={},
                            key_prefix=prefix, widgets=())
    return page(request, "design.html", title="Design system", sections=gallery.sections(prefix or "DEMO"),
                banners=gallery.banners(), frames=gallery.FRAMES, themes=themes, density=density, g=group,
                roles=("ok", "info", "you", "warn", "err", "neu"), ticket_cards=gallery.ticket_cards(prefix or "DEMO"))
