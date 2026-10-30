"""The local chain and the admin-only unified read model."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from webapi_helpers import WS, Web

from orch_hub.activity import ACTIVITY_TYPES, ActivityLog


def test_chain_persists_and_detects_tampering(tmp_path):
    path = tmp_path / "activity.db"
    log = ActivityLog(path, now_ms=lambda: 123)
    assert log.verify()
    for typ in ACTIVITY_TYPES:
        log.record(typ, actor_id="person", detail={"count": 1})
    assert log.verify()
    assert log.list()[0]["seq"] == len(ACTIVITY_TYPES)
    assert log.list(before_seq=3, limit=1)[0]["seq"] == 2
    log.close()
    assert path.stat().st_mode & 0o777 == 0o600
    log = ActivityLog(path)
    assert log.verify()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE event SET actor_id='someone else' WHERE seq=2")
    assert not log.verify()
    log.close()


def test_closed_types_bounded_fields_and_no_secret_details():
    log = ActivityLog()
    try:
        for kwargs in (
            {"type": "new.type"},
            {"type": "invite.created", "actor_id": "x" * 257},
            {"type": "invite.created", "detail": {"password": "secret"}},
            {"type": "invite.created", "detail": {"nested": {"token": "secret"}}},
            {"type": "invite.created", "detail": {"channel_id": "x" * 257}},
            {"type": "invite.created", "actor_kind": "agent", "actor_id": "person", "actor_session": "s"},
            {
                "type": "invite.created",
                "detail": {
                    k: '"' * 256
                    for k in (
                        "channel_id",
                        "change_id",
                        "connection_id",
                        "invite_id",
                        "pairing_id",
                        "requested_by_agent_session",
                        "approved_by",
                    )
                }
                | {"count": 999999999999999999},
            },
        ):
            with pytest.raises(ValueError):
                log.record(**kwargs)
        assert log.list() == []
        assert log.verify()
    finally:
        log.close()


def test_concurrent_writers(tmp_path):
    path = tmp_path / "activity.db"
    logs = [ActivityLog(path), ActivityLog(path)]
    try:
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda i: logs[i % 2].record("user.enabled"), range(20)))
        assert logs[0].verify()
        assert len(logs[0].list()) == 20
    finally:
        for log in logs:
            log.close()


@pytest.fixture
def w(tmp_path):
    web = Web(tmp_path)
    web.api.d.activity = ActivityLog(tmp_path / "activity.db", now_ms=web.clock)
    yield web
    web.api.d.activity.close()
    web.close()


def test_admin_only_and_unified_shape(w):
    assert w.call("GET", "/admin/activity")[0] == 401
    assert w.call("GET", "/admin/activity", token=w.login(w.ann))[0] == 403
    token = w.login(w.admin)
    w.api.d.activity.record("user.disabled", actor_id=w.admin, subject_user=w.ann, workspace_id=WS)
    status, body, _ = w.call("GET", "/admin/activity", token=token)
    assert status == 200
    assert any(r["id"].startswith("a:") for r in body["rows"])
    assert any(r["id"].startswith("e:") for r in body["rows"])
    assert all(
        {"id", "ts_ms", "type", "actor", "text", "workspace_id", "workspace_name"} <= r.keys() for r in body["rows"]
    )
    assert [r["ts_ms"] for r in body["rows"]] == sorted([r["ts_ms"] for r in body["rows"]], reverse=True)
    row = next(r for r in body["rows"] if r["id"] == "e:1")
    assert row["actor"]["name"] == "admin"
    activity = w.api.d.activity
    w.api.d.activity = None
    assert all(r["id"].startswith("a:") for r in w.call("GET", "/admin/activity", token=token)[1]["rows"])
    w.api.d.activity = activity


def test_agent_identity_filters_and_cursor(w):
    token = w.login(w.admin)
    log = w.api.d.activity
    log.record(
        "change.proposed",
        actor_kind="agent",
        actor_session=w.ann,
        workspace_id=WS,
        detail={"requested_by_agent_session": w.ann, "approved_by": w.admin},
    )
    log.record("user.disabled", actor_id=w.admin, subject_user=w.ann)

    def get(query=""):
        status, body, _ = w.call("GET", "/admin/activity" + query, token=token)
        assert status == 200, body
        return body

    row = get("?type=change.proposed")["rows"][0]
    assert row["actor"] == {"kind": "agent", "session": w.ann}
    assert row["approved_by"] == "admin"
    assert get("?type=change.proposed&person=" + w.ann)["rows"] == []
    assert get("?type=change.proposed&person=" + w.admin)["rows"] == [row]
    assert get("?type=change.proposed&workspace_id=" + WS)["rows"] == [row]
    assert get("?type=change.proposed&workspace_id=absent")["rows"] == []
    assert get("?since_ms=" + str(row["ts_ms"] + 1))["rows"] == []
    assert get("?type=change.proposed&until_ms=" + str(row["ts_ms"] - 1))["rows"] == []
    assert log.list(person=w.ann)[0]["type"] == "user.disabled"
    expected = get()["rows"]
    seen, cursor = [], None
    while True:
        page = get("?limit=1" + ("&before=" + cursor if cursor else ""))
        seen.extend(page["rows"])
        cursor = page["before"]
        if cursor is None:
            break
    assert seen == expected
    for query in ("?limit=201", "?limit=0", "?type=unknown", "?since_ms=-1", "?since_ms=2&until_ms=1", "?before=bad"):
        assert w.call("GET", "/admin/activity" + query, token=token)[0] == 400


@pytest.mark.parametrize("admin_route", [False, True])
def test_revoke_producer(w, admin_route):
    owner_token = w.login(w.ann)
    _, channel_id = w.register_channel(owner_token)
    token = w.login(w.admin) if admin_route else owner_token
    path = ("/admin" if admin_route else "") + "/channels/" + channel_id + "/revoke"
    assert w.call("POST", path, {}, token=token)[0] == 200
    row = w.api.d.activity.list()[0]
    assert row["type"] == "channel.revoked"
    assert row["actor_id"] == (w.admin if admin_route else w.ann)
    assert row["subject_user"] == w.ann
    assert row["detail"] == {"channel_id": channel_id}
    assert owner_token not in str(row) and token not in str(row)
    assert w.api.d.activity.verify()


def test_every_activity_type_renders_in_plain_words(w):
    from orch_hub.activity.feed import AUDIT_WORDS, VERB_WORDS, WORDS
    from orch_hub.activity.store import ACTIVITY_TYPES

    token = w.login(w.admin)
    for typ in ACTIVITY_TYPES:
        w.api.d.activity.record(typ, actor_id=w.admin, subject_user=w.ann)
    body = w.call("GET", "/admin/activity?limit=200", token=token)[1]
    texts = {r["type"]: r["text"] for r in body["rows"] if r["id"].startswith("e:")}
    assert set(texts) == set(ACTIVITY_TYPES)
    assert all(texts[t] == WORDS[t] and "." not in texts[t] for t in ACTIVITY_TYPES)
    assert set(WORDS) == set(ACTIVITY_TYPES)  # an explicit table: a new type cannot be mislabelled
    assert body["type_words"]["custodial"] == AUDIT_WORDS["custodial"] and body["type_words"]["user.disabled"]
    assert all("." not in v for v in VERB_WORDS.values())


def test_a_failed_revoke_record_does_not_turn_the_revoke_into_an_error(w, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(w.api.d.activity, "record", boom)
    owner_token = w.login(w.ann)
    _, channel_id = w.register_channel(owner_token)
    assert w.call("POST", f"/admin/channels/{channel_id}/revoke", {}, token=w.login(w.admin))[0] == 200
    assert w.env.svc.channel_state(w.ann, channel_id) == "revoked"


# --- HUB-AC-03 (#1426): agent events from a workspace (link op agent_events) ------------------------------------------


def _ev(**kw):
    return {"kind": "ticket.updated", "ts_ms": 5, "agent": "sess-1", "ticket": "ACME-12", **kw}


def test_agent_events_reach_the_activity_view_as_agent_rows():
    from conftest import Hub

    from orch_hub.contract import vectorgen as G

    log = ActivityLog(":memory:")
    hub = Hub(activity=log)
    try:
        c = hub.open(first=True)
        c.request("join", hub.join_body(mode="full"))
        c.mode = "full"
        assert c.request("agent_events", {"events": [_ev(), _ev(kind="event", ticket="")]}) == {}
        rows = log.list(type="agent.event")
        assert [(r["actor_kind"], r["actor_session"], r["workspace_id"]) for r in rows] == [
            ("agent", "sess-1", G.WS_A)
        ] * 2
        assert {r["detail"]["what"] for r in rows} == {"ticket.updated", "event"}
        assert any(r["detail"].get("ticket") == "ACME-12" for r in rows)
    finally:
        hub.close()


@pytest.mark.parametrize(
    "events",
    [
        [],
        [_ev()] * 51,
        [{**_ev(), "title": "secret plan"}],
        [{k: v for k, v in _ev().items() if k != "ticket"}],
        [_ev(kind="Ticket Updated")],
        [_ev(kind="ticket.something_new")],
        [_ev(kind="x" * 41)],
        [_ev(agent="")],
        [_ev(agent="a b\nc")],
        [_ev(agent="a" * 65)],
        [_ev(ticket="acme-12")],
        [_ev(ticket="ACME-12 fix the login")],
        [_ev(ts_ms=-1)],
        [_ev(ts_ms=True)],
        "x",
    ],
)
def test_agent_events_malformed_is_refused_and_stores_nothing(events):
    from conftest import Hub

    from orch_hub.link import LinkError

    log = ActivityLog(":memory:")
    hub = Hub(activity=log)
    try:
        c = hub.open(first=True)
        c.request("join", hub.join_body(mode="full"))
        c.mode = "full"
        with pytest.raises(LinkError) as error:
            c.request("agent_events", {"events": events})
        assert error.value.code == "malformed"
        assert log.list() == []
    finally:
        hub.close()


def test_agent_events_are_rate_limited_per_workspace_and_need_the_log(monkeypatch):
    from conftest import Hub

    from orch_hub.link import LinkError, ops

    monkeypatch.setattr(ops, "AGENT_EVENTS_PER_HOUR", 3)
    ops._AGENT_SEEN.clear()
    log = ActivityLog(":memory:")
    hub = Hub(activity=log)
    bare = Hub()
    try:
        c = hub.open(first=True)
        c.request("join", hub.join_body(mode="full"))
        c.mode = "full"
        c.request("agent_events", {"events": [_ev()] * 2})
        c.request("agent_events", {"events": [_ev()] * 2})  # one over the cap: dropped, still answered
        assert len(log.list(type="agent.event")) == 3
        d = bare.open(first=True)
        d.request("join", bare.join_body(mode="full"))
        d.mode = "full"
        with pytest.raises(LinkError) as error:
            d.request("agent_events", {"events": [_ev()]})
        assert error.value.code == "unavailable"
    finally:
        hub.close()
        bare.close()


def test_agent_event_rows_say_what_happened_in_plain_words(w):
    token = w.login(w.admin)
    w.api.d.activity.record(
        "agent.event", actor_kind="agent", actor_session="sess-1", workspace_id=WS,
        detail={"what": "ticket.updated", "ticket": "ACME-12"},
    )  # fmt: skip
    w.api.d.activity.record(
        "agent.event", actor_kind="agent", actor_session="sess-1", workspace_id=WS, detail={"what": "odd.kind"}
    )
    status, body, _ = w.call("GET", "/admin/activity?type=agent.event", token=token)
    assert status == 200
    assert sorted(r["text"] for r in body["rows"]) == ["changed a ticket on ACME-12", "did something"]
    assert all(r["actor"] == {"kind": "agent", "session": "sess-1"} for r in body["rows"])
    assert "agent.event" in body["types"]


def test_agent_events_refused_from_a_selective_binding_and_filed_under_the_caller():
    from conftest import Hub

    from orch_hub.contract import vectorgen as G
    from orch_hub.link import LinkError, ops

    ops._AGENT_SEEN.clear()
    log = ActivityLog(":memory:")
    hub = Hub(activity=log)
    try:
        sel = hub.open(first=True)
        sel.request("join", hub.join_body(mode="selective"))
        sel.send_raw(sel.build("agent_events", {"events": [_ev()]}))  # signed as the hub sees a selective link
        with pytest.raises(LinkError) as error:
            sel.recv_reply()
        assert error.value.code == "wrong_mode"
        assert log.list() == []
    finally:
        hub.close()
    log = ActivityLog(":memory:")
    hub = Hub(activity=log)
    try:
        a = hub.open(first=True)
        a.request("join", hub.join_body(mode="full"))
        a.mode = "full"
        with pytest.raises(LinkError):  # a body naming another workspace is not a valid body (and closes the link)
            a.request("agent_events", {"events": [{**_ev(), "workspace_id": G.WS_B}]})
        a = hub.open()
        a.mode = "full"
        a.request("agent_events", {"events": [_ev()]})
        assert {r["workspace_id"] for r in log.list(type="agent.event")} == {G.WS_A}
    finally:
        hub.close()
