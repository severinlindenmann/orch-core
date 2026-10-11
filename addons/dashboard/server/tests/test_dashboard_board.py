import pytest
from conftest import ORIGIN
from golden import check


def headers(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("route", ["tickets", "board"])
def test_lists_need_authentication(client, stub, route):
    assert client.get(f"/api/w/{stub.ws}/{route}").status_code == 401


@pytest.mark.parametrize(
    "query",
    [
        "limit=201",
        "limit=0",
        "sort=status",
        "order=up",
        "status=nope",
        "status=waiting",
        "status=",
        "assignee=sev",
        "epic=../x",
        "parent=DEMO",
        "q=",
        "file=/etc/passwd",
        "op=show",
        "page=../../x",
        "needs_me=maybe",
        "label=" + "x" * 80,
    ],
)
@pytest.mark.parametrize("route", ["tickets", "board"])
def test_typed_inputs_are_refused_without_echo(client, token, stub, route, query):
    res = client.get(f"/api/w/{stub.ws}/{route}?{query}", headers=headers(token))
    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "invalid.input"
    for secret in ("/etc/passwd", "../x", "../../x"):
        assert secret not in res.text


def test_rows_are_adapted_and_paged_from_one_snapshot(client, token, stub, rich):
    env, ids = rich
    first = client.get(f"/api/w/{stub.ws}/board?limit=2&sort=key&order=asc", headers=headers(token)).json()
    assert [r["key"] for r in first["rows"]] == ["DEMO-0001", "DEMO-0002"] and first["total"] == 4
    check("board-page", first)
    env.tick()
    env.new_ticket("Added between pages")
    second = client.get(
        f"/api/w/{stub.ws}/board?limit=2&sort=key&order=asc&page={first['next_page']}", headers=headers(token)
    ).json()
    assert [r["key"] for r in second["rows"]] == ["DEMO-0003", "DEMO-0004"] and second["next_page"] is None
    assert second["total"] == 4 and second["sub_cursor"] == first["sub_cursor"]
    row = first["rows"][0]
    assert row["status"] == "open" and row["labels"] == ["api", "ui"] and row["awaiting_gate"] == "requirements"
    assert row["turn"]["who"] == stub.person and row["open_questions"] == 1
    assert row["progress"]["tasks_total"] == 2 and row["restricted"] is False and row["blocking_questions"] == 0


def test_filters_reach_the_host(client, token, stub, rich):
    get = lambda q: client.get(f"/api/w/{stub.ws}/tickets?{q}", headers=headers(token)).json()  # noqa: E731
    assert [r["key"] for r in get("label=api")["rows"]] == ["DEMO-0001"]
    assert [r["key"] for r in get("type=bug&type=epic&sort=key&order=asc")["rows"]] == ["DEMO-0002", "DEMO-0003"]
    assert get("status=backlog&status=done")["total"] == 0
    assert get("q=req")["total"] == 1
    assert get("needs_me=true")["total"] == 1
    assert get("owner=" + stub.person)["total"] == 4
    missing = client.get(f"/api/w/{stub.ws}/tickets?epic=DEMO-0099", headers=headers(token))
    assert missing.status_code == 404


def test_expired_page_token(client, token, stub, rich):
    first = client.get(f"/api/w/{stub.ws}/tickets?limit=1&sort=key", headers=headers(token)).json()
    stub.pages.snapshots.clear()
    res = client.get(f"/api/w/{stub.ws}/tickets?limit=1&sort=key&page={first['next_page']}", headers=headers(token))
    assert res.status_code == 409 and res.json()["error"]["code"] == "page.expired"
    assert res.json()["error"]["retryable"] is True


def test_restricted_tickets_never_appear(client, token, stub, rich):
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
    hidden = ids["Second"]
    env.store.append(
        env.person_event(env.owner, hidden, "visibility.changed", visibility={"restricted": [other.ref]}), log=hidden
    )
    for route in ("board", "tickets"):
        body = client.get(f"/api/w/{stub.ws}/{route}?sort=key", headers=headers(token)).json()
        assert "DEMO-0002" not in [r["key"] for r in body["rows"]] and body["total"] == 3
        assert "Second" not in str(body)
    for path in ("", "/events", "/changes"):
        res = client.get(f"/api/w/{stub.ws}/tickets/DEMO-0002{path}", headers=headers(token))
        assert res.status_code == 404 and "Second" not in res.text


def test_lists_have_no_write_method(client, token, stub):
    for route in ("tickets", "board"):
        res = client.post(f"/api/w/{stub.ws}/{route}", json={}, headers={**headers(token), "Origin": ORIGIN})
        assert res.status_code == 405
