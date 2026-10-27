"""Calendar rules use local civil time and return UTC epoch seconds."""

from datetime import datetime

import pytest

from orch.model.schedules import occurrences, validate_set


def epoch(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


@pytest.mark.parametrize(
    "rule,start,end,want",
    [
        (
            {"every": "day", "at": "02:30", "tz": "Europe/Zurich"},
            "2026-03-29T00:00Z",
            "2026-03-30T00:00Z",
            "2026-03-29T01:00Z",
        ),
        (
            {"every": "day", "at": "02:30", "tz": "Europe/Zurich"},
            "2026-10-25T00:00Z",
            "2026-10-26T00:00Z",
            "2026-10-25T00:30Z",
        ),
        (
            {"every": "month", "at": "09:00", "tz": "UTC", "day": 31},
            "2026-02-01T00:00Z",
            "2026-03-01T00:00Z",
            "2026-02-28T09:00Z",
        ),
        (
            {"every": "week", "at": "09:00", "tz": "UTC", "weekdays": [0, 4]},
            "2026-11-03T00:00Z",
            "2026-11-10T00:00Z",
            "2026-11-06T09:00Z",
        ),
        (
            {"every": "once", "at": "09:00", "tz": "UTC", "date": "2026-11-02"},
            "2026-11-01T00:00Z",
            "2026-11-03T00:00Z",
            "2026-11-02T09:00Z",
        ),
    ],
)
def test_occurrences(rule, start, end, want):
    assert occurrences(rule, epoch(start), epoch(end), epoch(start) - 1) == epoch(want)
    if rule["every"] == "once" or "10-25" in start:
        assert occurrences(rule, epoch(start), epoch(end), epoch(want)) is None


def test_length_and_brief_caps():
    rule = {"every": "day", "at": "09:00", "tz": "UTC"}
    assert validate_set(rule, 3600, 3600 + 30 * 86400, 3600, "brief", 8) is None
    for ends, brief in [(3601 + 30 * 86400, "brief"), (3600, "brief"), (7200, "é" * 2049)]:
        assert validate_set(rule, 3600, ends, 3600, brief, 8)


def test_starts_is_the_signing_time_and_bounds_ends():
    rule = {"every": "day", "at": "09:00", "tz": "UTC"}
    now = 10 * 86400
    assert validate_set(rule, now - 300, now + 86400, now, "brief", 8) is None
    assert validate_set(rule, now + 300, now + 86400, now, "brief", 8) is None
    # a crafted far-future start would otherwise carry authority up to a year ahead
    year = 365 * 86400
    assert validate_set(rule, now + year, now + year + 86400, now, "brief", 8)
    assert validate_set(rule, now + 301, now + 86400, now, "brief", 8)
    assert validate_set(rule, now - 3600, now + 86400, now, "brief", 8) is None  # a slow signing flow
    assert validate_set(rule, now - 3601, now + 86400, now, "brief", 8)
    assert validate_set(rule, now + 300, now + 300 + 30 * 86400, now, "brief", 8)


@pytest.mark.parametrize("label", ["", "  ", "a\nb", "x\u202ey", "bell\x07", "l" * 201, 7])
def test_label_must_be_clean_one_line_text(label):
    rule = {"every": "day", "at": "09:00", "tz": "UTC"}
    assert validate_set(rule, 3600, 7200, 3600, "brief", 1, label)


def test_clean_label_accepted():
    rule = {"every": "day", "at": "09:00", "tz": "UTC"}
    assert validate_set(rule, 3600, 7200, 3600, "brief", 1, "Weekly report") is None
    assert validate_set(rule, 3600, 7200, 3600, "brief", 1, "l" * 200) is None


@pytest.mark.parametrize(
    "patch",
    [
        {"tz": "bad/zone"},
        {"at": "24:00"},
        {"weekdays": [0]},
        {"every": "week", "weekdays": []},
        {"every": "week", "weekdays": [0, 0]},
        {"every": "month", "day": 32},
    ],
)
def test_invalid_rules(patch):
    assert validate_set({"every": "day", "at": "09:00", "tz": "UTC", **patch}, 3600, 86400, 0, "brief", 1)


def test_schedule_root_expiry_is_checked_by_chain_and_usable():
    from orch.model.followups import GrantTerms, chain_valid, fgrant_usable

    root = GrantTerms("root", "p", "ticket", "agent", 0, 100, None, root_kind="schedule")
    child = GrantTerms("child", "p", "ticket", "agent", 1, 200, "root")
    chain = (child, root)
    assert chain_valid(chain, root_person_active=True, at=99)
    assert not chain_valid(chain, root_person_active=True, at=100)
    assert not fgrant_usable(child, at=101, outcome_recorded=False, chain=chain, root_person_active=True)
