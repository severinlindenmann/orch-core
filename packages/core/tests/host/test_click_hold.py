"""D5: unsigned signer holds, monotonic clock, FIFO, cancellation and deadline checks."""

import threading

import pytest

from orch.click.protocol import HOLD_OPS, RISK_BUILDERS
from tests.host import human_helpers
from tests.host.test_click import Click, pending, refused

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


@pytest.fixture
def click(served):
    c = Click(served)
    yield c
    c.close()


def hold(click, human, *, op="log", args=None):
    _, key = human.ticket()
    p = click.prepare(op, args or {"ref": key, "text": "held comment"}, session="session-a")
    rid = p["request_id"]
    v = click.view(rid)
    click.mono.t += 1.1
    return rid, v["view_digest"]


def advance(click, seconds):
    click.served.lab.ws.clock[0] += seconds
    click.mono.t += seconds


def sign(click, rid, digest):
    return click.sign(rid, digest, delay_s=10, session="session-a")


def cancel(click, rid, session="session-a"):
    return click.signer.cancel({"workspace_id": click.served.wid, "request_id": rid, "session": session})


def test_closed_list():
    assert not HOLD_OPS & RISK_BUILDERS.keys()
    assert HOLD_OPS == {"log", "set", "move", "section.set", "follow_up.reschedule"}


def test_non_hold_is_refused(click, human):
    rid, digest = hold(click, human, op="new", args={"title": "Immediate", "type": "chore"})
    refused(lambda: sign(click, rid, digest), "hold.not_allowed")
    assert click.signatures == 0


def test_cancel_at_99_and_session_binding(click, human, served):
    rid, digest = hold(click, human)
    assert sign(click, rid, digest)["state"] == "held"
    assert click.signatures == 0
    advance(click, 9.9)
    refused(lambda: cancel(click, rid, "other-session"), "hold.session")
    assert cancel(click, rid)["state"] == "cancelled"
    advance(click, 1)
    click.signer.session_alive({"session": "session-a"})
    click.signer.run_holds()
    assert click.signatures == 0
    assert pending(served, rid)["state"] == "cancelled"


def test_browser_absent_deadline_signs_once(click, human, served):
    rid, digest = hold(click, human)
    sign(click, rid, digest)
    advance(click, 10)
    click.signer.session_alive({"session": "session-a"})
    click.signer.run_holds()  # no browser call drives signing
    click.signer.session_alive({"session": "session-a"})
    click.signer.run_holds()
    assert click.signatures == 1
    assert pending(served, rid)["state"] == "submitted"
    assert cancel(click, rid)["state"] == "already_signed"


def test_cancel_deadline_race(click, human, served):
    rid, digest = hold(click, human)
    sign(click, rid, digest)
    advance(click, 10)
    gate = threading.Barrier(2)
    result = []

    def deadline():
        gate.wait()
        click.signer.session_alive({"session": "session-a"})
        click.signer.run_holds()

    thread = threading.Thread(target=deadline)
    thread.start()
    gate.wait()
    result.append(cancel(click, rid)["state"])
    thread.join()
    assert result[0] in {"cancelled", "already_signed"}
    assert click.signatures == (result[0] == "already_signed")
    assert pending(served, rid)["state"] == ("submitted" if click.signatures else "cancelled")


def test_sleep_drops_and_restart_has_no_signature(click, human, served):
    rid, digest = hold(click, human)
    sign(click, rid, digest)
    advance(click, 40.1)
    click.signer.session_alive({"session": "session-a"})
    click.signer.run_holds()
    assert click.signatures == 0
    args = {"workspace_id": served.wid, "request_id": rid, "session": "session-a"}
    assert click.signer.hold_status(args)["state"] == "not_sent"
    assert pending(served, rid)["state"] == "cancelled"
    click.close()
    replacement = Click(served)
    try:
        replacement.signer.run_holds()
        assert replacement.signatures == 0
        assert replacement.signer.hold_status(args)["state"] == "not_sent"
    finally:
        replacement.close()


def test_deadline_rechecks_custody(click, human):
    rid, digest = hold(click, human)
    sign(click, rid, digest)
    click.gen += 1
    advance(click, 10)
    click.signer.session_alive({"session": "session-a"})
    click.signer.run_holds()
    assert click.signatures == 0
    assert next(iter(click.signer.holds.values()))["state"] == "not_sent"


def test_fifo_even_if_later_deadline_is_earlier(click, human, monkeypatch):
    first, vd = hold(click, human)
    sign(click, first, vd)
    second, vd = hold(click, human)
    sign(click, second, vd)
    values = list(click.signer.holds.values())
    values[1]["deadline"] -= 5
    seen = []
    monkeypatch.setattr(click.signer, "_sign", lambda args: seen.append(args["request_id"]))
    advance(click, 6)
    click.signer.session_alive({"session": "session-a"})
    click.signer.run_holds()
    assert seen == []
    advance(click, 4)
    click.signer.session_alive({"session": "session-a"})
    click.signer.run_holds()
    assert seen == [first, second]


def test_twenty_holds_and_the_next_is_refused(click, human, monkeypatch):
    # Bypass the host's independent 20-request cap to exercise the signer's own bound.
    monkeypatch.setattr("orch.host.requests.MAX_PENDING", 21)
    for _ in range(20):
        rid, digest = hold(click, human)
        sign(click, rid, digest)
    rid, digest = hold(click, human)
    refused(lambda: sign(click, rid, digest), "hold.full")
    assert click.signatures == 0


def test_dwell_is_checked_when_hold_is_requested(click, human):
    rid, digest = hold(click, human)
    click.mono.t -= 1.1
    refused(lambda: sign(click, rid, digest), "human.click_refused", "too_soon")
    assert not click.signer.holds


def test_request_needs_delay_plus_thirty_seconds(click, human):
    rid, digest = hold(click, human)
    advance(click, 561)
    refused(lambda: sign(click, rid, digest), "hold.expiring")
    assert click.signatures == 0


def test_restart_during_live_hold_drops_it(click, human, served):
    rid, digest = hold(click, human)
    sign(click, rid, digest)
    advance(click, 5)
    click.close()
    replacement = Click(served)
    try:
        advance(click, 5)
        replacement.signer.run_holds()
        assert click.signatures == replacement.signatures == 0
        assert pending(served, rid)["state"] == "bound"
        assert replacement.signer.hold_status({"workspace_id": served.wid, "request_id": rid})["state"] == "not_sent"
    finally:
        replacement.close()


def test_details_title_is_not_holdable(click, human):
    _, key = human.ticket()
    p = click.prepare("set", {"ref": key, "pairs": ["title=Renamed"]}, session="session-a")
    view = click.view(p["request_id"])
    assert view["view"]["hold_allowed"] is False
    click.mono.t += 1.1
    refused(lambda: sign(click, p["request_id"], view["view_digest"]), "hold.not_allowed")


def test_deadline_checks_host_state_and_does_not_retry(click, human, monkeypatch):
    rid, digest = hold(click, human)
    sign(click, rid, digest)
    from orch.click.protocol import ClickError

    calls = []
    original = click.signer._fetch

    def changed(*args, **kwargs):
        calls.append(1)
        raise ClickError("role.denied", "membership removed")

    monkeypatch.setattr(click.signer, "_fetch", changed)
    advance(click, 10)
    click.signer.session_alive({"session": "session-a"})
    click.signer.run_holds()
    monkeypatch.setattr(click.signer, "_fetch", original)
    click.signer.session_alive({"session": "session-a"})
    click.signer.run_holds()
    assert calls == [1]
    assert click.signatures == 0


@pytest.mark.parametrize("kill", [False, True])
def test_signer_process_deadline_without_browser_and_kill(tmp_path, kill):
    """Exercise the real channel loop/process lifetime; replace only crypto with a log marker.

    The real-host tests above check the signatures themselves. Here there are no incoming
    browser requests after the hold is admitted, and SIGKILL must lose the memory queue.
    """
    import socket
    import subprocess
    import sys
    import textwrap
    import time

    from orch.click.client import ClickClient

    parent, child = socket.socketpair()
    marker = tmp_path / "signed"
    code = textwrap.dedent("""
        import socket, sys, time
        from pathlib import Path
        from orch.click.signer import ClickSigner
        from orch.click.server import serve
        signer = ClickSigner(Path(sys.argv[2]))
        start = time.monotonic()
        signer.holds[('a' * 32, 'hr_' + '0' * 26)] = {
            'state': 'held', 'deadline': start + 10, 'deadline_wall': time.time() + 10,
            'session': 's', 'person': 'p', 'args': {},
        }
        def sign(args):
            Path(sys.argv[3]).write_text(str(time.monotonic() - start))
        signer._sign = sign
        print('ready', flush=True)
        serve(socket.socket(fileno=int(sys.argv[1])), signer)
    """)
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(child.fileno()), str(tmp_path / "state"), str(marker)],
        pass_fds=(child.fileno(),),
        stdout=subprocess.PIPE,
        text=True,
    )
    child.close()
    client = ClickClient(parent)
    try:
        assert proc.stdout.readline().strip() == "ready"
        if kill:
            proc.kill()
            proc.wait(timeout=5)
            assert not marker.exists()
        else:
            for _ in range(5):
                client.call("session_alive", {"session": "s"})
                time.sleep(2)
            time.sleep(0.3)
            state = client.call("status", {"workspace_id": "a" * 32, "request_id": "hr_" + "0" * 26, "session": "s"})
            assert state["state"] == "submitted"
            assert 10 <= float(marker.read_text()) < 11
    finally:
        parent.close()
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=5)


def test_restart_after_delivery_reports_submitted(click, human, served):
    rid, digest = hold(click, human)
    sign(click, rid, digest)
    advance(click, 10)
    click.signer.session_alive({"session": "session-a"})
    click.signer.run_holds()
    assert click.signatures == 1
    click.close()
    replacement = Click(served)
    try:
        assert replacement.signer.hold_status({"workspace_id": served.wid, "request_id": rid})["state"] == "submitted"
        assert replacement.signatures == 0
    finally:
        replacement.close()


@pytest.mark.parametrize("outcome", ["submitted", "cancelled", "not_sent", "unknown"])
def test_finished_holds_are_swept_without_blocking_new_work(click, human, monkeypatch, outcome):
    monkeypatch.setattr("orch.click.signer.MAX_APPROVALS", 1)
    for _ in range(3):
        rid, digest = hold(click, human)
        sign(click, rid, digest)
        key = (click.served.wid, rid)
        if outcome == "cancelled":
            cancel(click, rid)
        else:
            if outcome == "unknown":

                def lost(args, key=key):
                    click.signer._attempted.add(key)
                    raise RuntimeError("lost reply")

                monkeypatch.setattr(click.signer, "_sign", lost)
            advance(click, 41 if outcome == "not_sent" else 10)
            click.signer.session_alive({"session": "session-a"})
            click.signer.run_holds()
            if outcome == "unknown":
                monkeypatch.undo()
                monkeypatch.setattr("orch.click.signer.MAX_APPROVALS", 1)
        assert click.signer.holds[key]["state"] == outcome
        advance(click, 59)
        click.signer.session_alive({"session": "session-a"})
        click.signer.run_holds()
        assert key in click.signer.holds
        advance(click, 1)
        click.signer.session_alive({"session": "session-a"})
        click.signer.run_holds()
        assert not click.signer.holds
    rid, digest = hold(click, human)
    assert sign(click, rid, digest)["state"] == "held"


def test_finished_tail_does_not_consume_approval_or_hold_quota(click, human, monkeypatch):
    monkeypatch.setattr("orch.click.signer.MAX_APPROVALS", 1)
    for _ in range(3):
        rid, digest = hold(click, human)
        sign(click, rid, digest)
        cancel(click, rid)
    assert len(click.signer.holds) == 3
    rid, digest = hold(click, human)
    refused(lambda: hold(click, human), "rate.limited")
    assert sign(click, rid, digest)["state"] == "held"


def test_cancel_all_unsigned_holds_by_session(click, human):
    first, digest = hold(click, human)
    sign(click, first, digest)
    second, digest = hold(click, human)
    sign(click, second, digest)
    click.signer.holds[(click.served.wid, second)]["session"] = "other"
    assert click.signer.handle("cancel_holds", {"session": "session-a"}) == {"state": "cancelled"}
    advance(click, 10)
    click.signer.session_alive({"session": "other"})
    click.signer.run_holds()
    assert click.signatures == 1
    assert click.signer.holds[(click.served.wid, first)]["state"] == "cancelled"


def test_relogin_can_cancel_normal_request(click, human):
    rid, _ = hold(click, human)
    assert cancel(click, rid, "new-session")["state"] == "cancelled"


def test_risk_lines_disable_hold(click, human, monkeypatch):
    from orch.click import view

    build = view.build
    monkeypatch.setattr(view, "build", lambda *a, **kw: {**build(*a, **kw), "risk_lines": ["Risk"]})
    rid, digest = hold(click, human)
    refused(lambda: sign(click, rid, digest), "hold.not_allowed")
    assert click.signatures == 0


@pytest.mark.parametrize("wall_jump", [-3600, 3600])
def test_stale_session_drops_at_monotonic_deadline_before_cancel(click, human, wall_jump):
    rid, digest = hold(click, human)
    sign(click, rid, digest)
    click.served.lab.ws.clock[0] += wall_jump
    click.mono.t += 10
    # Cancellation has not reached the signer. No channel delivery or restart is needed.
    click.signer.run_holds()
    assert click.signatures == 0
    status = click.signer.hold_status(
        {
            "workspace_id": click.served.wid,
            "request_id": rid,
            "session": "session-a",
        }
    )
    assert status["state"] == "cancelled"
    assert status["reason"] == "session"
    click.signer.session_alive({"session": "session-a"})
    click.signer.run_holds()
    assert click.signatures == 0


def test_channel_drains_cancel_queued_during_dispatch_before_deadline(monkeypatch):
    import socket

    from orch.click.server import serve
    from orch.host import frames

    parent, child = socket.socketpair()
    dispatched = []
    checked = threading.Event()

    class Signer:
        def handle(self, op, args):
            dispatched.append(op)
            if op == "status":
                # Arrives during dispatch, so it was not in the first recv's decoded batch.
                parent.sendall(frames.encode({"t": "req", "id": "2", "op": "cancel_holds", "args": {"session": "a"}}))
            return {}

        def run_holds(self):
            assert dispatched == ["status", "cancel_holds"]
            checked.set()

    parent.sendall(frames.encode({"t": "req", "id": "1", "op": "status", "args": {}}))
    thread = threading.Thread(target=serve, args=(child, Signer()))
    thread.start()
    try:
        assert checked.wait(2)
    finally:
        parent.shutdown(socket.SHUT_WR)
        thread.join(2)
        parent.close()
        child.close()
    assert not thread.is_alive()


def test_blocked_cancel_cannot_keep_session_hold_alive(click, human):
    rid, digest = hold(click, human)
    sign(click, rid, digest)
    release = threading.Event()
    queued = threading.Event()

    def blocked_cancel():
        queued.set()
        assert release.wait(2)
        click.signer.cancel_holds({"session": "session-a"})

    thread = threading.Thread(target=blocked_cancel)
    thread.start()
    try:
        assert queued.wait(1)
        advance(click, 10)
        click.signer.run_holds()
        assert click.signatures == 0
        assert click.signer.holds[(click.served.wid, rid)]["reason"] == "session"
    finally:
        release.set()
        thread.join(2)


def test_a_suspend_never_signs_a_hold(click, human, served):
    """The monotonic clock stops in sleep, the wall clock does not: mono stays put while wall jumps an hour between
    the lease renewal and run_holds, so neither the lease nor the deadline may look fresh (fail closed)."""
    rid, digest = hold(click, human)
    assert sign(click, rid, digest)["state"] == "held"
    advance(click, 10)
    click.signer.session_alive({"session": "session-a"})
    served.lab.ws.clock[0] += 3600  # wall only
    click.signer.run_holds()
    assert click.signatures == 0
    held = click.signer.holds[(served.wid, rid)]
    assert (held["state"], held["reason"]) == ("cancelled", "session")  # the stale lease dropped it


def test_a_suspend_with_a_renewal_on_wake_is_not_sent(click, human, served):
    """The renewer runs again on wake, so the lease is fresh on BOTH clocks; the hold's own wall deadline is an hour
    off, which makes it late: it is reported not sent and nothing is signed (the ``deadline_wall`` branch)."""
    rid, digest = hold(click, human)
    assert sign(click, rid, digest)["state"] == "held"
    assert click.signer.holds[(served.wid, rid)]["deadline_wall"] > 0  # set when the hold is created
    advance(click, 10)
    served.lab.ws.clock[0] += 3600  # wall only: the lid was closed
    click.signer.session_alive({"session": "session-a"})  # the renewer on wake
    click.signer.run_holds()
    assert click.signatures == 0
    assert click.signer.holds[(served.wid, rid)]["state"] == "not_sent"


def test_a_sequence_whose_first_step_is_a_hold_op_is_never_held(click, human, served):
    """An edit sequence starts with ``section.set`` (a HOLD_OPS op) but is N signatures for one review: it gets
    ``hold_allowed=False`` and ``delay_s=10`` is refused, so a hold can never sign part of a sequence."""
    from orch import canon
    from orch.click.protocol import HOLD_OPS
    from tests.host.human_helpers import ok
    from tests.host.test_sequence_click import prepare

    made = prepare(click)[1]
    v = click.view(made["request_id"])
    click.mono.t += 2
    key = click.sign(made["request_id"], v["view_digest"])["result"]["key"]
    data = ok(served.client().req("ticket.get", {"ref": key}))["data"]
    intent = {
        "v": 1,
        "kind": "edit_ticket",
        "ticket": {"key": key, "uid": data["doc"]["uid"]},
        "ticket_type": "feature",
        "head": data["doc"]["head"]["head"],
        "sections": {"summary": {"was": canon.section_hash("One line."), "text": "A better line."}},
        "acceptance": {"was": [], "edit": {}, "add": ["It opens"]},
    }
    before = click.signatures
    _, p = prepare(click, intent)
    assert click.signer.approvals[(served.wid, p["request_id"])].op in HOLD_OPS
    view = click.view(p["request_id"])
    assert view["view"]["hold_allowed"] is False
    click.mono.t += 2
    refused(
        lambda: click.sign(p["request_id"], view["view_digest"], delay_s=10, session="session-a"), "hold.not_allowed"
    )
    assert click.signatures == before  # nothing of the edit signed
