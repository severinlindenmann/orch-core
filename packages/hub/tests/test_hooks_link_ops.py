"""#1285: the four hook ops over a real link: scoping to the link's workspace, the off switch, the refusals."""

import pytest

from orch_hub.contract import vectorgen as G
from orch_hub.hooks.inbox import HookInbox
from orch_hub.hooks.store import HookStore
from orch_hub.link import LinkError

SECRET = "S" * 43


def th(i):
    return f"{i:064x}"


def put_body(i=1, **kw):
    b = {"token_hash": th(i), "followup": "FU-1", "source": "github", "secret": SECRET}
    b["expires_ms"] = G.NOW + 3_600_000
    b.update(kw)
    return b


def two(make_hub, on=True):
    store = HookStore()
    inbox = HookInbox(store, lambda: G.NOW, "https://hub.example/h/" if on else None)
    h = make_hub(hooks=inbox)
    a = h.open(first=True)
    a.request("join", h.join_body())
    b = h.open(G.WS_B, "wsk_b", first=True)
    b.request("join", h.join_body("invB", G.WS_B, wsk="wsk_b"))
    return h, store, inbox, a, b


def refused(c, op, body, code):
    with pytest.raises(LinkError) as e:
        c.request(op, body)
    assert e.value.code == code


def test_put_pull_ack_roundtrip(make_hub):
    _h, store, _i, a, _b = two(make_hub)
    assert a.request("hook_put", put_body()) == {"url_base": "https://hub.example/h/"}
    assert a.request("hook_pull", {"since": 0}) == {"rings": [], "head": 0}
    store.ring(store.lookup(th(1), G.NOW), G.NOW)
    r = a.request("hook_pull", {"since": 0})
    assert [x["token_hash"] for x in r["rings"]] == [th(1)] and r["head"] == 1
    assert a.request("hook_ack", {"seq": 1}) == {"acked": 1}
    assert a.request("hook_pull", {"since": 0})["rings"] == []


def test_workspace_b_cannot_see_ack_or_revoke_a(make_hub):
    _h, store, _i, a, b = two(make_hub)
    a.request("hook_put", put_body())
    store.ring(store.lookup(th(1), G.NOW), G.NOW)
    assert b.request("hook_pull", {"since": 0})["rings"] == []
    b.request("hook_ack", {"seq": 1_000_000})
    b.request("hook_revoke", {"token_hash": th(1)})
    r = a.request("hook_pull", {"since": 0})
    assert [x["token_hash"] for x in r["rings"]] == [th(1)]
    assert store.lookup(th(1), G.NOW) is not None
    refused(b, "hook_put", put_body(1), "hook_exists")  # a hash another workspace holds


def test_refusals(make_hub):
    _h, _s, _i, a, _b = two(make_hub)
    refused(a, "hook_put", put_body(expires_ms=G.NOW), "bad_expiry")
    refused(a, "hook_put", put_body(replaces=th(1)), "malformed")  # replaces itself
    for i in range(1, 65):
        a.request("hook_put", put_body(i))
    refused(a, "hook_put", put_body(99), "too_many_hooks")


def test_off_without_a_public_url_or_inbox(make_hub):
    _h, _s, _i, a, _b = two(make_hub, on=False)
    refused(a, "hook_put", put_body(), "unavailable")
    refused(a, "hook_pull", {"since": 0}, "unavailable")
    h = make_hub()  # no inbox object at all
    c = h.open(first=True)
    c.request("join", h.join_body())
    refused(c, "hook_ack", {"seq": 1}, "unavailable")


def test_unjoined_link_cannot_use_the_ops(make_hub):
    h = make_hub(hooks=HookInbox(HookStore(), lambda: G.NOW, "https://hub.example/h/"))
    c = h.open(first=True)
    refused(c, "hook_pull", {"since": 0}, "unknown_workspace")


@pytest.mark.parametrize("bad", [{"extra": 1}, {"source": "gitlab"}, {"secret": "short"}, {"followup": "a/b"}])
def test_bad_body_is_refused_at_the_table(make_hub, bad):
    from orch_hub.link.tls import LinkClosed

    _h, store, _i, a, _b = two(make_hub)
    with pytest.raises((LinkError, LinkClosed)):  # a malformed frame may end the link, as for every other op
        a.request("hook_put", {**put_body(), **bad})
    assert store.lookup(th(1), G.NOW) is None
