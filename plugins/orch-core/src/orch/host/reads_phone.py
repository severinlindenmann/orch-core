"""B10 Part B reads: ``phone.requests`` (columns P and R), ``phone.status`` (column P) and ``gate.input`` (P and R).
The logic is :mod:`orch.host.phonehints`; this module only registers the handlers."""

from __future__ import annotations

from typing import Any

from . import phonehints
from .errors import HostError
from .reads import ReadCtx, handler


def _person(ctx: ReadCtx, *, relay: bool) -> None:
    ok = ctx.call.column.name == "P" or (relay and ctx.call.column.name == "R")
    if not ok or ctx.viewer.person is None:
        raise HostError("op.not_allowed", f"{ctx.call.frame['op']} is a person's own")


@handler("phone.requests")
def phone_requests(ctx: ReadCtx) -> dict[str, Any]:
    _person(ctx, relay=True)
    return phonehints.requests_for(ctx.service, ctx.viewer)


@handler("phone.status")
def phone_status(ctx: ReadCtx) -> dict[str, Any]:
    _person(ctx, relay=False)
    return phonehints.status_for(ctx.service, ctx.viewer, ctx.args.get("id"))


@handler("gate.input")
def gate_input(ctx: ReadCtx) -> dict[str, Any]:
    return phonehints.gate_input(ctx.service, ctx.viewer, ctx.args["ref"], ctx.args["gate"])
