# ruff: noqa: F811
"""G6 B2 (#924): an add-on that holds ``choose_model`` picks the model of an agent start; core keeps the gate.

The add-on here is a stdlib-only package written into the workspace's store and granted with a real digest, run by the
real runner (``{python}``). Nothing starts a real harness (the fakes of test_agents).
T0: ``uv run pytest tests/host/test_launch_routing.py -x -q``"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from orch.addons import install
from orch.addons.manifest import load_package_manifest
from orch.addons.package import read_package
from orch.host import routing
from orch.modelchoices import MODEL_CHOICES
from tests.addons.helpers import manifest as base_manifest
from tests.host.dispatch_helpers import err, ok
from tests.host.test_agents import preview, read_out, ready, start, w  # noqa: F401

NAME = "model-routing"
SCHEMA = {
    "title": "Models",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            harness: {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    mode: {"type": "string", "enum": list(names)}
                    for mode in ("work", "refine", "fix", "continue", "context")
                },
            }
            for harness, names in MODEL_CHOICES.items()
        },
    },
}
TABLE = {"claude-code": {"work": "sonnet", "refine": "opus"}, "codex": {"fix": "gpt-5.4"}}

#: the add-on: appends the params it was given to LOG, then answers ``ANSWER`` (python source, may use ``params``)
MAIN = """
import json, sys, time
req = json.loads(sys.stdin.readline())
params = req["params"]
with open({log!r}, "a") as f:
    f.write(json.dumps({{"method": req["method"], "params": params}}) + "\\n")
{body}
sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": req["id"], "result": ANSWER}}) + "\\n")
"""
FROM_SETTINGS = """
s = params["settings"] or {}
ANSWER = {"model": s.get(params["harness"], {}).get(params["mode"])}
"""


@pytest.fixture(autouse=True)
def _fresh_memo():
    routing._memo.clear()
    routing._failed.clear()
    yield
    routing._memo.clear()
    routing._failed.clear()


def build(w, tmp_path, body=FROM_SETTINGS, name=NAME, caps=("choose_model",)) -> tuple[Path, Path]:
    """Write the package into the store; return (package dir, call log)."""
    log = tmp_path / f"{name}.calls"
    d = Path(w.service.store.layout.store) / "addons" / name
    d.mkdir(parents=True)
    m = base_manifest(
        schema="orch.addon/3",
        api=">=1.1,<2",
        name=name,
        title="Model routing",
        entry={"cmd": ["{python}", "{pkg}/main.py"]},
        capabilities=list(caps),
        fields={},
        sections=[],
        artifact_kinds=[],
        settings=SCHEMA,
    )
    (d / "orch-addon.json").write_text(json.dumps(m))
    (d / "main.py").write_text(MAIN.format(log=str(log), body=textwrap.dedent(body)))
    return d, log


def grant(w, d: Path, name=NAME) -> None:
    pkg = read_package(d)
    body = install.grant_event(load_package_manifest(pkg, name), pkg)
    body.pop("type")
    w.person_event("addon.granted", "workspace", **body)


def settings(w, value, name=NAME) -> None:
    w.person_event("settings.changed", "workspace", set={"addons": {name: value}})


def calls(log: Path) -> list[dict]:
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def route(w, tmp_path, body=FROM_SETTINGS, table=TABLE, name=NAME):
    d, log = build(w, tmp_path, body, name)
    grant(w, d, name)
    if table is not None:
        settings(w, table, name)
    return d, log


def pre(w, **over):
    c = w.conn()
    _, key = w.ticket()
    return preview(c, ticket=key, harness="claude-code", mode="work", **over)


def test_without_an_add_on_the_harness_default_and_no_call(ready):
    got = pre(ready)
    assert got["model"] is None and got["model_source"] == "default" and "--model" not in got["argv"]
    assert got["model_by"] is None


def test_the_add_ons_model_is_in_the_argv_the_spec_and_the_record(ready, tmp_path):
    _, log = route(ready, tmp_path)
    c = ready.conn()
    _, key = ready.ticket()
    got = preview(c, ticket=key, harness="claude-code", mode="work")
    assert got["blocked"] is None and got["model"] == "sonnet" and got["model_source"] == "addon"
    assert got["model_by"] == NAME
    assert got["argv"][:2] == [got["program"], "--model"] and got["argv"][2] == "sonnet"
    assert preview(c, ticket=key, harness="claude-code", mode="fix")["model"] is None  # the add-on said null
    assert preview(c, ticket=key, harness="codex", mode="fix")["model"] == "gpt-5.4"
    run = ok(start(c, ticket=key, harness="claude-code", mode="work", spec=got["spec"]))["data"]["run"]
    assert run["model"] == "sonnet"
    argv, _ = read_out(ready, "claude")
    assert argv[:2] == ["--model", "sonnet"]
    # the person's start binds the model: a different spec is refused
    assert err(start(c, ticket=key, harness="claude-code", mode="work", spec="0" * 64)) == "launch.changed"


def test_c1_the_input_is_exactly_harness_mode_settings(ready, tmp_path):
    _, log = route(ready, tmp_path)
    pre(ready)
    (one,) = calls(log)
    assert one["method"] == "launch.model"
    assert set(one["params"]) == {"harness", "mode", "settings"}
    assert one["params"] == {"harness": "claude-code", "mode": "work", "settings": TABLE}
    text = json.dumps(one)
    assert "DEMO" not in text and "ticket" not in text  # nothing an agent writes, no key, no person


def test_the_settings_come_from_the_signed_log_and_a_change_changes_the_spec(ready, tmp_path):
    _, log = route(ready, tmp_path)
    first = pre(ready)
    settings(ready, {"claude-code": {"work": "haiku"}})
    second = pre(ready)
    assert second["model"] == "haiku" and second["spec"] != first["spec"]
    assert calls(log)[-1]["params"]["settings"] == {"claude-code": {"work": "haiku"}}
    settings(ready, None)  # removed: the add-on gets null and answers null
    third = pre(ready)
    assert third["model"] is None and calls(log)[-1]["params"]["settings"] is None


def test_an_explicit_model_makes_zero_calls(ready, tmp_path):
    _, log = route(ready, tmp_path)
    got = pre(ready, model="haiku")
    assert got["model"] == "haiku" and got["model_source"] == "chosen" and got["model_by"] is None
    assert calls(log) == []


def test_the_answer_is_cached_so_preview_and_start_agree(ready, tmp_path):
    _, log = route(ready, tmp_path)
    c = ready.conn()
    _, key = ready.ticket()
    got = preview(c, ticket=key, harness="claude-code", mode="work")
    again = preview(c, ticket=key, harness="claude-code", mode="work")
    ok(start(c, ticket=key, harness="claude-code", mode="work", spec=got["spec"]))
    assert again["spec"] == got["spec"] and len(calls(log)) == 1


@pytest.mark.parametrize(
    "answer",
    [
        '{"model": "--yolo"}',  # argv smuggling
        '{"model": "opus"}',  # a name of the other harness' list is checked per harness below
        '{"model": "sonnet", "extra": 1}',
        '{"model": "x" * 10240}',
        '{"model": 7}',
        '{"model": ["sonnet"]}',
        '{"model": "Sonnet"}',
        '"sonnet"',
        "None",
        "{}",
    ],
)
def test_c1_an_answer_outside_the_closed_list_blocks_and_never_reaches_an_argv(ready, tmp_path, answer):
    route(ready, tmp_path, body=f"ANSWER = {answer}\n", table=None)
    c = ready.conn()
    _, key = ready.ticket()
    # "opus" is valid for claude-code, so ask for the harness whose list does not offer it
    harness = "codex" if answer == '{"model": "opus"}' else "claude-code"
    got = preview(c, ticket=key, harness=harness, mode="work")
    assert got["blocked"]["code"] == "launch.invalid", answer
    assert not got["blocked"]["code"].startswith("addon.")
    assert "--model" not in got["argv"] and "--yolo" not in " ".join(got["argv"])
    assert err(start(c, ticket=key, harness=harness, mode="work", spec=got["spec"])) == "launch.invalid"


def test_a_null_answer_means_the_harness_default(ready, tmp_path):
    route(ready, tmp_path, body='ANSWER = {"model": None}\n', table=None)
    got = pre(ready)
    assert got["blocked"] is None and got["model"] is None and got["model_source"] == "default"


def test_a_crash_blocks_with_the_one_plain_message(ready, tmp_path):
    route(ready, tmp_path, body="sys.exit(3)\nANSWER = None\n", table=None)
    got = pre(ready)
    assert got["blocked"]["code"] == "launch.invalid" and got["blocked"]["message"] == routing.UNANSWERED
    explicit = pre(ready, model="opus")  # an explicit model always works
    assert explicit["blocked"] is None and explicit["model"] == "opus"


@pytest.mark.slow
def test_a_hung_add_on_blocks_after_the_timeout(ready, tmp_path):
    route(ready, tmp_path, body="time.sleep(30)\nANSWER = None\n", table=None)
    got = pre(ready)
    assert got["blocked"]["code"] == "launch.invalid" and got["blocked"]["message"] == routing.UNANSWERED


def test_two_choose_model_grants_block(ready, tmp_path):
    route(ready, tmp_path)
    d, _ = build(ready, tmp_path, name="other-routing")
    grant(ready, d, "other-routing")
    got = pre(ready)
    assert got["blocked"]["code"] == "launch.invalid" and "More than one" in got["blocked"]["message"]
    assert pre(ready, model="opus")["blocked"] is None


def test_a_disabled_add_on_is_not_asked(ready, tmp_path):
    _, log = route(ready, tmp_path)
    ready.person_event("addon.disabled", "workspace", name=NAME)
    got = pre(ready)
    assert got["blocked"] is None and got["model"] is None and calls(log) == []


def test_a_package_that_is_not_the_granted_one_is_never_run(ready, tmp_path):
    d, log = route(ready, tmp_path)
    (d / "main.py").write_text(f'import sys\nopen({str(log)!r}, "a").write("ran")\nsys.exit(0)\n')
    got = pre(ready)
    assert got["blocked"]["code"] == "launch.invalid" and not log.exists()


def test_a_value_that_no_longer_fits_the_granted_schema_is_not_sent(ready, tmp_path):
    _, log = route(ready, tmp_path, table=None)
    ready.person_event(
        "settings.changed", "workspace", set={"addons": {NAME: {"claude-code": {"work": "not-a-model"}}}}
    )  # replay checks the shape only; the schema is the granted one
    got = pre(ready)
    assert got["blocked"]["code"] == "launch.invalid" and calls(log) == []


def test_an_add_on_without_choose_model_is_not_a_router(ready, tmp_path):
    d, log = build(ready, tmp_path, caps=())
    grant(ready, d)
    got = pre(ready)
    assert got["blocked"] is None and got["model"] is None and calls(log) == []


def test_a_person_run_shell_is_never_routed(ready, tmp_path):
    _, log = route(ready, tmp_path)
    c = ready.conn()
    ok(c.call("term.preview", {"harness": "claude-code"}))
    assert calls(log) == []


def test_b2_3_a_failure_before_the_call_is_launch_invalid_too(ready, tmp_path, monkeypatch):
    route(ready, tmp_path)
    from orch.addons import install

    def boom(*a, **k):
        raise ValueError("unexpected")

    monkeypatch.setattr(install, "read_installed", boom)
    got = pre(ready)
    assert got["blocked"]["code"] == "launch.invalid" and got["blocked"]["message"] == routing.UNANSWERED


def test_b2_4_a_failed_answer_is_not_asked_again_for_a_while(ready, tmp_path, monkeypatch):
    _, log = route(ready, tmp_path, body="sys.exit(3)\nANSWER = None\n", table=None)
    pre(ready)
    pre(ready)
    assert len(calls(log)) == 1  # the second preview did not run the add-on
    t = [routing.time.monotonic() + routing.FAIL_SECONDS + 1]
    monkeypatch.setattr(routing.time, "monotonic", lambda: t[0])
    pre(ready)
    assert len(calls(log)) == 2
