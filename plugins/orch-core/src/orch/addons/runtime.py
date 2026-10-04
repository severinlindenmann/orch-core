"""What a page asks of addons (v2 §11.5, §16). Reads the snapshot cache only; addon code runs inside
`rendering()`, so `ctx.run` refuses. Nothing here raises: failures become an err Callout plus a log line."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from orch.addons import cache
from orch.addons.api import PairingTarget, PendingDecision, worst_health
from orch.addons.loader import _log_error
from orch.addons.manifest import MENU_ICONS
from orch.addons.runner import rendering
from orch.addons.widgets import Callout, widget_problems

HEALTH_ROLE = {"ok": "ok", "stale": "warn", "auth_required": "warn", "offline": "warn", "rate_limited": "warn",
               "error": "err", "never_fetched": "neu"}
_SEE_ERRORS = "See recent addon errors on Workspace & addons."
MAX_PARAMS = 10
MAX_PARAM_LEN = 200
_CORE_PARAMS = frozenset({"msg", "err", "token"})  # flash messages and the login token are core's, never the addon's
_PARAM_SLOTS = ("board.external",)  # besides page.<name>: the slots whose view sees the GET query


def clean_params(query, *, drop=()) -> dict[str, str]:
    """The GET query as an addon may see it: at most MAX_PARAMS keys (first value each), values cut to
    MAX_PARAM_LEN characters, without core's own keys. Never raises."""
    out: dict[str, str] = {}
    if not query:
        return out
    try:
        items = query.multi_items() if hasattr(query, "multi_items") else list(dict(query).items())
        for key, value in items:
            if len(out) >= MAX_PARAMS:
                break
            if not isinstance(key, str) or not key or len(key) > 50 or key in _CORE_PARAMS or key in drop or key in out:
                continue
            out[key] = str(value)[:MAX_PARAM_LEN]
    except Exception:
        return {}
    return out


@dataclass(frozen=True)
class Banner:
    health: str
    role: str
    fetched_at: datetime | None = None
    retry_after: datetime | None = None
    message: str = ""
    complete: bool = True  # False: some scope returned only part of its data ("Showing part of the data")
    login: str = ""  # a scope's login-needed message when another state is worse, so the login still shows


def banner_for(snapshots) -> Banner:
    real = [s for s in snapshots if s.health != "never_fetched"]
    if not real:
        return Banner("never_fetched", "neu")
    worst = worst_health(s.health for s in real)
    s = next(x for x in real if x.health == worst)
    oldest = min(x.fetched_at for x in real)
    return Banner(worst, HEALTH_ROLE.get(worst, "err"), oldest if worst == "ok" else s.fetched_at, s.retry_after,
                  "" if worst == "ok" else s.message, all(getattr(x, "complete", True) for x in real),
                  next((x.message or "login needed" for x in real if x.health == "auth_required" and worst != "auth_required"), ""))


def ticket_prefix(ws) -> str:
    """The workspace's ticket key prefix ("DEMO"), or "" when a hand-edited config has none: keys are then not linked."""
    try:
        return str(ws.config["id"]["prefix"])
    except (KeyError, TypeError):
        return ""


@dataclass(frozen=True)
class SlotGroup:
    addon: str
    title: str
    widgets: tuple
    banner: Banner | None = None
    confirms: dict = field(default_factory=dict)
    uploads: dict = field(default_factory=dict)  # action id -> the HTML accept value ("" = any type)
    key_prefix: str = ""  # this workspace's ticket prefix: core links those keys in widget text


class SlotView:
    """What `widgets(slot, view)` and `decisions(view)` get: read-only, cache-backed."""

    def __init__(self, ws, loaded, slot: str, ticket=None, params=None):
        self._ws = ws
        self.slot = slot
        self.ticket = ticket
        # the cleaned GET query; only the addon page and board.external see one, every other slot gets {}
        self.params = dict(params or {}) if slot.startswith("page.") or slot in _PARAM_SLOTS else {}
        self.addon = loaded.name
        self._loaded = loaded
        self.root = ws.root
        self.workspace_name = ws.config.get("customer") or ws.root.name
        self.state_dir = loaded.ctx.state_dir  # the addon's own folder, for small files of its own
        self._links = None

    @property
    def settings(self) -> dict:
        """Read when asked (it reads workspaces.json), not for every slot an addon is asked about."""
        return self._loaded.ctx.settings

    def snapshots(self, provider: str | None = None) -> list:
        return cache.read_snapshots(self._ws, self.addon, provider)

    def repos(self) -> list:
        return self._loaded.ctx.repos()

    def trackers(self) -> list:
        return self._loaded.ctx.trackers()

    def document(self, ref: str) -> dict:
        """The ticket's schema document (see AddonContext.document); read-only."""
        return self._loaded.ctx.document(ref)

    def links(self):
        """The core's LinkIndex, built once per view."""
        if self._links is None:
            self._links = self._loaded.ctx.links()
        return self._links


def _confirms(manifest) -> dict:
    return {a.id: a.confirm or f"{a.label}?" for a in manifest.actions}


def _uploads(manifest) -> dict:
    return {a.id: ",".join(a.accepts_file[1]) for a in manifest.actions if a.accepts_file}


class AddonRuntime:
    def __init__(self, ws):
        self.ws = ws

    @property
    def registry(self):
        return self.ws.addons

    def reload(self) -> None:
        """Re-read enable/trust and import newly enabled addons now, inside the human POST that changed them
        (ruling F5): a page render never triggers an import."""
        from orch.addons.loader import AddonRegistry
        with self.ws.addons_lock:
            try:
                self.ws._addons = AddonRegistry.load(self.ws)
            except Exception:
                _log_error(self.ws, "dashboard", "reload addons")
                self.ws._addons = AddonRegistry(self.ws, {})

    def attention(self) -> int:
        """Enabled addons with a load problem plus loaded addons that need a login (the Workspace menu badge)."""
        try:
            registry = self.registry
            auth = sum(1 for la in registry if la.has("provider")
                       and banner_for(cache.read_snapshots(self.ws, la.name)).health == "auth_required")
            return len({name for name, msg in registry.problems if not msg.startswith("uses MC2-1 hook")}) + auth
        except Exception:
            return 0

    def nav(self) -> list[tuple[str, str, str, str]]:
        """Addon pages for the menu as (title, url, icon path, problem): `problem` names what is broken (a load
        problem, or a provider whose last fetch failed) for the menu's red dot, "" when the addon is fine."""
        try:
            problems = dict(self.registry.problems)
            return [(la.manifest.menu["title"], f"/addons/{la.name}/",
                     MENU_ICONS.get(la.manifest.menu.get("icon"), MENU_ICONS["box"]), self._broken(la, problems))
                    for la in self.registry if la.has("page") and la.manifest.menu]
        except Exception:
            _log_error(self.ws, "dashboard", "addon nav")
            return []

    def _broken(self, la, problems: dict) -> str:
        if la.name in problems:
            return "load problem"
        if la.has("provider") and banner_for(cache.read_snapshots(self.ws, la.name)).health == "error":
            return "last fetch failed"
        return ""

    def declares(self, slot: str) -> bool:
        try:
            return any(slot in la.manifest.slots for la in self.registry)
        except Exception:
            _log_error(self.ws, "dashboard", f"declares {slot}")
            return False

    def _prefix(self) -> str:
        return ticket_prefix(self.ws)

    def _banner(self, la) -> Banner | None:
        if not la.has("provider"):
            return None
        return banner_for(cache.read_snapshots(self.ws, la.name))

    def _widgets(self, la, slot: str, ticket=None, params=None) -> tuple:
        fn = getattr(la.obj, "widgets", None)
        if fn is None:
            return ()
        title = la.manifest.title
        try:
            with rendering():
                result = list(fn(slot, SlotView(self.ws, la, slot, ticket, params)) or [])
        except Exception:
            _log_error(self.ws, la.name, f"widgets {slot}")
            return () if slot == "today.summary" else (Callout("err", f"{title} could not render", _SEE_ERRORS),)
        out, dropped = [], []
        for w in result:
            problems = widget_problems(w, slot=slot, manifest=la.manifest)
            (dropped if problems else out).append(problems or w)
        if dropped:
            _log_error(self.ws, la.name, f"invalid widget in {slot}", "; ".join(p for ps in dropped for p in ps))
            if slot != "today.summary":
                out.append(Callout("err", f"{title} returned an invalid widget", _SEE_ERRORS))
        return tuple(out)

    def slot(self, name: str, ticket=None, params=None) -> list[SlotGroup]:
        groups = []
        try:
            addons = list(self.registry)
        except Exception:
            _log_error(self.ws, "dashboard", f"slot {name}")
            return groups
        for la in addons:  # one failing addon never hides the others' groups
            try:
                if name not in la.manifest.slots:
                    continue
                widgets = self._widgets(la, name, ticket, params)
                # the banner is never shown in today.summary, and on other slots only when health is not ok
                banner = None if name == "today.summary" else self._banner(la)
                if banner is not None and banner.health == "ok":
                    banner = None
                if widgets or banner is not None:
                    groups.append(SlotGroup(la.name, la.manifest.title, widgets, banner, _confirms(la.manifest),
                                            _uploads(la.manifest), self._prefix()))
            except Exception:
                _log_error(self.ws, la.name, f"slot {name}")
        return groups

    def page(self, name: str, params=None) -> SlotGroup | None:
        """None only when no such page is enabled here; a failure while building it is a page with an err Callout."""
        try:
            la = self.registry.get(name)
            if la is None or not la.has("page") or not la.manifest.menu:
                return None
            title = la.manifest.menu["title"]
        except Exception:
            _log_error(self.ws, "dashboard", f"page {name}")
            return None
        try:
            return SlotGroup(la.name, title, self._widgets(la, f"page.{name}", params=params),
                             self._banner(la), _confirms(la.manifest), _uploads(la.manifest), self._prefix())
        except Exception:
            _log_error(self.ws, name, f"page {name}")
            return SlotGroup(la.name, title, (Callout("err", f"{la.manifest.title} could not render", _SEE_ERRORS),))

    def review_index(self) -> dict[str, list[dict]]:
        """{ticket id: cached items of every loaded `reviews` provider linked to it by URL, branch or title} (no addon
        names), built once per request (store.memo) and shared by every card and ticket page that needs it."""
        return self._review_indexes()[0]

    def mention_index(self) -> dict[str, list[dict]]:
        """{ticket id: review items that only name the ticket in their description} (#14): shown as "mentioned in",
        never as the ticket's own PR (a "follow-up of DEMO-5" must not become DEMO-5's main PR)."""
        return self._review_indexes()[1]

    def _review_indexes(self):
        from orch.core import store
        return store.memo(self.ws, "review-index", self._build_review_indexes)

    def _build_review_indexes(self):
        from orch.core.links import shared_index
        strong: dict[str, list[dict]] = {}
        mentions: dict[str, list[dict]] = {}
        try:
            links = None
            for la in self.registry:
                ids = {p.id for p in la.providers() if p.kind == "reviews"}
                if not ids:
                    continue
                links = links or shared_index(self.ws)
                for snap in cache.read_snapshots(self.ws, la.name):
                    if snap.provider not in ids:
                        continue
                    for item in snap.items:
                        hits = links.for_review(url=str(item.get("url") or ""), branch=str(item.get("source_branch") or ""),
                                                title=str(item.get("title") or ""))
                        for t in hits:
                            strong.setdefault(t.id, []).append(item)
                        refs = item.get("body_refs")
                        body = " ".join(r for r in refs if isinstance(r, str)) if isinstance(refs, list) else ""
                        for t in links.for_review(body=body) if body else ():
                            if t not in hits:
                                mentions.setdefault(t.id, []).append(item)
        except Exception:
            _log_error(self.ws, "dashboard", "review items")
            return {}, {}
        return strong, mentions

    def review_items(self, ticket) -> list[dict]:
        """Cached items of every loaded `reviews` provider that the core links to `ticket` (no addon names)."""
        return list(self.review_index().get(ticket.id, []))

    def failing_prs(self, ticket) -> list[str]:
        """URLs of open linked PRs whose checks failed (Start agent turns work into fix-checks)."""
        return [i["url"] for i in self.review_items(ticket)
                if i.get("state", "open") == "open" and isinstance(i.get("checks"), dict)
                and i["checks"].get("state") == "failed" and isinstance(i.get("url"), str)]

    def _decisions_of(self, la) -> list[PendingDecision]:
        fn = getattr(la.obj, "decisions", None)
        if not la.has("decisions") or not callable(fn):
            return []
        try:
            with rendering():
                items = list(fn(SlotView(self.ws, la, "today.from_addons")) or [])
        except Exception:
            _log_error(self.ws, la.name, "decisions")
            return []
        return [d for d in items if isinstance(d, PendingDecision)]

    def decisions(self) -> list[tuple[str, PendingDecision]]:
        try:
            addons = list(self.registry)
        except Exception:
            _log_error(self.ws, "dashboard", "decisions")
            return []
        return [(la.name, d) for la in addons for d in self._decisions_of(la)]

    def pairing_targets(self) -> list[tuple[str, PairingTarget]]:
        """(addon, target) for each loaded addon with remote_humans: true that offers a phone pairing target here."""
        out = []
        try:
            addons = list(self.registry)
        except Exception:
            _log_error(self.ws, "dashboard", "pairing targets")
            return []
        for la in addons:
            fn = getattr(la.obj, "pairing_target", None)
            if la.manifest is None or not la.manifest.remote_humans or not callable(fn):
                continue
            try:
                with rendering():
                    target = fn(SlotView(self.ws, la, "workspace.settings"))
            except Exception:
                _log_error(self.ws, la.name, "pairing_target")
                continue
            if isinstance(target, PairingTarget) and target.url.startswith("https://"):
                out.append((la.name, target))
        return out

    def decisions_for(self, ticket_id: str) -> list[tuple[str, PendingDecision]]:
        """The pending decisions about one ticket (ID compared case-insensitively), for its ticket page."""
        wanted = str(ticket_id).strip().upper()
        return [(a, d) for a, d in self.decisions() if d.ticket and str(d.ticket).strip().upper() == wanted]

    def find_decision(self, name: str, decision_id: str):
        try:
            la = self.registry.get(name)
        except Exception:
            return None
        if la is None:
            return None
        return next(((la, d) for d in self._decisions_of(la) if d.id == decision_id), None)
