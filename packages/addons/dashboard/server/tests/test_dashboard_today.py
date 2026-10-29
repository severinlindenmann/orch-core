from conftest import ORIGIN
from golden import check


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_today_needs_authentication(client, stub):
    assert client.get(f"/api/w/{stub.ws}/today").status_code == 401


def test_today_lists_the_viewers_needs_with_ticket_titles(client, token, stub, rich):
    res = client.get(f"/api/w/{stub.ws}/today", headers=headers(token))
    assert res.status_code == 200, res.text
    data = res.json()
    kinds = {(n["kind"], n["ticket"], n["ref"]) for n in data["needs_you"]}
    assert ("question", "DEMO-0001", "Q2") in kinds and ("approval", "DEMO-0001", "requirements") in kinds
    assert all(n["title"] == "Real P1 ticket" for n in data["needs_you"])
    assert all(n["since"] for n in data["needs_you"])  # v1.4 dates them
    assert data["counts"] == {"open": 4} and data["read_only_open"] == []
    # The feed is tickets only and carries nothing of the host's journal: no jpos, ws_seq, log or uid.
    assert data["recent"] and all(r["ticket"] for r in data["recent"])
    assert not {"jpos", "ws_seq", "log", "uid"} & {k for r in data["recent"] for k in r}
    check("today", data)


def test_today_hides_restricted_tickets(client, token, stub, rich):
    env, ids = rich
    from orch import crypto

    from tests.store.helpers import Person

    other = Person()
    env.store.append(
        env.person_event(
            env.owner,
            "workspace",
            "member.added",
            person=other.ref,
            name="Other",
            role="member",
            pk_pub=crypto.b64u(other.pk_pub),
            device_cert=other.cert(created=1_790_000_000_000),
        ),
        log="workspace",
    )
    first = ids["First"]
    env.store.append(
        env.person_event(env.owner, first, "visibility.changed", visibility={"restricted": [other.ref]}), log=first
    )
    data = client.get(f"/api/w/{stub.ws}/today", headers=headers(token)).json()
    assert data["needs_you"] == [] and data["counts"] == {"open": 3}
    assert "Real P1 ticket" not in str(data)


def test_today_has_no_write_method(client, token, stub):
    res = client.post(f"/api/w/{stub.ws}/today", json={}, headers={**headers(token), "Origin": ORIGIN})
    assert res.status_code == 405


# -- terminal_wait: "Agent may need you: check its terminal" (run-9 feedback 3.6) ------------------------------------


def _rows():
    return [
        {
            "key": "DEMO-0001",
            "uid": "u1",
            "title": "A ticket",
            "updated_at": "2026-10-26T09:00:00Z",
            "status": "in_progress",
        }
    ]


def _runs(since="2026-10-26T09:10:00Z"):
    return [{"run": "r_1", "ticket": "DEMO-0001", "waiting": {"since": since, "source": "hook"}}]


def test_a_waiting_run_is_a_terminal_wait_row_with_the_run_id():
    from orch_dashboard.adapters.today import TERMINAL_WAIT, terminal_waits

    got = terminal_waits(_runs(), _rows(), {})
    assert got == [
        {
            "kind": "terminal_wait",
            "ticket": "DEMO-0001",
            "uid": "u1",
            "title": "A ticket",
            "text": TERMINAL_WAIT,
            "ref": "r_1",
            "since": "2026-10-26T09:10:00Z",
            "blocking": False,
        }
    ]


def test_a_real_need_hides_the_terminal_wait_but_a_newer_ticket_event_does_not():
    from orch_dashboard.adapters.today import terminal_waits

    # run-9 J04: the person approved the plan after the agent went idle; the agent is still idle (the host clears
    # `waiting` when it prints again), so the wait stays and the ticket never shows "working"
    assert [w["ref"] for w in terminal_waits(_runs("2026-10-26T08:00:00Z"), _rows(), {})] == ["r_1"]
    assert terminal_waits(_runs(), _rows(), {"u1": [{"kind": "question"}]}) == []  # a real question is open
    assert terminal_waits(_runs(), [], {}) == []  # a ticket the person does not see


def test_terminal_waits_are_listed_but_never_counted(monkeypatch):
    import importlib

    from orch_dashboard.adapters import actionable_count, today

    today_mod = importlib.import_module("orch_dashboard.adapters.today")
    monkeypatch.setattr(today_mod, "summary", lambda *a, **k: {})

    doc = today({"items": [], "sub_cursor": None}, _rows(), "ws", "now", "Owner", "owner", runs=_runs())
    assert [n["kind"] for n in doc["needs_you"]] == ["terminal_wait"]
    assert actionable_count(doc["needs_you"], "owner") == 0


def test_an_idle_agent_is_not_listed_as_working(monkeypatch):
    # run-9 J04: Today's "working" list never shows a ticket whose agent waits idle at its prompt
    import importlib

    from orch_dashboard.adapters import today

    today_mod = importlib.import_module("orch_dashboard.adapters.today")
    monkeypatch.setattr(today_mod, "summary", lambda r, *a, **k: {"uid": r["uid"]})
    busy = today({"items": [], "sub_cursor": None}, _rows(), "ws", "now", "Owner", "owner", runs=[])
    assert busy["working"] == [{"uid": "u1"}]
    idle = today({"items": [], "sub_cursor": None}, _rows(), "ws", "now", "Owner", "owner", runs=_runs())
    assert idle["working"] == [] and [n["kind"] for n in idle["needs_you"]] == ["terminal_wait"]
