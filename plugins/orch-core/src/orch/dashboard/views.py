from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from orch import __version__
from orch.clock import now as clock_now
from orch.clock import parse_stamp
from orch.core import query
from orch.dashboard import switcher
from orch.dashboard import terminals
from orch.dashboard.assets import static_url
from orch.dashboard.keys import link_keys
from orch.dashboard.markdown import (ArtifactScope, artifact_scope, md_page_filter, pinned_images, render_inline,
                                     render_markdown, render_section)
from orch.dashboard.qr import qr_matrix, qr_matrix_uncached, qr_runs
from orch.errors import OrchError

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))


def _ticket_options(ws, ticket_id):
    from orch.addons import ticket_options
    try:
        return ticket_options.views(ws, ticket_id)
    except Exception:
        return []


def _finalize(value):
    """Every value a template prints: a plain string holding hidden characters (bidi, zero-width, controls; see
    orch.textsafe) is escaped with each one as a visible `<U+XXXX>`, so ticket text can never reorder or hide what
    the human reads. Safe in text and attributes; Markup (rendered Markdown) is already handled."""
    from markupsafe import Markup

    from orch.textsafe import has_hidden, html_text
    if isinstance(value, str) and not isinstance(value, Markup) and has_hidden(value):
        return Markup(html_text(value))
    return value


TEMPLATES.env.finalize = _finalize
def _scope(ctx, scope):
    """The artifact scope for `md`: the one passed (`text|md(d.art)`), else the page's ticket `t` when it has one."""
    if isinstance(scope, ArtifactScope):
        return scope
    t = ctx.get("t")
    if not (isinstance(getattr(t, "meta", None), dict) and hasattr(t, "id")):
        return None
    request = ctx.get("request")
    return artifact_scope(t, getattr(getattr(getattr(request, "app", None), "state", None), "ws", None))


def _key_prefix(ctx) -> str | None:
    """This workspace's ticket key prefix, so bare keys in rendered text link to their tickets."""
    ws = getattr(getattr(getattr(ctx.get("request"), "app", None), "state", None), "ws", None)
    prefix = ((getattr(ws, "config", None) or {}).get("id") or {}).get("prefix")
    return prefix if isinstance(prefix, str) and prefix else None


@pass_context
def _md_filter(ctx, text, scope=None):
    return render_markdown(text, _scope(ctx, scope), key_prefix=_key_prefix(ctx))


@pass_context
def _md_inline_filter(ctx, text, scope=None):
    return render_inline(text, _scope(ctx, scope), key_prefix=_key_prefix(ctx))


@pass_context
def _pinned_filter(ctx, text, scope=None, limit=3):
    """M: the bound inline images of `text` (markdown.pinned_images), for thumbnails beside it."""
    return pinned_images(text, _scope(ctx, scope), limit)


TEMPLATES.env.filters["md"] = _md_filter
TEMPLATES.env.filters["pinned"] = _pinned_filter
TEMPLATES.env.filters["md_inline"] = _md_inline_filter
TEMPLATES.env.filters["md_page"] = md_page_filter  # addon Markdown widget: never a ticket's artifact scope


@pass_context
def _section_filter(ctx, text, section, widgets=None, scope=None):
    """A ticket section: `md` plus its ```orch blocks drawn as widgets."""
    return render_section(text, section, widgets, _scope(ctx, scope), key_prefix=_key_prefix(ctx))


TEMPLATES.env.filters["section_md"] = _section_filter
TEMPLATES.env.globals["qr_matrix"] = qr_matrix
TEMPLATES.env.globals["qr_matrix_uncached"] = qr_matrix_uncached
TEMPLATES.env.globals["qr_runs"] = qr_runs
TEMPLATES.env.globals["static_url"] = static_url
TEMPLATES.env.globals["link_keys"] = link_keys


def qr_widget(text: str, caption: str = ""):
    """A core-built QR widget (the Phones card's pairing link): the same widget an addon would return,
    except it never goes through the cached module-matrix path (the text carries a one-time secret)."""
    from orch.addons.widgets import QR
    return QR(text, caption, uncached=True)


TEMPLATES.env.globals["qr_widget"] = qr_widget


def _aware(dt: datetime) -> datetime:
    """Stamps are always UTC; treat a naive value as UTC too, so subtraction never raises."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _age_text(minutes: float) -> str:
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{int(minutes)} min ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)} h ago"
    days = int(hours / 24)
    return "yesterday" if days == 1 else f"{days} days ago"


def ago(dt: datetime | str | None, now: datetime | None = None) -> str:
    """A short relative time, local to the viewer: "3 min ago", "2 h ago", "yesterday". An ISO string (an
    addon's Time widget) is parsed first; one that does not parse is shown as is."""
    if dt is None or dt == "":
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt
    at_now = _aware(now or clock_now())
    seconds = max(0.0, (at_now - _aware(dt)).total_seconds())
    return _age_text(seconds / 60)


def minutes_ago(n: int | None) -> str:
    """The same rendering as `ago`, from an age already given in minutes (e.g. Decision.age_minutes)."""
    if n is None:
        return ""
    return _age_text(max(0, n))


def local(dt: datetime | str | None, fmt: str = "%d.%m. %H:%M") -> str:
    """The server's local wall-clock time (the machine running `orch serve`); stamps are stored
    and parsed as UTC. A stamp string is parsed first; one that does not parse is shown as is."""
    if dt is None or dt == "":
        return ""
    if isinstance(dt, str):
        try:
            dt = parse_stamp(dt)
        except ValueError:
            try:  # any other ISO 8601 time with an offset (an addon's Time widget)
                dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            except ValueError:
                return dt
    if not isinstance(dt, datetime):
        return str(dt)
    return _aware(dt).astimezone().strftime(fmt)


def widget_time(at: str, style: str = "ago") -> tuple[str, str]:
    """(tooltip, text) for an addon's Time widget: the full local time, and "2 min ago" or "03.10. 14:32"."""
    return local(at, "%d.%m.%Y %H:%M"), ago(at) if style == "ago" else local(at)


TEMPLATES.env.filters["ago"] = ago
TEMPLATES.env.filters["local"] = local


def epoch_local(seconds) -> str:
    """Epoch seconds as the server's local wall-clock time (a time-axis chart's table row)."""
    try:
        return datetime.fromtimestamp(float(seconds)).strftime("%d.%m. %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return str(seconds)


TEMPLATES.env.filters["epoch_local"] = epoch_local
TEMPLATES.env.globals["widget_time"] = widget_time


_LOGIN_RUN = re.compile(r"\brun\s+(`?)([^`\n]{2,120})\1\s*\.?\s*$")


def login_command(message: str) -> str:
    """The command in an addon's login-needed message ("login needed: run gh auth login" → "gh auth login"), for the
    health callout's Copy button; "" when the message names none (it is then shown as text only)."""
    m = _LOGIN_RUN.search(str(message or ""))
    return m.group(2).strip() if m else ""


TEMPLATES.env.globals["login_command"] = login_command


def _duration(value, unit: str = "h") -> str:
    from orch.dashboard.data.metrics import duration
    return duration(None if value is None else value * (24 if unit == "d" else 1))


TEMPLATES.env.filters["duration"] = _duration
TEMPLATES.env.filters["minutes_ago"] = minutes_ago


def _via(value) -> str:
    from orch.dashboard.data.timeline import via_label
    return via_label(value)


TEMPLATES.env.filters["via"] = _via


THEMES = ("light", "dark", "system")
BRANDS = ("mission-control", "none")  # an unknown value falls back to "none"


def _theme(request, ws) -> str:
    """The viewer's cookie wins; without one the workspace default; anything odd means system."""
    cookie = request.cookies.get("orch_theme")
    if cookie in THEMES:
        return cookie
    default = ws.config.get("dashboard", {}).get("theme", "system")
    return default if default in THEMES else "system"


def invalidate_setup_count(ws) -> None:
    """Mark the setup checks stale after a POST to /workspace/* that may change setup state: the next page (the
    Workspace page the POST redirects to) computes them again instead of showing the last background round."""
    from orch.dashboard import setup_state

    setup_state.invalidate(ws)


def _setup_count(ws, checks=None) -> int:
    """Open, non-dismissed setup items; cheap and safe to run on every page.

    Reads the last round of setup_state (doctor shells out to git; `orch serve` reruns it in the background). When
    `checks` is given (the Workspace page's own list) the count is taken from those, so the badge always matches
    what that page shows.
    """
    from orch import onboarding
    from orch.dashboard import setup_state

    try:
        if checks is None:
            snap = setup_state.state(ws).peek()
            return setup_state.open_count(snap, ws) if snap is not None else 0
        state = onboarding.load_state()
        dismissed = set(state["dismissed_items"].get(str(Path(ws.root).resolve()), []))
        return len([c for c in checks if not c.ok and c.code in onboarding.OPEN_ITEM_CODES
                    and c.code not in dismissed and c.code not in setup_state.NOT_COUNTED])
    except Exception:
        return 0


def records_nav(ws) -> dict | None:
    """The menu's "Not in git" row from the last setup round (no git call here); None when nothing waits."""
    from orch.core import gitfiles, ledger
    from orch.dashboard import setup_state

    try:
        snap = setup_state.state(ws).peek()
        rec = snap.records if snap is not None else None
        if not rec or not rec["paths"]:
            return None
        return {"count": len(rec["paths"]), "busy": rec["busy"], "auto": ledger.records_auto_state(ws) == "on",
                "keys": gitfiles.few(sorted(gitfiles.record_keys(ws, rec["paths"])), 4)}
    except Exception:
        return None


def _density(ws) -> str:
    try:
        from orch.addons import userfiles
        return userfiles.density(ws.root)
    except Exception:  # a broken workspaces.json never breaks a page
        return "comfortable"


def _shortcuts(ws) -> bool:
    from orch.addons import userfiles

    try:
        return userfiles.keyboard_shortcuts(ws.root)
    except Exception:  # an unreadable user file never breaks a page; shortcuts stay on
        return True


def _switcher_on(ws) -> bool:
    from orch.addons import userfiles

    try:
        return userfiles.workspace_switcher(ws.root)
    except Exception:  # an unreadable user file never breaks a page; the switcher stays off
        return False


GRAPH_ADDON = "graph"


def addon_on(ws, name: str) -> bool:
    """The default addon `name` is enabled and trusted here: the switch for a core page it stands for."""
    try:
        return ws.addons.get(name) is not None
    except Exception:  # a broken addon registry never breaks a page
        return False


def _live_version(ws) -> str:
    from orch.dashboard.routes_live import live_version

    try:
        return live_version(ws)
    except Exception:  # never break a page over the live-reload fingerprint
        return ""


def page(request, name: str, status_code: int = 200, *, nav: str = "", title: str = "", **ctx):
    ws = request.app.state.ws
    dashboard = ws.config.get("dashboard", {})
    # Reuse the caller's `waiting` or `needs` (e.g. the board route already computed it) instead of scanning and
    # parsing every ticket a second time on every page load. Badge and tab title show the blocking count (#11).
    waiting = ctx["waiting"] if "waiting" in ctx else query.waiting(ws, needs=ctx.get("needs"))
    needs_count = query.counts(waiting)["blocking"]
    switcher.write_needs(ws, needs_count)
    brand = dashboard.get("brand", "none")
    runtime = getattr(request.app.state, "addons", None)
    switcher_on = _switcher_on(ws)
    base = {
        "customer": ws.config.get("customer") or ws.root.name,
        "prefix": ws.config.get("id", {}).get("prefix", ""),
        "repo_count": len(ws.config.get("git", {}).get("repos") or {}),
        "orch_version": __version__,
        "theme": _theme(request, ws),
        "live_version": _live_version(ws),
        "brand": brand if brand in BRANDS else "none",
        "needs_count": needs_count,
        "setup_count": _setup_count(ws, ctx.get("checks")) + (runtime.attention() if runtime else 0),
        "nav": nav,
        "records_nav": records_nav(ws),
        # Addon pages in the menu as (label, url, icon path); the group shows only when there is one.
        "addon_nav": runtime.nav() if runtime else [],
        "terminals_nav": terminals.enabled(ws, request),  # issue #40: addon on, tmux installed, a local request
        "quick_nav": _quick_nav(ws),  # quick tasks (orch.core.quick): the menu item and its open count
        "graph_nav": addon_on(ws, GRAPH_ADDON),  # #167: the Graph page is the `graph` default addon's
        "schedules_nav": addon_on(ws, "schedules"),  # docs/schedules.md: the Schedules page is the addon's
        "dedupe_prs": lambda prs, groups=(): __import__("orch.dashboard.routes_ticket", fromlist=["dedupe_prs"]).dedupe_prs(prs, groups),
        "addon_slot": runtime.slot if runtime else (lambda name, ticket=None, params=None, always_banner=False: []),
        # the yes/no options enabled, trusted addons add to a ticket (the form, the approve card, the ticket page)
        "ticket_options": (lambda ticket_id=None: _ticket_options(ws, ticket_id)) if runtime else (lambda ticket_id=None: []),
        # spec §4.2: other running workspaces, for the footer switcher line; only once the human turned it on (#167)
        "other_workspaces": switcher.others(ws) if switcher_on else [],
        "switcher_on": switcher_on,
        "page_title": title or nav.title(),
        # spec §8: "(N) " in the tab title while N blocking items wait (the same count as the menu badge)
        "page_title_prefix": f"({needs_count}) " if needs_count > 0 else "",
        "shortcuts": _shortcuts(ws),
        # "Widgets for this section" links to authoring docs: shown only on request (dashboard.authoring_hints)
        "authoring_hints": dashboard.get("authoring_hints") is True,
        "density": _density(ws),
        # The page's own address, for app.js: the dashboard never reads it back from the address bar.
        "here": request.url.path + ("?" + request.url.query if request.url.query else ""),
        "msg": request.query_params.get("msg"),
        "err": request.query_params.get("err"),
    }
    # A show-once secret from an action (a Reveal): taken out of its store by this one render, never cached.
    # The store is shared with ?pair= tokens: peek the kind first, so a pairing token is never popped here.
    reveals = getattr(request.app.state, "reveals", None)
    reveal_token = request.query_params.get("reveal")
    reveal = None
    if reveals is not None and reveal_token:
        peeked = reveals.peek(reveal_token)
        if peeked is not None and not (isinstance(peeked, dict) and peeked.get("kind") == "pair"):
            reveal = reveals.pop(reveal_token)
    base["reveal"] = reveal
    response = TEMPLATES.TemplateResponse(request, name, {**base, **ctx}, status_code=status_code)
    if reveal is not None:
        response.headers["Cache-Control"] = "no-store"
        return response
    # Revalidated on every visit (live data); a page that did not change since the browser's copy costs a 304
    # instead of its whole body.
    response.headers["Cache-Control"] = "no-cache"
    if status_code == 200:
        etag = 'W/"' + hashlib.sha1(response.body).hexdigest()[:20] + '"'  # weak: the gzip and plain bodies share it
        if etag[2:] in [t.strip().removeprefix("W/") for t in request.headers.get("if-none-match", "").split(",")]:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
        response.headers["ETag"] = etag
    return response


def _quick_nav(ws) -> dict:
    try:
        from orch.dashboard.routes_quick import summary
        return summary(ws)
    except Exception:  # noqa: BLE001  a broken quick-task file never breaks a page
        return {"enabled": False, "open": 0, "outgrew": 0, "done_today": 0}


def as_list(v) -> list:
    """Frontmatter value as a list; hand-edited files may hold any shape."""
    return v if isinstance(v, list) else []


def as_dict(v) -> dict:
    """Frontmatter value as a dict; hand-edited files may hold any shape."""
    return v if isinstance(v, dict) else {}


def back(url: str, *, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    """303 to `url` with `msg`/`err` merged into its query string (replacing any earlier ones)."""
    params = [(k, v) for k, v in (("msg", msg), ("err", err)) if v]
    if params:
        parts = urlsplit(url)
        query_items = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in ("msg", "err")]
        url = urlunsplit(parts._replace(query=urlencode(query_items + params)))
    return RedirectResponse(url, status_code=303)


_UNSAFE_NEXT = re.compile(r"[\x00-\x1f\x7f\\]")


def safe_next(value: str | None) -> str | None:
    """A same-origin path to return to after a form POST, or None when it is not one.

    Accepted only if it starts with "/" but not "//", holds no backslash or control character
    (also once percent-decoded) and has neither scheme nor host, so it can never send the
    browser to another site or split the Location header.
    """
    if not value or not isinstance(value, str):
        return None
    for candidate in (value, unquote(value)):
        if not candidate.startswith("/") or candidate.startswith("//") or _UNSAFE_NEXT.search(candidate):
            return None
        parts = urlsplit(candidate)
        if parts.scheme or parts.netloc:
            return None
    return value


def error_text(e: OrchError) -> str:
    return e.message + (f" — {e.hint}" if e.hint else "")


def confirm_page(request, *, action: str, fields: list[tuple[str, str]], title: str, body: str, confirm: str,
                 cancel: str = "Cancel", cancel_href: str = "/", danger: bool = False, nav: str = ""):
    """The server-side confirm for a dialog-tier action posted without JS (its form carries ask=1): the same POST
    again, without ask, does it. Cancel goes back to `cancel_href` (a same-origin path)."""
    c = {"action": action, "fields": [(k, v) for k, v in fields if k != "ask"], "title": title, "body": body,
         "confirm": confirm, "cancel": cancel, "cancel_href": safe_next(cancel_href) or "/", "danger": danger}
    return page(request, "confirm.html", nav=nav, title=title, c=c)


_INITIAL_SPLIT = re.compile(r"[^A-Za-z0-9]+")


def initials(name) -> str:
    """M: an agent's two-letter mark on board cards ("claude-code" -> "CC", "copilot" -> "CO"); its full name is
    always the tooltip and the screen-reader text beside it."""
    words = [w for w in _INITIAL_SPLIT.split(str(name or "")) if w]
    if not words:
        return "?"
    mark = "".join(w[0] for w in words[:2]) if len(words) > 1 else words[0][:2]
    return mark.upper()


TEMPLATES.env.filters["initials"] = initials
