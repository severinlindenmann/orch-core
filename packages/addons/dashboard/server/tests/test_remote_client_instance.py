"""R5 T4: one remote client per OS user; a second ``orch remote open`` hands over to the running one."""

from __future__ import annotations

import io
import os
import stat
import threading
import time

from orch_dashboard.remote_client.__main__ import main
from orch_dashboard.remote_client.instance import SingleInstance


def test_the_lock_is_held_by_one_and_the_pidfile_is_private(tmp_path):
    a, b = SingleInstance(tmp_path / "rc"), SingleInstance(tmp_path / "rc")
    assert a.acquire() and not b.acquire()
    assert a.running_pid() == os.getpid()
    assert stat.S_IMODE(os.stat(a.pid_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(a.lock_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(a.root).st_mode) == 0o700
    a.release()
    assert a.running_pid() is None and b.acquire()
    b.release()


def test_handover_is_taken_once_and_a_stale_request_is_ignored_at_start(tmp_path):
    a = SingleInstance(tmp_path / "rc")
    a.handover_path.parent.mkdir(parents=True, exist_ok=True)
    a.handover_path.write_text("1")
    assert a.acquire()
    assert not a.take_handover()  # the stale request died at start
    t = threading.Thread(target=lambda: time.sleep(0.2) or a.take_handover())
    t.start()
    assert SingleInstance(tmp_path / "rc").request_handover(wait_s=3, sleep=0.05)
    t.join()
    a.release()


def test_a_second_open_hands_over_and_exits_with_a_plain_message(tmp_path):
    env = {"ORCH_STATE_DIR": str(tmp_path / "st"), "HOME": str(tmp_path)}
    running = SingleInstance(tmp_path / "st" / "remote-client")
    assert running.acquire()
    got = []
    stop = threading.Event()

    def client():
        while not stop.is_set():
            if running.take_handover():
                got.append(1)
            time.sleep(0.05)

    t = threading.Thread(target=client)
    t.start()
    out, err = io.StringIO(), io.StringIO()
    try:
        assert main([], env=env, out=out, err=err) == 0
    finally:
        stop.set()
        t.join()
        running.release()
    assert got and "already running" in out.getvalue() and err.getvalue() == ""


def test_a_running_client_that_does_not_answer_is_said_plainly(tmp_path, monkeypatch):
    from orch_dashboard.remote_client import instance

    monkeypatch.setattr(instance, "HANDOVER_WAIT_S", 0.2)
    monkeypatch.setattr(instance.SingleInstance, "request_handover", lambda self, wait_s=0.2, sleep=0.05: False)
    env = {"ORCH_STATE_DIR": str(tmp_path / "st"), "HOME": str(tmp_path)}
    running = SingleInstance(tmp_path / "st" / "remote-client")
    assert running.acquire()
    out, err = io.StringIO(), io.StringIO()
    try:
        assert main([], env=env, out=out, err=err) == 1
    finally:
        running.release()
    assert "did not answer" in err.getvalue()
