"""Opus review G6 B2 (#1199): `settings.models` is gone everywhere; an old signed `models` event fails replay cleanly
(V3-D36, breaking accepted). T0: uv run pytest tests/model/test_g6b2_review_models_gone.py -x -q"""

from __future__ import annotations

import pytest

from orch import schema
from orch.model import admit
from tests.model.world import World

OLD = {"models": {"claude-code": {"work": "sonnet"}}}


def test_admit_refuses_a_models_setting():
    w = World().bootstrap({"tom": "member"})
    e = w.build_unappended("workspace", "settings.changed", "sev", set=OLD)
    assert getattr(admit(w.state(), e, log="workspace"), "code", None) is not None


def test_the_event_schema_refuses_models():
    w = World().bootstrap({"tom": "member"})
    e = w.build_unappended("workspace", "settings.changed", "sev", set=OLD)
    with pytest.raises(Exception):  # noqa: B017
        schema.validate("event.settings.changed", e, log="workspace")


def test_replay_of_an_old_signed_models_event_fails_cleanly():
    w = World(validate=False).bootstrap({"tom": "member"})
    w.wev("settings.changed", "sev", set=OLD)
    try:
        st = w.state()
    except Exception as exc:  # a typed refusal is fine; a crash type is not
        assert type(exc).__name__ not in ("KeyError", "TypeError", "AttributeError"), repr(exc)
        return
    (bad,) = st.chain_errors  # the log stops at the old event: chain.broken with the schema reason, no crash
    assert bad.code == "chain.broken" and "'models' was unexpected" in bad.detail
    assert "models" not in st.workspace.settings


def test_no_signed_path_knows_models():
    from orch.click import protocol
    from orch.host import signer
    from orch.ops import person_ops

    assert "models" not in signer._SETTINGS_KEYS
    assert "models" not in person_ops._SET["properties"]
    assert person_ops._SET.get("additionalProperties") is False
    assert "models" not in protocol.SETTINGS_TERMINAL_KEYS
    ws = schema.load("workspace") if hasattr(schema, "load") else None
    if ws is not None:
        assert "models" not in str(ws)
