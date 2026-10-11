"""The out-of-process runner: JSON-RPC over stdio, caps, deadline, closed environment (ticket-format §8.1)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from orch.addons.manifest import load_package_manifest
from orch.addons.package import read_package
from orch.addons.runner import Limits, RunnerError, call, child_env
from tests.addons.helpers import make_package

TICKET = {"key": "DEMO-0001", "type": "feature", "status": "open", "fields": {}}


@pytest.fixture
def pkg(tmp_path):
    d = make_package(tmp_path)
    package = read_package(d)
    return d, package, load_package_manifest(package, "echo")


def run(pkg, trigger, **limits):
    _, package, manifest = pkg
    return call(
        package,
        manifest,
        granted_digest=package.digest,
        trigger=trigger,
        ticket=TICKET,
        limits=Limits(**limits) if limits else None,
    )


def refused(pkg, trigger, code, **limits):
    with pytest.raises(RunnerError) as e:
        run(pkg, trigger, **limits)
    assert e.value.code == code, e.value
    return e.value


def test_a_call_returns_the_result_of_the_response(pkg):
    assert run(pkg, "ok") == {
        "set": {"points": 5},
        "sections": {"notes": "looked at DEMO-0001"},
        "artifacts": [],
    }


def test_the_environment_holds_no_secret_and_nothing_of_the_hosts(pkg, monkeypatch):
    monkeypatch.setenv("ORCH_GRANT", "gr_SECRET.secret")
    monkeypatch.setenv("ORCH_STATE_DIR", "/home/x/.config/orch")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "hunter2")
    monkeypatch.setenv("HOME", "/home/real-user")
    out = run(pkg, "env")
    env = out["env"]
    assert set(env) <= {
        "PATH",
        "LANG",
        "HOME",
        "TMPDIR",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPATH",
        "ORCH_ADDON",
        "ORCH_ADDON_CAPABILITIES",
        "LC_CTYPE",
        "__CF_USER_TEXT_ENCODING",  # the last two are added by the OS or Python
    }
    assert "ORCH_GRANT" not in env and "hunter2" not in repr(env) and "real-user" not in repr(env)
    assert env["ORCH_ADDON"] == "echo" and env["ORCH_ADDON_CAPABILITIES"] == "serve_http"
    assert env["HOME"] == env["TMPDIR"] == out["cwd"] or out["cwd"].endswith(env["HOME"].split("/")[-1])
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_child_env_is_built_from_nothing():
    e = child_env("x", ["network", "pty"], "/w", "/p")
    assert e == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": "/w",
        "TMPDIR": "/w",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": "/p",
        "ORCH_ADDON": "x",
        "ORCH_ADDON_CAPABILITIES": "network,pty",
    }


def test_it_runs_from_a_private_copy_in_an_empty_working_directory(pkg, tmp_path):
    d, _, _ = pkg
    out = run(pkg, "env")
    assert Path(out["pkg"]) != d and not str(out["pkg"]).startswith(str(tmp_path))
    assert Path(out["cwd"]) != d
    assert not Path(out["cwd"]).exists() and not Path(out["pkg"]).exists()  # removed after the call


def test_it_cannot_write_into_the_package_or_the_workspace(pkg, monkeypatch, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    d, _, _ = pkg
    monkeypatch.setenv("ORCH_TEST_WORKSPACE", str(tmp_path))  # not passed on: the addon never learns where it is
    out = run(pkg, "writes")
    results = list(out.values())
    assert results.count("written") <= 1  # only its own scratch working directory
    assert not (d / "pwned.txt").exists() and not (tmp_path / "pwned.txt").exists()
    package_entries = [v for k, v in out.items() if k.endswith("/pkg")]
    assert package_entries and package_entries[0] != "written"  # the staged package is read-only


def test_only_the_standard_file_descriptors_are_open(pkg):
    fds = run(pkg, "fds")["fds"]
    assert {0, 1, 2} <= set(fds) and max(fds) < 10  # the listing's own descriptor may add one


def test_a_timeout_kills_the_process_and_is_prompt(pkg):
    t0 = time.monotonic()
    e = refused(pkg, "hang", "addon.timeout", timeout=0.6)
    assert time.monotonic() - t0 < 5
    assert e.stderr == ""


def test_a_partial_answer_then_silence_is_a_timeout(pkg):
    refused(pkg, "hang_after_start", "addon.timeout", timeout=0.6)


def test_a_grandchild_does_not_outlive_the_call(pkg):
    t0 = time.monotonic()
    pid = run(pkg, "grandchild")["grandchild"]  # the grandchild holds stdout open for 30 s
    assert time.monotonic() - t0 < 5
    for _ in range(50):  # killed with the process group (it may take a moment to be reaped by init)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the grandchild is still running")


def test_oversized_output_is_refused_at_the_cap(pkg):
    refused(pkg, "big", "addon.output_too_large")


def test_endless_output_is_cut_off_at_the_cap_not_at_the_deadline(pkg):
    t0 = time.monotonic()
    refused(pkg, "endless", "addon.output_too_large", timeout=20)
    assert time.monotonic() - t0 < 10


def test_the_cap_is_a_limit_you_can_set(pkg):
    refused(pkg, "ok", "addon.output_too_large", max_response=10)


def test_a_crash_is_an_error_with_stderr_kept_apart(pkg):
    e = refused(pkg, "crash", "addon.crashed")
    assert "3" in e.detail and "boom" not in e.detail and e.stderr == "boom\n"


def test_stderr_is_capped(pkg):
    assert run(pkg, "stderr_flood") == {}
    assert Limits().max_stderr == 16 << 10


@pytest.mark.parametrize(
    "trigger",
    ["garbage", "two_lines", "no_newline", "wrong_id", "extra_key", "both", "float", "dup_keys", "exit0_no_answer"],
)
def test_malformed_answers_are_refused(pkg, trigger):
    refused(pkg, trigger, "addon.bad_response")


def test_a_json_rpc_error_is_an_error_and_never_echoes_the_message(pkg):
    e = refused(pkg, "error", "addon.error")
    assert "\x1b" not in str(e) and "hi" not in e.detail


def test_a_changed_package_never_starts(pkg):
    d, package, manifest = pkg
    with pytest.raises(RunnerError) as e:
        call(package, manifest, granted_digest="sha256:" + "0" * 64, trigger="ok", ticket=TICKET)
    assert e.value.code == "addon.digest_mismatch"


def test_a_missing_command_is_a_start_failure(tmp_path):
    from tests.addons.helpers import manifest as mk

    m = mk(entry={"cmd": ["definitely-not-a-command-orch"]})
    d = make_package(tmp_path, man=m)
    package = read_package(d)
    with pytest.raises(RunnerError) as e:
        call(
            package, load_package_manifest(package, "echo"), granted_digest=package.digest, trigger="ok", ticket=TICKET
        )
    assert e.value.code == "addon.start_failed"


def test_a_request_that_is_not_strict_json_or_too_big_is_refused_before_starting(pkg):
    _, package, manifest = pkg
    for ticket in ({"x": 1.5}, {"x": "y" * (70 << 10)}, {"x": object()}):
        with pytest.raises(RunnerError) as e:
            call(package, manifest, granted_digest=package.digest, trigger="ok", ticket=ticket)
        assert e.value.code == "addon.bad_request"


def test_limits_are_validated():
    for bad in (dict(timeout=0), dict(timeout=61), dict(max_response=0), dict(max_stderr=-1)):
        with pytest.raises(ValueError):
            Limits(**bad)


@pytest.mark.slow
def test_no_temporary_directory_is_left_behind(pkg):
    import tempfile

    before = {p for p in os.listdir(tempfile.gettempdir()) if p.startswith("orch-addon-")}
    run(pkg, "ok")
    refused(pkg, "crash", "addon.crashed")
    refused(pkg, "hang", "addon.timeout", timeout=0.5)
    after = {p for p in os.listdir(tempfile.gettempdir()) if p.startswith("orch-addon-")}
    assert after <= before


def test_a_hand_built_package_with_a_path_escape_is_refused_before_anything_is_written(tmp_path):
    from orch.addons.package import Package, package_digest

    d = make_package(tmp_path)
    real = read_package(d)
    for evil in ("../escape.py", "/abs.py", "a/../../b", ".hidden", "a//b"):
        files = {**real.files, evil: b"x"}
        forged = Package(files, package_digest(files))
        with pytest.raises(RunnerError) as e:
            call(forged, load_package_manifest(real, "echo"), granted_digest=forged.digest, trigger="ok", ticket=TICKET)
        assert e.value.code == "addon.digest_mismatch"
    assert not (tmp_path / "escape.py").exists()


def test_the_digest_field_of_a_package_is_not_trusted(pkg):
    from orch.addons.package import Package

    _, package, manifest = pkg
    lying = Package({**package.files, "addon.py": b"print('evil')"}, package.digest)  # old digest, new bytes
    with pytest.raises(RunnerError) as e:
        call(lying, manifest, granted_digest=package.digest, trigger="ok", ticket=TICKET)
    assert e.value.code == "addon.digest_mismatch"
