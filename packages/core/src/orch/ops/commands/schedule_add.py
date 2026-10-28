"""Person-signed bounded authority to start sessions on a calendar rule."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from orch.identity import new_ulid
from orch.ops import schedule
from orch.ops._dsl import FILE, REF, STR, S, err, obj, operation
from orch.ops.errors import OrchError
from orch.ops.human import Human


def handle(ctx, args):
    h = Human(ctx, "schedule.add")
    c = h.call
    view = c.resolve(args["ref"], live_only=False)
    now = c.now
    try:
        tz = args.get("tz", "UTC")
        zone = ZoneInfo(tz)
        every = args.get("every", "once")
        at = args["at"]
        rule = {"every": every, "at": at, "tz": tz}
        if every == "once":
            local = datetime.fromisoformat(at)
            if local.tzinfo is not None or local.second or local.microsecond:
                raise ValueError("once time must be local YYYY-MM-DD HH:MM")
            rule.update(at=local.strftime("%H:%M"), date=local.strftime("%Y-%m-%d"))
        if every == "week":
            rule["weekdays"] = [int(d) for d in args["weekdays"].split(",")]
        if every == "month":
            rule["day"] = args["day"]
        ends = now + 30 * 86400
        if args.get("until"):
            dt = datetime.fromisoformat(args["until"])
            ends = int((dt if dt.tzinfo else dt.replace(tzinfo=zone)).timestamp())
        if every != "week" and "weekdays" in args or every != "month" and "day" in args:
            raise ValueError("weekdays is only for week; day is only for month")
    except (ValueError, KeyError) as exc:
        raise OrchError("schedule.denied", f"invalid schedule rule: {exc}") from None

    def iso(at):
        return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    sid = "sc_" + new_ulid()
    if ("brief" in args) == ("brief_file" in args):
        raise OrchError("schedule.denied", "give exactly one of brief, brief_file")
    brief = c.read_text_source({"text": args["brief"]} if "brief" in args else {"file": args["brief_file"]})
    event = {
        "type": "schedule.set",
        "schedule": sid,
        "rule": rule,
        "starts": iso(now),
        "ends": iso(ends),
        "brief": brief,
        "run_hours": args.get("run_hours", 8),
    }
    action = schedule.describe(rule, view.key, event["ends"])
    done = h.run(
        event,
        view.uid,
        action,
        review=[*h.header(view, action), schedule.RISK, "No click or confirmation is required at firing time."],
    )
    return h.ticket_result(view, done, {"id": sid}, f"orch schedule show {sid}")


OP = operation(
    "schedule.add",
    "Lifecycle",
    "Schedule automatic agent sessions on this ticket for up to 30 days.",
    who="human",
    props={
        "ref": REF(),
        "at": S("once: YYYY-MM-DD HH:MM; recurring: HH:MM"),
        "every": S("day, week or month", enum=["day", "week", "month"]),
        "tz": S("IANA timezone; default UTC"),
        "weekdays": S("comma-separated weekdays 0..6, Monday=0"),
        "day": {
            "type": "integer",
            "minimum": 1,
            "maximum": 31,
            "description": "monthly day; clamps to the last day of shorter months",
        },
        "brief": S("the brief as text", maxLength=4096),
        "brief_file": FILE,
        "until": S("end instant; defaults to 30 days"),
        "run_hours": {"type": "integer", "minimum": 1, "maximum": 8, "description": "maximum hours per run; default 8"},
    },
    required=("ref", "at"),
    positional=("ref",),
    emits=("schedule.set",),
    text="ok {key} schedule.set {id} seq={seq}\nnext: {next}",
    data=obj({"id": STR}),
    errors=tuple(err(c) for c in ("schedule.denied", "not_found", "parse.text")),
    handler=handle,
    addon="schedules",
)
