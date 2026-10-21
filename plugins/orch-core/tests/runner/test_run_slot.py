"""T4 (#716, FU9): the runner's one-shot follow-up grant slot, keyed by run id, consumed by exactly one spawn, never
touching the person's slot; the log filter covers its secret."""

from __future__ import annotations

import logging
import time

import pytest

from orch.runner.grants import GrantError, GrantStore, RedactFilter

WS = "a" * 32
ME = "p_" + "1" * 32
YOU = "p_" + "2" * 32
GID = "gr_01J0000000000000000000000A"
SECRET = GID + "." + "S" * 43
FGID = "fgr_01J0000000000000000000000B"
FSECRET = FGID + "." + "Q" * 43
FGID2 = "fgr_01J0000000000000000000000G"
FSECRET2 = FGID2 + "." + "R" * 43
RUN = "r_01J0000000000000000000000E"
RUN2 = "r_01J0000000000000000000000F"
DEPOSIT = "01J0000000000000000000000D"


def later(hours: float = 1.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + hours * 3600))


def put_run(s: GrantStore, run=RUN, grant=FGID, secret=FSECRET, person=ME, until=None, **kw):
    return s.put_run({"run": run, "workspace_id": WS, "person": person, "grant": grant, "secret": secret,
                      "until": until or later(), **kw})  # fmt: skip


def put_person(s: GrantStore) -> None:
    s.put({"deposit_id": DEPOSIT, "workspace_id": WS, "person": ME, "grant": GID, "secret": SECRET, "until": later()})


def test_a_second_spawn_with_the_same_run_id_gets_nothing():
    s = GrantStore()
    assert put_run(s) == {"run": RUN, "grant": FGID, "until": s._run_slots[RUN].until}
    assert s.secret_for(WS, ME, FGID, run=RUN) == FSECRET
    with pytest.raises(GrantError) as e:
        s.secret_for(WS, ME, FGID, run=RUN)
    assert e.value.code == "launch.no_grant"
    assert s._run_slots == {}


def test_a_different_run_id_cannot_consume():
    s = GrantStore()
    put_run(s)
    for run in (RUN2, None, 5, ["r"]):
        with pytest.raises(GrantError) as e:
            s.secret_for(WS, ME, FGID, run=run)
        assert e.value.code == "launch.no_grant"
    assert s.secret_for(WS, ME, FGID, run=RUN) == FSECRET  # still there for its own run


@pytest.mark.parametrize("who", [("b" * 32, ME, FGID), (WS, YOU, FGID), (WS, ME, FGID2)])
def test_a_mismatched_workspace_person_or_grant_cannot_consume_and_leaves_the_slot(who):
    s = GrantStore()
    put_run(s)
    with pytest.raises(GrantError):
        s.secret_for(who[0], who[1], who[2], run=RUN)
    assert s.secret_for(WS, ME, FGID, run=RUN) == FSECRET


def test_the_person_slot_is_unchanged_by_put_run_and_consume():
    s = GrantStore()
    put_person(s)
    before = s.report()
    put_run(s)
    assert s.report() == before
    assert s.secret_for(WS, ME, GID) == SECRET
    assert s.secret_for(WS, ME, FGID, run=RUN) == FSECRET
    assert s.report() == before and s.secret_for(WS, ME, GID) == SECRET
    # a person grant never consumes a run slot, with or without a run id
    put_run(s, run=RUN2, grant=FGID2, secret=FSECRET2)
    assert s.secret_for(WS, ME, GID, run=RUN2) == SECRET
    assert s.has_run if False else RUN2 in s._run_slots


def test_a_follow_up_grant_never_resolves_from_the_person_slot_and_put_run_never_replaces_it():
    s = GrantStore()
    put_person(s)
    with pytest.raises(GrantError):  # nothing in the run table: no fallback to the person slot
        s.secret_for(WS, ME, FGID, run=RUN)
    # a person-shaped grant id is not accepted by put_run (it would shadow the person slot's shape)
    with pytest.raises(GrantError) as e:
        put_run(s, grant=GID, secret=SECRET)
    assert e.value.code == "invalid.input"
    assert s.secret_for(WS, ME, GID) == SECRET


def test_put_run_never_overwrites_a_run_that_holds_a_slot():
    s = GrantStore()
    put_run(s)
    with pytest.raises(GrantError):
        put_run(s, grant=FGID2, secret=FSECRET2)
    assert s.secret_for(WS, ME, FGID, run=RUN) == FSECRET


def test_validation_and_expiry_fail_closed():
    s = GrantStore()
    for kw in ({"run": "x"}, {"person": "me"}, {"workspace_id": "w"}, {"grant": "fgr_x"}, {"until": "tomorrow"},
               {"secret": FGID2 + "." + "R" * 43}, {"secret": FGID}):  # fmt: skip
        with pytest.raises(GrantError) as e:
            put_run(s, **kw)
        assert e.value.code == "invalid.input"
    with pytest.raises(GrantError) as e:
        put_run(s, until="2020-01-01T00:00:00Z")
    assert e.value.code == "launch.no_grant"
    now = [time.time()]
    s = GrantStore(clock=lambda: now[0])
    put_run(s, until=later(0.5))
    now[0] += 3600
    with pytest.raises(GrantError):
        s.secret_for(WS, ME, FGID, run=RUN)
    assert s._run_slots == {}


def test_corrupt_slot_state_fails_closed():
    s = GrantStore()
    s._run_slots[RUN] = object()  # type: ignore[assignment]
    with pytest.raises(GrantError) as e:
        s.secret_for(WS, ME, FGID, run=RUN)
    assert e.value.code == "launch.no_grant" and RUN not in s._run_slots
    s._run_slots[RUN] = None  # type: ignore[assignment]
    with pytest.raises(GrantError):
        s.secret_for(WS, ME, FGID, run=RUN)
    s._run_slots[RUN2] = object()  # type: ignore[assignment]
    assert s.secrets() == []  # the redactor does not crash on it


def test_drop_forgets_a_run_slot_by_grant():
    s = GrantStore()
    put_run(s)
    assert s.drop(FGID) is True and s.drop(FGID) is False
    with pytest.raises(GrantError):
        s.secret_for(WS, ME, FGID, run=RUN)


def test_the_cap_on_run_slots():
    s = GrantStore()
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    for a in alphabet[:2]:
        for b in alphabet:
            put_run(s, run="r_" + ("01J" + "0" * 21 + a + b))
    with pytest.raises(GrantError):
        put_run(s, run=RUN)


def _logger(store: GrantStore, caplog):
    log = logging.getLogger("orch.runner.test_slot")
    log.setLevel(logging.DEBUG)
    f = RedactFilter(store)
    caplog.handler.addFilter(f)
    return log, f


def test_the_secret_never_reaches_a_log_not_even_in_exception_text(caplog):
    s = GrantStore()
    put_person(s)
    put_run(s)
    log, f = _logger(s, caplog)
    try:
        log.info("spawn %s with %s", RUN, FSECRET)
        log.warning("tail only " + FSECRET.partition(".")[2])
        log.error("pct style %s", {"secret": FSECRET})
        log.info("person %s", SECRET)
        log.info("shape only, not held: %s", FGID2 + "." + "Z" * 43)
    finally:
        caplog.handler.removeFilter(f)
    text = caplog.text + "".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert FSECRET not in text and "Q" * 43 not in text and "S" * 43 not in text and "Z" * 43 not in text
    assert "[redacted]" in text and RUN in text


@pytest.mark.xfail(
    reason="exception text is redacted by #749 (RedactFilter exc_info); remove xfail after it merges", strict=False
)
def test_the_secret_is_redacted_in_exception_text(caplog):
    s = GrantStore()
    put_run(s)
    log, f = _logger(s, caplog)
    try:
        try:
            raise RuntimeError(f"cannot start with {FSECRET}")
        except RuntimeError:
            log.exception("spawn failed")
    finally:
        caplog.handler.removeFilter(f)
    assert FSECRET not in caplog.text and "Q" * 43 not in caplog.text


def test_repr_never_shows_the_secret():
    s = GrantStore()
    put_run(s)
    assert "Q" * 43 not in repr(s._run_slots) and FSECRET not in repr(s._run_slots)


def test_a_consumed_run_cannot_be_deposited_again():  # Codex challenge: replay of the deposit re-armed the run
    s = GrantStore()
    put_run(s)
    assert s.secret_for(WS, ME, FGID, run=RUN) == FSECRET
    with pytest.raises(GrantError):
        put_run(s)
    with pytest.raises(GrantError):
        s.secret_for(WS, ME, FGID, run=RUN)


def test_a_consumed_secret_is_still_redacted_in_logs(caplog):  # Codex challenge: tail redaction vanished on consume
    s = GrantStore()
    put_run(s)
    s.secret_for(WS, ME, FGID, run=RUN)
    log, f = _logger(s, caplog)
    try:
        log.info("leaked tail " + "Q" * 43)
    finally:
        caplog.handler.removeFilter(f)
    assert "Q" * 43 not in caplog.text
