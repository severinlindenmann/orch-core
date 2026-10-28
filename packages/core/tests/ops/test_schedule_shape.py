"""The new schedule shape stays disabled at authoring until anchor creation exists."""

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
        ({"target": "new", "title": "Mail"}, "new-ticket schedules are not switched on yet"),
        ({"target": "new", "title": "x" * 100}, "new-ticket schedules are not switched on yet"),
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
