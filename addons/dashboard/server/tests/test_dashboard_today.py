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
    assert all(n["since"] == "" for n in data["needs_you"])  # not served: empty, never invented
    assert data["counts"] == {"open": 4} and data["recent"] == [] and data["read_only_open"] == []
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
