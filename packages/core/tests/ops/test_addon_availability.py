"""AP D5 steps 1 and 1b: an operation of an add-on runs and is listed only while the add-on is on (``addon.off``),
and ``orch set REF <addon>.<field>=VALUE`` / ``orch describe <addon>.<field>`` work from the add-on's grant."""

from __future__ import annotations

import pytest

from orch.ops import all as all_ops
from tests.ops.test_task_ac import claimed

FIELDS = {
    "points": {"type": "integer", "min": 0, "max": 100, "set_by": ["owner", "agent"]},
    "note": {"type": "string", "max_len": 20, "set_by": ["agent"]},
    "mood": {"type": "enum", "values": ["good", "bad"], "set_by": ["owner"]},
    "done": {"type": "boolean", "set_by": ["agent"]},
}


def grant(ws, name, fields=None, **kw):
    ws.store.append(
        ws.person_event(
            ws.owner,
            "workspace",
            "addon.granted",
            name=name,
            version="0.1.0",
            package_sha256="sha256:" + "0" * 64,
            capabilities=[],
            fields=fields or {},
            sections=[],
            artifact_kinds=[],
            **kw,
        ),
        log="workspace",
    )


def turn_off(ws, name):
    ws.store.append(ws.person_event(ws.owner, "workspace", "addon.disabled", name=name), log="workspace")


def test_the_addon_operations_are_declared(ws):
    got = {op.name: op.addon for op in all_ops() if op.addon}
    assert got == {
        "quick.list": "quick",
        "follow_up": "schedules",
        "follow_up.list": "schedules",
        "follow_up.show": "schedules",
        "follow_up.cancel": "schedules",
        "webhook.new": "schedules",
    }
    for op in all_ops():
        if op.addon:
            assert "addon.off" in {e["code"] for e in op.errors}


def test_addon_off_on_a_quick_op_and_a_follow_up_op(ws, cli):
    claimed(cli)
    for argv in (("quick", "list"), ("follow-up", "--in", "2d", "--brief", "x"), ("webhook", "new", "--label", "x")):
        r = cli.j(*argv)
        assert r.err_code == "addon.off", (argv, r.out)
        assert "off in this workspace" in r.out and "do not work around it" in r.out
    assert cli.j("quick", "list", "--limit", "5").doc["error"]["retryable"] is False
    grant(ws, "quick")
    assert cli("quick", "list").code == 0
    assert cli.j("follow-up", "list").err_code == "addon.off"  # schedules is still off
    turn_off(ws, "quick")
    assert cli.j("quick", "list", "--limit", "6").err_code == "addon.off"


def test_a_new_quick_task_needs_the_addon_through_the_same_code(ws, cli):
    assert cli.j("new", "x", "--label", "quick").err_code == "addon.off"
    cli("new", "plain")
    assert cli.j("set", "1", "labels=quick").err_code == "addon.off"
    assert cli.j("quick", "add", "small").err_code == "addon.off"


def test_help_and_describe_hide_and_show_addon_commands(ws, cli):
    assert "follow-up" not in cli("describe").out and "quick list" not in cli("describe").out
    assert "Add-on commands" not in cli("describe").out
    assert "wait:" not in cli("help").out  # every step of that workflow belongs to Schedules
    assert cli.j("help", "wait").err_code == "addon.off"
    assert cli.j("describe", "follow-up").err_code == "addon.off"
    assert cli.j("describe", "follow-up", "list").err_code == "addon.off"
    assert cli.j("describe", "quick.list").err_code == "addon.off"
    assert {c["name"] for c in cli.j("describe").data["commands"]}.isdisjoint({"follow_up", "quick.list"})

    grant(ws, "schedules")
    out = cli("help").out
    assert "wait:" in out and "Add-on commands: follow-up" in out and "quick" not in out
    assert "follow-up cancel|show|list" in cli("describe").out and "quick list" not in cli("describe").out
    assert cli("help", "wait").code == 0
    assert "webhook.new" in cli("help", "wait").out or "orch webhook new" in cli("help", "wait").out
    assert cli("describe", "follow-up").code == 0
    assert cli("describe", "follow-up", "list").code == 0

    grant(ws, "quick")
    assert "quick list" in cli("describe").out
    turn_off(ws, "schedules")
    assert "follow-up" not in cli("describe").out


def test_without_a_workspace_everything_is_listed(tmp_path):
    import io

    from orch.cli.main import main

    out = io.StringIO()
    env = {"HOME": str(tmp_path), "ORCH_STATE_DIR": str(tmp_path / "st")}
    assert main(["describe"], env=env, stdout=out, stderr=io.StringIO()) == 0
    assert "follow-up" in out.getvalue() and "quick list" in out.getvalue()
    assert "Add-on commands" not in out.getvalue()


def field_addon(ws, cli):
    grant(ws, "echo", FIELDS)
    claimed(cli)


def shown(ws, name, fname):
    return ws.ticket_json("1")["addons"].get(name, {}).get(fname)


def test_set_an_addon_string_and_int_field(ws, cli):
    field_addon(ws, cli)
    r = cli.j("set", "1", "echo.note=hello world")
    assert r.code == 0, r.out
    assert cli.j("set", "1", "echo.points=42", "echo.done=true").code == 0
    assert (shown(ws, "echo", "note"), shown(ws, "echo", "points"), shown(ws, "echo", "done")) == (
        "hello world",
        42,
        True,
    )
    assert cli.j("set", "1", "echo.points=none").code == 0  # none clears
    assert shown(ws, "echo", "points") is None
    r = cli.j("set", "1", "title=Renamed", "echo.points=7")  # core and add-on fields in one call
    assert r.code == 0 and r.data["fields"] == ["title", "echo.points"]


def test_set_refusals(ws, cli):
    field_addon(ws, cli)
    assert cli.j("set", "1", "echo.points=500").err_code == "invalid.input"  # over max
    assert cli.j("set", "1", "echo.points=five").err_code == "invalid.input"
    assert cli.j("set", "1", "echo.note=" + "x" * 21).err_code == "invalid.input"  # over max_len
    assert cli.j("set", "1", "echo.done=yes").err_code == "invalid.input"
    r = cli.j("set", "1", "echo.mood=good")  # set_by is owner only: not the agent
    assert r.err_code == "invalid.input" and "set_by" in r.out, r.out
    r = cli.j("set", "1", "echo.nope=1")
    assert r.err_code == "invalid.input" and "declares no field nope" in r.out
    assert shown(ws, "echo", "mood") is None and shown(ws, "echo", "points") is None
    turn_off(ws, "echo")
    assert cli.j("set", "1", "echo.points=1").err_code == "addon.off"
    assert cli.j("set", "1", "missing.points=1").err_code == "addon.off"


def test_describe_an_addon_field(ws, cli):
    field_addon(ws, cli)
    r = cli.j("describe", "echo.points")
    assert r.code == 0, r.out
    assert r.data["type"] == "integer" and r.data["limits"] == {"min": 0, "max": 100}
    assert r.data["set_by"] == ["agent", "owner"] and r.data["usage"] == "orch set REF echo.points=VALUE"
    text = cli("describe", "echo.mood").out
    assert "type: enum" in text and "set_by: owner" in text and '"good"' in text
    assert cli.j("describe", "echo.nope").err_code == "not_found"
    assert cli.j("describe", "other.field").err_code == "addon.off"
    turn_off(ws, "echo")
    assert cli.j("describe", "echo.points").err_code == "addon.off"


@pytest.mark.parametrize("arg", ["Echo.points=1", "echo.=1", "echo.points", "echo..x=1"])
def test_the_pair_shape_is_checked_before_anything_runs(ws, cli, arg):
    field_addon(ws, cli)
    assert cli.j("set", "1", arg).code != 0
