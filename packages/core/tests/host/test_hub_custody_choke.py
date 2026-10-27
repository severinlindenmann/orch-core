# ruff: noqa: F811
"""A1 (D83): the full-mode refusal of convenience custody (V3 C11) is gone: One click works in every hub mode, so
nothing in the custody choke points may mention a hub mode."""

from __future__ import annotations

import inspect

from orch.custody import select
from orch.host import convenience


def test_no_custody_path_refuses_by_hub_mode():
    for mod in (select, convenience):
        src = inspect.getsource(mod)
        assert "refuse_full_mode" not in src and "sign.hub_only" not in src, mod.__name__
