"""The runner of add-on API 1.1 (G6 A1, #924): generic methods, ``{python}``, settings, the credential channel.
T0: ``uv run pytest tests/addons/test_runner_1_1.py -x -q``"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from orch.addons import runner
from orch.addons.manifest import declarations, load_package_manifest
from orch.addons.package import read_package
from orch.addons.runner import PYTHON_ARGV, Limits, RunnerError, child_env
from orch.addons.runner import call as _call
from orch.model.types import Addon
from tests.addons.helpers import FIXTURES, manifest


def call(*a, **kw):  # standalone: these tests are not about the hub policy
    return _call(*a, gate=None, **kw)


SETTINGS = {
    "title": "Routing",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"mode": {"type": "string", "enum": ["fast", "slow"]}},
    },
}
SECRET = "sk-test-SECRET-value_123"


def build(tmp_path: Path, *, caps=("choose_model",), cmd=None, settings=SETTINGS, name="m11") -> Path:
    d = tmp_path / "addons" / name
    d.mkdir(parents=True)
    over = {"settings": settings} if settings else {}
    m = manifest(
        schema="orch.addon/3",
        api=">=1.1,<2",
        name=name,
        title="M11",
        entry={"cmd": cmd or ["{python}", "{pkg}/main.py"]},
        capabilities=list(caps),
        fields={},
        sections=[],
        artifact_kinds=[],
        **over,
    )
    (d / "orch-addon.json").write_text(json.dumps(m))
    shutil.copy(FIXTURES / "m11" / "main.py", d / "main.py")
    return d


def make(tmp_path, **kw):
    package = read_package(build(tmp_path, **kw))
    m = load_package_manifest(package, "m11")
    return package, Addon("m11", m.version, package.digest, m.capabilities, **declarations(m))


def run(tmp_path, params, method="launch.model", caps=("choose_model",), **kw):
    package, granted = make(tmp_path, caps=caps)
    return call(package, granted, method, params, **kw)


def refused(code, fn, *a, **kw):
    with pytest.raises(RunnerError) as e:
        fn(*a, **kw)
    assert e.value.code == code, e.value
    return e.value


def test_python_token_expands_to_core_interpreter_without_site(tmp_path):
    out = run(tmp_path, {"mode": "flags"})
    assert out["exe"] == sys.executable == PYTHON_ARGV[0]
    assert PYTHON_ARGV[1:] == ("-B", "-E", "-s", "-S")
    assert out["flags"] == [1, 1, 1, 1]
    assert out["site"] is False
    assert out["argv"][0].endswith("main.py")  # the script dir is on the path (not -I)


def test_an_addon_under_python_token_cannot_import_orch(tmp_path):
    assert run(tmp_path, {"mode": "import_orch"}) == {"imported": False}


def test_python_token_never_from_path_and_only_as_cmd0(tmp_path):
    package, granted = make(tmp_path, cmd=["{python}", "{pkg}/main.py"])
    assert call(package, granted, "launch.model", {"mode": "params"})["method"] == "launch.model"
    # a hand-built manifest (the schema refuses these; the runner does not rely on it) never expands anywhere else
    for cmd in (["{pkg}/x", "{python}"], ["{python}x", "a"], ["x{python}"]):
        m = load_package_manifest(package)
        doc = dict(m.doc, entry={"cmd": cmd})
        files = {**package.files, "orch-addon.json": json.dumps(doc).encode()}
        from orch.addons.package import Package, package_digest

        forged = Package(files, package_digest(files))
        with pytest.raises(RunnerError) as e:
            call(forged, granted, "launch.model", {})
        assert e.value.code in {"addon.digest_mismatch", "addon.start_failed"}


def test_a_pkg_path_is_not_rescanned_for_the_token(tmp_path, monkeypatch):
    # tempfile names never hold braces, but even a forged one must not turn into an interpreter
    monkeypatch.setattr(
        runner.tempfile,
        "mkdtemp",
        lambda prefix: (
            __import__("os").makedirs(str(tmp_path / "r{python}"), mode=0o700) or str(tmp_path / "r{python}")
        ),
    )
    package, granted = make(tmp_path)
    out = call(package, granted, "launch.model", {"mode": "env_args"})
    assert out["argv"][0].endswith("r{python}/pkg/main.py")


def test_methods_are_closed_and_bound_to_a_capability(tmp_path):
    package, granted = make(tmp_path)
    refused("addon.bad_request", call, package, granted, "nope", {})
    refused("addon.bad_request", call, package, granted, "launch.model.extra", {})
    package2, granted2 = make(tmp_path / "b", caps=("serve_http",))
    refused("addon.not_granted", call, package2, granted2, "launch.model", {})


def test_a_disabled_or_purged_grant_is_never_called(tmp_path):
    package, granted = make(tmp_path)
    for over in ({"enabled": False}, {"purged": True}):
        g = Addon(
            granted.name,
            granted.version,
            granted.package_sha256,
            granted.capabilities,
            **declarations(load_package_manifest(package, "m11")),
        )
        for k, v in over.items():
            setattr(g, k, v)
        refused("addon.inactive", call, package, g, "launch.model", {})


def test_params_arrive_exactly_as_built_by_core(tmp_path):
    params = {"harness": "claude", "mode": "params", "settings": {"mode": "fast"}}
    out = run(tmp_path, params)
    assert out["params"] == params and out["method"] == "launch.model"


def test_settings_are_validated_against_the_granted_schema_before_the_call(tmp_path):
    for bad in ({"mode": "other"}, {"mode": "fast", "x": 1}, "str", 3):
        refused("addon.bad_request", run, tmp_path / str(len(str(bad))), {"mode": "params", "settings": bad})
    assert run(tmp_path / "ok", {"mode": "params", "settings": None})["params"]["settings"] is None


def test_settings_for_an_addon_without_a_settings_schema_are_refused(tmp_path):
    package, granted = make(tmp_path, settings=None)
    refused("addon.bad_request", call, package, granted, "launch.model", {"settings": {"mode": "fast"}})
    assert call(package, granted, "launch.model", {"settings": None, "mode": "params"})


def test_a_float_or_nan_in_params_is_not_strict_json(tmp_path):
    for v in (1.5, float("nan")):
        refused("addon.bad_request", run, tmp_path / str(v), {"mode": "params", "x": v})


# credential ---------------------------------------------------------------------------------------------------------


def cred(tmp_path, params, caps=("credential", "network"), provider=lambda _n: SECRET, **kw):
    package, granted = make(tmp_path, caps=caps)
    return call(package, granted, "propose", params, credential=provider, **kw)


def test_the_credential_goes_in_params_only_and_only_with_credential_and_network(tmp_path):
    seen = []

    def provider(_n):
        seen.append(1)
        return SECRET

    out = cred(tmp_path / "a", {"mode": "params"}, provider=provider)
    assert out["params"]["credential"] == hashlib.sha256(SECRET.encode()).hexdigest() and seen == [1]
    for i, caps in enumerate((("network",), ("choose_model",), ("serve_http", "network"))):
        seen.clear()
        out = cred(tmp_path / f"n{i}", {"mode": "params"}, caps=caps, provider=provider)
        assert "credential" not in out["params"] and seen == []  # the provider is not even asked
    assert "credential" not in cred(tmp_path / "none", {"mode": "params"}, provider=lambda _n: None)["params"]


def test_the_credential_is_never_in_env_or_argv(tmp_path):
    out = cred(tmp_path, {"mode": "env_args"})
    assert SECRET not in json.dumps(out)


def test_the_caller_cannot_pass_a_credential_and_a_bad_provider_value_is_refused(tmp_path):
    refused("addon.bad_request", cred, tmp_path / "a", {"mode": "params", "credential": "x"})
    for v in ("", 5, b"x"):
        refused("addon.bad_request", cred, tmp_path / f"b{v!r}", {"mode": "params"}, provider=lambda _n, v=v: v)


@pytest.mark.parametrize("mode", ["echo_credential", "echo_credential_escaped", "echo_credential_key"])
def test_an_answer_that_holds_the_credential_is_refused_and_not_returned(tmp_path, mode):
    e = refused("addon.bad_response", cred, tmp_path, {"mode": mode})
    assert SECRET not in repr(e) and SECRET not in e.stderr and SECRET not in e.detail


def test_stderr_is_redacted_when_the_call_fails(tmp_path):
    package, granted = make(tmp_path, caps=("credential", "network"))
    e = refused(
        "addon.crashed", call, package, granted, "propose", {"mode": "crash_credential"}, credential=lambda _n: SECRET
    )
    assert SECRET not in e.stderr and "[redacted]" in e.stderr


def _no_part_of_it(out: str, secret: str, minimum: int = 2) -> None:
    for form in runner._forms(secret):  # noqa: SLF001
        for k in range(minimum, len(form) + 1):
            assert form[:k] not in out, (form[:k], out)


def test_safe_text_leaks_no_part_of_the_secret_at_any_cut_offset_or_cap():
    for secret in (SECRET, 'a"b\\c-secret', "Zq9"):
        for around in ("", "é", "日本", "\U0001f680"):
            full = (around + "x" + secret + around + "tail " + secret + around).encode()
            for cut in range(len(full) + 1):  # the pipe ended (EOF, kill, timeout, slack boundary) at every offset
                for cap in (0, 1, 3, 8, 16, 1000):
                    out = runner._safe_text(full[:cut], secret, cap)  # noqa: SLF001
                    assert len(out.encode()) <= cap + 3 * out.count("\ufffd")
                    _no_part_of_it(out, secret)


def test_safe_text_keeps_the_rest_and_marks_whole_copies():
    assert runner._safe_text(f"a {SECRET} b".encode(), SECRET, 100) == "a [redacted] b"  # noqa: SLF001
    assert runner._safe_text(b"plain text", None, 100) == "plain text"  # noqa: SLF001
    assert runner._safe_text(b"plain text", None, 5) == "plain"  # noqa: SLF001
    assert runner._safe_text(b"caf\xc3\xa9", None, 4) == "caf\ufffd"  # noqa: SLF001


def test_a_killed_child_leaves_no_partial_secret_in_stderr(tmp_path):
    package, granted = make(tmp_path, caps=("credential", "network"))
    e = refused(
        "addon.timeout",
        call,
        package,
        granted,
        "propose",
        {"mode": "stderr_then_hang"},
        Limits(timeout=1),
        credential=lambda _n: SECRET,
    )
    assert "\u00e9" in e.stderr or e.stderr == ""
    assert SECRET[:2] not in e.stderr


def test_a_secret_cut_at_the_stderr_cap_leaves_no_prefix_end_to_end(tmp_path):
    package, granted = make(tmp_path, caps=("credential", "network"))
    for cap in range(0, 60, 4):
        e = refused(
            "addon.crashed",
            call,
            package,
            granted,
            "propose",
            {"mode": "crash_credential"},
            Limits(max_stderr=cap),
            credential=lambda _n: SECRET,
        )
        assert SECRET[:3] not in e.stderr and len(e.stderr) <= cap


def test_the_request_is_never_in_an_error(tmp_path):
    package, granted = make(tmp_path, caps=("credential", "network"))
    for mode in ("crash_credential", "echo_credential"):
        with pytest.raises(RunnerError) as e:
            call(package, granted, "propose", {"mode": mode}, credential=lambda _n: SECRET)
        assert SECRET not in str(e.value) and SECRET not in repr(e.value.__dict__)


def test_child_env_has_no_credential_key_whatever_the_capabilities():
    env = child_env("x", ["credential", "network"], "/w", "/p")
    assert set(env) == {
        "PATH",
        "LANG",
        "HOME",
        "TMPDIR",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPATH",
        "ORCH_ADDON",
        "ORCH_ADDON_CAPABILITIES",
    }


def test_the_oversized_request_with_a_credential_is_refused_before_start(tmp_path):
    refused("addon.bad_request", cred, tmp_path, {"mode": "params", "pad": "x" * (70 << 10)})


def test_a_provider_of_another_addon_is_refused_and_nothing_is_sent(tmp_path):
    asked = []

    def alpha_provider(name):
        asked.append(name)
        if name != "alpha":
            raise PermissionError("not for this add-on")  # what credential.py's reader does
        return SECRET

    package, granted = make(tmp_path, caps=("credential", "network"))  # this add-on is "m11", not alpha
    e = refused("addon.bad_request", call, package, granted, "propose", {"mode": "params"}, credential=alpha_provider)
    assert asked == ["m11"] and SECRET not in repr(e) and SECRET not in e.stderr
