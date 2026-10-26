# ruff: noqa: F811
"""Opus review G6 B2 (#1199): adversarial probes on routing (memo, grant swap, tamper after a cached answer,
agent caller, title as the shown word). T0: uv run pytest tests/host/test_g6b2_review_routing.py -x -q"""

from __future__ import annotations

import json

from orch.host import routing
from tests.host.dispatch_helpers import SESSION, err
from tests.host.test_agents import preview, read_out, ready, start, w  # noqa: F401
from tests.host.test_launch_routing import (  # noqa: F401
    NAME,
    TABLE,
    _fresh_memo,
    build,
    calls,
    grant,
    pre,
    route,
    settings,
)


def test_memo_hit_still_rechecks_the_active_grant(ready, tmp_path):
    _, log = route(ready, tmp_path)
    assert pre(ready)["model"] == "sonnet" and len(routing._memo) == 1
    ready.person_event("addon.disabled", "workspace", name=NAME)
    got = pre(ready)
    assert got["model"] is None and got["model_source"] == "default"  # a cached answer is not served once off


def test_memo_hit_still_rechecks_the_installed_digest(ready, tmp_path):
    d, log = route(ready, tmp_path)
    assert pre(ready)["model"] == "sonnet"
    (d / "main.py").write_text("raise SystemExit(0)\n")  # same-UID tamper after the answer was cached
    got = pre(ready)
    assert got["blocked"]["code"] == "launch.invalid" and got["model"] != "sonnet"


def test_a_regrant_of_new_code_is_not_served_the_old_answer(ready, tmp_path):
    d, log = route(ready, tmp_path)
    assert pre(ready)["model"] == "sonnet"
    # new version, same name, same settings: answers haiku
    m = json.loads((d / "orch-addon.json").read_text())
    m["version"] = "1.0.1"
    (d / "orch-addon.json").write_text(json.dumps(m))
    (d / "main.py").write_text(
        (d / "main.py")
        .read_text()
        .replace('ANSWER = {"model": s.get(params["harness"], {}).get(params["mode"])}', 'ANSWER = {"model": "haiku"}')
    )
    grant(ready, d)
    got = pre(ready)
    assert got["model"] == "haiku" and got["model_source"] == "addon"


def test_a_cached_answer_does_not_cross_harness_or_mode(ready, tmp_path):
    route(ready, tmp_path)
    assert pre(ready)["model"] == "sonnet"
    c = ready.conn()
    _, key = ready.ticket()
    assert preview(c, ticket=key, harness="claude-code", mode="refine")["model"] == "opus"
    assert preview(c, ticket=key, harness="codex", mode="work")["model"] is None


def test_an_agent_cannot_preview_or_start_so_cannot_trigger_routing(ready, tmp_path):
    _, log = route(ready, tmp_path)
    c = ready.conn()
    _, key = ready.ticket()
    assert (
        err(
            c.call(
                "agent.preview",
                {"ticket": key, "harness": "claude-code", "mode": "work"},
                grant=ready.grant,
                session=SESSION,
            )
        )
        == "op.not_allowed"
    )
    assert calls(log) == []


def test_the_shown_word_is_the_signed_name_not_the_packages_title(ready, tmp_path):
    """Low finding B2-2: the dialog says 'chosen by <title>' and the title is unsigned package text; the spec binds the
    signed name (routed_by) only. A package can title itself e.g. 'you'."""
    d, _ = build(ready, tmp_path)
    m = json.loads((d / "orch-addon.json").read_text())
    m["title"] = "you"
    (d / "orch-addon.json").write_text(json.dumps(m))
    grant(ready, d)
    settings(ready, TABLE)
    got = pre(ready)
    assert got["model_by"] == NAME  # B2-2: the signed name, never the unsigned title
