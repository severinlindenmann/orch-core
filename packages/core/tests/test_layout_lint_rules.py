"""What the store-layout lint counts: path text yes, mapping keys no (see test_layout_lint.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_layout_lint import count

NOT_COUNTED = {
    "subscript": 'v = full["events"]\n',
    "nested subscript": 'v = shown[0]["events"][0]["text"]\n',
    "dict literal key": 'd = {"events": 1, "tickets": 2}\n',
    "dict key inside a call": 'f({"addons": []})\n',
    "get": 'v = m.get("events")\n',
    "pop": 'v = m.pop("events", None)\n',
    "setdefault": 'v = m.setdefault("events", [])\n',
}
COUNTED = {
    "slash operator": 'p = root / "events"\n',
    "slash on the left": 'p = "events" / root\n',
    "Path()": 'p = Path("tickets")\n',
    "os.path.join": 'p = os.path.join(x, "events")\n',
    "joinpath": 'p = x.joinpath("events")\n',
    "f-string path": 'p = f"{root}/events"\n',
    "literal path": 'p = "tickets/x"\n',
    "assignment": 'x = "events"\n',
    "other call argument": 'g("events")\n',
    "comparison": 'ok = name == "events"\n',
    "value of a dict entry": 'd = {"k": "events"}\n',
}


@pytest.mark.parametrize("src", NOT_COUNTED.values(), ids=NOT_COUNTED.keys())
def test_mapping_keys_are_not_counted(tmp_path: Path, src: str) -> None:
    f = tmp_path / "m.py"
    f.write_text(src)
    assert count(f) == 0


@pytest.mark.parametrize("src", COUNTED.values(), ids=COUNTED.keys())
def test_path_text_is_counted(tmp_path: Path, src: str) -> None:
    f = tmp_path / "m.py"
    f.write_text(src)
    assert count(f) == 1
