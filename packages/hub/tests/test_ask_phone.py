# ruff: noqa: F811
"""HUB-AP05 (#1381): ``POST /workspaces/{ws}/items/{item}/ask-phone``. A hint to the caller's own phones, nothing else."""

from __future__ import annotations

import pytest
from test_phone_routes import (
    phone,
    push_key,
    push_obj,
    put_push,
    w,  # noqa: F401  fixture: a Web with a fake relay and a notifier
)
from webapi_helpers import ORIGIN, WS, WS_OTHER, WS_SEL, Web, shared_item

ITEM = "22" * 16
TICKET = "aa" * 16


def _ask(w: Web, tok, item=ITEM, ws=WS, body=None):
    return w.call("POST", f"/workspaces/{ws}/items/{item}/ask-phone", body, token=tok)


@pytest.fixture
def ready(w):
    """ann has a paired phone with a push key, and one waiting gate in the full workspace."""
    signer, cid, ctok = phone(w, w.ann)
    _, pub = push_key()
    assert put_push(w, cid, ctok, push_obj(w, signer, cid, pub))[0] == 200
    row = {"item_id": ITEM, "kind": "gate", "type": "gate", "title": "Ship", "log": TICKET, "gate": "code"}
    w.view.rows = [row]
    w.view.detail = {**row, "content": {}}
    w.relay.sent.clear()
    w.cid = cid
    return w


def test_ask_sends_one_hint_to_my_phone_and_says_only_how_many(ready):
    w = ready
    s, o, _ = _ask(w, w.login(w.ann))
    assert (s, o) == (200, {"asked": 1})
    w.notifier.drain()
    assert len(w.relay.sent) == 1


def test_no_phone_asks_nobody(w):
    row = {"item_id": ITEM, "kind": "gate", "log": TICKET}
    w.view.rows, w.view.detail = [row], {**row, "content": {}}
    assert _ask(w, w.login(w.ann))[:2] == (200, {"asked": 0})
    assert w.relay.sent == []


def test_the_request_carries_nothing(ready):
    s, o, _ = _ask(ready, ready.login(ready.ann), body={"action": {"type": "gate.approved"}})
    assert (s, o["error"]) == (400, "malformed")


@pytest.mark.parametrize("case", ["not_in_workspace", "other_ws", "missing", "not_pending", "summary_only"])
def test_every_unknown_case_is_the_same_unknown_item(ready, case):
    w = ready
    if case == "not_in_workspace":
        tok = w.login(w.env.user("bob"))
        got = _ask(w, tok)
    elif case == "other_ws":
        got = _ask(w, w.login(w.ann), ws=WS_OTHER)
    elif case == "missing":
        w.view.detail = None
        got = _ask(w, w.login(w.ann))
    elif case == "not_pending":
        w.view.rows = []
        got = _ask(w, w.login(w.ann))
    else:
        w.reg.add(WS_SEL, "selective", owner=w.ann)
        w.shares.items[(w.ann, WS_SEL)] = [shared_item("11" * 16, "needs_you", "Pick")]
        got = _ask(w, w.login(w.ann), item="11" * 16, ws=WS_SEL)
    assert got[:2] == (404, {"error": "unknown_item"})
    w.notifier.drain()
    assert w.relay.sent == []


def test_only_a_browser_cookie_session_may_ask(ready):
    w = ready
    path = f"/workspaces/{WS}/items/{ITEM}/ask-phone"
    assert _ask(w, w.login(w.ann, "app"))[0] == 403  # a pairing session
    assert w.call("POST", path, None, token=w.login(w.ann), cookie=False)[0] in (401, 403)  # cookie session as a bearer
    _, _, ctok = phone(w, w.env.user("carl"))
    assert _ask(w, ctok)[0] == 403  # a phone (channel) session
    tok = w.login(w.ann)
    assert w.call("POST", path, None, token=tok, csrf=False)[0] in (400, 403)
    assert w.call("POST", path, None, token=tok, origin="http://evil.example")[0] in (400, 403)
    assert w.call("POST", path, None)[0] in (401, 403)  # nobody


def test_rate_limits_are_per_user_and_per_item(ready):
    w = ready
    tok = w.login(w.ann)
    assert _ask(w, tok)[0] == 200
    assert _ask(w, tok)[:2] == (429, {"error": "rate_limited"})  # same item within 10 s
    w.clock.advance(11_000)
    assert _ask(w, tok)[0] == 200
    # per user: 6 a minute across items (every item asked once; an unknown one still counts against the user)
    codes = []
    for i in range(8):
        w.clock.advance(11_000)
        codes.append(_ask(w, tok, item=f"{i:02x}" * 16)[0])
    assert 429 in codes


def test_a_switch_session_is_refused_and_no_hint_is_sent(ready):
    from orch_hub.switch import SwitchService

    w = ready
    w.api.d.switch = SwitchService(hub_origin=lambda: ORIGIN, hub_name="Acme", now_ms=w.clock)
    _, _, h = w.call("POST", "/switch/redeem", {"t": w.api.d.switch.issue_to_hub(w.ann, WS, "/")})
    tok = h["Set-Cookie"].split(";")[0].split("=", 1)[1]
    w.browser_tokens.add(tok)
    s, o, _ = _ask(w, tok)
    assert (s, o["error"]) == (403, "reauth_required")
    w.notifier.drain()
    assert w.relay.sent == []


def test_a_revoked_phone_is_not_asked(ready):
    w = ready
    s, _, _ = w.call("POST", f"/admin/channels/{w.cid}/revoke", {}, token=w.login(w.admin))
    assert s == 200
    w.notifier.drain()
    w.relay.sent.clear()
    assert _ask(w, w.login(w.ann))[:2] == (200, {"asked": 0})
    w.notifier.drain()
    assert w.relay.sent == []
