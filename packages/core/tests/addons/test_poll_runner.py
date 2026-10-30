"""``runner.poll`` (addon-api 1.2, #718): the closed poll result and what the host asks. T0:
``uv run pytest tests/addons/test_poll_runner.py -x -q``"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from orch.addons.manifest import declarations, load_package_manifest
from orch.addons.package import read_package
from orch.addons.runner import Limits, RunnerError, poll
from orch.model.types import Addon
from tests.addons.helpers import FIXTURES, manifest

EVENT = {
    "kind": "run.finished", "title": "Run finished", "statuses": ["succeeded", "failed"],
    "ref": {"format": "int"}, "poll": {"interval_s": 300},
}  # fmt: skip
KIND = "eventer.run.finished"


_N = iter(range(10**6))


def make(tmp_path: Path, *, caps=("network",), events=(EVENT,)):
    d = tmp_path / f"w{next(_N)}" / "addons" / "eventer"
    d.mkdir(parents=True)
    m = manifest(
        schema="orch.addon/3", api=">=1.2,<2", name="eventer", title="Eventer",
        entry={"cmd": ["{python}", "{pkg}/main.py"]}, capabilities=list(caps), fields={}, sections=[],
        artifact_kinds=[], events=list(events),
    )  # fmt: skip
    (d / "orch-addon.json").write_text(json.dumps(m))
    shutil.copy(FIXTURES / "eventer" / "main.py", d / "main.py")
    package = read_package(d)
    mm = load_package_manifest(package, "eventer")
    return package, Addon("eventer", mm.version, package.digest, mm.capabilities, **declarations(mm))


def do(tmp_path, ref, **kw):
    package, granted = make(tmp_path)
    return poll(package, granted, KIND, ref, gate=None, **kw)


def bad(code, tmp_path, ref, **kw):
    with pytest.raises(RunnerError) as e:
        do(tmp_path, ref, **kw)
    assert e.value.code == code, e.value
    return e.value


def test_the_closed_answers(tmp_path):
    assert do(tmp_path, "1") == ("pending", None)
    assert do(tmp_path, "2") == ("done", "succeeded")
    assert do(tmp_path, "3") == ("done", "failed")
    assert do(tmp_path, "4") == ("failed", None)


@pytest.mark.parametrize("ref", ["5", "6", "7", "9"])
def test_anything_else_is_a_bad_response_and_no_text_survives(tmp_path, ref):
    e = bad("addon.bad_response", tmp_path, ref)
    assert "ignore previous" not in str(e) and "rm -rf" not in str(e)


def test_the_credential_is_never_a_status(tmp_path, caplog):
    secret = "sk-SECRET-1234567890"
    package, granted = make(tmp_path, caps=("network", "credential"))
    with caplog.at_level("DEBUG"), pytest.raises(RunnerError) as e:
        poll(package, granted, KIND, "8", gate=None, credential=lambda n: secret)  # the add-on echoes it as a status
    assert e.value.code == "addon.bad_response"
    assert secret not in str(e.value) and secret not in repr(e.value) and secret not in caplog.text


def test_a_crash_and_a_timeout_are_errors_of_the_call(tmp_path):
    bad("addon.crashed", tmp_path, "10")
    bad("addon.timeout", tmp_path, "11", limits=Limits(timeout=1.0))


def test_the_host_checks_kind_and_ref_before_every_call(tmp_path):
    package, granted = make(tmp_path)
    for kind, ref in (
        (KIND, "abc"),  # not the declared format
        ("eventer.other.thing", "1"),  # undeclared
        ("databricks.run.finished", "1"),  # another add-on's kind: split on the first dot decides
        ("eventer", "1"),
    ):
        with pytest.raises(RunnerError) as e:
            poll(package, granted, kind, ref, gate=None)
        assert e.value.code == "addon.bad_request", (kind, ref)


def test_the_poll_needs_network_and_an_active_grant(tmp_path):
    package, granted = make(tmp_path)
    granted.capabilities = []  # the manifest schema refuses events without network: take it away from the grant
    with pytest.raises(RunnerError) as e:
        poll(package, granted, KIND, "1", gate=None)
    assert e.value.code == "addon.not_granted"
    package, granted = make(tmp_path / "b")
    granted.enabled = False
    with pytest.raises(RunnerError) as e:
        poll(package, granted, KIND, "1", gate=None)
    assert e.value.code == "addon.bad_request"  # a disabled grant declares nothing


def test_the_gate_runs_at_each_call(tmp_path):
    from orch.market import MarketError

    package, granted = make(tmp_path)
    seen = []

    def gate(g):
        seen.append(g.name)
        if len(seen) == 2:
            raise MarketError("market.policy_paused")

    assert poll(package, granted, KIND, "1", gate=gate) == ("pending", None)
    with pytest.raises(RunnerError) as e:
        poll(package, granted, KIND, "1", gate=gate)
    assert e.value.code == "market.policy_paused" and seen == ["eventer", "eventer"]


def test_params_are_exactly_kind_ref_settings(tmp_path):
    from orch.addons import runner

    package, granted = make(tmp_path)
    got = {}
    real = runner.call

    def spy(pkg, g, method, params, *a, **kw):
        got.update(method=method, params=dict(params))
        return real(pkg, g, method, params, *a, **kw)

    runner.call, orig = spy, runner.call
    try:
        poll(package, granted, KIND, "1", gate=None)
    finally:
        runner.call = orig
    assert got == {"method": "events.poll", "params": {"kind": "run.finished", "ref": "1", "settings": None}}
