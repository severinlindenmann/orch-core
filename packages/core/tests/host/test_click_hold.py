"""D5: unsigned signer holds, wall clock, FIFO, cancellation and deadline checks."""

import threading

import pytest

from orch.click.protocol import HOLD_OPS
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
    click.signer.run_holds()
    assert click.signatures == 0
    assert pending(served, rid)["state"] == "cancelled"


def test_browser_absent_deadline_signs_once(click, human, served):
    rid, digest = hold(click, human)
    sign(click, rid, digest)
    advance(click, 10)
    click.signer.run_holds()  # no browser call drives signing
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
    click.signer.run_holds()
    assert seen == []
    advance(click, 4)
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
    click.signer.run_holds()
    monkeypatch.setattr(click.signer, "_fetch", original)
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
        start = time.time()
        signer.holds[('a' * 32, 'hr_' + '0' * 26)] = {
            'state': 'held', 'deadline': start + 10, 'session': 's', 'person': 'p', 'args': {},
        }
        def sign(args):
            Path(sys.argv[3]).write_text(str(time.time() - start))
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
            time.sleep(10.3)  # no request, poll or browser drives the deadline
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
            click.signer.run_holds()
            if outcome == "unknown":
                monkeypatch.undo()
                monkeypatch.setattr("orch.click.signer.MAX_APPROVALS", 1)
        assert click.signer.holds[key]["state"] == outcome
        advance(click, 59)
        click.signer.run_holds()
        assert key in click.signer.holds
        advance(click, 1)
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
