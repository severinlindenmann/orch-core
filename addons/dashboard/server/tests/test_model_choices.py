"""The model of a start, the server side: the preview projection and the static choices route. Who chooses the model
(an add-on that holds choose_model) is core's: tests/host/test_launch_routing.py."""

from orch_dashboard.adapters import runs as project


def test_the_preview_shows_the_model_and_who_chose_it():
    got = project.agent_preview({"model": "sonnet", "model_source": "addon", "model_by": "Model routing", "cwd": {}})
    assert got["model"] == "sonnet" and got["model_source"] == "addon" and got["model_by"] == "Model routing"
    odd = project.agent_preview({"model": "bad name", "model_source": "somebody", "model_by": "x", "cwd": {}})
    assert odd["model"] is None and odd["model_source"] is None and odd["model_by"] is None
    old = project.agent_preview({"model": "sonnet", "model_source": "workspace", "cwd": {}})
    assert old["model_source"] is None  # the signed table is gone: no such source
    chosen = project.agent_preview(
        {"model": "sonnet", "model_source": "chosen", "model_by": "Model routing", "cwd": {}}
    )
    assert chosen["model_by"] is None  # only an add-on's choice names the add-on


def test_the_choices_route_serves_core_s_closed_lists_in_plain_words(client, token, stub, monkeypatch, gateway):
    from conftest import ORIGIN

    monkeypatch.setattr(gateway, "enabled", lambda ws: object())
    res = client.get(f"/api/w/{stub.ws}/model-routing", headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN})
    assert res.status_code == 200, res.text
    got = res.json()
    claude = next(h for h in got["harnesses"] if h["id"] == "claude-code")
    assert claude["label"] == "Claude" and [m["id"] for m in claude["models"]] == ["fable", "opus", "sonnet", "haiku"]
    assert "modes" not in got
