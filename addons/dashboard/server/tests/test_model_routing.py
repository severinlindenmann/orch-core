"""Model routing, the server side: the typed `models_set` action (closed lists), the preview projection and the
static choices route. The host side is in orch-core's tests/host/test_model_routing.py."""

import pytest
from pydantic import TypeAdapter, ValidationError

from orch_dashboard.adapters import runs as project
from orch_dashboard.adapters import settings as reduced
from orch_dashboard.handoff import ModelsSet, PrepareBody, expected, host_call


def parse(body):
    return TypeAdapter(PrepareBody).validate_python(body)


def test_models_set_is_one_signed_settings_set_with_the_closed_table():
    got = parse({"action": "models_set", "models": {"codex": {"fix": "gpt-5.4"}, "claude-code": {"work": "sonnet"}}})
    assert isinstance(got, ModelsSet)
    assert host_call(got) == (
        "settings.set",
        {"set": {"models": {"claude-code": {"work": "sonnet"}, "codex": {"fix": "gpt-5.4"}}}},
    )
    clear = parse({"action": "models_set", "models": {}})
    assert host_call(clear) == ("settings.set", {"set": {"models": None}})
    assert host_call(parse({"action": "models_set", "models": {"codex": {}}})) == (
        "settings.set",
        {"set": {"models": None}},
    )


@pytest.mark.parametrize(
    "models",
    [
        {"claude-code": {"work": "gpt-5"}},
        {"codex": {"work": "opus"}},
        {"claude-code": {"work": "--dangerously-skip-permissions"}},
        {"claude-code": {"work": "opus extra"}},
        {"shell": {"work": "opus"}},
        {"claude-code": {"nap": "opus"}},
    ],
)
def test_a_name_outside_the_closed_lists_is_refused_before_the_host_sees_it(models):
    with pytest.raises(ValidationError):
        parse({"action": "models_set", "models": models})


def test_the_prepared_event_must_be_exactly_that_table():
    action = parse({"action": "models_set", "models": {"claude-code": {"work": "sonnet"}}})
    check = expected(action)
    assert check([{"type": "settings.changed", "set": {"models": {"claude-code": {"work": "sonnet"}}}}])
    assert not check([{"type": "settings.changed", "set": {"models": {"claude-code": {"work": "opus"}}}}])
    assert not check(
        [{"type": "settings.changed", "set": {"models": {"claude-code": {"work": "sonnet"}}, "grant_hours": 1}}]
    )
    assert not check([{"type": "settings.changed", "set": {"publish": None}}])


def test_the_preview_shows_the_model_and_who_chose_it():
    got = project.agent_preview({"model": "sonnet", "model_source": "workspace", "cwd": {}})
    assert got["model"] == "sonnet" and got["model_source"] == "workspace"
    odd = project.agent_preview({"model": "bad name", "model_source": "somebody", "cwd": {}})
    assert odd["model"] is None and odd["model_source"] is None


def test_the_settings_reader_takes_only_the_closed_table():
    assert reduced.models_table(None) is None
    assert reduced.models_table({"codex": {"work": "o3"}}) == {"codex": {"work": "o3"}}
    for bad in ({"codex": {"work": "opus"}}, {"x": {}}, {"codex": {"nap": "o3"}}, [], {"codex": "o3"}):
        with pytest.raises(Exception):  # noqa: B017 - the server's own "host does not match its contract" error
            reduced.models_table(bad)


def test_the_choices_route_serves_core_s_closed_lists_in_plain_words(client, token, stub, monkeypatch, gateway):
    from conftest import ORIGIN

    monkeypatch.setattr(gateway, "enabled", lambda ws: object())
    res = client.get(f"/api/w/{stub.ws}/model-routing", headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN})
    assert res.status_code == 200, res.text
    got = res.json()
    claude = next(h for h in got["harnesses"] if h["id"] == "claude-code")
    assert claude["label"] == "Claude" and [m["id"] for m in claude["models"]] == ["fable", "opus", "sonnet", "haiku"]
    assert [m["label"] for m in got["modes"]] == [
        "Do the work",
        "Write requirements and plan",
        "Fix what was sent back",
        "Continue",
        "Read and answer",
    ]
