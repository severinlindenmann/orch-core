"""Manual calendar schedules: pure rules, supplied clock, UTC epoch seconds."""

from __future__ import annotations

import calendar
import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from orch.canon.text import TextError, check_plain, is_clean_text

from .followups import FU_BRIEF_MAX_BYTES, FU_RUN_HOURS
from .followups import REASONS as FU_REASONS

MAX_DAYS = 30
SET_SKEW_S = 300  # a signed `starts` is at most this far after the event `at` (a crafted far-future start is refused)
SET_PAST_S = 3600  # and at most this far before it (a slow signing flow)
LABEL_MAX = 200
REASONS = (
    *FU_REASONS,
    "role_lost",
    "device_lost",
    "access_lost",
    "ticket_closed",
    "claimed_by_other",
    "busy",
    "missed",
    "addon_disabled",
    "skipped_open_child",
)


def validate_rule(rule):
    if not isinstance(rule, dict) or set(rule) - {"every", "at", "tz", "weekdays", "day", "date"}:
        raise ValueError("invalid rule fields")
    every = rule.get("every")
    if every not in ("once", "day", "week", "month"):
        raise ValueError("unknown frequency")
    if not isinstance(rule.get("at"), str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", rule["at"]):
        raise ValueError("at must be HH:MM")
    try:
        zone = ZoneInfo(rule["tz"])
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("unknown IANA timezone") from exc
    for field, frequency in (("weekdays", "week"), ("day", "month"), ("date", "once")):
        if (field in rule) != (every == frequency):
            raise ValueError(f"{field} is required only for {frequency}")
    if every == "week":
        days = rule["weekdays"]
        if (
            not isinstance(days, list)
            or not days
            or any(type(d) is not int or not 0 <= d <= 6 for d in days)
            or len(set(days)) != len(days)
        ):
            raise ValueError("weekdays must be unique integers 0..6")
    if every == "month" and (type(rule["day"]) is not int or not 1 <= rule["day"] <= 31):
        raise ValueError("day must be 1..31")
    if every == "once":
        if not isinstance(rule["date"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", rule["date"]):
            raise ValueError("date must be YYYY-MM-DD")
        date.fromisoformat(rule["date"])
    return zone


def occurrences(rule, starts: int, ends: int, after: int) -> int | None:
    """First occurrence strictly after `after`, inside the inclusive signed interval.

    Resolve fold=0 (first overlap). Gaps advance minute by minute until a roundtrip
    through UTC preserves the wall time, including zones that skip an entire day.
    """
    zone = validate_rule(rule)
    if ends < starts or after >= ends:
        return None
    day = datetime.fromtimestamp(max(starts, after), zone).date() - timedelta(days=1)
    last = datetime.fromtimestamp(ends, zone).date()
    local_time = time.fromisoformat(rule["at"])
    while day <= last:
        every = rule["every"]
        matches = (
            every == "day"
            or (every == "once" and day.isoformat() == rule["date"])
            or (every == "week" and day.weekday() in rule["weekdays"])
            or (every == "month" and day.day == min(rule["day"], calendar.monthrange(day.year, day.month)[1]))
        )
        if matches:
            local = datetime.combine(day, local_time)
            while True:
                utc = local.replace(tzinfo=zone, fold=0).astimezone(UTC)
                if utc.astimezone(zone).replace(tzinfo=None) == local:
                    break
                local += timedelta(minutes=1)
            instant = int(utc.timestamp())
            if starts <= instant <= ends and instant > after:
                return instant
        day += timedelta(days=1)
    return None


def validate_set(rule, starts, ends, now, brief, run_hours, label=None):
    try:
        validate_rule(rule)
    except ValueError as exc:
        return str(exc)
    if any(type(v) is not int for v in (starts, ends, now)) or not 0 < ends - starts <= MAX_DAYS * 86400 or ends <= now:
        return "schedule interval must be positive, future, and at most 30 days"
    # `starts` is the signing time and the interval is bounded from the event itself, so a crafted
    # far-future `starts` cannot stretch the authority past MAX_DAYS from when it was signed.
    if not (-SET_PAST_S <= starts - now <= SET_SKEW_S) or ends > now + MAX_DAYS * 86400:
        return "starts must be the signing time and ends at most 30 days after it"
    if type(run_hours) is not int or not 1 <= run_hours <= min(8, FU_RUN_HOURS):
        return "run_hours must be 1..8"
    if (
        not isinstance(brief, str)
        or not brief.strip()
        or not is_clean_text(brief)
        or len(brief.encode("utf-8")) > FU_BRIEF_MAX_BYTES
    ):
        return "brief must be clean text of at most 4096 bytes"
    if label is not None and (
        not isinstance(label, str)
        or not label.strip()
        or len(label) > LABEL_MAX
        or not is_clean_text(label, one_line=True)
    ):
        return "label must be one line of clean text of at most 200 characters"
    return None


def validate_target(event):
    """Absent target is the original shape; titles are literal clean single-line text."""
    target = event.get("target", "existing")
    if target not in ("existing", "new"):
        return "target must be existing or new"
    if target == "new":
        title = event.get("title")
        try:
            check_plain(title, one_line=True, limit=100, what="title")
        except TextError:
            return "title must be 1..100 characters of clean text on one line"
    elif "title" in event:
        return "title is only allowed for new-ticket schedules"
    return None


OUTCOMES = ("ran", "ran_nothing", "failed", "did_not_run")


def child_uid(schedule_id, occurrence):
    """Stable 128-bit identity, independent of host clocks, retries and ticket keys."""
    import hashlib

    value = int.from_bytes(hashlib.sha256((schedule_id + "\0" + occurrence).encode()).digest()[:16], "big")
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "".join(alphabet[(value >> (5 * i)) & 31] for i in reversed(range(26)))


def child_title(rec, occurrence):
    local = datetime.fromisoformat(occurrence.replace("Z", "+00:00")).astimezone(ZoneInfo(rec.rule["tz"]))
    return f"{rec.title} {local:%Y-%m-%d}"
