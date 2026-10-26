"""Settings > Add-ons (R2-2): which add-ons this workspace has, on or off, and why a switch cannot move.

A read only. Turning an add-on on or off is a signed hand-off like every other settings change (`addon_grant`,
`addon_disable` in handoff.py); nothing here signs or writes. The words come from the setup wizard's catalog
(`orch.setup.facts.catalog`), so the wizard and this page never say different things about one add-on.
"""

from __future__ import annotations

import re
from importlib import metadata

from fastapi import Depends, FastAPI

from . import reads
from .adapters import runs as project
from .errors import fail
from .gateway import Gateway

#: the add-ons a person can switch here: shipped inside orch-core, signed with `addon.grant` / `addon.disable`
SWITCHABLE = (
    "start-agent",
    "terminals",
    "worktrees",
    "publish",
    "usage",
    "estimate",
    "explorer",
    "wiki",
    "quick",
)
#: what the dashboard cannot turn off or on at all
LOCKED = {
    "dashboard": "The dashboard is always on: it is what shows this workspace.",
}
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,39}$")
_RELEASE = re.compile(r"^[0-9a-f]{12}$")


def clean_version(value: object) -> str | None:
    """A version string as a manifest or package writes it, or ``None``: anything odd is dropped, never shown."""
    return value if isinstance(value, str) and _VERSION.fullmatch(value) else None


def package_version(name: str) -> str | None:
    try:
        return clean_version(metadata.version(name))
    except metadata.PackageNotFoundError:
        return None


def versions_of(link) -> dict:
    """What this install runs, read only: the orch-core package, the host's release digest (from its welcome; ``None``
    when an older host does not say) and this dashboard's own package. No host op, nothing signed."""
    release = link.welcome.get("release")
    return {
        "core": {
            "version": package_version("orch-core"),
            "release": release if isinstance(release, str) and _RELEASE.fullmatch(release) else None,
        },
        "dashboard": {"version": package_version("orch-dashboard")},
    }


def live_runs(gateway: Gateway, ws: str) -> list[dict] | None:
    """The signed-in person's own runs that are starting or running now (the host lists no one else's);
    ``None`` when the host cannot say (then a switch-off is
    refused: unknown counts as busy)."""
    link = gateway.link(ws)
    if not link.supports("run.list", "run/1"):
        return []  # a host with no runs has nothing running
    out, page = [], None
    for _ in range(5):
        data = gateway.call(ws, "run.list", {"state": "live", **({"page": page} if page else {})})
        out += [project.run(r) for r in data.get("runs") or [] if isinstance(r, dict)]
        page = data.get("next_page")
        if not page:
            return out
    return None


def busy(name: str, runs: list[dict] | None) -> str | None:
    """Why ``name`` cannot be turned off now (plain words), or ``None``."""
    if runs is None:
        return "Could not check what is running. Try again in a moment."
    if name == "start-agent":
        n = sum(1 for r in runs if r.get("kind") == "agent")
        what = "an agent of yours is" if n == 1 else f"{n} agents of yours are"
    elif name == "terminals":
        n = sum(1 for r in runs if r.get("kind") == "shell")
        what = "a terminal of yours is" if n == 1 else f"{n} terminals of yours are"
    elif name == "worktrees":
        n = sum(1 for r in runs if (r.get("cwd") or {}).get("kind") == "worktree")
        what = "one of your running jobs uses" if n == 1 else f"{n} of your running jobs use"
        if n:
            return f"Stop first: {what} a worktree right now."
        return None
    else:
        return None
    return f"Stop first: {what} running right now." if n else None


def needs(name: str, runner: dict | None) -> str | None:
    """What is missing before ``name`` can be turned on (a greyed switch gets this as its reason), or ``None``."""
    if name in ("start-agent", "terminals"):
        if runner is None:
            return "This host does not report dashboard execution, which this add-on needs."
        if not runner["enabled"]:
            return "Needs dashboard execution first. Turn it on here, then switch this on."
        if name == "terminals" and not runner["terminals"]:
            return "Terminals are not turned on in dashboard execution here. Turn them on here, then switch this on."
    return None


def runner_fix(name: str, runner: dict | None) -> bool:
    """Whether what ``needs`` says is missing is the dashboard-execution switch, which the page now offers to turn on
    (#1198 C14, ``POST /click/runner``)."""
    return runner is not None and needs(name, runner) is not None and name in ("start-agent", "terminals")


def register(app: FastAPI, gateway: Gateway) -> None:
    from orch.setup import facts

    @app.get("/api/w/{ws}/versions")
    def versions(ws: str = Depends(reads.workspace)):
        return versions_of(gateway.enabled(ws))

    @app.get("/api/w/{ws}/add-ons/{name}/settings")
    def addon_settings(name: str, ws: str = Depends(reads.workspace)):
        """The side panel: the settings an On add-on declares (title, schema) and the value the workspace holds."""
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", name):
            raise fail("invalid.input", "not an add-on name", exit=1)
        link = gateway.enabled(ws)
        row = next(
            (a for a in link.call("addon.list")["addons"] if isinstance(a, dict) and a.get("name") == name), None
        )
        spec = row.get("settings") if row else None
        if not isinstance(spec, dict):
            raise fail("not_found", "this add-on has no settings", exit=1)
        held = link.call("workspace.settings", {}).get("addons")
        value = held.get(name) if isinstance(held, dict) else None
        return {"name": name, "title": spec.get("title"), "schema": spec.get("schema"), "value": value}

    @app.get("/api/w/{ws}/add-ons")
    def add_ons(ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        listed = {a["name"]: a for a in link.call("addon.list")["addons"] if isinstance(a, dict)}
        runner = project.runner(gateway.call(ws, "whoami").get("runner"))
        served = link.welcome.get("ops", {})
        can_sign = served.get("addon.grant") == 1 and served.get("addon.disable") == 1
        runs = live_runs(gateway, ws)
        own = versions_of(link)
        try:
            settings = gateway.call(ws, "workspace.settings", {})
        except Exception:  # noqa: BLE001 - an unread setting is unknown, not unset
            settings = None
        rows = []
        for info in facts.catalog(optional=True):
            if info.id in LOCKED:
                rows.append(
                    {
                        "id": info.id,
                        "title": info.title,
                        "desc": info.desc,
                        "state": "on",
                        "needs": None,
                        "set_up": True,
                        "locked": LOCKED[info.id],
                        "blocked": None,
                        "runner_switch": False,
                        # always-on parts ship with the install: the dashboard's own package, quick tasks are core
                        "version": own["dashboard"]["version"] if info.id == "dashboard" else own["core"]["version"],
                    }
                )
                continue
            if info.id not in SWITCHABLE:
                continue
            got = listed.get(info.id)
            raw = got.get("state") if got else "not_granted"
            on = raw == "active"
            state = "on" if on else ("attention" if raw in ("changed", "missing") else "off")
            if not can_sign:
                blocked = "This host cannot change add-ons from the dashboard yet."
            else:
                blocked = busy(info.id, runs) if on else needs(info.id, runner)
            setting = {"publish": "publish"}.get(info.id)
            set_up = True
            if setting:
                set_up = None if settings is None else isinstance(settings.get("publish"), dict)
            rows.append(
                {
                    "id": info.id,
                    "title": info.title,
                    "desc": info.desc,
                    "state": state,
                    "needs": setting,
                    "set_up": set_up,
                    "locked": None,
                    "blocked": blocked,
                    "runner_switch": (not on) and can_sign and runner_fix(info.id, runner),
                    "version": clean_version(got.get("version")) if got else None,
                }
            )
        return {"available": True, "add_ons": rows}


def check_off(gateway: Gateway, ws: str, name: str) -> None:
    """The hand-off for turning ``name`` off is refused while its work is running (the page greys the switch too)."""
    why = busy(name, live_runs(gateway, ws))
    if why:
        raise fail("addon.busy", why, exit=1)


#: `addon.list` states that mean "off" ("changed" and "missing" are neither: granting again is a real change)
OFF_STATES = ("disabled", "not_granted", "purged")


def addon_title(name: str) -> str:
    from orch.setup import facts

    return next((a.title for a in facts.catalog(optional=True) if a.id == name), name)


def addon_base(link, name: str, turning_on: bool) -> dict:
    """The intent base of an add-on switch: the add-on and its latest `addon.granted` / `addon.disabled` event.

    The host dedupes one idem to ONE request for 24 h, so an intent that is the same text again (on, off, on) would
    return the first, already signed request and the person's new change would never be asked for. The latest
    switch event moves whenever the state moves, so each real change is its own intent; a double click, with no
    event in between, is still one. The state is read here too: asking for what already is, is refused in plain
    words (`addon.already`) and never prepared, signed or dressed up as "do it again".

    Fails closed: if the host cannot say, nothing is prepared (the host error passes through)."""
    state = next(
        (a.get("state") for a in link.call("addon.list")["addons"] if isinstance(a, dict) and a.get("name") == name),
        "not_granted",
    )
    if turning_on and state == "active":
        raise fail("addon.already", f"{addon_title(name)} is already on.", exit=1)
    if not turning_on and state in OFF_STATES:
        raise fail("addon.already", f"{addon_title(name)} is already off.", exit=1)
    page = None
    for _ in range(5):
        args = {"scope": "workspace", "types": ["addon.granted", "addon.disabled"], "order": "desc", "limit": 100}
        data = link.call("activity.list", {**args, **({"page": page} if page else {})})
        for item in data.get("items") or []:
            if (
                isinstance(item, dict)
                and item.get("type") in ("addon.granted", "addon.disabled")
                and (item.get("data") or {}).get("name") == name
                and isinstance(item.get("id"), str)
            ):
                return {"addon": name, "head": item["id"]}
        page = data.get("next_page")
        if not page:
            return {"addon": name, "head": None}
    raise fail("host.busy", "Could not read this add-on's history. Try again in a moment.", exit=5)
