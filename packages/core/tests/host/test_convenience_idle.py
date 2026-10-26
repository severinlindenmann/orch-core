"""#1198 C17: One click switches itself off after N idle hours (flag semantics: the key stays, signing is refused with
a plain reason, the person turns it on again). Unit tests on the flag file, then the host and the click signer."""

from __future__ import annotations

import json
import os

import pytest

from orch.host import convenience
from orch.host.errors import HostError
from tests.host import human_helpers
from tests.host.lifecycle_helpers import UID
from tests.host.test_click import Click, _click_sign, refused

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human
WS = "0123456789abcdef0123456789abcdef"
H = 3600


class Clock:
    t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(convenience, "_now", c)
    return c


@pytest.fixture
def state(tmp_path):
    d = tmp_path / "state"
    d.mkdir(mode=0o700)
    return d


def test_idle_default_is_twelve_hours_and_activity_resets(state, clock):
    convenience.set_enabled(state, WS, True, UID)
    assert convenience.idle_hours(state, WS, UID) == 12
    clock.t += 11 * H
    assert convenience.enabled(state, WS, UID) and convenience.idle_off(state, WS, UID) is None
    convenience.touch(state, WS, UID)  # a click signature at 11 h
    clock.t += 11 * H
    assert convenience.enabled(state, WS, UID)  # 22 h after switch-on, 11 h after the signature
    clock.t += 2 * H
    assert not convenience.enabled(state, WS, UID)
    assert convenience.idle_off(state, WS, UID) == 12


def test_an_idle_flag_is_not_revived_by_activity_but_by_switching_on_again(state, clock):
    convenience.set_enabled(state, WS, True, UID)
    clock.t += 13 * H
    convenience.touch(state, WS, UID)  # a late touch must not revive it
    assert not convenience.enabled(state, WS, UID)
    convenience.set_enabled(state, WS, True, UID)
    assert convenience.enabled(state, WS, UID) and convenience.idle_off(state, WS, UID) is None


def test_the_idle_setting_takes_effect_at_once_and_is_range_checked(state, clock):
    convenience.set_enabled(state, WS, True, UID)
    convenience.set_idle_hours(state, WS, 2, UID)
    clock.t += 3 * H
    assert convenience.idle_off(state, WS, UID) == 2
    convenience.set_enabled(state, WS, True, UID)
    convenience.set_idle_hours(state, WS, 24, UID)  # longer: a switched-on flag is still on at 13 h
    clock.t += 13 * H
    assert convenience.enabled(state, WS, UID)
    for bad in (0, 169, -1, True, "5", 1.5):
        with pytest.raises(HostError) as info:
            convenience.set_idle_hours(state, WS, bad, UID)
        assert info.value.code == "invalid.input"
    assert convenience.idle_hours(state, WS, UID) == 24  # the setting survives off and on
    convenience.set_enabled(state, WS, False, UID)
    assert convenience.idle_hours(state, WS, UID) == 24


def test_nothing_changes_while_one_click_is_off(state, clock):
    assert convenience.idle_off(state, WS, UID) is None and not convenience.enabled(state, WS, UID)
    clock.t += 100 * H
    assert convenience.idle_off(state, WS, UID) is None
    convenience.touch(state, WS, UID)  # no flag, no write
    assert not (state / "host" / "convenience.json").exists()


def test_a_flag_without_last_used_or_from_the_future_counts_as_idle(state, clock):
    convenience.set_enabled(state, WS, True, UID)
    path = state / "host" / "convenience.json"
    doc = json.loads(path.read_text())
    doc["workspaces"][WS]["last_used"] = int(clock.t) + 2 * H  # clock set back or a hand edit
    path.write_text(json.dumps(doc))
    os.chmod(path, 0o600)
    assert not convenience.enabled(state, WS, UID)
    del doc["workspaces"][WS]["last_used"]
    path.write_text(json.dumps(doc))
    assert convenience.idle_off(state, WS, UID) == 12


def test_the_signer_refuses_with_the_plain_reason_and_the_status_says_why(served, human, clock):
    c = Click(served)
    try:
        _, key = human.ticket()
        clock.t = convenience._now() + 0  # the host-clock the flag was switched on with
        _click_sign(c, key)  # a signature while on and in use
        clock.t += 13 * H
        e = refused(lambda: c.prepare("log", {"ref": key, "text": "late"}), "sign.convenience_unavailable", "idle")
        assert "One click switched itself off after 12 hours without use. Turn it on again to continue." in str(e)
        entry = c.signer._workspaces()[0]
        row = c.signer._row(entry, lambda state, wid: {"custody": "convenience", "device": "d_" + "0" * 32})
        assert row["enabled"] is False and row["problem"] == "idle" and row["idle_hours"] == 12
        assert row["available"] is False
        convenience.set_idle_hours(served.lab.state, served.wid, 30, UID)  # the configured limit, off or on
        assert (
            c.signer._row(entry, lambda state, wid: {"custody": "convenience", "device": "d_" + "0" * 32})["idle_hours"]
            == 30
        )
        convenience.set_enabled(served.lab.state, served.wid, False, UID)
        row = c.signer._row(entry, lambda state, wid: {"custody": "convenience", "device": "d_" + "0" * 32})
        assert row["idle_hours"] == 30 and row["problem"] == "disabled"  # a manual off is not "idle"
    finally:
        c.close()


def test_a_click_signature_resets_the_idle_clock_and_turning_on_again_continues(served, human, clock):
    c = Click(served)
    try:
        _, key = human.ticket()
        clock.t = convenience._now()
        clock.t += 11 * H
        _click_sign(c, key, "one")
        clock.t += 11 * H  # 22 h since switch-on, 11 h since the signature
        _click_sign(c, key, "two")
        clock.t += 13 * H
        refused(lambda: c.prepare("log", {"ref": key, "text": "three"}), "sign.convenience_unavailable", "idle")
        convenience.set_enabled(served.lab.state, served.wid, True, UID)
        _click_sign(c, key, "four")
    finally:
        c.close()
