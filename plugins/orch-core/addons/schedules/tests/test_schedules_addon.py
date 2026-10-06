import json
from pathlib import Path

from orch_schedules import STATUS, Schedules, read_status, sub_line


class View:
    def __init__(self, state_dir):
        self.state_dir = state_dir


def test_no_status_yet_shows_an_unknown_tile(tmp_path):
    [tile] = Schedules(None).widgets("today.summary", View(tmp_path))
    assert tile.value is None and tile.href == "/schedules"


def test_the_tile_counts_armed_schedules_and_says_what_waits(tmp_path):
    (tmp_path / STATUS).write_text(json.dumps({"armed": 3, "findings": 2, "needs_fix": 0,
                                               "next": "2026-10-06T14:00:00+02:00"}))
    [tile] = Schedules(None).widgets("today.summary", View(tmp_path))
    assert tile.value == 3 and tile.role == "info"
    assert tile.sub == "2 findings for you · next 14:00"


def test_a_schedule_that_needs_a_look_turns_the_tile_amber(tmp_path):
    (tmp_path / STATUS).write_text(json.dumps({"armed": 1, "findings": 0, "needs_fix": 1, "next": None}))
    [tile] = Schedules(None).widgets("today.summary", View(tmp_path))
    assert tile.role == "warn" and tile.sub == "1 needs a look"


def test_a_broken_status_file_is_no_status(tmp_path):
    (tmp_path / STATUS).write_text("{nope")
    assert read_status(tmp_path) is None
    assert sub_line({"armed": 0}) == "armed"


def test_other_slots_get_nothing(tmp_path):
    assert Schedules(None).widgets("page.schedules", View(Path(tmp_path))) == []
