from __future__ import annotations

import os
import random
import threading
from dataclasses import replace

import pytest

from orch.github import cache, token
from orch.github.client import GitHubClient
from orch.host import github as service_module
from orch.host.github import GitHubService

from .fake import busy_day
from .test_poller import SETTINGS, Clock

SECRET = "github_pat_service_synthetic_only"


class ManualThread:
    def __init__(self, *, target, name, daemon):
        self.target, self.name, self.daemon = target, name, daemon
        self.alive = False

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout):
        assert timeout <= 1
        self.alive = False


@pytest.fixture
def setup(tmp_path, monkeypatch):
    (tmp_path / "hosts" / "ws").mkdir(parents=True, mode=0o700)
    token.store(tmp_path, "ws", os.getuid(), token.Token(SECRET))
    monkeypatch.setattr(service_module.threading, "Thread", ManualThread)
    monkeypatch.setattr(service_module, "_core_disabled", False)
    calls = []
    monkeypatch.setattr(service_module.resource, "setrlimit", lambda *args: calls.append(args))
    fake, clock = busy_day(), Clock()
    for name in ("alpha", "beta", "gamma"):
        for route in ("pulls", "issues"):
            target = f"/repos/team/{name}/{route}?page=1&per_page=100"
            fake.responses[target + "&state=open"] = replace(fake.responses[target], headers={"etag": '"one"'})
    service = GitHubService(
        tmp_path,
        "ws",
        clock=clock,
        rng=random.Random(1),
        client_factory=lambda source: GitHubClient(source, transport=fake, clock=clock),
    )
    yield service, tmp_path, clock, calls, fake
    service.close()


def test_lifecycle_limits_disable_purge(setup):
    service, state, clock, calls, fake = setup
    assert service.snapshot().state == "empty"
    service.reconcile(SETTINGS, True)
    assert service._thread.daemon and service._thread.name == "orch-github-ws"
    assert service.run_once().state == "ok"
    service.run_once()
    assert len(calls) == 1 and calls[0][1] == (0, 0)
    assert service.refresh_now("alice").accepted
    assert service.refresh_now("alice").retry_after_s == 20
    clock.now += 20
    assert service.refresh_now("alice").accepted
    for n in range(10):
        assert service.refresh_now(str(n)).accepted
    assert service.refresh_now("next").reason == "workspace_limit"
    clock.now += 3600
    assert service.refresh_now("next").accepted
    directory = token.github_dir(state, "ws")
    assert (directory / "cache.json").exists()
    service.reconcile(None, False)
    assert not (directory / "cache.json").exists()
    assert token.load(state, "ws", os.getuid()).reveal() == SECRET
    assert service.snapshot().state == "empty" and service.poller.etags.size_bytes == 0
    assert not service._thread.is_alive()
    service.purge()
    with pytest.raises(token.NoToken):
        token.load(state, "ws", os.getuid())
    service.close()
    assert fake.closed


def test_warm_start_and_logout(setup):
    service, state, clock, _, fake = setup
    service.reconcile(SETTINGS, True)
    live = service.run_once()
    second = GitHubService(
        state, "ws", clock=clock, client_factory=lambda source: GitHubClient(source, transport=fake, clock=clock)
    )
    assert not second.snapshot().verified and second.snapshot().pulls == live.pulls
    second.reconcile(SETTINGS, True)
    assert second.run_once().verified
    token.remove(state, "ws")
    assert second.run_once().state == "no_token"
    assert not second.snapshot().pulls and second.poller.etags.size_bytes == 0
    second.close()


def test_mapping_change_wipes_and_leak_sweep(setup, caplog):
    service, state, _, _, _ = setup
    service.reconcile(SETTINGS, True)
    service.run_once()
    path = token.github_dir(state, "ws") / "cache.json"
    assert SECRET not in path.read_text() + repr(service.snapshot()) + caplog.text
    changed = replace(SETTINGS, repos={"a": "team/alpha"})
    service.reconcile(changed, True)
    assert not path.exists() and not service.snapshot().pulls and service.poller.etags.size_bytes == 0
    service.run_once()
    assert len(service.snapshot().pulls) == 4
    assert SECRET not in repr(cache.load(state, "ws", os.getuid())) + path.read_text()


def test_loop_logs_only_class_and_injected_wait(setup, monkeypatch, caplog):
    service, _, _, _, _ = setup
    service.reconcile(SETTINGS, True)

    def fail():
        raise RuntimeError(SECRET)

    delays = []

    def wait(delay):
        delays.append(delay)
        service._stop.set()

    monkeypatch.setattr(service, "run_once", fail)
    service._wait = wait
    service._loop()
    assert len(delays) == 1 and 270 <= delays[0] <= 330
    assert "RuntimeError" in caplog.text and SECRET not in caplog.text


def test_real_daemon_without_sleep(tmp_path, monkeypatch):
    (tmp_path / "hosts" / "ws").mkdir(parents=True, mode=0o700)
    reached = threading.Event()
    service = GitHubService(tmp_path, "ws", client_factory=lambda source: GitHubClient(source, transport=busy_day()))

    def wait(delay):
        service._stop.set()
        reached.set()

    service._wait = wait
    service.reconcile(SETTINGS, True)
    service._thread.join(timeout=1)
    assert reached.is_set() and service._thread.daemon and not service._thread.is_alive()
    assert service.snapshot().state == "no_token"
    service.close()


def test_warm_cache_dropped_on_rotation_before_success(setup):
    service, state, clock, _, fake = setup
    service.reconcile(SETTINGS, True)
    service.run_once()
    second = GitHubService(
        state, "ws", clock=clock, client_factory=lambda source: GitHubClient(source, transport=fake, clock=clock)
    )
    second.reconcile(SETTINGS, True)
    target = "/repos/team/alpha/pulls?page=1&per_page=100&state=open"
    fake.fail(target)
    assert not second.run_once().verified and second.snapshot().pulls
    token.store(state, "ws", os.getuid(), token.Token("github_pat_new_synthetic"))
    fake.register(target, status=401)
    result = second.run_once()
    assert result.state == "bad_token" and not result.pulls
    assert not (token.github_dir(state, "ws") / "cache.json").exists()
    second.close()


def test_disable_during_poll_cannot_republish(setup, monkeypatch):
    service, state, _, _, _ = setup
    service.reconcile(SETTINGS, True)
    original = service.poller.poll_once

    def disable(settings):
        result = original(settings)
        service.reconcile(None, False)
        return result

    monkeypatch.setattr(service.poller, "poll_once", disable)
    assert service.run_once().state == "empty"
    assert service.poller.etags.size_bytes == 0
    assert not (token.github_dir(state, "ws") / "cache.json").exists()
    assert token.load(state, "ws", os.getuid())


def test_warm_start_shows_what_the_first_poll_found(setup):
    service, state, clock, _, fake = setup
    service.reconcile(SETTINGS, True)
    live = service.run_once()
    second = GitHubService(
        state, "ws", clock=clock, client_factory=lambda source: GitHubClient(source, transport=fake, clock=clock)
    )
    fake.bad_token("/repos/team/alpha/pulls?page=1&per_page=100&state=open")
    second.reconcile(SETTINGS, True)
    shown = second.run_once()
    # the old rows stay (unverified), but the page is told the token no longer works
    assert shown.pulls == live.pulls and not shown.verified
    assert shown.state == "bad_token" and shown.last_error_kind == "unauthorized"
    second.close()


def test_core_dumps_are_disabled_before_the_token_is_loaded(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(service_module, "_core_disabled", False)
    monkeypatch.setattr(service_module.resource, "setrlimit", lambda *a: order.append("core"))
    monkeypatch.setattr(service_module.token, "load", lambda *a: order.append("load") or token.Token(SECRET))
    service = GitHubService(tmp_path, "ws")
    try:
        service.poller._token_source()
    finally:
        service.close()
    assert order == ["core", "load"]


def test_a_failed_core_limit_is_logged_without_the_token(monkeypatch, caplog):
    monkeypatch.setattr(service_module, "_core_disabled", False)

    def boom(*a):
        raise OSError("nope")

    monkeypatch.setattr(service_module.resource, "setrlimit", boom)
    with caplog.at_level("WARNING", logger=service_module.log.name):
        service_module._disable_core()
    assert "core dumps" in caplog.text and SECRET not in caplog.text
