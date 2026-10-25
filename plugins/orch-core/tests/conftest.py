"""Suite-wide guards."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def real_keychains() -> str:
    """The account's real ``~/Library/Keychains``, from the password database (``$HOME`` may be redirected)."""
    import pwd

    return os.path.realpath(os.path.join(pwd.getpwuid(os.getuid()).pw_dir, "Library", "Keychains"))


def refuse_real_keychain(path) -> None:
    if path is None:
        raise AssertionError("a test tried to open the login keychain; pass a throwaway keychain path")
    target = os.path.realpath(os.fspath(path))
    real = real_keychains()
    if target == real or target.startswith(real + os.sep):
        raise AssertionError(f"a test tried to reach the real keychain {target}")


def child_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """The environment of a child process in a test: HOME is always a temporary directory, never the owner's, and the
    child never reaches launchctl or systemctl (``ORCH_NO_SERVICE_MANAGER=1``, H7)."""
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "ORCH_NO_SERVICE_MANAGER": "1",
        **extra,
    }
    home = os.path.realpath(env["HOME"])
    assert home == os.path.realpath(tmp_path) or home.startswith(os.path.realpath(tmp_path) + os.sep), home
    assert not real_keychains().startswith(home + os.sep), "HOME must not be the owner's home"
    return env


@pytest.fixture(autouse=True)
def _no_plain_review_leak():
    """The one-shot "a plain review was just shown" flag of the passphrase prompt never survives a test (a review
    confirmed in one test and no passphrase asked would shorten the next test's prompt)."""
    yield
    from orch.custody import passphrase

    passphrase._PLAIN_REVIEW.update(shown=False, hint=True)


def hub_seams() -> dict[str, object]:
    """The process-wide seams the host's hub code reads: a test that swaps one by plain assignment and does not put it
    back makes every later hub test in the same xdist worker talk to its stub (``bad_signature`` on the first reply)."""
    from orch.host import breakglass, hubops

    return {
        "hubops.TRANSPORT": (hubops, "TRANSPORT"), "hubops.CARD_SOURCE": (hubops, "CARD_SOURCE"),
        "hubops.workspace_dev": (hubops, "workspace_dev"), "breakglass.TRANSPORT": (breakglass, "TRANSPORT"),
    }  # fmt: skip


@pytest.fixture(autouse=True)
def _hub_seams_never_leak():
    """Every hub seam is the same object after a test as before it. The leak is repaired (so the next test is clean)
    and the leaking test fails, by name. Set seams with ``monkeypatch``, never by assignment."""
    seams = hub_seams()
    before = {k: getattr(m, a) for k, (m, a) in seams.items()}
    yield
    leaked = [k for k, (m, a) in seams.items() if getattr(m, a) is not before[k]]
    for k in leaked:
        setattr(seams[k][0], seams[k][1], before[k])
    assert not leaked, f"this test left a hub seam swapped (use monkeypatch.setattr): {leaked}"


@pytest.fixture(autouse=True, scope="session")
def _never_the_real_keychain():
    """No test may reach the owner's real keychains (H5c): the store, ``_open`` and every keychain test helper refuse
    None and any path under the account's real ``~/Library/Keychains``. Session scope and no ``monkeypatch``: the guard
    must not share or reorder any test's own patches."""
    try:
        from orch.custody import keychain
    except Exception:  # noqa: BLE001 - not importable here: nothing to guard
        yield
        return
    saved = {
        "init": keychain.KeychainStore.__init__,
        "_open": keychain._open,
        "create_throwaway_keychain": keychain.create_throwaway_keychain,
    }
    real_init, real_open, real_create = saved["init"], saved["_open"], saved["create_throwaway_keychain"]

    def init(self, keychain_path=None):
        refuse_real_keychain(keychain_path)
        real_init(self, keychain_path)

    def open_(path):
        refuse_real_keychain(path)
        return real_open(path)

    def create(path, password):
        refuse_real_keychain(path)
        return real_create(path, password)

    keychain.KeychainStore.__init__ = init
    keychain._open = open_  # unlock, lock and delete go through _open
    keychain.create_throwaway_keychain = create
    try:
        yield
    finally:
        keychain.KeychainStore.__init__ = real_init
        keychain._open = real_open
        keychain.create_throwaway_keychain = real_create


# --- H7: the service manager, the unit files and the release are never the owner's ----------------------------------


def real_home() -> str:
    import pwd

    return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)


#: the owner's own service files and release pointer: a test run must leave them exactly as it found them
REAL_SERVICE_FILES = (
    ("Library", "LaunchAgents", "io.orch.host.plist"),
    (".config", "systemd", "user", "orch-host.service"),
    ("Library", "LaunchAgents", "io.orch.runner.plist"),
    (".config", "systemd", "user", "orch-runner.service"),
    (".local", "share", "orch", "releases", "current"),
    ("Library", "LaunchAgents", "io.orch.records.plist"),  # H11e: the records timer
    (".config", "systemd", "user", "orch-records.service"),
    (".config", "systemd", "user", "orch-records.timer"),
)


#: the release pointer is flipped by any other session's release install on this shared machine; this run cannot
#: change it (``install_release`` refuses a real-home target via ``refuse_real_home``), so it is not compared
SHARED_POINTERS = {(".local", "share", "orch", "releases", "current")}


def _service_snapshot() -> dict[str, object]:
    out: dict[str, object] = {}
    for parts in REAL_SERVICE_FILES:
        if parts in SHARED_POINTERS:
            continue
        path = os.path.join(real_home(), *parts)
        try:
            st = os.lstat(path)
            out[path] = (st.st_mode, st.st_size, st.st_mtime_ns, os.readlink(path) if os.path.islink(path) else None)
        except FileNotFoundError:
            out[path] = None
    return out


def refuse_real_home(path, what: str) -> None:
    target = os.path.realpath(os.fspath(path))
    home = real_home()
    if target == home or target.startswith(home + os.sep):
        raise AssertionError(f"a test tried to write {what} under the real home: {target}")


@pytest.hookimpl(trylast=True)  # after -m has deselected: the split sees the tests that would run
def pytest_collection_modifyitems(config, items):
    spec = os.environ.get("ORCH_TEST_SHARD")
    if not spec:
        return
    from tests._shard import assign, load_weights, module_weights, parse

    index, count = parse(spec)
    weights: dict[str, int] = {}
    for item in items:
        module = item.nodeid.split("::")[0]
        weights[module] = weights.get(module, 0) + 1
    slices = assign(module_weights(weights, load_weights()), count)
    kept = [i for i in items if slices[i.nodeid.split("::")[0]] == index]
    config.hook.pytest_deselected(items=[i for i in items if i not in kept])
    items[:] = kept


def pytest_sessionstart(session):
    session.config._orch_service_files = _service_snapshot()


def pytest_sessionfinish(session, exitstatus):
    before = getattr(session.config, "_orch_service_files", None)
    if before is not None and _service_snapshot() != before:
        session.exitstatus = 1
        raise AssertionError(f"the test run changed the owner's service files: {before} -> {_service_snapshot()}")


@pytest.fixture(autouse=True, scope="session")
def _no_real_service_manager(tmp_path_factory):
    """No test reaches launchctl or systemctl, and none writes a unit or a release under the real home (H7). The
    service manager's commands all fail unless a test installs its own fake (``tests/host/test_host_mode_on.py``);
    ``Manager.install`` and ``install_release`` refuse a target under the account's real home whatever ``HOME`` says;
    ``ORCH_HOST_RUN_DIR`` always names a test directory, so no test talks to a real host's sockets. The production
    runner refuses on its own as well while ``PYTEST_CURRENT_TEST`` is set. Session scope with its own MonkeyPatch, as
    the keychain guard: it must not share the tests' ``monkeypatch`` (that would reorder their teardown)."""
    import subprocess

    try:
        from orch.host import cli, enable, release
    except Exception:  # noqa: BLE001 - not importable here: nothing to guard
        yield
        return

    def refuse(argv):
        return subprocess.CompletedProcess(list(argv), 1, "", "service managers are off in tests")

    monkeypatch = pytest.MonkeyPatch()

    monkeypatch.setattr(cli, "SERVICE_MANAGER", lambda unit: enable.Manager(unit, run=refuse))
    real_install = enable.Manager.install

    def install(self):
        refuse_real_home(self.plan.unit_path, "a service unit")
        return real_install(self)

    monkeypatch.setattr(enable.Manager, "install", install)
    real_release = release.install_release

    def install_release(version=None, *, target=None, source=None, python=None, **more):
        refuse_real_home(target if target is not None else release.releases_dir(), "a release")
        return real_release(version, target=target, source=source, python=python, **more)

    monkeypatch.setattr(release, "install_release", install_release)
    try:  # H11e: the records timer has the same fences (no real service manager, no unit under the real home)
        from orch.records import timer
    except Exception:  # noqa: BLE001 - not importable here: nothing to guard
        timer = None
    if timer is not None:
        real_timer_install = timer.TimerManager.install

        def timer_install(self, **kw):
            for path, _content in self.plan.files:
                refuse_real_home(path, "a records timer unit")
            return real_timer_install(self, **kw)

        monkeypatch.setattr(timer.TimerManager, "install", timer_install)
        real_timer_remove = timer.TimerManager.remove

        def timer_remove(self):
            for path, _content in self.plan.files:
                refuse_real_home(path, "a records timer unit (removal)")
            return real_timer_remove(self)

        monkeypatch.setattr(timer.TimerManager, "remove", timer_remove)
        monkeypatch.setattr(timer, "_default_run", refuse)
    monkeypatch.setenv("ORCH_HOST_RUN_DIR", str(tmp_path_factory.getbasetemp() / "no-host-run"))
    monkeypatch.setenv("ORCH_NO_SERVICE_MANAGER", "1")  # every child process inherits it (subprocess, Popen)
    try:
        yield
    finally:
        monkeypatch.undo()
