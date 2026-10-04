"""Contract checks for providers and whole addons (spec A1 §5.9). Plain functions returning problem lists,
used by `orch addon check`; the pytest classes wrap them."""
from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from orch.addons.api import HEALTH, PROVIDER_KINDS, PendingDecision, Snapshot, item_problems
from orch.addons.loader import V1_HOOKS, valid_name
from orch.testing.fakes import PAYLOAD, FakeRunner, make_snapshot, sample_items


class SideEffect(AssertionError):
    pass


@contextlib.contextmanager
def no_side_effects():
    def refuse(what):
        def _raise(*a, **k):
            raise SideEffect(f"{what} was used directly; use ctx.run")
        return _raise
    patches = [(subprocess, "Popen", "subprocess"), (os, "system", "os.system"), (os, "fork", "os.fork"),
               (os, "posix_spawn", "os.posix_spawn"), (os, "posix_spawnp", "os.posix_spawnp"),
               (socket.socket, "connect", "a socket"), (socket.socket, "connect_ex", "a socket"),
               (socket.socket, "sendto", "a socket"), (socket, "create_connection", "a socket"),
               (socket, "getaddrinfo", "a socket")]
    with contextlib.ExitStack() as stack:
        for target, name, what in patches:
            if hasattr(target, name):  # os.fork and os.posix_spawn do not exist on Windows
                stack.enter_context(mock.patch.object(target, name, refuse(what)))
        yield


def provider_identity_problems(provider) -> list[str]:
    out = []
    if not valid_name(getattr(provider, "id", None)):
        out.append(f"provider id {getattr(provider, 'id', None)!r} must match ^[a-z][a-z0-9-]*$")
    if getattr(provider, "kind", None) not in PROVIDER_KINDS:
        out.append(f"provider kind {getattr(provider, 'kind', None)!r} must be one of {', '.join(PROVIDER_KINDS)}")
    interval = getattr(provider, "interval_s", 300)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < 5:
        out.append("interval_s must be a number of seconds ≥ 5")
    for method in ("scopes", "fetch"):
        if not callable(getattr(provider, method, None)):
            out.append(f"provider has no {method}()")
    return out


def check_provider(provider, ctx, *, scopes=None) -> list[str]:
    problems = provider_identity_problems(provider)
    if problems:
        return problems
    try:
        with no_side_effects():
            got = list(provider.scopes(ctx))
    except Exception as e:
        return [f"scopes() raised {type(e).__name__}: {e}"]
    if not all(isinstance(s, str) and s for s in got):
        problems.append("scopes() must return non-empty strings")
    for scope in scopes or [s for s in got if isinstance(s, str) and s]:
        try:
            with no_side_effects():
                snap = provider.fetch(ctx, scope, None)
        except Exception as e:
            problems.append(f"fetch({scope!r}) raised {type(e).__name__}: {e}")
            continue
        if not isinstance(snap, Snapshot):
            problems.append(f"fetch({scope!r}) must return a Snapshot, got {type(snap).__name__}")
            continue
        if snap.provider != provider.id or snap.scope != scope:
            problems.append(f"fetch({scope!r}) returned provider/scope {snap.provider!r}/{snap.scope!r}")
            continue
        try:
            again = Snapshot.from_dict(json.loads(json.dumps(snap.to_dict())))
            if again.to_dict() != snap.to_dict():
                problems.append(f"fetch({scope!r}): snapshot does not survive a JSON round trip")
        except ValueError as e:
            problems.append(f"fetch({scope!r}): {e}")
            continue
        for i, item in enumerate(snap.items):
            problems.extend(f"fetch({scope!r}) item {i}: {p}" for p in item_problems(provider.kind, item))
        try:
            with no_side_effects():
                provider.fetch(ctx, scope, snap)
        except Exception as e:
            problems.append(f"fetch({scope!r}, previous) raised {type(e).__name__}: {e}")
    return problems


def _render_problems(group) -> list[str]:
    try:
        import jinja2
    except ImportError:
        return []  # the dashboard extra is not installed: widget_problems already ran
    import orch.dashboard
    from orch.dashboard.keys import link_keys
    from orch.dashboard.qr import qr_matrix, qr_matrix_uncached, qr_runs
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(orch.dashboard.__file__).with_name("templates"))),
                             autoescape=True)
    from orch.dashboard.markdown import md_page_filter
    env.filters["md_page"] = md_page_filter
    env.filters["ago"] = lambda v: "just now"
    env.filters["local"] = lambda v, fmt="": "09:00"
    env.globals["qr_matrix"] = qr_matrix
    env.globals["qr_matrix_uncached"] = qr_matrix_uncached
    env.globals["qr_runs"] = qr_runs
    env.globals["link_keys"] = link_keys
    env.globals["widget_time"] = lambda at, style="ago": ("03.10.2026 09:00", "just now")
    html = env.from_string('{% import "_widgets.html" as w %}{{ w.widgets(g.widgets, g) }}').render(g=group)
    return [f"widget output contains unescaped HTML from data ({PAYLOAD})"] if PAYLOAD in html else []


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def run_addon_check(folder, *, runner=None) -> tuple[list[str], list[str]]:
    """(errors, design warnings) for `orch addon check`: the contract's problems, and W1-W18 from every render."""
    warnings: list[str] = []
    errors = run_addon_contract(folder, runner=runner, warnings=warnings)
    return errors, warnings


def run_addon_contract(folder, *, runner=None, warnings: list[str] | None = None) -> list[str]:
    """The contract's problems. With a `warnings` list, the design warnings (W1-W18, design-system spec §4.5) of
    every slot render are appended to it; they never count as problems."""
    from orch.addons.check import static_problems
    from orch.addons.discovery import Found
    from orch.addons.loader import LoadedAddon, import_entry
    from orch.addons.manifest import load_manifest
    from orch.addons.outbox import Outbox
    from orch.addons.runner import rendering
    from orch.addons.runtime import SlotGroup, SlotView
    from orch.addons.userfiles import folder_hash
    from orch.addons.design_lint import manifest_warnings, own_names, widget_warnings
    from orch.addons.widgets import widget_problems
    from orch.core import store
    from orch.core.events import Event
    from orch.testing.workspace import fake_workspace

    folder = Path(folder)
    problems = static_problems(folder)
    if problems:
        return problems
    m = load_manifest(folder)
    lint: list[str] = manifest_warnings(m)
    # The fake workspace lives in a temp dir. The contract only reads user files (settings fall back to the
    # manifest defaults for this unknown workspace) and never writes them, so the environment stays as it is.
    with tempfile.TemporaryDirectory(prefix="orch-contract-") as tmp_name:
        tmp = Path(tmp_name)
        fw = fake_workspace(tmp / "workspace", tickets=[{"title": f"Contract {PAYLOAD}", "status": "open", "external": ["GH-1"]}])
        ctx = fw.context(m, runner=runner or FakeRunner(strict=False))
        try:
            with no_side_effects():
                obj = import_entry(Found(m.name, "custom", folder, m), "contract-" + folder_hash(folder))(ctx)
        except Exception as e:
            return [f"import or factory failed: {type(e).__name__}: {e}"]
        for hook in V1_HOOKS:
            if getattr(obj, hook, None) is not None:
                problems.append(f"uses MC2-1 hook {hook}, which API 2 does not support")
        needs = {"page": ("widgets",), "panel": ("widgets",), "decisions": ("decisions", "resolve"),
                 "events": ("on_event", "drain")}
        for cap, methods in needs.items():
            if m.has(cap):
                problems.extend(f"capability {cap!r} needs {meth}()" for meth in methods if not callable(getattr(obj, meth, None)))
        if m.actions and not callable(getattr(obj, "act", None)):
            problems.append("actions are declared but the addon has no act()")
        la = LoadedAddon(m.name, "custom", m, folder, obj, ctx, "contract")
        if m.has("provider"):
            if not la.providers():
                problems.append("capability 'provider' needs a non-empty providers list of valid providers")
            for p in la.providers():
                problems.extend(f"provider {p.id}: {x}" for x in check_provider(p, ctx.provider_context()))
        slots = ([f"page.{m.name}"] if m.has("page") else []) + list(m.slots)
        ticket = store.load(fw.ws, fw.tickets[0])[1]
        for health in HEALTH:
            for p in la.providers():
                items = sample_items(p.kind, PAYLOAD) if health in ("ok", "stale") else ()
                fw.cache(m.name, make_snapshot(p.id, "contract", health=health, items=items, message=PAYLOAD))
            for slot in slots:
                params = {"q": PAYLOAD} if slot.startswith("page.") or slot == "board.external" else None
                view = SlotView(fw.ws, la, slot, ticket if slot.startswith("ticket.") else None, params)
                try:
                    with no_side_effects(), rendering():
                        widgets = list(obj.widgets(slot, view) or [])
                except Exception as e:
                    problems.append(f"widgets({slot!r}) with health {health} raised {type(e).__name__}: {e}")
                    continue
                for w in widgets:
                    problems.extend(f"widgets({slot!r}): {x}" for x in widget_problems(w, slot=slot, manifest=m))
                valid = tuple(w for w in widgets if not widget_problems(w, slot=slot, manifest=m))
                lint.extend(f"{w.split(' ', 1)[0]} widgets({slot!r}) {w.split(' ', 1)[1]}"
                            for w in widget_warnings(list(valid), slot, sample=PAYLOAD, names=own_names(m)))
                if slot != "today.summary":
                    prefix = str(fw.ws.config.get("id", {}).get("prefix", ""))
                    problems.extend(_render_problems(SlotGroup(m.name, m.title, valid, key_prefix=prefix)))
            if m.has("decisions") and callable(getattr(obj, "decisions", None)):
                try:
                    with no_side_effects(), rendering():
                        ds = list(obj.decisions(SlotView(fw.ws, la, "today.from_addons")) or [])
                except Exception as e:
                    problems.append(f"decisions() with health {health} raised {type(e).__name__}: {e}")
                    ds = []
                problems.extend(f"decisions() returned {type(d).__name__}, not PendingDecision" for d in ds
                                if not isinstance(d, PendingDecision))
                if health == "ok":
                    problems.extend(_resolve_problems(obj, [d for d in ds if isinstance(d, PendingDecision)], ctx))
        if m.has("events") and callable(getattr(obj, "on_event", None)):
            event = Event(1, "2026-10-02T09:00:00Z", fw.tickets[0], "question.asked", "agent:contract", "cli", {"qids": ["Q1"]})
            try:
                with no_side_effects(), rendering():
                    obj.on_event(event, Outbox(tmp / "outbox.jsonl"))
            except Exception as e:
                problems.append(f"on_event() raised {type(e).__name__}: {e}")
        problems.extend(_act_problems(obj, m, ticket, ctx))
    if warnings is not None:
        warnings.extend(_dedupe(lint))
    return _dedupe(problems)


def _act_problems(obj, manifest, ticket, ctx) -> list[str]:
    """act(action_id, target, ctx) gets a ProviderContext (never an Ops) and must return a str, a FileResult,
    a Reveal, an Intent (for an action declared with "tickets": true) or None; anything else is refused by core
    (`intents.as_intent`). The contract has no real target for most actions (that comes from the addon's own
    cached items, in a format only it knows), so a clean `OrchError` refusal ("not in the cache any more") is not
    a problem, only an unhandled exception or a bad return type is. Actions declared with accepts_file are
    skipped here (no sample upload)."""
    from orch.addons.api import FileResult, Intent, Reveal
    from orch.errors import OrchError

    out = []
    if not callable(getattr(obj, "act", None)):
        return out
    for spec in manifest.actions:
        if spec.accepts_file:
            continue
        target = ticket.id if spec.tickets else PAYLOAD
        try:
            with no_side_effects():
                result = obj.act(spec.id, target, ctx.provider_context())
        except OrchError:
            continue  # a clean refusal of a target the contract made up
        except Exception as e:
            out.append(f"act({spec.id!r}, {target!r}) raised {type(e).__name__}: {e}")
            continue
        if result is not None and not isinstance(result, (str, FileResult, Reveal, Intent)):
            out.append(f"act({spec.id!r}, {target!r}) returned {type(result).__name__}, "
                       "not a str, FileResult, Reveal, Intent or None")
    return out


def _resolve_problems(obj, decisions, ctx) -> list[str]:
    """resolve(id, choice, ctx) gets a ProviderContext (never an Ops) and returns None, a message, an Intent about
    the decision's own ticket or a `new` intent (no ref); core executes the Intent (ruling R-A1-INTENT)."""
    from orch.addons.api import TICKET_INTENTS, Intent

    out = []
    if not callable(getattr(obj, "resolve", None)):
        return out
    for d in decisions[:5]:
        for value, _label in d.choices:
            try:
                with no_side_effects():
                    result = obj.resolve(d.id, value, ctx.provider_context())
            except Exception as e:
                out.append(f"resolve({d.id!r}, {value!r}) raised {type(e).__name__}: {e}")
                continue
            if result is not None and not isinstance(result, (str, Intent)):
                out.append(f"resolve({d.id!r}, {value!r}) returned {type(result).__name__}, not an Intent, a str or None")
            elif isinstance(result, Intent) and result.kind != "none":
                if result.kind in TICKET_INTENTS:
                    out.append(f"resolve({d.id!r}, {value!r}) returned a {result.kind} intent; only actions with "
                               "\"tickets\": true may")
                elif result.kind == "new":
                    if result.ref is not None:
                        out.append(f"resolve({d.id!r}, {value!r}) returned a new intent with ref {result.ref!r}; "
                                   "a new ticket has no ref yet")
                elif not d.ticket or (result.ref or "").upper() != d.ticket.upper():
                    out.append(f"resolve({d.id!r}, {value!r}) returned an intent for {result.ref!r}, "
                               f"not the decision's ticket {d.ticket!r}")
    return out


class ProviderContract:
    """Subclass in your tests and define two fixtures: `provider` and `provider_ctx` (a ProviderContext whose runner
    is a FakeRunner with your recorded CLI output, e.g. `orch_workspace.provider_context(manifest, runner=...)`)."""

    def test_identity(self, provider):
        assert provider_identity_problems(provider) == []

    def test_scopes(self, provider, provider_ctx):
        scopes = list(provider.scopes(provider_ctx))
        assert scopes and all(isinstance(s, str) and s for s in scopes)

    def test_fetch_contract(self, provider, provider_ctx):
        assert check_provider(provider, provider_ctx) == []


class AddonContract:
    """Subclass and set `addon_dir` (the folder holding orch-addon.json); optionally `runner` (a FakeRunner)."""

    addon_dir: Path
    runner = None

    def test_static(self):
        from orch.addons.check import static_problems
        assert static_problems(self.addon_dir) == []

    def test_contract(self):
        assert run_addon_contract(self.addon_dir, runner=self.runner) == []
