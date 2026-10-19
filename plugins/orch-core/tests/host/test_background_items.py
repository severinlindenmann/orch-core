"""R3-C4c: how macOS lists our launchd agents under Login Items & Extensions, and what is said when the person (or
macOS) turned one off. Fakes only: no real launchctl is ever called."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

from orch.host import enable as svc
from orch.host import status, units
from orch.runner import service as runner_service

DISABLED = '{\n\t"io.orch.host" => disabled\n\t"com.other" => enabled\n}\n'


def done(code: int = 0, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, "")


def plan(tmp_path: Path, **kw) -> svc.Plan:
    return svc.plan(
        {"HOME": str(tmp_path)},
        tmp_path / "s",
        tmp_path / "s" / "run",
        tmp_path / "rel",
        platform="darwin",
        python=Path("/py"),
        uid=501,
        **kw,
    )


def test_the_plist_program_is_the_launcher_so_macos_lists_orch_host_not_python():
    doc = plistlib.loads(
        units.launchd_plist(Path("/uv/bin/python3.14"), Path("/rel/v"), Path("/s"), run_dir=Path("/r"))
    )
    argv = doc["ProgramArguments"]
    assert argv[0] == "/rel/v/bin/orch-host" and Path(argv[0]).name == "orch-host"
    assert not any("python" in a for a in argv)
    runner = plistlib.loads(
        runner_service.launchd_plist(Path("/uv/bin/python3.14"), Path("/rel/v"), Path("/s"), Path("/r"))
    )
    assert runner["ProgramArguments"][0] == "/rel/v/bin/orch-runner"


def test_the_launcher_names_the_isolated_interpreter_itself(tmp_path, monkeypatch):
    from orch.host import release

    monkeypatch.setattr(release.sys, "executable", "/the/python3")
    rel = release.install_release("9.9", target=tmp_path / "releases")
    for name in ("orch-host", "orch-runner"):
        launcher = rel / "bin" / name
        assert launcher.read_text().splitlines()[0] == "#!/the/python3 -I"
        assert launcher.stat().st_mode & 0o111  # directly executable: launchd runs it without an interpreter in front


def test_an_older_plist_with_python_in_front_is_not_another_setup(tmp_path):
    unit = plan(tmp_path)
    old = plistlib.dumps(
        {
            "Label": "io.orch.host",
            "ProgramArguments": [
                "/uv/python3",
                "-I",
                str(tmp_path / "rel/bin/orch-host"),
                "run",
                "--state",
                str(tmp_path / "s"),
                "--run",
                str(tmp_path / "s" / "run"),
            ],
        }
    )
    unit.unit_path.parent.mkdir(parents=True)
    unit.unit_path.write_bytes(old)
    assert svc.differences(unit) == []  # only the shape changed: enable rewrites and reloads it, no --replace needed
    assert svc.Manager(unit, run=lambda a: done()).installed() is False


def test_label_disabled_reads_print_disabled():
    assert svc.label_disabled(DISABLED, "io.orch.host") is True
    assert svc.label_disabled(DISABLED, "com.other") is False
    assert svc.label_disabled(DISABLED, "io.orch.runner") is False
    assert svc.label_disabled('\t"io.orch.host" => true\n', "io.orch.host") is True  # older macOS
    assert svc.label_disabled("", "io.orch.host") is False


def test_manager_asks_launchctl_read_only(tmp_path):
    ran: list[list[str]] = []

    def run(argv):
        ran.append(list(argv))
        return done(0, DISABLED)

    unit = plan(tmp_path)
    assert svc.Manager(unit, run=run).disabled_in_background() is True
    assert ran == [["launchctl", "print-disabled", "gui/501"]]
    assert svc.Manager(unit, run=lambda a: done(1)).disabled_in_background() is None  # cannot tell

    def refused(argv):
        raise OSError("off")

    assert svc.Manager(unit, run=refused).disabled_in_background() is None


def test_a_bootstrap_refused_because_it_is_off_says_so_plainly(tmp_path):
    def run(argv):
        verb = argv[1]
        if verb == "print":
            return done(113)
        if verb == "print-disabled":
            return done(0, DISABLED)
        return done(5)  # bootstrap: Input/output error

    unit = plan(tmp_path)
    try:
        svc.Manager(unit, run=run).start(changed=True)
    except OSError as exc:
        assert str(exc) == (
            "macOS has turned off orch in the background. Open System Settings → General → Login Items & Extensions "
            "and turn on orch-host."
        )
    else:
        raise AssertionError("start must fail")


def test_status_reports_a_service_that_macos_turned_off(tmp_path, monkeypatch):
    from orch.host import cli

    unit_holder: list[svc.Plan] = []

    def run(argv):
        verb = argv[1]
        if verb == "print-disabled":
            return done(0, DISABLED)
        return done(113) if verb == "print" else done()

    def manager(unit):
        unit_holder.append(unit)
        return svc.Manager(unit, run=run)

    monkeypatch.setattr(cli, "SERVICE_MANAGER", manager)
    monkeypatch.setattr(svc, "plan", lambda *a, **k: plan(tmp_path))
    state = tmp_path / "s"
    plan(tmp_path).unit_path.parent.mkdir(parents=True)
    plan(tmp_path).unit_path.write_bytes(plan(tmp_path).content)
    rep = status.report(state, {"HOME": str(tmp_path)}, state / "run")
    assert rep["service"]["disabled_in_background"] is True
    msg = (
        "macOS has turned off orch in the background. Open System Settings → General → Login Items & Extensions "
        "and turn on orch-host."
    )
    assert any(p["what"] == msg for p in rep["problems"])
    assert msg in status.render(rep)
    assert rep["ok"] is False
