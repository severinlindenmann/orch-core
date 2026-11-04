"""Closed target and literal title validation at the command boundary."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orch.ops.commands.schedule_add import handle
from orch.ops.errors import OrchError


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "ref is required"),
        ({"target": "other"}, "target must"),
        ({"target": None}, "target must"),
        ({"title": "Mail"}, "title is only"),
        ({"target": "new"}, "title must"),
        *[
            ({"target": "new", "title": t}, "title must")
            for t in ("", "x" * 101, "a\nb", "a\tb", "a\u200bb", "a\u202eb")
        ],
        ({"target": "new", "title": "Mail", "ref": "1"}, "ref is only"),
    ],
)
def test_schedule_shape_handler(args, message):
    with patch("orch.ops.commands.schedule_add.Human", return_value=SimpleNamespace(call=object())):
        with pytest.raises(OrchError, match=message):
            handle(None, args)


@pytest.mark.parametrize(
    "args",
    [{"target": "other"}, {"target": None}, {"target": "new"}, {"title": "Mail"}, {"target": "new", "title": "a\tb"}],
)
def test_schedule_input_denial(args):
    from orch.ops.commands.schedule_add import OP
    from orch.ops.inputs import check_input

    with pytest.raises(OrchError, match="schedule.denied"):
        check_input(OP, {"at": "09:00", **args})


def test_new_schedule_direct_action_creates_anchor_and_validates_before_writing(hws, me):
    from datetime import UTC, datetime

    hws.store.append(
        hws.person_event(
            hws.owner,
            "workspace",
            "addon.granted",
            name="schedules",
            version="0.1.0",
            package_sha256="sha256:" + "0" * 64,
            capabilities=[],
            fields={},
            sections=[],
            artifact_kinds=[],
        ),
        log="workspace",
    )
    args = ("schedule", "add", "--target", "new", "--title", "Mail {literal}", "--brief", "Check mail")
    bad = me.j(*args, "--at", "invalid")
    assert bad.err_code == "schedule.denied"
    assert not hws.store.state._core.tickets
    at = datetime.fromtimestamp(hws.clock[0] + 7200, UTC).strftime("%Y-%m-%d %H:%M")
    result = me.j(*args, "--at", at)
    assert result.code == 0, result.out
    anchor = hws.view(result.doc["key"])
    assert anchor.title == "Mail {literal}" and anchor.status == "open" and anchor.type == "chore"
    assert anchor.schedules[0]["id"] == result.data["id"]
    assert "creates a NEW ticket every run" in hws.reviews[-1]
