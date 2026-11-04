"""H11c: the runner grant on the host (D-P2-27, §12.4.2): grant.deposit, grant.deposit_status, grant.forget, the
hello revalidation, the watcher, ``whoami.runner.grant``, and the canary: the secret goes only to the runner.

The runner runs in this process (as in test_runner_socket); its grant store is inspected directly.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from orch import canon, crypto
from orch.identity import new_ulid
from tests.host.dispatch_helpers import err, ok
from tests.host.lifecycle_helpers import UID
from tests.host.live_helpers import world
from tests.host.test_runner_socket import GEN2, Runner, wait_for
from tests.store.helpers import GRANT_SECRET, ts

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")
SECRET_TAIL = crypto.b64u(GRANT_SECRET)


@pytest.fixture
def w(tmp_path):
    w = world(tmp_path, runner_identity=lambda cred: cred.pid == os.getpid())
    yield w
    w.h.stop()
    w.lab.close()


@pytest.fixture
def rstate(tmp_path):
    d = tmp_path / "runner-state"
    d.mkdir(mode=0o700)
    return d


def enable(w, on: bool = True) -> None:
    from orch.runner import enablement

    enablement.set_enabled(Path(w.lab.state), w.wid, on, UID, terminals=True, text="t")


@pytest.fixture
def ready(w, rstate):
    enable(w)
    r = Runner(w, rstate)
    w.runner = r
    try:
        yield w
    finally:
        r.close()


def issue(w, *, label: str | None = "runner", hours: int = 8, verbs="agent", who=None, issued_at=None) -> str:
    """A grant issued in the workspace log; returns its ``auth.grant`` value (the test secret)."""
    gid = "gr_" + new_ulid()
    issued = issued_at if issued_at is not None else w.lab.ws.clock[0] + 1
    payload = {"grant": gid, "scope": "workable", "verbs": verbs, "issued_at": ts(issued), "hours": hours,
               "expires_at": ts(issued + 3600 * hours),
               "secret_hash": canon.grant_secret_hash(GRANT_SECRET)}  # fmt: skip
    if label is not None:
        payload["label"] = label
    w.person_event("grant.issued", who=who, **payload)
    return f"{gid}.{SECRET_TAIL}"


def deposit(c, value: str, deposit_id: str | None = None) -> dict:
    return c.call("grant.deposit", {"deposit_id": deposit_id or new_ulid()}, grant=value)


def gid_of(value: str) -> str:
    return value.partition(".")[0]


# -- deposit ---------------------------------------------------------------------------------------------------------


def test_a_runner_grant_is_deposited_once_and_shows_in_whoami(ready):
    w = ready
    c = w.conn()
    value = issue(w)
    did = new_ulid()
    res = ok(deposit(c, value, did))["data"]
    assert res["deposit"] == "confirmed" and res["grant"] == gid_of(value) and res["scope"] == "workable"
    assert [g["grant"] for g in w.runner.grants.report()] == [gid_of(value)]
    me = ok(c.call("whoami", {}))["data"]["runner"]
    assert me["grant"]["id"] == gid_of(value) and me["grant"]["scope"] == "workable"
    assert ok(c.call("grant.deposit_status", {"deposit_id": did}))["data"]["deposit"] == "confirmed"
    assert ok(c.call("grant.deposit_status", {"deposit_id": new_ulid()}))["data"]["deposit"] == "none"
    again = ok(deposit(c, value, did))["data"]  # the same deposit_id: the recorded outcome, nothing sent again
    assert again["deposit"] == "confirmed"


@pytest.mark.parametrize(
    ("kw", "code"),
    [({"label": None}, "grant.not_runner"), ({"label": "quinn"}, "grant.not_runner"),
     ({"verbs": ["log"]}, "grant.verbs"), ({"hours": 13}, "op.not_allowed")],
    ids=["no-label", "other-label", "verbs", "too-long"],
)  # fmt: skip
def test_only_a_runner_labelled_agent_grant_of_at_most_12_hours_can_be_deposited(ready, kw, code):
    w = ready
    c = w.conn()
    value = issue(w, **kw)
    res = deposit(c, value)
    assert err(res) == code and res["exit"] == 3  # the §0.6 codes (#466 review, Opus L1)
    assert w.runner.grants.report() == []


def test_the_runner_cap_is_the_workspace_setting_and_is_read_at_admission(ready):
    """B8: min(grant_hours, 168), read on the state at deposit time (the model takes an owner's 168 h)."""
    w = ready
    c = w.conn()
    w.person_event("settings.changed", **{"set": {"grant_hours": 168}})
    week = issue(w, hours=168)
    assert ok(deposit(c, week))["data"]["deposit"] == "confirmed"
    day = issue(w, hours=24)
    w.person_event("settings.changed", **{"set": {"grant_hours": 12}})  # lowered after the grant was issued
    res = deposit(c, day)
    assert err(res) == "op.not_allowed" and "12 hours" in str(res)  # existing grants stay valid; the runner refuses
    assert [g["grant"] for g in w.runner.grants.report()] == [gid_of(week)]


def test_a_grant_with_under_15_minutes_left_is_refused(ready):
    w = ready
    c = w.conn()
    value = issue(w, hours=1)
    w.lab.ws.clock[0] += 3000  # ten minutes left
    assert err(deposit(c, value)) == "grant.expiring"


def test_deposit_needs_a_person_and_their_own_verified_grant(ready):
    w = ready
    c = w.conn()
    assert err(c.call("grant.deposit", {"deposit_id": new_ulid()})) == "op.not_allowed"  # no grant: P
    value = issue(w)
    wrong = gid_of(value) + "." + "A" * 43
    assert err(deposit(c, wrong)) == "grant.expired"  # unverified
    theirs = issue(w, who=w.quinn)  # quinn's own runner grant, presented by the owner's UID
    assert err(deposit(c, theirs)) == "op.not_allowed"
    assert err(c.call("grant.deposit", {"deposit_id": "x"}, grant=value)) == "invalid.input"
    assert w.runner.grants.report() == []


def test_the_switch_and_the_runner_are_required(w, rstate):
    c = w.conn()
    value = issue(w)
    assert err(deposit(c, value)) == "runner.disabled"
    enable(w)
    assert err(deposit(c, value)) == "runner.unavailable"


def test_a_newer_runner_grant_replaces_the_old_which_is_forgotten(ready):
    w = ready
    c = w.conn()
    first, second = issue(w), issue(w)
    assert ok(deposit(c, first))["data"]["deposit"] == "confirmed"
    assert ok(deposit(c, second))["data"]["deposit"] == "confirmed"
    assert [g["grant"] for g in w.runner.grants.report()] == [gid_of(second)]
    res = deposit(c, first)
    assert err(res) == "grant.forgotten" and res["exit"] == 3
    assert [g["grant"] for g in w.runner.grants.report()] == [gid_of(second)]


def test_forget_drops_it_and_it_can_never_be_deposited_again(ready):
    w = ready
    c = w.conn()
    value = issue(w)
    ok(deposit(c, value))
    assert err(c.call("grant.forget", {"grant": gid_of(value)}, grant=value)) == "op.not_allowed"  # G never
    assert ok(c.call("grant.forget", {"grant": gid_of(value)}))["data"]["forgotten"] is True
    wait_for(lambda: w.runner.grants.report() == [])
    assert ok(c.call("whoami", {}))["data"]["runner"]["grant"] is None
    assert err(deposit(c, value)) == "grant.forgotten"
    other = issue(w, who=w.quinn)
    assert err(c.call("grant.forget", {"grant": gid_of(other)})) == "not_found"  # another person's grant


def test_a_lost_ack_is_unknown_never_a_false_failure_and_hello_settles_it(ready):
    w = ready
    c = w.conn()
    hub = w.h.host.runners
    hub.command_timeout = 1.0
    w.runner.link.drop_reports.add("grant.put")
    value = issue(w)
    did = new_ulid()
    assert ok(deposit(c, value, did))["data"]["deposit"] == "unknown"
    assert ok(c.call("grant.deposit_status", {"deposit_id": did}))["data"]["deposit"] == "unknown"
    assert [g["grant"] for g in w.runner.grants.report()] == [gid_of(value)]  # the runner did take it
    w.runner.link.drop_reports.clear()
    hub.drop_session(hub.session)  # the next hello reports what the runner holds
    wait_for(lambda: ok(c.call("grant.deposit_status", {"deposit_id": did}))["data"]["deposit"] == "confirmed")
    assert ok(c.call("whoami", {}))["data"]["runner"]["grant"]["id"] == gid_of(value)


def test_deposit_status_names_the_reason_of_a_rejected_deposit(ready, rstate):
    w = ready
    c = w.conn()
    w.h.host.runners.command_timeout = 1.0
    w.runner.link.drop_reports.add("grant.put")
    did = new_ulid()
    assert ok(deposit(c, issue(w), did))["data"]["deposit"] == "unknown"
    w.runner.close()
    w.runner = Runner(w, rstate, gen=GEN2)  # the runner restarted: it does not hold the grant
    wait_for(lambda: ok(c.call("grant.deposit_status", {"deposit_id": did}))["data"]["deposit"] == "rejected")
    assert ok(c.call("grant.deposit_status", {"deposit_id": did}))["data"]["reason"] == "runner_restarted"


def test_a_runner_restart_loses_the_grant_and_whoami_says_so(ready, rstate):
    w = ready
    c = w.conn()
    ok(deposit(c, issue(w)))
    w.runner.close()
    w.runner = Runner(w, rstate, gen=GEN2)  # a new process: an empty store
    wait_for(lambda: ok(c.call("whoami", {}))["data"]["runner"]["grant"] is None)


def test_a_revoked_grant_is_dropped_at_the_runner(ready):
    w = ready
    c = w.conn()
    value = issue(w)
    ok(deposit(c, value))
    w.person_event("grant.revoked", grant=gid_of(value))
    wait_for(lambda: w.runner.grants.report() == [])
    assert ok(c.call("whoami", {}))["data"]["runner"]["grant"] is None


def test_hello_drops_a_grant_the_host_does_not_allow(ready):
    w = ready
    value = issue(w)
    stray = issue(w)
    until = ts(w.lab.ws.clock[0] + 3600)
    # the runner holds a grant the host never confirmed (a same-UID process put it there): dropped at the next hello
    w.runner.grants.put({"deposit_id": new_ulid(), "workspace_id": w.wid, "person": w.lab.owner,
                         "grant": gid_of(stray), "secret": stray, "until": until})  # fmt: skip
    hub = w.h.host.runners
    hub.drop_session(hub.session)
    wait_for(lambda: w.runner.grants.report() == [])
    c = w.conn()
    assert ok(deposit(c, value))["data"]["deposit"] == "confirmed"


def test_the_secret_reaches_no_frame_log_or_file(ready, caplog, tmp_path):
    w = ready
    caplog.set_level(logging.DEBUG)
    c = w.conn()
    value = issue(w)
    res = deposit(c, value)
    ok(c.call("whoami", {}))
    ok(c.call("run.list", {}))
    assert ok(res)["data"]["deposit"] == "confirmed"
    seen = repr(c.frames) + repr(res)
    assert SECRET_TAIL not in seen
    assert SECRET_TAIL not in caplog.text
    for path in Path(tmp_path).rglob("*"):
        if path.is_file() and not path.is_symlink():
            with open(path, "rb") as fh:
                assert SECRET_TAIL.encode() not in fh.read(), path


def test_switching_dashboard_execution_off_drops_the_grant_at_the_runner(ready):
    w = ready
    c = w.conn()
    ok(deposit(c, issue(w)))
    enable(w, False)
    wait_for(lambda: w.runner.grants.report() == [])
    assert ok(c.call("whoami", {}))["data"]["runner"]["grant"] is None


# -- review round 1 (#466) ---------------------------------------------------------------------------------------------


def _slow_puts(w, seconds: float = 1.5):
    """Delay the runner's grant.put (a deposit stays in flight)."""
    import time as _time

    real = w.runner.grants.put

    def put(args):
        _time.sleep(seconds)
        return real(args)

    w.runner.grants.put = put
    return real


def test_a_forget_during_an_in_flight_deposit_wins(ready):
    """Opus M1 PoC 1: deposit G1 in flight, then forget G1: G1 must not end up current or held."""
    w = ready
    c, d = w.conn(), w.conn()
    value = issue(w)
    _slow_puts(w)
    rid = c.send("grant.deposit", {"deposit_id": new_ulid()}, grant=value)
    import time as _time

    _time.sleep(0.3)
    ok(d.call("grant.forget", {"grant": gid_of(value)}))
    c.answer(rid)
    wait_for(lambda: w.runner.grants.report() == [])
    assert ok(d.call("whoami", {}))["data"]["runner"]["grant"] is None
    assert err(deposit(d, value)) == "grant.forgotten"


def test_an_in_flight_redeposit_never_beats_a_newer_replacement(ready):
    """Opus M1 PoC 2: G1 current; an agent re-deposits G1 (in flight) while the person deposits G2: G2 stays."""
    w = ready
    c, d = w.conn(), w.conn()
    g1, g2 = issue(w), issue(w)
    ok(deposit(c, g1))
    real = _slow_puts(w)
    rid = c.send("grant.deposit", {"deposit_id": new_ulid()}, grant=g1)
    import time as _time

    _time.sleep(0.3)
    w.runner.grants.put = real
    assert ok(deposit(d, g2))["data"]["deposit"] == "confirmed"
    c.answer(rid)
    assert [g["grant"] for g in w.runner.grants.report()] == [gid_of(g2)]
    assert ok(d.call("whoami", {}))["data"]["runner"]["grant"]["id"] == gid_of(g2)
    assert ok(deposit(d, g2))["data"]["deposit"] == "confirmed"  # never marked forgotten


def test_the_watcher_drops_a_current_grant_that_is_forgotten(ready):
    w = ready
    c = w.conn()
    value = issue(w)
    ok(deposit(c, value))
    hub = w.h.host.runners
    table = hub.grants.table(w.wid)
    table.forgotten[gid_of(value)] = "2099-01-01T00:00:00Z"  # a forgotten id that is still current (any cause)
    wait_for(lambda: w.runner.grants.report() == [])
    assert ok(c.call("whoami", {}))["data"]["runner"]["grant"] is None


def test_a_pending_deposit_of_the_current_grant_settles_at_hello(ready):
    """Codex P2 #3: the grant is current; a second deposit of it loses its ack; hello confirms it."""
    w = ready
    c = w.conn()
    value = issue(w)
    ok(deposit(c, value))
    hub = w.h.host.runners
    hub.command_timeout = 1.0
    w.runner.link.drop_reports.add("grant.put")
    did = new_ulid()
    assert ok(deposit(c, value, did))["data"]["deposit"] == "unknown"
    w.runner.link.drop_reports.clear()
    hub.drop_session(hub.session)
    wait_for(lambda: ok(c.call("grant.deposit_status", {"deposit_id": did}))["data"]["deposit"] == "confirmed")


def test_hello_after_a_host_restart_clears_persisted_grants_the_runner_does_not_hold(ready, rstate):
    """Codex P2 #2: both restart; the host's tables are not loaded yet; hello must still settle them."""
    w = ready
    c = w.conn()
    value = issue(w)
    ok(deposit(c, value))
    hub = w.h.host.runners
    hub.command_timeout = 1.0
    w.runner.link.drop_reports.add("grant.put")
    did = new_ulid()
    assert ok(deposit(c, issue(w), did))["data"]["deposit"] == "unknown"  # pending
    w.runner.close()
    hub.grants.tables.clear()  # as a restarted host: nothing loaded
    w.runner = Runner(w, rstate, gen=GEN2)  # a restarted runner holds nothing
    wait_for(lambda: hub.grants.tables.get(w.wid) is not None and not hub.grants.tables[w.wid].current)
    assert ok(c.call("whoami", {}))["data"]["runner"]["grant"] is None
    assert ok(c.call("grant.deposit_status", {"deposit_id": did}))["data"]["deposit"] == "rejected"


def test_a_grant_dropped_by_the_switch_is_forgotten_for_good(ready):
    """Item 7 (owner decision): re-enabling never resurrects an old grant; a new one must be issued."""
    w = ready
    c = w.conn()
    value = issue(w)
    ok(deposit(c, value))
    enable(w, False)
    wait_for(lambda: w.runner.grants.report() == [])
    enable(w, True)
    assert err(deposit(c, value)) == "grant.forgotten"
    assert ok(deposit(c, issue(w)))["data"]["deposit"] == "confirmed"


def test_a_grant_cleared_after_a_runner_restart_is_forgotten(ready, rstate):
    w = ready
    c = w.conn()
    value = issue(w)
    ok(deposit(c, value))
    w.runner.close()
    w.runner = Runner(w, rstate, gen=GEN2)
    wait_for(lambda: ok(c.call("whoami", {}))["data"]["runner"]["grant"] is None)
    assert err(deposit(c, value)) == "grant.forgotten"


def test_deposits_are_rate_limited_per_person(ready):
    """Opus L5: 10 a minute, burst 5."""
    w = ready
    c = w.conn()
    for _ in range(5):
        ok(deposit(c, issue(w)))
    assert err(deposit(c, issue(w))) == "rate.limited"


def test_pending_deposits_are_never_pruned(tmp_path, monkeypatch):
    from orch.host import runnergrants

    monkeypatch.setattr(runnergrants, "KEEP_DEPOSITS", 2)
    (tmp_path / "hosts").mkdir(mode=0o700)
    table = runnergrants.GrantTable(tmp_path, os.getuid(), "a" * 32)
    table.deposits = {f"0{i}": {"outcome": "pending" if i == 0 else "confirmed", "at": f"2026-01-0{i + 1}"}
                      for i in range(5)}  # fmt: skip
    table.save()
    assert "00" in table.deposits and len(table.deposits) == 3  # the pending one plus the two newest


def test_an_agent_class_peer_cannot_deposit_even_its_own_runner_grant(ready):
    """D-P2-27 (amended): the op requires the person class on top of the verified grant."""
    from tests.host.test_dispatch_matrix import as_agent_class

    w = ready
    value = issue(w)
    as_agent_class(w)
    assert err(deposit(w.conn(), value)) == "op.not_allowed"
    assert w.runner.grants.report() == []


# -- review round 2 (#466): the race across a disconnect and a timeout -------------------------------------------------


def test_a_forget_after_the_depositing_client_disconnected_still_wins(ready):
    """Codex delta 2: the depositing client goes away while grant.put is in flight; a forget follows."""
    import time as _time

    w = ready
    c, d = w.conn(), w.conn()
    value = issue(w)
    _slow_puts(w)
    c.send("grant.deposit", {"deposit_id": new_ulid()}, grant=value)
    _time.sleep(0.3)
    c.client.close()  # the depositing client disconnects
    ok(d.call("grant.forget", {"grant": gid_of(value)}))
    _time.sleep(2.0)  # the slow put has long finished at the runner
    assert w.runner.grants.report() == []
    assert ok(d.call("whoami", {}))["data"]["runner"]["grant"] is None
    assert err(deposit(d, value)) == "grant.forgotten"


def test_a_forget_after_a_deposit_timed_out_still_wins(ready):
    """Codex delta 2: grant.put outlives the host's command timeout (unknown); a forget follows at once."""
    import time as _time

    w = ready
    c = w.conn()
    w.h.host.runners.command_timeout = 0.5
    value = issue(w)
    _slow_puts(w)
    assert ok(deposit(c, value))["data"]["deposit"] == "unknown"
    ok(c.call("grant.forget", {"grant": gid_of(value)}))
    _time.sleep(2.0)
    assert w.runner.grants.report() == []
    assert ok(c.call("whoami", {}))["data"]["runner"]["grant"] is None


# -- run.sub: the runner item (H11c-2) ---------------------------------------------------------------------------------


def _runner_items(c, sub, n=1, timeout=10):
    """The next ``n`` items of ``sub`` (each evt carries one)."""
    out = []
    while len(out) < n:
        frame = c.evt(sub, timeout)
        assert "end" not in frame, frame.get("end")
        out += frame["items"]
    return out


def test_run_sub_sends_a_runner_item_when_the_callers_grant_changes(ready):
    w = ready
    c = w.conn()
    sub = ok(c.call("run.sub", {}))["data"]["sub"]
    value = issue(w)
    ok(deposit(c, value))  # confirmed
    (item,) = _runner_items(c, sub)
    assert item["t"] == "runner" and item["id"].endswith(".g1")
    assert set(item["grant"]) == {"id", "until", "scope"} and item["grant"]["id"] == gid_of(value)
    assert value.partition(".")[2] not in str(item)  # never the secret
    ok(c.call("grant.forget", {"grant": gid_of(value)}))
    (gone,) = _runner_items(c, sub)
    assert gone["t"] == "runner" and gone["grant"] is None and gone["id"].endswith(".g2")
    c.quiet(sub)


def test_run_sub_runner_item_follows_a_runner_restart(ready, rstate):
    w = ready
    c = w.conn()
    ok(deposit(c, issue(w)))
    sub = ok(c.call("run.sub", {}))["data"]["sub"]
    w.runner.close()
    w.runner = Runner(w, rstate, gen=GEN2)  # a new process: an empty store
    items = []
    deadline = 0
    while not any(i["t"] == "runner" and i["grant"] is None for i in items):
        items += _runner_items(c, sub)
        deadline += 1
        assert deadline < 10, items


def test_run_sub_sends_nothing_for_another_persons_grant_change(ready):
    w = ready
    c = w.conn()
    sub = ok(c.call("run.sub", {}))["data"]["sub"]
    hub = w.h.host.runners
    gid = "gr_" + new_ulid()

    async def theirs():  # quinn's runner grant is confirmed and then dropped: nothing for the owner's subscription
        table = hub.grants.table(w.wid)
        hub.grants._approve(table, w.quinn.ref, gid, ts(w.lab.ws.clock[0] + 9999), "workable", new_ulid())
        hub.grants._forget_current(table, w.quinn.ref, "test")

    w.h.run(theirs())
    c.quiet(sub)
    ok(deposit(c, issue(w)))  # the owner's own change still arrives
    (item,) = _runner_items(c, sub)
    assert item["t"] == "runner" and item["grant"] is not None


def test_run_sub_starts_from_the_current_grant_without_a_replay(ready):
    w = ready
    c = w.conn()
    ok(deposit(c, issue(w)))
    sub = ok(c.call("run.sub", {}))["data"]["sub"]
    c.quiet(sub)  # the state at registration is read from whoami or the first change, not replayed


def test_a_g_column_subscriber_gets_runner_items_for_its_own_person_only(ready):
    w = ready
    c = w.conn()
    sub = ok(c.call("run.sub", {}, grant=w.lab.ws.grant))["data"]["sub"]  # an agent grant of the owner
    hub = w.h.host.runners
    gid = "gr_" + new_ulid()

    async def theirs():  # quinn's grant: confirmed, then dropped
        table = hub.grants.table(w.wid)
        hub.grants._approve(table, w.quinn.ref, gid, ts(w.lab.ws.clock[0] + 9999), "workable", new_ulid())
        hub.grants._forget_current(table, w.quinn.ref, "test")

    w.h.run(theirs())
    c.quiet(sub)
    p = w.conn()
    value = issue(w)
    ok(deposit(p, value))
    (item,) = _runner_items(c, sub)
    assert item["t"] == "runner" and item["grant"]["id"] == gid_of(value)
    assert value.partition(".")[2] not in str(item)


def test_a_g_subscriber_whose_grant_is_revoked_gets_no_runner_item(ready):
    w = ready
    c, p = w.conn(), w.conn()
    sub = ok(c.call("run.sub", {}, grant=w.lab.ws.grant))["data"]["sub"]
    w.person_event("grant.revoked", grant=w.lab.ws.grant_id)
    ok(deposit(p, issue(w)))
    frame = c.evt(sub)
    assert frame["items"] == [] and frame.get("end")  # revalidated before delivery: ended, not served


def test_whoami_says_whether_the_grant_can_start_agents_now_and_until_when(ready):
    """Run 8 (owner 3): the host's own launch rule, host-side, so the dashboard never re-derives it."""
    from orch.host.runnergrants import MIN_LEFT, _epoch, _iso

    w = ready
    c = w.conn()
    ok(deposit(c, issue(w)))
    grant = ok(c.call("whoami", {}))["data"]["runner"]["grant"]
    assert grant["startable"] is True
    assert grant["startable_until"] == _iso(_epoch(grant["until"]) - MIN_LEFT)


def _service(entry, *, role="owner", person_ok=True, now=1000.0):
    from types import SimpleNamespace as NS

    auth = NS(grants={"gr_1": entry} if entry else {}, person_ok=lambda p, t: person_ok, roles={"p_1": role})
    return NS(auth=auth, clock=lambda: now)


@pytest.mark.parametrize(
    ("entry", "role", "scope", "person_ok", "want"),
    [
        ({"expires_s": 1000.0 + 3600}, "owner", "workable", True, None),
        ({"expires_s": 1000.0 + 3600}, "member", "workable", True, None),
        ({"expires_s": 1000.0 + 3600}, "member", "all", True, "scope"),
        ({"expires_s": 1000.0 + 3600}, "owner", "all", True, None),
        ({"expires_s": 1000.0 + 600}, "owner", "workable", True, "expiring"),  # under the 15-minute floor
        ({"expires_s": 1000.0 + 900}, "owner", "workable", True, None),  # exactly the floor still starts
        ({"expires_s": 1000.0 - 1}, "owner", "workable", True, "revoked"),
        ({"expires_s": 1000.0 + 3600, "revoked": True}, "owner", "workable", True, "revoked"),
        ({"expires_s": 1000.0 + 3600}, "viewer", "workable", True, "revoked"),  # a role change
        ({"expires_s": 1000.0 + 3600}, "owner", "workable", False, "revoked"),  # the person left
        (None, "owner", "workable", True, "revoked"),
    ],
)
def test_launch_reason_is_the_one_rule(entry, role, scope, person_ok, want):
    from types import SimpleNamespace as NS

    from orch.host.runnergrants import RunnerGrants

    e = NS(**{"person": "p_1", "revoked": False, **entry}) if entry else None
    svc = _service(e, role=role, person_ok=person_ok)
    rg = RunnerGrants.__new__(RunnerGrants)
    assert rg.launch_reason(svc, "p_1", {"grant": "gr_1", "scope": scope}) == want


def test_a_hello_before_the_workspace_is_served_keeps_the_grant_and_settles_later(ready):
    """Run 13: the host restarts, the live runner's hello lands before the workspace is bound. The runner's grant must
    not be dropped while the persisted table still reads valid ("the runner holds no such runner grant")."""
    w = ready
    c = w.conn()
    value = issue(w)
    ok(deposit(c, value))
    hub = w.h.host.runners
    services = w.h.host.services
    service = services.pop(w.wid)  # as a host that has not served its workspaces yet
    hub.drop_session(hub.session)  # the runner reconnects: hello
    wait_for(
        lambda: hub.grants._hello is not None and w.wid not in hub.grants._hello["served"] and hub.session is not None
    )
    assert [g["grant"] for g in w.runner.grants.report()] == [gid_of(value)]  # not dropped
    services[w.wid] = service
    w.h.run(_served(hub, w.wid))
    assert [g["grant"] for g in w.runner.grants.report()] == [gid_of(value)]
    assert ok(c.call("whoami", {}))["data"]["runner"]["grant"]["id"] == gid_of(value)


async def _served(hub, wid):
    coro = hub.grants.workspace_served(wid)
    if coro is not None:
        await coro


def test_a_current_grant_changed_between_hello_and_serving_is_dropped(ready):
    w = ready
    c = w.conn()
    value = issue(w)
    ok(deposit(c, value))
    hub = w.h.host.runners
    service = w.h.host.services.pop(w.wid)
    hub.drop_session(hub.session)
    wait_for(
        lambda: hub.grants._hello is not None and w.wid not in hub.grants._hello["served"] and hub.session is not None
    )
    hub.grants.table(w.wid).current.clear()  # the persisted current no longer names what the runner reported
    w.h.host.services[w.wid] = service
    w.h.run(_served(hub, w.wid))
    wait_for(lambda: w.runner.grants.report() == [])


def test_a_failing_settle_at_bind_never_breaks_serving(ready, caplog, monkeypatch):
    w = ready
    host = w.h.host

    def boom(_wid):
        raise RuntimeError("x")

    monkeypatch.setattr(host.runners.grants, "workspace_served", boom)

    async def go():
        host._settle_grants(w.wid)  # an unavailable workspace is bound too: must not raise

    with caplog.at_level(logging.WARNING):
        w.h.run(go())
    assert "runner grant settle failed: RuntimeError" in caplog.text
