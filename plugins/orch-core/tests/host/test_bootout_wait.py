"""Owner e2e run 4 (B1.2): ``launchctl bootout`` returns before the old service finished unloading, and the first
bootstrap of a new or changed login item can fail with 5 (EIO). ``Manager.start`` waits (bounded) and retries exit 5.
Fake launchctl only."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest

from orch.host import enable as svc
from orch.setup.steps import HIDDEN_REASON, Creator, Run, Secrets, _runner_failure


def done(code: int = 0, err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, "", err)


class FakeLaunchd:
    """bootout returns 0; the service stays listed for ``polls`` ``print`` calls; bootstrap in that window gives 5."""

    def __init__(self, polls: int) -> None:
        self.polls, self.is_loaded, self.ran = polls, True, []
        self.stopping = False

    def __call__(self, argv):
        self.ran.append(list(argv))
        verb = argv[1]
        if verb == "bootout":
            self.stopping = True
            return done()
        if verb == "print":
            if self.stopping:
                if self.polls <= 0:
                    self.is_loaded, self.stopping = False, False
                else:
                    self.polls -= 1
            return done(0 if self.is_loaded else 113)
        if verb == "bootstrap":
            if self.stopping or self.is_loaded:
                return done(5, "Bootstrap failed: 5: Input/output error")
            self.is_loaded = True
            return done()
        return done(1)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def manager(tmp_path: Path, fake, clock: Clock, *, runner: bool = False) -> svc.Manager:
    unit = svc.plan(
        {"HOME": str(tmp_path)}, tmp_path / "s", tmp_path / "s" / "run", tmp_path / "rel",
        platform="darwin", python=Path("/py"), uid=501,
    )  # fmt: skip
    if runner:
        unit = dataclasses.replace(unit, launchd_label="io.orch.runner")
    return svc.Manager(unit, run=fake, clock=clock.now, sleep=clock.sleep)


def test_start_waits_for_the_old_service_to_unload_then_bootstraps(tmp_path):
    fake, clock = FakeLaunchd(polls=6), Clock()
    m = manager(tmp_path, fake, clock)
    ran = m.start(changed=True)
    assert [a[1] for a in ran] == ["bootout", "bootstrap"] and fake.is_loaded
    assert clock.t > 0  # it slept, instead of bootstrapping at once
    assert [a[1] for a in fake.ran].count("bootstrap") == 1  # no failed attempt in the window


def test_start_gives_up_with_a_plain_message_at_the_deadline(tmp_path):
    fake, clock = FakeLaunchd(polls=10**6), Clock()
    m = manager(tmp_path, fake, clock)
    with pytest.raises(OSError, match="still stopping; wait a moment and run the command again"):
        m.start(changed=True)
    assert 14 <= clock.t <= 16 and not any(a[1] == "bootstrap" for a in fake.ran)  # bounded, never bootstrapped


def test_a_bootstrap_5_while_still_loaded_is_retried_once_after_the_wait(tmp_path):
    class Late(FakeLaunchd):  # reports unloaded, then the service reappears as loaded for the first bootstrap only
        def __init__(self) -> None:
            super().__init__(polls=0)
            self.first = True

        def __call__(self, argv):
            if argv[1] == "bootstrap" and self.first:
                self.first, self.ran = False, [*self.ran, list(argv)]
                self.is_loaded, self.stopping = True, True
                self.polls = 3
                return done(5, "Bootstrap failed: 5: Input/output error")
            return super().__call__(argv)

    fake, clock = Late(), Clock()
    m = manager(tmp_path, fake, clock, runner=True)
    ran = m.start(changed=True)
    assert [a[1] for a in ran] == ["bootout", "bootstrap", "bootstrap"] and fake.is_loaded


def test_a_persistent_bootstrap_5_fails_after_three_tries(tmp_path):
    class Stuck(FakeLaunchd):
        def __call__(self, argv):
            if argv[1] == "bootstrap":
                self.ran.append(list(argv))
                return done(5, "Bootstrap failed: 5: Input/output error")
            return super().__call__(argv)

    fake, clock = Stuck(polls=0), Clock()
    m = manager(tmp_path, fake, clock)
    with pytest.raises(OSError, match=r"failed \(5\)"):
        m.start(changed=True)
    assert [a[1] for a in fake.ran].count("bootstrap") == 3  # bounded: three tries, no loop


def test_a_first_bootstrap_5_on_a_not_loaded_service_works_on_the_retry(tmp_path):
    class Once(FakeLaunchd):
        def __init__(self) -> None:
            super().__init__(polls=0)
            self.is_loaded, self.fails = False, 1

        def __call__(self, argv):
            if argv[1] == "bootstrap" and self.fails:
                self.fails -= 1
                self.ran.append(list(argv))
                return done(5, "Bootstrap failed: 5: Input/output error")
            return super().__call__(argv)

    fake, clock = Once(), Clock()
    m = manager(tmp_path, fake, clock, runner=True)
    ran = m.start(changed=True)
    assert [a[1] for a in ran] == ["bootstrap", "bootstrap"] and fake.is_loaded and clock.t >= 5


def test_an_always_failing_bootstrap_5_names_the_retry_command_and_login_items(tmp_path):
    class Never(FakeLaunchd):
        def __call__(self, argv):
            if argv[1] == "bootstrap":
                self.ran.append(list(argv))
                return done(5, "Bootstrap failed: 5: Input/output error")
            return super().__call__(argv)

    fake, clock = Never(polls=0), Clock()
    fake.is_loaded = False
    m = manager(tmp_path, fake, clock, runner=True)
    with pytest.raises(OSError) as e:
        m.start(changed=False)
    assert [a[1] for a in fake.ran].count("bootstrap") == 3 and clock.t <= 12
    assert "orch runner enable" in str(e.value) and "Login Items" in str(e.value)


def _same(text, limit=None):
    return text if limit is None else text[:limit]


def _creator(code: str) -> Creator:
    c = Creator.__new__(Creator)
    c.secrets, c._passphrase = Secrets(show_passphrase=True), None
    c.secrets.confirmed = True
    c.secrets._code = bytearray(code.encode("ascii"))  # fixed, not random: the test must not depend on the draw
    return c


@pytest.mark.parametrize(
    "code",
    ["orchid vessel marble zenith quartz harbor", "enable runner vessel marble zenith quartz"],  # 2nd: wording words
)
def test_setup_runner_reason_never_shows_a_secret(code):
    c = _creator(code)
    words = code.split()
    for shown in (code, "\n".join(words), "\t".join(words).upper(), f"bootstrap failed ({words[2]}) x"):
        text = _runner_failure(OSError(shown), c._safe)
        assert HIDDEN_REASON in text and "orch runner enable" in text, text
        assert not any(w in text for w in words if w not in ("enable", "runner")), text  # those two are our wording
    assert "vessel" not in _runner_failure(OSError("a\nves\nsel"), c._safe).replace("\n", "")  # split inside a word


def test_setup_shows_why_the_runner_failed_and_the_next_command():
    differ = OSError(
        "a runner service (io.orch.runner) is already installed for another setup (python differ); replacing it "
        "ends its runs. To do that on purpose: orch runner enable --replace"
    )
    assert "orch runner enable --replace" in _runner_failure(differ, _same) and "python differ" in _runner_failure(
        differ, _same
    )
    other = _runner_failure(OSError("boom\nx"), _same)
    assert "boom x" in other and "Next: orch runner enable" in other and "\n" not in other
    run, lines = Run("r", [("runner", "Turning on dashboard execution")]), []
    run.echo = lines.append
    run.explain("runner", _runner_failure(differ, _same))
    assert "--replace" in lines[0] and run.view()["reasons"]["runner"] == _runner_failure(differ, _same)
