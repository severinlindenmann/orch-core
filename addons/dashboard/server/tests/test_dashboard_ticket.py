import hashlib

import pytest
from conftest import ORIGIN, git
from golden import check


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def url(stub, tail):
    return f"/api/w/{stub.ws}/tickets/{tail}"


@pytest.mark.parametrize("tail", ["DEMO-0001", "DEMO-0001/events", "DEMO-0001/changes", "DEMO-0001/artifacts/out.log"])
def test_ticket_routes_need_authentication(client, stub, tail):
    assert client.get(url(stub, tail)).status_code == 401


@pytest.mark.parametrize(
    "tail",
    [
        "demo-1/x",
        "DEMO-0001/events?since=-1",
        "DEMO-0001/events?limit=501",
        "DEMO-0001/events?file=x",
        "DEMO-0001/changes?repo=../../x",
        "DEMO-0001/changes?context=11",
        "DEMO-0001/changes?ref=other",
        "DEMO-0001/artifacts/..%2Fconfig.json",
        "DEMO-0001/artifacts/out.log?offset=0",
        "DEMO-1x",
    ],
)
def test_typed_inputs_are_refused(client, token, stub, rich, tail):
    res = client.get(url(stub, tail), headers=headers(token))
    assert res.status_code in (400, 404), res.text
    assert "config.json" not in res.text and "../../x" not in res.text


def test_document_is_adapted_to_the_ui_type(client, token, stub, rich):
    res = client.get(url(stub, "DEMO-0001"), headers=headers(token))
    assert res.status_code == 200, res.text
    doc = res.json()
    assert doc["key"] == "DEMO-0001" and doc["status"] == "open" and doc["priority"] == "high"
    assert [a["id"] for a in doc["acceptance_state"]] == ["AC1"] and doc["acceptance_state"][0]["state"] == "unproven"
    assert [t["state"] for t in doc["tasks_state"]] == ["todo", "todo"]
    assert {q["id"]: q["state"] for q in doc["questions_state"]} == {"Q1": "answered", "Q2": "open"}
    answered = doc["questions_state"][0]["answer"]
    assert answered["by"] == stub.person and answered["at"].startswith("2026-")
    assert doc["gates"]["requirements"]["state"] == "pending" and doc["gates"]["code"]["required"] is False
    assert doc["gates"]["requirements"]["hash"] and doc["body"]["requirements"] == "req"
    assert doc["links"]["branches"] == {"code": "feat/one"} and doc["branch"] is None
    assert doc["restricted"] is False and doc["verdict"] is None
    assert "verdict" in doc["unavailable"]  # named, not invented
    assert doc["turn"]["who"] == stub.person
    assert doc["head"]["seq"] > 0 and doc["created_at"] and doc["updated_at"]
    check("ticket", doc)


def test_events_are_objects_not_strings(client, token, stub, rich):
    events = client.get(url(stub, "DEMO-0001/events"), headers=headers(token)).json()
    assert events[0]["type"] == "ticket.created" and events[0]["seq"] == 1
    assert all(set(e) >= {"v", "id", "seq", "at", "type", "actor", "summary"} for e in events)
    assert any("AC1 added" in e["summary"] for e in events)
    tail = client.get(url(stub, f"DEMO-0001/events?since={events[2]['seq']}&limit=2"), headers=headers(token)).json()
    assert [e["seq"] for e in tail] == [events[3]["seq"], events[4]["seq"]]
    check("events", events)


def test_evidence_per_criterion_and_content(client, token, stub, rich):
    env, ids = rich
    data = b"first line\nsecond line\n"
    env.store.append(
        {
            "type": "artifact.added",
            "actor": env.agent,
            "name": "out.log",
            "kind": "log",
            "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "ac": "AC1",
        },
        log=ids["First"],
        artifacts={"out.log": data},
    )
    doc = client.get(url(stub, "DEMO-0001"), headers=headers(token)).json()
    assert doc["acceptance_state"][0]["state"] == "proven"
    assert doc["artifacts"][0]["name"] == "out.log" and doc["artifacts"][0]["sha256"].endswith(
        hashlib.sha256(data).hexdigest()
    )
    assert doc["artifacts"][0]["ac"] == "AC1"
    content = client.get(url(stub, "DEMO-0001/artifacts/out.log"), headers=headers(token)).json()
    assert content["text"] == data.decode() and content["verified"] is True and content["truncated"] is False
    assert client.get(url(stub, "DEMO-0001/artifacts/missing.log"), headers=headers(token)).status_code == 404
    # A file that changed on disk after its digest was recorded is refused, never shown as evidence.
    (env.root / "tickets" / ids["First"] / "artifacts" / "out.log").write_bytes(b"tampered")
    bad = client.get(url(stub, "DEMO-0001/artifacts/out.log"), headers=headers(token))
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "artifact.mismatch"


def test_changes_are_bound_to_the_observed_source(client, token, stub, rich):
    env, ids = rich
    res = client.get(url(stub, "DEMO-0001/changes"), headers=headers(token))
    assert res.status_code == 200, res.text
    changes = res.json()
    assert changes["branch"] == "feat/one" and len(changes["head"]) == 40 and changes["base"] != changes["head"]
    assert {f["path"]: f["status"] for f in changes["files"]} == {"a.txt": "M", "b.txt": "A"}
    assert "+two" in next(f for f in changes["files"] if f["path"] == "a.txt")["lines"]
    assert changes["additions"] == 2 and changes["truncated"] is False
    check("changes", changes)
    repo = env.tmp / "code"
    (repo / "c.txt").write_text("late\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "late")
    stale = client.get(url(stub, "DEMO-0001/changes"), headers=headers(token))
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "observe.unavailable"
    assert "late" not in stale.text


def test_unknown_and_closed_tickets(client, token, stub, rich):
    env, ids = rich
    assert client.get(url(stub, "DEMO-0099"), headers=headers(token)).status_code == 404
    env.tick()
    env.close(ids["Third"])
    res = client.get(url(stub, "DEMO-0004"), headers=headers(token))
    assert res.status_code == 409 and res.json()["error"]["code"] == "ticket.closed"
    listed = client.get(f"/api/w/{stub.ws}/board", headers=headers(token)).json()
    assert "DEMO-0004" not in [r["key"] for r in listed["rows"]]


def test_ticket_routes_have_no_write_method(client, token, stub):
    for tail in ("DEMO-0001", "DEMO-0001/events", "DEMO-0001/changes"):
        res = client.post(url(stub, tail), json={}, headers={**headers(token), "Origin": ORIGIN})
        assert res.status_code == 405
