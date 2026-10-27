"""Manual schedule lookup and the words used in signing reviews."""

from orch.ops import followup
from orch.ops.errors import OrchError
from orch.store.render import thaw

__all__ = ["RISK", "describe", "find", "thaw"]

RISK = "An agent will start by itself, with your rights on this ticket, each time"


def find(c, sid):
    for view in followup.tickets(c):
        for rec in view.schedules:
            if rec["id"] == sid:
                return view, rec
    raise OrchError("not_found", f"no schedule {sid}")


def describe(rule, ticket, ends):
    every = rule["every"]
    when = {
        "day": "every day",
        "month": f"every month on day {rule.get('day')} (last day if shorter)",
        "once": f"once on {rule.get('date')}",
    }.get(every)
    if every == "week":
        names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
        when = "every " + ", ".join(names[d] for d in rule["weekdays"])
    return f"Runs {when} at {rule['at']} ({rule['tz']}) on {ticket}, until {ends}"
