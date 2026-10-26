"""Opus review of orch-core#1046 (D15b core), tag l2-d15bH: a hub answer to a projection refresh must not buy a
full-log resend past the C14 per-log resend limit (L2-C14-3)."""

from __future__ import annotations

from typing import Any

from orch.hubclient import HubLink, HubRefused, sync_logs

from .test_full_join import WS, mk, sign_wsk  # noqa: F401 - mk is a fixture
from .test_log_sync import TKT, Sender, full  # noqa: F401 - full is a fixture
from .test_log_sync_proj import ProjLogs
from .vec import V, pub


def _run(s: Sender, call_wrap, **over: Any):  # noqa: ANN001
    h = s.host
    link = HubLink(h.wire, hub_id=h.state.hub_id, workspace_id=WS, hk_op_pub=pub("hk_op"), sign=sign_wsk,
                   mode=h.state.mode, now_ms=lambda: V["now_ms"])  # fmt: skip
    kw = dict(state=h.state, workspace_id=WS, call=call_wrap(link.call), source=s.logs, view_key=h.epochs.view_key,
              position=s.pos, save=lambda p: None)  # fmt: skip
    return sync_logs(**{**kw, **over})


def test_a_view_gap_on_a_refresh_does_not_bypass_the_resend_limit(full):  # noqa: F811
    logs = ProjLogs()
    logs.add("workspace")
    for _ in range(5):
        logs.add(TKT)
    s = Sender(full, logs)
    s.run()
    asked: list[str] = []

    def deny(log: str) -> bool:
        asked.append(log)
        return False

    def gap_on_refresh(call):  # noqa: ANN001 - a hub that answers view_gap to every one-line ticket overlap
        def c(op: str, body: dict[str, Any]) -> Any:
            if op == "log_sync" and body["log"] == TKT and body["count"] == 1 and body["from_seq"] == 5:
                raise HubRefused("view_gap")
            return call(op, body)

        return c

    sent_before = len(s.pages())
    logs.status = "done"  # P changes, no new line: a refresh
    try:
        _run(s, gap_on_refresh, allow_resend=deny)
        _run(s, gap_on_refresh, allow_resend=deny)
    except HubRefused as e:
        assert e.code == "resend_limited"
    full_resends = [p for p in s.pages()[sent_before:] if p["log"] == TKT and p["from_seq"] == 1]
    assert full_resends == [] and asked == [TKT]  # today: the whole log goes again and allow_resend is never asked
