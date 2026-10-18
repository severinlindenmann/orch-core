"""Settings > Add-ons (R2-2): which add-ons this workspace has, on or off, and why a switch cannot move.

A read only. Turning an add-on on or off is a signed hand-off like every other settings change (`addon_grant`,
`addon_disable` in handoff.py); nothing here signs or writes. The words come from the setup wizard's catalog
(`orch.setup.facts.catalog`), so the wizard and this page never say different things about one add-on.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI

from . import reads
from .adapters import runs as project
from .errors import fail
from .gateway import Gateway

#: the add-ons a person can switch here: shipped inside orch-core, signed with `addon.grant` / `addon.disable`
SWITCHABLE = ("start-agent", "terminals", "worktrees", "publish", "usage", "estimate", "explorer")
#: what the dashboard cannot turn off or on at all
LOCKED = {
    "dashboard": "The dashboard is always on: it is what shows this workspace.",
    "quick": "Always available. There is nothing to switch.",
}
RUNNER_ENABLE = "orch runner enable"


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
            return f"Needs dashboard execution first. In your terminal run: {RUNNER_ENABLE}"
        if name == "terminals" and not runner["terminals"]:
            return f"Terminals are not turned on in dashboard execution here. In your terminal run: {RUNNER_ENABLE}"
    return None


def register(app: FastAPI, gateway: Gateway) -> None:
    from orch.setup import facts

    @app.get("/api/w/{ws}/add-ons")
    def add_ons(ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        listed = {a["name"]: a for a in link.call("addon.list")["addons"] if isinstance(a, dict)}
        runner = project.runner(gateway.call(ws, "whoami").get("runner"))
        served = link.welcome.get("ops", {})
        can_sign = served.get("addon.grant") == 1 and served.get("addon.disable") == 1
        runs = live_runs(gateway, ws)
        rows = []
        for info in facts.catalog(optional=True):
            if info.id in LOCKED:
                rows.append(
                    {
                        "id": info.id,
                        "title": info.title,
                        "desc": info.desc,
                        "state": "on",
                        "locked": LOCKED[info.id],
                        "blocked": None,
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
            rows.append(
                {
                    "id": info.id,
                    "title": info.title,
                    "desc": info.desc,
                    "state": state,
                    "locked": None,
                    "blocked": blocked,
                }
            )
        return {"available": True, "add_ons": rows}


def check_off(gateway: Gateway, ws: str, name: str) -> None:
    """The hand-off for turning ``name`` off is refused while its work is running (the page greys the switch too)."""
    why = busy(name, live_runs(gateway, ws))
    if why:
        raise fail("addon.busy", why, exit=1)
