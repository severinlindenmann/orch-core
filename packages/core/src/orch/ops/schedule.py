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


def risk(target="existing"):
    if target == "new":
        return "An agent will start by itself, with your rights on each new ticket, each time"
    if target != "existing":
        raise ValueError("target must be existing or new")
    return RISK


def describe(rule, ticket, ends, target="existing", title=None):
    from orch.model.schedules import validate_target

    if error := validate_target({"target": target, **({"title": title} if title is not None else {})}):
        raise ValueError(error)
    every = rule["every"]
    when = {
        "day": "every day",
        "month": f"every month on day {rule.get('day')} (last day if shorter)",
        "once": f"once on {rule.get('date')}",
    }.get(every)
    if every == "week":
        names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
        when = "every " + ", ".join(names[d] for d in rule["weekdays"])
    if target == "new":
        return (
            f'Schedule creates a NEW ticket every run, titled "{title}", '
            f'{when} at {rule["at"]} ({rule["tz"]}), until {ends}'
        )
    return f"Runs {when} at {rule['at']} ({rule['tz']}) on {ticket}, until {ends}"
