"""Checking an operation's arguments against its input schema: shared by the CLI (direct mode) and the host."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from jsonschema import Draft202012Validator

from .errors import OrchError

__all__ = ["check_input", "short_error"]


def short_error(e: Any) -> str:
    """One short line for a jsonschema error: where, then what."""
    where = ".".join(str(p) for p in e.absolute_path)
    kind = e.validator
    if kind == "enum":
        what = "must be one of " + ", ".join(str(v) for v in e.validator_value)
    elif kind == "type":
        what = f"must be {e.validator_value}"
    elif kind == "pattern":
        what = "has the wrong form"
    elif kind in ("minimum", "maximum", "maxLength", "minLength"):
        what = f"fails {kind} {e.validator_value}"
    elif kind == "required":
        what = e.message.split(" is a required")[0] + " is required" if "is a required" in e.message else "is required"
    elif kind == "oneOf":
        names = [n for sub in e.validator_value for n in sub.get("required", [])]
        what = "give exactly one of " + ", ".join(names)
    else:
        what = "is not valid"
    return f"{where}: {what}" if where else what


def check_input(op: Any, args: dict[str, Any], clean: Callable[[str], str] = lambda t: t[:200]) -> None:
    """``invalid.input`` for the first schema error of ``args`` (``clean`` redacts and cuts the message)."""
    if op.name == "schedule.add":
        from orch.model.schedules import validate_target

        if error := validate_target(args):
            raise OrchError("schedule.denied", error)
    errors = sorted(Draft202012Validator(op.input).iter_errors(args), key=lambda e: (list(map(str, e.path)), e.message))
    if errors:
        msg = clean(f"{op.cli}: {short_error(errors[0])}")
        if op.name == "ask":  # a mistake in the call, with the fix in the hint (the agent must not give up)
            from orch.ops.plans import ASK_HINT

            raise OrchError("invalid.input", msg, hint=ASK_HINT, retryable=True)
        raise OrchError("invalid.input", msg)
