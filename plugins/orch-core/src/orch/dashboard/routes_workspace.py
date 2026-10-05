from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from orch import onboarding, update
from orch.addons import cache, manage, userfiles
from orch.addons.discovery import custom_addons_dir, discover, find
from orch.addons.loader import valid_name
from orch.addons.runtime import banner_for
from orch.addons.settings import form_value, parse_settings
from orch.core.check import record_invalidations, run_checks
from orch.core.maintenance import tidy
from orch.dashboard.auth import strict_same_origin
from orch.dashboard import routes_widgets, setup_state
from orch.dashboard.views import HUMAN, _theme, back, confirm_page, error_text, invalidate_setup_count, page
from orch.errors import OrchError
from orch.hooks.install import hook_state
from orch.remote import store as phone_store

router = APIRouter()

_BACK = "/workspace#addons"
_KIND_LABEL = {"capabilities": "capability", "binaries": "binary", "env": "env", "actions": "action",
               "uploads": "file upload action"}


def _checks(ws, hook_states=None) -> tuple[list, str | None]:
    """`orch doctor` never breaks the Workspace page; a failure shows as one error line instead."""
    try:
        return onboarding.doctor(ws.root, hook_states=hook_states), None
    except Exception as e:  # noqa: BLE001 - any failure here is shown, not raised
        return [], f"could not run the setup checks: {e}"


def _repos(ws) -> list[dict]:
    repos_cfg = ws.config.get("git", {}).get("repos") or {}
    out = []
    for name, cfg in repos_cfg.items():
        path = (ws.root / ((cfg or {}).get("path") or name)).resolve()
        out.append({"name": name, "path": path, "hook": hook_state(path)})
    return out


TABS = ("addons", "setup", "widgets", "phones", "advanced")


def _relative(path, root) -> str:
    """A repository path as the workspace sees it (`./ingest`, `../shared`); the full path stays in the title."""
    try:
        rel = os.path.relpath(str(path), str(root))
    except ValueError:  # another drive on Windows
        return str(path)
    return rel if rel.startswith("..") else "./" + rel if rel != "." else "."


def _refused() -> PlainTextResponse:
    return PlainTextResponse("cross-origin request refused", status_code=403)


def _update_path():
    return custom_addons_dir() / ".update-check.json"


def _addon_rows(ws, runtime) -> list[dict]:
    """The Addons table: every default and custom addon with trust state, health and the per-workspace switch.
    Reads files and the snapshot cache only; never imports addon code or runs a command."""
    enabled = userfiles.workspace_addons(ws.root)
    entries = userfiles.registry_entries()
    updates = userfiles.read_json_object(_update_path())
    problems = dict(runtime.registry.problems)
    settings_groups = {g.addon: [g] for g in runtime.slot("workspace.settings")}
    rows = []
    for f in discover():
        state = userfiles.trust_state(f)
        role, label = userfiles.TRUST_LABELS[state]
        la = runtime.registry.get(f.name)
        review = None
        if f.kind == "custom" and state in ("untrusted", "changed") and f.name in entries:
            try:
                review = manage.review(f.name)
            except OrchError:
                review = None
        saved = enabled.get(f.name, {}).get("config", {})
        fields = [{"key": x.key, "label": x.label, "type": x.type, "options": x.options, "value": form_value(x, saved)}
                  for x in (f.manifest.settings_schema if f.manifest and state == "trusted" else ())]
        upd = updates.get(f.name) if isinstance(updates.get(f.name), dict) else None
        rows.append({
            "name": f.name, "title": f.manifest.title if f.manifest else f.name, "kind": f.kind,
            "version": f.manifest.version if f.manifest else None,
            "source": "orch-core plugin" if f.kind == "default"
            else manage._source_text((entries.get(f.name) or {}).get("source") or {}),
            "trust": state, "trust_role": role, "trust_label": label, "error": f.error,
            "enabled": enabled.get(f.name, {}).get("enabled", False), "can_enable": state == "trusted",
            "problem": problems.get(f.name),
            "health": banner_for(cache.read_snapshots(ws, f.name)) if la is not None and la.has("provider") else None,
            "review": review, "fields": fields, "settings_groups": settings_groups.get(f.name, []),
            "update": upd if upd and upd.get("available") else None,
            # the "Keep syncing while Mission Control runs" switch, only for an addon that has an always_on provider
            "background_capable": la is not None and any(getattr(p, "always_on", False) is True for p in la.providers()),
            "background": enabled.get(f.name, {}).get("background", False),
        })
    return rows


def _suggested(ws, rows) -> list[dict]:
    """`suggested_addons` from orchestrator/config.json: only a hint, nothing is enabled until the human clicks."""
    by = {r["name"]: r for r in rows}
    out = []
    suggested = ws.config.get("suggested_addons")
    for name in suggested if isinstance(suggested, list) else []:
        if not valid_name(name) or by.get(name, {}).get("enabled") or any(s["name"] == name for s in out):
            continue
        r = by.get(name)
        out.append({"name": name, "title": r["title"] if r else name, "found": r is not None,
                    "can_enable": bool(r and r["can_enable"])})
    return out


@router.get("/workspace")
def workspace(request: Request):
    ws = request.app.state.ws
    temp_files = [p for p in ws.temporary_dir.rglob("*") if p.is_file()] if ws.temporary_dir.is_dir() else []
    static_files = sorted(p.relative_to(ws.static_dir).as_posix()
                          for p in ws.static_dir.rglob("*") if p.is_file()) if ws.static_dir.is_dir() else []
    runtime = request.app.state.addons
    rows = _addon_rows(ws, runtime)
    dashboard = ws.config.get("dashboard", {})
    # doctor, hook states and orch check (git calls, every ticket read): the last background round under
    # `orch serve`, asked for again when older than a few seconds so the next visit is current; computed here
    # when there is no such loop or a Workspace POST made them stale.
    setup = setup_state.state(ws)
    snap = setup.get() if setup.background else setup.refresh()
    if time.monotonic() - snap.at > setup_state.RECHECK_AFTER:
        setup.refresh_soon()
    record_invalidations(ws, snap.findings)
    from orch.dashboard import launch
    from orch.dashboard.data import agent_start

    launch_settings = launch.load_settings()
    pairing_targets = runtime.pairing_targets()
    # The one-time pairing link (it carries the phone's key): taken out of its store by this one render. Peek
    # first: a token of some other kind (e.g. an action's Reveal) must not be burned by a mismatched ?pair=.
    reveals = request.app.state.reveals
    pair_token = request.query_params.get("pair")
    peeked = reveals.peek(pair_token)
    pairing = reveals.pop(pair_token) if isinstance(peeked, dict) and peeked.get("kind") == "pair" else None
    tab = request.query_params.get("tab", "")
    tab = tab if tab in TABS else ("phones" if pairing is not None else "addons")
    widgets = {}
    if tab == "widgets":
        from markupsafe import Markup

        from orch.core import ledger
        from orch.widgets import registry
        from orch.widgets.render import css_names, inline
        theme = _theme(request, ws)
        widgets = dict(cat=routes_widgets.catalog(ws, request.query_params, theme), widget_css=css_names(),
                       inline_md=lambda text: Markup(inline(text)), html_state=ledger.widgets_html_state(ws),
                       unused_days=routes_widgets.UNUSED_DAYS, broken=registry.template_problems(ws.home))
    response = page(request, "workspace.html", nav="workspace", title="Workspace & addons", tab=tab, **widgets,
                checks=snap.checks, checks_error=snap.checks_error,
                repos=[{**r, "rel": _relative(r["path"], ws.root)} for r in snap.repos],
                theme_default=dashboard.get("theme", "system"),
                brand=dashboard.get("brand", "none"),
                findings=snap.findings,
                temp_count=len(temp_files),
                temp_kb=round(sum(p.stat().st_size for p in temp_files) / 1024, 1),
                max_age=ws.config["temporary"]["max_age_days"],
                static_files=static_files[:500], static_hidden=max(0, len(static_files) - 500),
                addon_rows=rows, suggested=_suggested(ws, rows), has_custom=any(r["kind"] == "custom" for r in rows),
                kind_label=_KIND_LABEL,
                terminal=launch_settings["terminal"], launch_path=launch_settings["path"],
                default_harness=agent_start.default_harness(ws, launch_settings) or "none",
                launch_warnings=agent_start.launch_warnings(ws, launch_settings),
                addon_errors=runtime.registry.errors()[-4000:],
                config_text=json.dumps(ws.config, indent=2, ensure_ascii=False),
                phones=phone_store.phones(ws.root), phone_permissions=phone_store.permissions(ws.root),
                pairing_targets=pairing_targets, pairing=pairing)
    if pairing is not None:
        response.headers["Cache-Control"] = "no-store"
        if "etag" in response.headers:
            del response.headers["etag"]  # a one-time page is never revalidated
    return response


@router.post("/workspace/tidy")
def tidy_now(request: Request, ask: str = Form("")):
    if ask:
        return confirm_page(request, action="/workspace/tidy", fields=[], title="Delete old files from temporary/?",
                            body="Files older than the tidy age and old Start agent run scripts are removed. Tickets,"
                                 " artifacts and the event log are not touched.",
                            confirm="Tidy now", cancel="Keep files", cancel_href="/workspace?tab=advanced", nav="workspace")
    ws = request.app.state.ws
    removed = tidy(ws)
    invalidate_setup_count(ws)
    return back("/workspace?tab=advanced", msg=f"removed {len(removed)} old file(s) from temporary/ and old Start agent run scripts")


@router.post("/workspace/shortcuts")
def keyboard_shortcuts(request: Request, on: str = Form("0")):
    """The human's switch for Mission Control's keyboard shortcuts in this workspace (kept per user)."""
    if not strict_same_origin(request):
        return _refused()
    userfiles.set_keyboard_shortcuts(request.app.state.ws.root, on == "1")
    return back("/workspace?tab=setup#shortcuts", msg="Keyboard shortcuts on" if on == "1" else "Keyboard shortcuts off")


@router.post("/workspace/addons/check-updates")
async def addon_check_updates(request: Request):
    """Human POST only; never run during a render (the daily automatic check is not part of A1)."""
    if not strict_same_origin(request):
        return _refused()
    try:
        infos = await asyncio.to_thread(manage.update_check, None)
    except OrchError as e:
        return back(_BACK, err=error_text(e))
    at = datetime.now(timezone.utc).isoformat()

    def mutate(data: dict) -> None:
        data.clear()
        data.update({i.name: {"available": i.available, "message": i.message, "checked_at": at} for i in infos})
    await asyncio.to_thread(userfiles.update_json, _update_path(), mutate)
    n = sum(i.has_update for i in infos)
    return back(_BACK, msg=f"{n} update(s) available" if n else "All custom addons are up to date")


def _update_everything() -> tuple[list[str], list[str]]:
    """(done, problems). Core is pulled and reinstalled but cannot restart a running server; an addon that asks for
    something new is installed and left for the human to review and trust in its row."""
    done, problems = [], []
    try:
        core = update.core_check()
        if core:
            done.append(f"orch-core updated ({core.behind} commits); restart orch serve to run it. {update.core_apply(core)}")
    except OrchError as e:
        problems.append(f"orch-core: {e.message}")
    updated = set()
    for info in manage.update_check(None):
        if not info.has_update:
            continue
        try:
            before, m, r, trusted = update.apply_addon(info.name, actor=HUMAN)
        except OrchError as e:
            problems.append(f"{info.name}: {e.message}")
            continue
        updated.add(info.name)
        done.append(f"{info.name} {before} → {m.version}" + ("" if trusted else " (review and trust it below)"))
    userfiles.update_json(_update_path(), lambda d: [d.pop(n, None) for n in updated])
    return done, problems


@router.post("/workspace/addons/update-all")
async def addon_update_all(request: Request, ask: str = Form("")):
    if not strict_same_origin(request):
        return _refused()
    if ask:
        return confirm_page(request, action="/workspace/addons/update-all", fields=[],
                            title="Update orch and its addons?", body="Pulls orch-core and reinstalls it, then updates"
                            " every custom addon. An addon that asks for nothing new is trusted again; any other waits for"
                            " your review.", confirm="Update all", cancel="Cancel", cancel_href=_BACK, nav="workspace")
    try:
        done, problems = await asyncio.to_thread(_update_everything)
    except OrchError as e:
        return back(_BACK, err=error_text(e))
    await asyncio.to_thread(request.app.state.addons.reload)
    invalidate_setup_count(request.app.state.ws)
    if problems:
        return back(_BACK, err="; ".join(problems), msg="; ".join(done) or None)
    return back(_BACK, msg="; ".join(done) if done else "Everything is up to date")


@router.post("/workspace/addons/{name}/enable")
def addon_enable(request: Request, name: str, enabled: str = Form(...)):
    if not strict_same_origin(request):
        return _refused()
    ws = request.app.state.ws
    on = enabled == "1"
    try:
        (manage.enable if on else manage.disable)(ws.root, name, actor=HUMAN)
    except OrchError as e:
        return back(_BACK, err=error_text(e))
    request.app.state.addons.reload()  # import now, in this POST: the next page render imports nothing
    invalidate_setup_count(ws)  # orch check reports addon trust and enablement
    return back(_BACK, msg=f"{'Enabled' if on else 'Disabled'} {name} for this workspace")


@router.post("/workspace/addons/{name}/background")
def addon_background(request: Request, name: str, on: str = Form("0")):
    """The human-only switch "Keep syncing while Mission Control runs" (a dashboard POST; there is no CLI for it)."""
    if not strict_same_origin(request):
        return _refused()
    if not valid_name(name):
        return back(_BACK, err="unknown addon")
    try:
        userfiles.set_background(request.app.state.ws.root, name, on == "1")
    except OrchError as e:
        return back(_BACK, err=error_text(e))
    return back(_BACK, msg=f"Background sync on for {name}" if on == "1" else f"Background sync off for {name}")


@router.post("/workspace/addons/{name}/trust")
async def addon_trust(request: Request, name: str, seen: str = Form(...), ask: str = Form("")):
    if not strict_same_origin(request):
        return _refused()
    if ask:
        return confirm_page(request, action=f"/workspace/addons/{name}/trust", fields=[("seen", seen)],
                            title=f"Trust {name}?", body="It runs inside Mission Control with your permissions. Read"
                            " what changed in the review on Workspace & addons first.", confirm="Trust this version",
                            cancel="Cancel", cancel_href=_BACK, nav="workspace")
    try:
        await asyncio.to_thread(manage.trust_addon, name, seen_digest=seen, actor=HUMAN)
    except OrchError as e:
        return back(_BACK, err=error_text(e))
    await asyncio.to_thread(request.app.state.addons.reload)
    invalidate_setup_count(request.app.state.ws)
    return back(_BACK, msg=f"Trusted {name}; enable it when you want it in this workspace")


@router.post("/workspace/addons/{name}/settings")
async def addon_settings(request: Request, name: str):
    if not strict_same_origin(request):
        return _refused()
    ws = request.app.state.ws
    f = find(name) if valid_name(name) else None
    if f is None or f.manifest is None or not f.manifest.settings_schema or userfiles.trust_state(f) != "trusted":
        return back(_BACK, err=f"{name} has no settings to save here")
    values, errors = parse_settings(f.manifest, await request.form())
    loaded = request.app.state.addons.registry.get(name)
    check = getattr(loaded.obj, "check_settings", None) if loaded is not None else None
    notes: list[str] = []
    if callable(check) and not errors:  # an addon may refuse a value it cannot use, or say what it could not find
        try:
            more, notes = await asyncio.to_thread(check, values)
            errors += [str(e) for e in more]
            notes = [str(n) for n in notes]
        except Exception:  # a broken check never blocks saving
            notes = []
    if errors:
        return back(_BACK, err="; ".join(errors))
    await asyncio.to_thread(userfiles.save_addon_config, ws.root, name, values)
    await asyncio.to_thread(request.app.state.addons.reload)
    invalidate_setup_count(ws)
    # a changed setting should show on the addon's page now, not after the next manual Refresh
    request.app.state.scheduler.request_refresh(name)
    return back(_BACK, msg=f"Saved settings for {f.manifest.title}" + ("".join(f". {n}" for n in notes[:2]) if notes else ""))


# -- Phones (remote humans): human-only dashboard POSTs, no CLI -----------------------------

_PHONES = "/workspace?tab=phones#phones"


@router.post("/workspace/phones/pair")
def phone_pair(request: Request, addon: str = Form(...), label: str = Form("Phone")):
    if not strict_same_origin(request):
        return _refused()
    target = dict(request.app.state.addons.pairing_targets()).get(addon)
    if target is None:
        return back(_PHONES, err="that addon cannot pair phones here")
    try:
        phone, code = phone_store.pair(request.app.state.ws.root, label=label, addon=addon)
    except OrchError as e:
        return back(_PHONES, err=error_text(e))
    # The link (with the key) goes into the one-time store; the redirect carries only the store's token.
    token = request.app.state.reveals.put({"kind": "pair", "url": phone_store.pair_link(target.url, phone.id, phone.key),
                                           "code": code, "label": phone.label, "target": target.label})
    return RedirectResponse(f"/workspace?pair={token}#phones", status_code=303)


@router.post("/workspace/phones/{phone_id}/revoke")
def phone_revoke(request: Request, phone_id: str, ask: str = Form("")):
    if not strict_same_origin(request):
        return _refused()
    ws = request.app.state.ws
    if ask:
        phone = phone_store.find(ws.root, phone_id)
        label = phone.label if phone is not None else "this phone"
        return confirm_page(request, action=f"/workspace/phones/{phone_id}/revoke", fields=[],
                            title=f"Revoke {label}?", body="Its decisions stop applying. Pair it again to use it.",
                            confirm="Revoke phone", cancel="Keep phone", cancel_href=_PHONES, danger=True,
                            nav="workspace")
    if phone_store.find(ws.root, phone_id) is None:
        return back(_PHONES, err="no such phone in this workspace")
    phone_store.revoke(ws.root, phone_id)
    return back(_PHONES, msg="Phone revoked")


@router.post("/workspace/phones/permissions")
async def phone_permissions(request: Request):
    if not strict_same_origin(request):
        return _refused()
    form = await request.form()
    await asyncio.to_thread(phone_store.set_permissions, request.app.state.ws.root,
                            {k: form.get(k) == "1" for k in phone_store.KINDS})
    return back(_PHONES, msg="Phone permissions saved")


@router.post("/workspace/density")
def display_density(request: Request, density: str = Form("comfortable")):
    """Comfortable or compact for this workspace, kept per user like the shortcuts switch; layout.html puts it on
    the page root (data-density), where tokens.css switches control, row and card sizes."""
    if not strict_same_origin(request):
        return _refused()
    userfiles.set_density(request.app.state.ws.root, density)
    return back("/workspace?tab=setup#density", msg=f"Density: {density}" if density in userfiles.DENSITIES else None)
