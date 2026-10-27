"""What ``orch stop`` and ``orch start`` do in each case, as data (pure: no machine access)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .units import ORDER_START, ORDER_STOP, PERSON_KEYS, Svc

__all__ = ["Plan", "Step", "plan_start", "plan_stop"]


@dataclass(frozen=True)
class Step:
    action: str  # "agents" (end this workspace's agents) | "stop" | "start"
    key: str = ""


@dataclass
class Plan:
    scope: str  # "all" | "workspace"
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _by_key(svcs: list[Svc]) -> dict[str, Svc]:
    return {s.key: s for s in svcs}


def plan_stop(scope: str, svcs: list[Svc], marked: set[str], other_workspaces: int = 0) -> Plan:
    """``scope`` all: every installed service, in the order that ends agents before the host goes. ``scope`` workspace:
    this workspace's agents; the services are shared by every workspace of the person, so they stop only when no
    other workspace is on this computer."""
    by = _by_key(svcs)
    plan = Plan(scope)

    def add(keys: tuple[str, ...]) -> None:
        for k in keys:
            s = by.get(k)
            if s is None or not s.installed or s.kind == "unsupported":
                continue
            if s.state == "stopped" and s.name in marked:
                continue  # stopped by orch stop already
            plan.steps.append(Step("stop", k))

    if scope == "all":
        add(ORDER_STOP)
        return plan
    plan.steps.append(Step("agents"))
    if other_workspaces == 0:
        add(tuple(k for k in ORDER_STOP if k in PERSON_KEYS))
    else:
        plan.notes.append(
            f"The host, the agent runner and the dashboard are shared with {other_workspaces} other workspace"
            f"{'s' if other_workspaces != 1 else ''} on this computer, so they keep running. "
            "To stop everything on this computer: orch stop all"
        )
    return plan


def plan_start(scope: str, svcs: list[Svc]) -> Plan:
    """``scope`` all: everything installed. ``scope`` workspace: the services every workspace shares (the hub, relay
    and publish come back only with ``orch start all``)."""
    by = _by_key(svcs)
    keys = ORDER_START if scope == "all" else tuple(k for k in ORDER_START if k in PERSON_KEYS)
    plan = Plan(scope)
    for k in keys:
        s = by.get(k)
        if s is not None and s.installed and s.kind != "unsupported":
            plan.steps.append(Step("start", k))
    return plan
