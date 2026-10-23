"""Model routing: the signed ``settings.models`` picks the model of a Start agent mode (host-chosen, bound by the
person's review through the spec digest), from a closed list per harness. Nothing here starts a real harness."""

from __future__ import annotations

import pytest

from orch.host import launch, signcontent
from orch.modelchoices import MODEL_CHOICES, ROUTE_MODES
from orch.schema import load
from tests.host import human_helpers
from tests.host.dispatch_helpers import err, ok
from tests.host.human_helpers import err as herr
from tests.host.test_agents import grant_addon, preview, read_out, ready, start, w  # noqa: F401

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

TABLE = {"claude-code": {"work": "sonnet", "refine": "opus"}, "codex": {"fix": "gpt-5.4"}}


def route(w, table=TABLE, on=True):  # noqa: F811
    if on:
        grant_addon(w, "model-routing", caps=())
    w.person_event("settings.changed", "workspace", set={"models": table})


def test_the_closed_lists_equal_the_schema_and_the_clients_words():
    ev = load("event.settings.changed")["properties"]["set"]["properties"]["models"]["oneOf"][0]["properties"]
    ws = load("workspace")["properties"]["settings"]["properties"]["models"]["oneOf"][0]["properties"]
    for doc in (ev, ws):
        assert set(doc) == set(MODEL_CHOICES)
        for harness, names in MODEL_CHOICES.items():
            assert set(doc[harness]["properties"]) == set(ROUTE_MODES)
            assert all(p["enum"] == list(names) for p in doc[harness]["properties"].values())
    assert set(launch.MODEL_LABELS) == {m for names in MODEL_CHOICES.values() for m in names}


def test_routed_model_is_closed_and_fails_closed():
    assert launch.routed_model({}, "codex", "work") == (None, None)
    assert launch.routed_model({"models": TABLE}, "claude-code", "work") == ("sonnet", None)
    assert launch.routed_model({"models": TABLE}, "claude-code", "fix") == (None, None)  # not set: harness default
    assert launch.routed_model({"models": TABLE}, "claude-code", None) == (None, None)
    value, problem = launch.routed_model({"models": {"codex": {"work": "opus"}}}, "codex", "work")
    assert value is None and problem  # a name of another harness is never passed on
    assert launch.routed_model({"models": {"codex": {"work": "--yolo"}}}, "codex", "work")[1]


@pytest.mark.parametrize(("harness", "model"), [("codex", "opus"), ("claude-code", "gpt-5"), ("claude-code", "opus-x")])
def test_an_explicit_model_must_be_on_the_harness_list(ready, harness, model):  # noqa: F811
    c = ready.conn()
    _, key = ready.ticket()
    pre = preview(c, ticket=key, harness=harness, mode="work", model=model)
    assert pre["blocked"]["code"] == "launch.invalid" and "model" in pre["blocked"]["message"]
    assert "--model" not in pre["argv"]


def test_a_routed_start_uses_the_model_in_the_argv_the_spec_and_the_record(ready):  # noqa: F811
    wd = ready
    c = wd.conn()
    _, key = wd.ticket()
    plain = preview(c, ticket=key, harness="claude-code", mode="work")
    assert plain["model"] is None and plain["model_source"] == "default" and "--model" not in plain["argv"]
    route(wd)
    pre = preview(c, ticket=key, harness="claude-code", mode="work")
    assert pre["blocked"] is None and pre["model"] == "sonnet" and pre["model_source"] == "workspace"
    assert pre["argv"][:2] == [pre["program"], "--model"] and pre["argv"][2] == "sonnet"
    assert pre["spec"] != plain["spec"]  # the model is part of what the person reviews
    assert preview(c, ticket=key, harness="claude-code", mode="fix")["model"] is None  # a mode not set
    assert preview(c, ticket=key, harness="codex", mode="fix")["model"] == "gpt-5.4"
    run = ok(start(c, ticket=key, harness="claude-code", mode="work", spec=pre["spec"]))["data"]["run"]
    assert run["model"] == "sonnet"
    argv, _ = read_out(wd, "claude")
    assert argv[:2] == ["--model", "sonnet"]


def test_a_persons_own_choice_beats_the_setting_and_is_marked(ready):  # noqa: F811
    wd = ready
    c = wd.conn()
    _, key = wd.ticket()
    route(wd)
    pre = preview(c, ticket=key, harness="claude-code", mode="work", model="haiku")
    assert pre["model"] == "haiku" and pre["model_source"] == "chosen" and pre["argv"][2] == "haiku"


def test_routing_off_means_the_harness_default_and_a_changed_setting_changes_the_spec(ready):  # noqa: F811
    wd = ready
    c = wd.conn()
    _, key = wd.ticket()
    route(wd, on=False)  # the add-on was never turned on: the setting is ignored
    off = preview(c, ticket=key, harness="claude-code", mode="work")
    assert off["model"] is None and "--model" not in off["argv"]
    grant_addon(wd, "model-routing", caps=())
    pre = preview(c, ticket=key, harness="claude-code", mode="work")
    assert pre["model"] == "sonnet"
    route(wd, {"claude-code": {"work": "opus"}}, on=False)  # changed after the person reviewed it
    assert err(start(c, ticket=key, harness="claude-code", mode="work", spec=pre["spec"])) == "launch.changed"


def grant_routing(human, tmp_path):
    """Model routing on, with its settings, the only way there (AE4): the one-review sequence. The built-in manifest
    installs like any other add-on's."""
    from tests.host.test_sequence import Rig
    from tests.host.test_sequence_addon import enable

    rig = Rig(human, tmp_path, enable(human, "model-routing"))
    rig.driver().run(rig.first())


def test_the_setting_is_refused_while_the_add_on_is_off_and_signed_with_the_word_when_on(human, served, tmp_path):
    from orch.click import protocol

    args = {"set": {"models": {"claude-code": {"work": "sonnet"}}}}
    assert herr(human.prepare_res("settings.set", args)) == "invalid.input"
    human.prepare("settings.set", {"set": {"models": None}})  # clearing is allowed while the add-on is off
    grant_routing(human, tmp_path)
    assert herr(human.prepare_res("settings.set", {"set": {"models": None}})) == "invalid.input"  # AE4: while on, not
    assert herr(human.prepare_res("settings.set", {"set": {"models": {"codex": {}}}})) == "invalid.input"
    prepared, _, signed = human.run("settings.set", args)
    assert signed.events[0]["set"] == args["set"]
    assert protocol.op_class("settings.set", args=args) == "click"  # a pick from a closed list, no authority change (run 8, 1.6)
    got = ok(served.client().req("workspace.settings"))["data"]
    assert got["models"] == {"claude-code": {"work": "sonnet"}}


@pytest.mark.parametrize(
    "models",
    [{"claude-code": {"work": "gpt-5"}}, {"codex": {"work": "opus"}}, {"claude-code": {"work": "--yolo"}},
     {"shell": {"work": "opus"}}, {"claude-code": {"nap": "opus"}}, {}, {"claude-code": {"work": None}}],
)  # fmt: skip
def test_the_setting_takes_only_names_of_the_closed_list(human, served, models, tmp_path):
    grant_routing(human, tmp_path)
    assert herr(human.prepare_res("settings.set", {"set": {"models": models}})) == "invalid.input"


def test_the_review_says_the_models_in_words_and_the_unset_ones_as_default(human, served, tmp_path):
    grant_routing(human, tmp_path)
    human.run("settings.set", {"set": {"models": {"claude-code": {"work": "sonnet"}}}})
    p = human.prepare("settings.set", {"set": {"models": {"claude-code": {"work": "opus"}, "codex": {"fix": "o3"}}}})
    rows = {r["field"]: r for r in signcontent.build(p, verified=True)["fields_changed"]}
    assert rows["Claude: Do the work"]["after"] == "Opus" and rows["Claude: Do the work"]["before"] == "Sonnet"
    assert rows["Codex: Fix what was sent back"]["after"] == "o3"
    assert rows["Claude: Read and answer"]["after"] == "Harness default"
    assert len(rows) == 10


def test_the_runner_takes_a_model_flag_only_from_the_harness_closed_list():
    import pytest as _pt

    from orch.runner.config import RunnerConfig
    from orch.runner.link import LOGIN_EXEC, SpawnError, registry_argv

    own = {"SHELL": "/bin/sh"}
    cfg = RunnerConfig()
    login = ["$SHELL", "-l", "-c", LOGIN_EXEC]
    assert registry_argv([*login, "claude", "--model", "sonnet", "go"], own, cfg)[-3:] == ["--model", "sonnet", "go"]
    assert registry_argv([*login, "codex", "--model", "o3", "go"], own, cfg)
    assert registry_argv([*login, "claude", "go"], own, cfg)  # no model: the agent default
    for bad in (["claude", "--model", "gpt-5"], ["codex", "--model", "opus"], ["claude", "--model", "--yolo", "x"],
                ["claude", "go", "--model", "opus"], ["claude", "--model"]):  # fmt: skip
        with _pt.raises(SpawnError):
            registry_argv([*login, *bad], own, cfg)


def test_a_stored_before_that_is_not_a_plain_table_falls_back_to_the_generic_row():
    out: dict = {}

    class Ctx:
        def note(self, _):
            return None

    assert (
        signcontent._models_rows(out, {"claude-code": {"work": "opus"}}, {"claude-code": {"work": "--x"}}, Ctx())
        is False
    )
    assert signcontent._models_rows(out, {"claude-code": {"work": "opus"}}, "junk", Ctx()) is False
    assert signcontent._models_rows(out, {"claude-code": {"work": "opus"}}, None, Ctx()) is True
