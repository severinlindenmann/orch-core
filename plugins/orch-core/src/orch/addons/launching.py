"""The `launch` addon capability, as core runs it: what an enabled, trusted addon asks of a Start agent preview or launch.

Core owns every step. An addon's `launch(req, ctx)` returns a `LaunchPlan` (api.py); this module calls it, checks it
and merges the answers of all such addons (the first addon by name wins a conflict), and the Start agent code applies
the result: a model argument, extra variables for the launched process only, an optional prompt sentence. With no
enabled addon that has the capability, `resolve` returns None and nothing about a launch changes.

Preview and launch both come through `resolve`, so the command the box shows is the command that runs. A preview
(`strict=False`) never raises: an addon that fails shows a warning. A launch (`strict=True`) refuses to start: an
addon that was switched on to choose a model must not be skipped silently.
"""
from __future__ import annotations

from dataclasses import dataclass

from orch.addons.api import LaunchPlan, LaunchRequest
from orch.errors import UsageError


@dataclass(frozen=True)
class Routing:
    """The merged, checked plan. `addons` names the addons that took part (for `launched`)."""
    model: str | None = None
    env: tuple = ()  # ((name, value), ...) in a fixed order
    note: str = ""
    label: str = ""
    reason: str = ""
    warnings: tuple = ()
    addons: tuple = ()

    @property
    def active(self) -> bool:
        return bool(self.model or self.env or self.note)


def _loaded(ws) -> list:
    try:
        return [la for la in ws.addons if la.manifest is not None and la.manifest.has("launch")
                and callable(getattr(la.obj, "launch", None))]
    except Exception:  # a broken addon registry never breaks a page
        return []


def active(ws) -> bool:
    """Some enabled, trusted addon takes part in launches (cheap: lets callers skip all of this when none does)."""
    return bool(_loaded(ws))


def _log(ws, name: str, where: str) -> None:
    from orch.addons.loader import _log_error
    try:
        _log_error(ws, name, where)
    except Exception:
        pass


def resolve(ws, req: LaunchRequest, *, strict: bool = False) -> Routing | None:
    """The merged plan for `req`, or None when no addon plans anything. See the module text for `strict`."""
    model, env, notes, label, reason, warnings, took = None, {}, [], "", "", [], []
    for la in _loaded(ws):
        try:
            plan = la.obj.launch(req, la.ctx)
            if plan is None:
                continue
            if not isinstance(plan, LaunchPlan):
                raise ValueError(f"launch() returned {type(plan).__name__}, not a LaunchPlan")
        except Exception as e:
            _log(ws, la.name, "launch")
            msg = f"{la.manifest.title} could not choose how to start this session ({type(e).__name__}: {e})"
            if strict:
                raise UsageError(msg, hint="see addon-errors.log in the state folder; turn the addon off to start "
                                           "without it") from e
            warnings.append(msg)
            continue
        took.append(la.name)
        model = model or plan.model
        for k, v in plan.env.items():
            env.setdefault(k, v)
        if plan.note:
            notes.append(plan.note)
        label, reason = label or plan.label, reason or plan.reason
        warnings.extend(w for w in plan.warnings if w not in warnings)
    if not took and not warnings:
        return None
    return Routing(model, tuple(sorted(env.items())), " ".join(notes),
                   label, reason, tuple(warnings), tuple(took))


def launched(ws, req: LaunchRequest, routing: Routing | None, session: str) -> None:
    """Tell the addons that took part that the session started (they drop one-shot state, remember the start).
    Best effort: the session is already running, so a failure here is logged, never raised."""
    if routing is None:
        return
    for la in _loaded(ws):
        if la.name not in routing.addons:
            continue
        fn = getattr(la.obj, "launched", None)
        if not callable(fn):
            continue
        try:
            fn(req, routing, la.ctx, session)
        except Exception:
            _log(ws, la.name, "launched")
