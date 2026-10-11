"""The out-of-process runner: JSON-RPC over stdio, caps, deadline, closed environment (ticket-format §8.1)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.addons.manifest import declarations, load_package_manifest
from orch.addons.package import read_package
from orch.addons.runner import Limits, RunnerError, call, child_env
from orch.model.types import Addon
from tests.addons.helpers import make_package


def ticket(**over):
    """A stand-in for a ``TicketView``: more than the runner may send, to show what it leaves out."""
    t = SimpleNamespace(
        key="DEMO-0001",
        type="feature",
        status="open",
        visibility="workspace",
        title="secret title",
        people={"assignees": ("p_x",)},
        fields={"title": "secret title", "addons": {"echo": {"points": 3}, "other": {"x": 1}}},
    )
    for k, v in over.items():
        setattr(t, k, v)
    return t


TICKET = ticket()


def granted_for(package, **over):
    m = load_package_manifest(package, "echo")
    a = Addon("echo", m.version, package.digest, m.capabilities, **declarations(m))
    for k, v in over.items():
        setattr(a, k, v)
    return a


@pytest.fixture
def pkg(tmp_path):
    d = make_package(tmp_path)
    package = read_package(d)
    return d, package, granted_for(package)


def run(pkg, trigger, **limits):
    _, package, granted = pkg
    return call(
        package,
        granted,
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
    d, package, granted = pkg
    granted.package_sha256 = "sha256:" + "0" * 64
    with pytest.raises(RunnerError) as e:
        call(package, granted, trigger="ok", ticket=TICKET)
    assert e.value.code == "addon.digest_mismatch"


def test_the_command_capabilities_and_identity_come_from_the_granted_package_never_from_elsewhere(pkg, tmp_path):
    """The manifest is parsed from the package bytes whose digest was granted; the grant's name, version and
    capabilities must equal it. There is no way to pass a different manifest."""
    import inspect

    assert "manifest" not in inspect.signature(call).parameters
    _, package, _ = pkg
    for over in ({"capabilities": ["network"]}, {"capabilities": []}, {"version": "9.9.9"}, {"name": "other"}):
        with pytest.raises(RunnerError) as e:
            call(package, granted_for(package, **over), trigger="ok", ticket=TICKET)
        assert e.value.code == "addon.digest_mismatch", over
    # a package whose manifest names another command has another digest: it is not the granted one
    evil = make_package(
        tmp_path / "evil",
        man={
            **__import__("tests.addons.helpers", fromlist=["x"]).MANIFEST,
            "entry": {"cmd": ["/bin/sh", "-c", "echo hi"]},
        },
    )
    other = read_package(evil)
    with pytest.raises(RunnerError) as e:
        call(other, granted_for(package), trigger="ok", ticket=TICKET)
    assert e.value.code == "addon.digest_mismatch"


def test_only_key_type_status_and_the_addons_own_fields_are_sent(pkg):
    params = run(pkg, "params")["params"]
    assert params["ticket"] == {"key": "DEMO-0001", "type": "feature", "status": "open", "fields": {"points": 3}}
    assert set(params) == {"addon", "version", "capabilities", "trigger", "ticket"}
    assert params["capabilities"] == ["serve_http"]
    assert "secret" not in str(params) and "p_x" not in str(params) and "other" not in str(params["ticket"])


def test_a_ticket_that_is_not_visible_to_everyone_is_never_run(pkg):
    _, package, granted = pkg
    for vis in ({"restricted": ["p_x"]}, "restricted", None):
        with pytest.raises(RunnerError) as e:
            call(package, granted, trigger="ok", ticket=ticket(visibility=vis))
        assert e.value.code == "addon.not_visible"


def test_a_disabled_or_purged_addon_is_never_run(pkg):
    _, package, _ = pkg
    for over in ({"enabled": False}, {"purged": True}):
        with pytest.raises(RunnerError) as e:
            call(package, granted_for(package, **over), trigger="ok", ticket=TICKET)
        assert e.value.code == "addon.inactive"


def test_a_missing_command_is_a_start_failure(tmp_path):
    from tests.addons.helpers import manifest as mk

    m = mk(entry={"cmd": ["definitely-not-a-command-orch"]})
    d = make_package(tmp_path, man=m)
    package = read_package(d)
    with pytest.raises(RunnerError) as e:
        call(package, granted_for(package), trigger="ok", ticket=TICKET)
    assert e.value.code == "addon.start_failed"


def test_a_request_that_is_not_strict_json_or_too_big_is_refused_before_starting(pkg):
    _, package, granted = pkg
    for own in ({"x": 1.5}, {"x": "y" * (70 << 10)}, {"x": object()}):
        t = ticket(fields={"addons": {"echo": own}})
        with pytest.raises(RunnerError) as e:
            call(package, granted, trigger="ok", ticket=t)
        assert e.value.code == "addon.bad_request"


def test_limits_are_validated():
    for bad in (dict(timeout=0), dict(timeout=61), dict(max_response=0), dict(max_stderr=-1)):
        with pytest.raises(ValueError):
            Limits(**bad)


@pytest.mark.slow
def test_no_temporary_directory_is_left_behind(pkg, tmp_path, monkeypatch):
    import tempfile

    scratch = tmp_path / "scratch"  # a private temp dir: nothing else can add to it while this runs
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    run(pkg, "ok")
    refused(pkg, "crash", "addon.crashed")
    refused(pkg, "hang", "addon.timeout", timeout=0.5)
    assert os.listdir(scratch) == []


def test_a_hand_built_package_with_a_path_escape_is_refused_before_anything_is_written(tmp_path):
    from orch.addons.package import Package, package_digest

    d = make_package(tmp_path)
    real = read_package(d)
    for evil in ("../escape.py", "/abs.py", "a/../../b", ".hidden", "a//b", "x.so"):
        files = {**real.files, evil: b"x"}
        forged = Package(files, package_digest(files))
        with pytest.raises(RunnerError) as e:
            call(forged, granted_for(forged), trigger="ok", ticket=TICKET)
        assert e.value.code == "addon.digest_mismatch"
    assert not (tmp_path / "escape.py").exists()


def test_the_digest_field_of_a_package_is_not_trusted(pkg):
    from orch.addons.package import Package

    _, package, granted = pkg
    lying = Package({**package.files, "addon.py": b"print('evil')"}, package.digest)  # old digest, new bytes
    with pytest.raises(RunnerError) as e:
        call(lying, granted, trigger="ok", ticket=TICKET)
    assert e.value.code == "addon.digest_mismatch"


def test_the_resource_limits_are_in_force_in_the_child(pkg):
    out = run(pkg, "limits")
    assert out["core"] == 0 and out["fsize"] == 1 << 20
    assert out["cpu"] <= int(Limits().timeout) + 2


def test_a_process_that_leaves_its_group_is_not_tracked(pkg):
    """The documented P1 limit (§8.1): setsid escapes the group kill. Killing every process of the addon needs its own
    UID or a cgroup (P2); this test pins that the limit is real so nobody claims otherwise."""
    pid = run(pkg, "escape")["pid"]
    try:
        time.sleep(0.2)
        os.kill(pid, 0)  # still alive: the group kill did not reach it
    except ProcessLookupError:
        pytest.skip("the escaped process was reaped quickly on this system")
    finally:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
