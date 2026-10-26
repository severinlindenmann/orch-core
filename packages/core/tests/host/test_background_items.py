"""R3-C4c: how macOS lists our launchd agents under Login Items & Extensions, and what is said when the person (or
macOS) turned one off. Fakes only: no real launchctl is ever called."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

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


def _release_with_launcher(tmp_path: Path, python: str) -> svc.Plan:
    unit = plan(tmp_path)
    (unit.release / "bin").mkdir(parents=True)
    for name in ("orch-host", "orch-runner"):
        (unit.release / "bin" / name).write_text(f"#!{python} -I\n")
    return unit


def _legacy(unit: svc.Plan, python: str) -> bytes:
    argv = [
        python,
        "-I",
        str(unit.release / "bin" / "orch-host"),
        "run",
        "--state",
        str(unit.state),
        "--run",
        str(unit.run),
    ]
    return plistlib.dumps({"Label": "io.orch.host", "ProgramArguments": argv})


def test_an_older_plist_with_the_same_interpreter_is_not_another_setup(tmp_path):
    unit = _release_with_launcher(tmp_path, "/uv/python3")
    unit.unit_path.parent.mkdir(parents=True)
    unit.unit_path.write_bytes(_legacy(unit, "/uv/python3"))
    assert svc.differences(unit) == []  # only the shape changed: enable rewrites and reloads it, no --replace needed
    assert svc.Manager(unit, run=lambda a: done()).installed() is False


def test_another_interpreter_in_the_launcher_or_an_older_plist_is_seen_as_python_differing(tmp_path):
    unit = _release_with_launcher(tmp_path, "/dev/venv/python3")
    unit.unit_path.parent.mkdir(parents=True)
    unit.unit_path.write_bytes(_legacy(unit, "/uv/python3"))
    assert svc.differences(unit) == ["python"]
    assert svc.unit_fields("launchd", unit.content, unit.release)["python"] == "/dev/venv/python3"


def test_only_the_expected_launcher_counts_as_one(tmp_path):
    unit = _release_with_launcher(tmp_path, "/uv/python3")
    other = tmp_path / "elsewhere" / "orch-host"
    other.parent.mkdir()
    other.write_text("#!/uv/python3 -I\n")
    doc = plistlib.loads(unit.content)
    doc["ProgramArguments"][0] = str(other)
    fields = svc.unit_fields("launchd", plistlib.dumps(doc), unit.release)
    assert fields["python"] == str(other)  # not the launcher's shebang: it is its own "interpreter", so it differs


def test_the_release_identity_has_the_interpreter_and_a_reuse_checks_it(tmp_path, monkeypatch):
    from orch.host import release

    monkeypatch.setattr(release.sys, "executable", "/py/one")
    v1 = release.default_version()
    monkeypatch.setattr(release.sys, "executable", "/py/two")
    assert release.default_version() != v1
    monkeypatch.setattr(release.sys, "executable", "/py/one")
    target = tmp_path / "releases"
    rel = release.install_release("9.9", target=target)
    assert release.install_release("9.9", target=target) == rel  # same interpreter: reused
    monkeypatch.setattr(release.sys, "executable", "/py/two")
    with pytest.raises(release.ReleaseError, match="another interpreter"):
        release.install_release("9.9", target=target)


def test_an_interpreter_path_with_whitespace_is_refused(tmp_path, monkeypatch):
    from orch.host import release

    monkeypatch.setattr(release.sys, "executable", "/my py/python3")
    with pytest.raises(release.ReleaseError, match="whitespace"):
        release.install_release("9.9", target=tmp_path / "releases")
    assert not (tmp_path / "releases" / "9.9").exists()


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
    unit = plan(tmp_path)
    monkeypatch.setattr(svc, "plan", lambda *a, **k: unit)
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
