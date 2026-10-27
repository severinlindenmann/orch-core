"""Manual schedules through the live host: real store, real ScheduleRunner, the fake runner of the follow-up tests.

Mirrors ``test_followups_real_host`` without the subprocess bring-up: the workspace here is not a dev sandbox (its
prefix is DEMO), so the code minimum lead and the 5 min early tolerance hold, and a fake clock walks to the occurrence
instead of waiting for it. Scenarios: a person-signed once-schedule fires with a FRESH per-run grant minted by the
host at firing time (its hash in the ledger, its secret only in grant.put_run); a host restart never fires the
occurrence again; a cancelled schedule never fires.
"""

import json
from datetime import UTC, datetime

from orch import canon, crypto
from orch.host.schedules import ScheduleRunner
from orch.identity import new_ulid
from tests.host.test_followups_runner import env, w  # noqa: F401
from tests.store.helpers import ts


def _set_once(w, uid, due):  # noqa: F811 - imported fixture
    local = datetime.fromtimestamp(due, UTC)
    sid = "sc_" + new_ulid()
    w.person_event(
        "schedule.set",
        uid,
        schedule=sid,
        brief="BRIEF-" + sid + " stays in the ledger",
        rule={"every": "once", "at": local.strftime("%H:%M"), "tz": "UTC", "date": local.strftime("%Y-%m-%d")},
        starts=ts(w.lab.ws.clock[0]),
        ends=ts(due + 3600),
        run_hours=2,
    )
    return sid


def _schedule(w, uid, sid):  # noqa: F811 - imported fixture
    view = w.h.run(w.service.call(lambda: w.service.store.ticket(uid)))
    return next(r for r in view.schedules if r["id"] == sid)


def _events(w, uid):  # noqa: F811 - imported fixture
    return w.h.run(w.service.call(lambda: [e for e, _ in w.service.store.events_after(uid, 0, 1 << 30)]))


def _grant_ids(w):  # noqa: F811 - imported fixture
    return w.h.run(w.service.call(lambda: set(w.service.store.state._core.ws.grants)))


def _tick(env, at):  # noqa: F811 - imported fixture
    """One tick of a NEW ScheduleRunner (a restarted host keeps nothing but the ledger) at wall time ``at``."""
    env.w.lab.ws.clock[0] = at
    runner = ScheduleRunner(env.w.service, clock=lambda: env.w.lab.ws.clock[0], mono=lambda: env.mono[0])
    env.w.h.run(runner.tick_once())
    return runner


def test_once_fires_with_a_fresh_grant_and_never_twice_across_restarts(env, w):  # noqa: F811 - imported fixtures
    assert env.w is w
    uid, _ = w.ticket("Scheduled once")
    due = int(w.lab.ws.clock[0] // 60 * 60 + 7200)
    sid = _set_once(w, uid, due)
    before = _grant_ids(w)
    assert _schedule(w, uid, sid)["state"] == "live"

    runner = ScheduleRunner(w.service, clock=lambda: w.lab.ws.clock[0], mono=lambda: env.mono[0])
    w.lab.ws.clock[0] = due - 600  # before the early tolerance: nothing
    w.h.run(runner.tick_once())
    assert not env.hub.commands and not _schedule(w, uid, sid)["occurrences"]

    w.lab.ws.clock[0] = due - 120  # about 2 minutes ahead: inside the 5 min early tolerance, the host fires
    w.h.run(runner.tick_once())
    assert [name for name, _ in env.hub.commands] == ["grant.put_run", "spawn"]
    put, spawn = (payload for _, payload in env.hub.commands)

    # a fresh grant, minted by the host at firing time: no earlier grant (the setter's, the schedule's root) is reused
    occurrences = _schedule(w, uid, sid)["occurrences"]
    assert list(occurrences) == [ts(due)]
    row = occurrences[ts(due)]
    assert row["grant"] == put["grant"] and row["grant"] not in before and row["run"].startswith("r_")
    fired = [e for e in _events(w, uid) if e["type"] == "schedule.fired"]
    assert len(fired) == 1 and fired[0]["actor"]["kind"] == "host" and fired[0]["grant"] == put["grant"]
    secret = crypto.unb64u(put["secret"].split(".")[1])
    assert fired[0]["secret_hash"] == canon.grant_secret_hash(secret)
    assert w.service.store.grant_secret_hash(put["grant"]) == canon.grant_secret_hash(secret)
    # the secret goes only to the runner; the brief stays in the ledger (the session reads it with schedule show)
    assert put["secret"] not in json.dumps(_events(w, uid)) + json.dumps(spawn) + json.dumps(env.hub.store.records)
    assert sid in spawn["argv"][-1] and "BRIEF-" not in spawn["argv"][-1]

    # restarted hosts, at the occurrence and inside its late window: the occurrence key in the ledger holds
    for at in (due, due + 60, due + 1800):
        _tick(env, at)
    assert len(env.hub.commands) == 2
    assert len([e for e in _events(w, uid) if e["type"] == "schedule.fired"]) == 1
    assert list(_schedule(w, uid, sid)["occurrences"]) == [ts(due)]

    # a second schedule fires with yet another grant: one fresh grant per firing
    uid2, _ = w.ticket("Scheduled once more")
    due2 = int(w.lab.ws.clock[0] // 60 * 60 + 7200)
    sid2 = _set_once(w, uid2, due2)
    _tick(env, due2)
    puts = [payload["grant"] for name, payload in env.hub.commands if name == "grant.put_run"]
    assert len(puts) == 2 and puts[1] != puts[0] and puts[1] not in before
    assert _schedule(w, uid2, sid2)["occurrences"][ts(due2)]["grant"] == puts[1]


def test_a_cancelled_schedule_never_fires(env, w):  # noqa: F811 - imported fixtures
    uid, _ = w.ticket("Scheduled then cancelled")
    due = int(w.lab.ws.clock[0] // 60 * 60 + 7200)
    sid = _set_once(w, uid, due)
    w.person_event("schedule.cancelled", uid, schedule=sid)
    assert _schedule(w, uid, sid)["state"] == "cancelled"
    for at in (due - 120, due, due + 60):
        _tick(env, at)
    assert not env.hub.commands
    assert not _schedule(w, uid, sid)["occurrences"]
    assert not [e for e in _events(w, uid) if e["type"] in ("schedule.fired", "schedule.outcome")]
