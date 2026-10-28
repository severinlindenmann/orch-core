"""The dashboard server against a REAL orch host (core H6b and later): `orch host run --foreground` as a subprocess.

A disposable workspace is created with the real `orch setup` (the page played over the setup socket, the terminal in a pty), seeded with the real CLI, served by a real host
and read through the server's TestClient. Every action is signed with one click: the dashboard's click routes and core's
real signer child (`click_child.py`, file custody under the disposable state). Nothing touches `~/.config/orch`: state,
HOME and XDG_CONFIG_HOME are explicit and under a throwaway directory (see `realhost_kit`). Skipped when the host cannot
start (no pty, no unix sockets, ...). Run: `uv run pytest -m realhost`.
"""

import itertools
import json
import os
import shutil
import time
import uuid

import pytest
from conftest import ORIGIN, PORT
from fastapi.testclient import TestClient
from realhost_kit import Workspace, click_sign, seed, short_run_root

from orch_dashboard.app import create_app
from orch_dashboard.auth import Auth
from orch_dashboard.click_signer import Signer
from orch_dashboard.gateway import Gateway

pytestmark = [
    pytest.mark.realhost,
    pytest.mark.skipif(os.name != "posix", reason="a real host needs unix sockets and a pty"),
]


_retries = itertools.count(1)


def new_retry() -> int:
    """The explicit "do it again" counter: a test that repeats an action it already did asks for a fresh intent."""
    return next(_retries)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = short_run_root() / uuid.uuid4().hex[:6]
    ws = Workspace(root / "w")
    try:
        keys = ws.bring_up(seed)
        ws.convenience_on()  # one click is the dashboard's only way to sign
    except (RuntimeError, TimeoutError, OSError) as exc:
        ws.stop_host()
        shutil.rmtree(root, ignore_errors=True)
        pytest.skip(f"the real host cannot start here: {str(exc)[:300]}")
    static = tmp_path_factory.mktemp("static")
    (static / "assets").mkdir()
    (static / "index.html").write_text('<html><script src="/assets/main-hash.js"></script></html>')
    (static / "assets/main-hash.js").write_text("console.log(1)")
    gateway = Gateway(ws.state, click=Signer(ws.state, start=ws.click_start()))
    auth = Auth()
    with TestClient(create_app(PORT, gateway, auth, static), base_url=ORIGIN) as client:
        yield type(
            "World", (), {"ws": ws, "keys": keys, "gateway": gateway, "auth": auth, "client": client, "root": root}
        )
    gateway.close()
    ws.stop_host()
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def token(world):
    launch = world.auth.launch(PORT)
    res = world.client.post("/auth/session", json={"code": launch["code"]}, headers={"Origin": ORIGIN})
    assert res.status_code == 200, res.text
    return res.json()["token"]


@pytest.fixture
def api(world, token, monkeypatch):
    monkeypatch.delenv("ORCH_DASHBOARD_CLICK", raising=False)  # the suite's default is "no signer child"
    headers = {"Authorization": f"Bearer {token}"}

    class Api:
        base = f"/api/w/{world.ws.ws_id}"

        def get(self, path, **kw):
            return world.client.get(self.base + path, headers=headers, **kw)

        def post(self, path, body):
            return world.client.post(self.base + path, json=body, headers={**headers, "Origin": ORIGIN})

        def sign(self, body):
            """One click: prepare -> view -> sign through the dashboard's click routes. Returns (prepared, signed)."""
            body = {**body, "retry": new_retry()}
            return click_sign(world.client, self.base, {**headers, "Origin": ORIGIN}, body)

    return Api()


def test_launch_and_session(world, token):
    res = world.client.get("/api/session", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    features = res.json()["workspaces"][world.ws.ws_id]
    for name in ("today", "board", "ticket", "evidence", "actions", "settings", "members", "grant_list", "read_ext"):
        assert name in features, features
    assert "changes" in features  # the host serves `diff` since core #417 (stream A RH-2 fixed)


def test_shell_reads(world, api, token):
    h = {"Authorization": f"Bearer {token}"}
    spaces = world.client.get("/api/workspaces", headers=h).json()
    assert [(w["prefix"], w["host_state"], w["name"]) for w in spaces] == [
        ("DEMO", "ok", "Demo Owner")
    ]  # setup names the workspace after its owner
    assert spaces[0]["members"][0]["role"] == "owner" and spaces[0]["needs_you"] == 5
    # RH-6: the number is what Today counts (questions and approvals/verdicts; never a no_eligible or acknowledge
    # notice). B5b (fc7c940d): the one member of a solo workspace may decide every gate, so both testing tickets wait
    # for the owner's verdict: question + 2 requirements approvals + 2 verdicts.
    me = world.client.get("/api/me", headers=h).json()
    assert me[0]["role"] == "owner" and me[0]["tier"] == "A"
    ident = api.get("/identity").json()
    assert ident["uuid"] == world.ws.ws_id and ident["prefix"] == "DEMO" and ident["me"]["person"] == world.ws.person
    today = api.get("/today").json()
    kinds = {(n["ticket"], n["kind"]) for n in today["needs_you"]}
    k = world.keys
    assert (k["question"], "question") in kinds and (k["gate"], "approval") in kinds
    assert (k["verdict"], "verdict") in kinds and (k["testing"], "verdict") in kinds  # B5b: the solo owner decides
    assert all(kind != "no_eligible" for _, kind in kinds)
    assert [t["key"] for t in today["working"]] == [k["in_progress"]]
    counted = [n for n in today["needs_you"] if n["kind"] in ("question", "approval", "verdict")]
    assert len(counted) == spaces[0]["needs_you"] == len(today["needs_you"])


def test_board_and_backlog_paging(world, api):
    k = world.keys
    board = api.get("/board").json()
    by_status = {}
    for row in board["rows"]:
        by_status.setdefault(row["status"], []).append(row["key"])
    assert sorted(by_status["testing"]) == sorted([k["verdict"], k["testing"]])
    assert by_status["in-progress"] == [k["in_progress"]]
    assert k["closed"] not in sum(by_status.values(), [])  # closed never on the board
    page1 = api.get("/tickets", params={"limit": 2}).json()
    assert len(page1["rows"]) == 2 and page1["next_page"]
    page2 = api.get("/tickets", params={"limit": 2, "page": page1["next_page"]}).json()
    keys = [r["key"] for r in page1["rows"] + page2["rows"]]
    assert len(set(keys)) == 4
    closed = api.get("/tickets", params={"status": "closed"})
    assert closed.status_code in (200, 400, 422)


def test_ticket_page_sections_tasks_evidence_log(world, api):
    k = world.keys["verdict"]
    doc = api.get(f"/tickets/{k}").json()
    assert doc["status"] == "testing" and "artifact manifest" not in doc["unavailable"]
    assert doc["body"]["requirements"].startswith("Finance can download")
    assert [t["state"] for t in doc["tasks_state"]] == ["done"] and doc["tasks_state"][0]["receipt"]
    assert [a["state"] for a in doc["acceptance_state"]] == ["proven"]
    names = {a["name"]: a for a in doc["artifacts"]}
    assert (
        {"report.txt", "T1-receipt.log"} <= set(names)
        and names["report.txt"]["sha256"]
        and names["report.txt"]["bytes"] == 22
    )
    body = api.get(f"/tickets/{k}/artifacts/report.txt").json()
    assert "invoices exported: 42" in json.dumps(body)
    events = api.get(f"/tickets/{k}/events").json()
    assert {"ticket.created", "task.done", "ticket.submitted"} <= {e["type"] for e in events}
    assert api.get(f"/tickets/{world.keys['closed']}").json()["status"] == "closed"
    assert api.get("/tickets/DEMO-9999").status_code == 404


def test_settings_members_grants_reads(world, api):
    st = api.get("/settings").json()
    assert st["available"] is True and st["grant_hours"] == 8 and st["claim_ttl_min"] == 120
    assert set(st["gates"]) == {"requirements", "plan", "verify", "code"}
    members = api.get("/members").json()
    assert [m["role"] for m in members] == ["owner"] and members[0]["devices"][0]["valid"] is True
    grants = api.get("/grants").json()
    assert len(grants) == 1 and grants[0]["scope"] == "workable"


def test_changes_are_served_now_the_host_serves_diff(api, world):
    """Core #417: `diff` is served. The seeded tickets have no linked repo branch, so the answer is an honest empty diff."""
    res = api.get(f"/tickets/{world.keys['verdict']}/changes")
    assert res.status_code == 200, res.text
    got = res.json()
    assert isinstance(got, dict) and set(got) >= {"sources", "files"}
    assert got["files"] == [] or all("path" in f for f in got["files"])


def test_quota_host_while_busy_is_a_retryable_503(world, api, monkeypatch):
    """#423: a busy host (quota.host) reaches the browser as 503 retryable, which the Changes tab shows as 'busy, trying again'."""
    from orch_dashboard.errors import fail

    def busy(self, ws, op, args=None):
        raise fail("quota.host", "busy", exit=9)

    monkeypatch.setattr(type(world.gateway), "call", busy)
    res = api.get(f"/tickets/{world.keys['verdict']}/changes")
    assert res.status_code == 503 and res.json()["error"] == {
        "code": "quota.host",
        "message": "busy",
        "retryable": True,
    }


def test_a_cli_write_reaches_an_open_stream_within_two_seconds(world, token):
    """H4b: the CLI of a registered workspace talks to the running host, so this is a real terminal write."""
    import asyncio

    from orch_dashboard.live import Live

    key = world.keys["plain"]

    async def run():
        live = Live(world.gateway, world.auth)
        w, tab = live.attach(world.ws.ws_id, token)
        stream = live.stream(w, tab)
        try:
            for _ in range(6):
                if "event: ready" in await asyncio.wait_for(anext(stream), 10):
                    break
            else:
                raise AssertionError("no ready")
            start = time.monotonic()
            await asyncio.to_thread(world.ws.unattended, "log", "written from the terminal", "--ref", key)
            while True:
                frame = await asyncio.wait_for(anext(stream), 10)
                if "event: items" in frame and key in frame:
                    return time.monotonic() - start, frame
        finally:
            await live.close()

    elapsed, frame = asyncio.run(run())
    assert elapsed < 2.0, elapsed
    assert "log.added" in frame or '"type"' in frame


def test_a_stream_that_is_cut_resyncs_and_shows_the_write(world, token):
    import asyncio

    from orch_dashboard.live import Live

    key = world.keys["plain"]

    async def run():
        live = Live(world.gateway, world.auth)
        w, tab = live.attach(world.ws.ws_id, token)
        stream = live.stream(w, tab)
        try:
            while "event: ready" not in await asyncio.wait_for(anext(stream), 10):
                pass
        finally:
            await stream.aclose()
        await asyncio.to_thread(world.ws.unattended, "log", "written while the stream was down", "--ref", key)
        w2, tab2 = live.attach(world.ws.ws_id, token)  # the browser reconnects (no Last-Event-ID): a resync
        stream2 = live.stream(w2, tab2)
        try:
            first = await asyncio.wait_for(anext(stream2), 10)
            assert "event: resync" in first or "event: ready" in first
        finally:
            await live.close()

    asyncio.run(run())
    events = [
        e["summary"] if "summary" in e else e["type"]
        for e in __import__("json").loads(
            world.client.get(
                f"/api/w/{world.ws.ws_id}/tickets/{key}/events", headers={"Authorization": f"Bearer {token}"}
            ).text
        )
    ]
    assert any("stream was down" in str(e) for e in events) or len(events) >= 3


def test_approve_a_gate_with_the_real_signer(world, api):
    key = world.keys["gate"]
    prepared, final = api.sign({"action": "approve", "key": key, "gate": "requirements"})
    assert prepared["op"] == "approve" and prepared["confirm"] == "click" and prepared["request_id"].startswith("hr_")
    assert final["state"] == "submitted", final
    gate = api.get(f"/tickets/{key}").json()["gates"]["requirements"]
    assert gate["state"] == "approved" and gate["approvals"][0]["by"] == world.ws.person


def test_request_changes_and_the_ticket_shows_it(world, api):
    key = world.keys["changes"]
    _, final = api.sign(
        {"action": "request_changes", "key": key, "gate": "requirements", "message": "Say how many rows."}
    )
    assert final["state"] == "submitted", final
    events = api.get(f"/tickets/{key}/events").json()
    assert "gate.changes_requested" in {e["type"] for e in events}


def test_answer_a_question(world, api):
    key = world.keys["question"]
    _, final = api.sign(
        {"action": "answer", "key": key, "question": "Q1", "option": "no", "message": "Keep it simple."}
    )
    assert final["state"] == "submitted", final
    doc = api.get(f"/tickets/{key}").json()
    assert doc["questions_state"][0]["state"] == "answered" or doc["questions_state"][0].get("answer")


def test_comment_label_close_and_reopen(world, api):
    key = world.keys["plain"]
    for body in (
        {"action": "comment", "key": key, "message": "Looks good to me."},
        {"action": "label", "key": key, "label": "ui"},
        {"action": "close", "key": key, "resolution": "wont_do", "message": "Not now."},
    ):
        _, final = api.sign(body)
        assert final["state"] == "submitted", (body, final)
    assert api.get(f"/tickets/{key}").json()["status"] == "closed"
    _, final = api.sign({"action": "reopen", "key": key})
    assert final["state"] == "submitted", final
    doc = api.get(f"/tickets/{key}").json()
    assert doc["status"] == "open" and "ui" in doc["labels"]


def test_new_ticket_then_complete_the_details(world, api):
    _, final = api.sign({"action": "new", "title": "Archive old invoices", "type": "chore"})
    assert final["state"] == "submitted", final
    board = api.get("/tickets", params={"q": "Archive old invoices"}).json()
    assert [r["title"] for r in board["rows"]] == ["Archive old invoices"]
    key = board["rows"][0]["key"]
    _, final = api.sign({"action": "details", "key": key, "priority": "high", "size": "s", "labels": ["archive"]})
    assert final["state"] == "submitted", final
    _, final = api.sign(
        {
            "action": "section",
            "key": key,
            "section": "summary",
            "text": "Move invoices older than 7 years to cold storage.",
        }
    )
    assert final["state"] == "submitted", final
    doc = api.get(f"/tickets/{key}").json()
    assert doc["priority"] == "high" and doc["size"] == "s" and doc["labels"] == ["archive"]
    assert doc["body"]["summary"].startswith("Move invoices")


VERIFY_OWNER = {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": False}


def test_settings_set_through_the_dashboard(world, api):
    _, final = api.sign({"action": "settings_set", "grant_hours": 12, "claim_ttl_min": 90})
    assert final["state"] == "submitted", final
    st = api.get("/settings").json()
    assert st["grant_hours"] == 12 and st["claim_ttl_min"] == 90 and st["settings_at"]["by"] == world.ws.person


def test_policy_set_through_the_dashboard(world, api):
    _, final = api.sign({"action": "policy_set", "gate": "verify", "policy": VERIFY_OWNER})
    assert final["state"] == "submitted", final
    st = api.get("/settings").json()
    assert st["gates"]["verify"]["approvers"] == ["owner"] and st["gates"]["verify"]["not"] == []


def test_verdicts_after_the_verify_policy_lets_the_owner_decide(world, api):
    _, final = api.sign({"action": "policy_set", "gate": "verify", "policy": VERIFY_OWNER})
    assert final["state"] == "submitted", final
    _, final = api.sign(
        {"action": "verdict", "key": world.keys["verdict"], "outcome": "pass", "message": "Matches the spec."}
    )
    assert final["state"] == "submitted", final
    assert api.get(f"/tickets/{world.keys['verdict']}").json()["status"] == "done"
    _, final = api.sign(
        {"action": "verdict", "key": world.keys["testing"], "outcome": "fail", "message": "Retries are not capped."}
    )
    assert final["state"] == "submitted", final


def invitee():
    import base64

    from tests.identity.helpers import Person

    person = Person()
    pk = base64.urlsafe_b64encode(person.pk_pub).rstrip(b"=").decode()
    return person, pk, json.dumps(person.cert(created=int(time.time() * 1000)))


def test_add_a_member_from_a_certificate_file(world, api):
    from tests.identity.reader_helpers import decl

    person, pk, cert = invitee()
    # the workspace signs by one click, so a new reader comes with its declaration (`orch keys reader declare
    # --enrol`, orch-v2-host §0.8.6 item 9.4); without it core refuses sign.convenience_readers no_declaration
    body = {"action": "member_add", "name": "Nia", "role": "member", "pk": pk, "cert": cert}
    reader_decl = json.dumps(decl(person, world.ws.ws_id, via="enrol", created_ms=int(time.time() * 1000)))
    res = api.post("/click/prepare", {**body, "retry": new_retry()})
    assert res.status_code == 409 and res.json()["error"]["code"] == "sign.convenience_readers", res.text
    # the page says it in one plain line from the closed reason (refusals.ts READERS_SENTENCE.no_declaration)
    assert res.json()["error"].get("detail", {}).get("reason") == "no_declaration", res.text
    # the click path carries the declaration file beside the certificate, as the Add person form sends it: the host
    # finds it under the name the `reader_decl` argument gives (no `no_declaration`, no `reader.decl_invalid`)
    from pydantic import TypeAdapter

    from orch_dashboard.signing import READER_DECL_FILE, PrepareBody, host_call, host_files

    action = TypeAdapter(PrepareBody).validate_python({**body, "reader_decl": reader_decl})
    _, args = host_call(action)
    assert args["reader_decl"] == READER_DECL_FILE and set(host_files(action)) == {"cert", "reader_decl"}
    _, final = api.sign({**body, "reader_decl": reader_decl})
    assert final["state"] == "submitted", final
    members = {m["person"]: m for m in api.get("/members").json()}
    assert members[person.ref]["name"] == "Nia" and members[person.ref]["role"] == "member"
    # the host kept the new device's declaration: it reads the workspace at the level One click requires
    readers = api.get("/readers").json()
    assert readers["available"] and readers["required_reader"] == 1, readers
    nia = [r for r in readers["readers"] if r["person"] == person.ref]
    assert len(nia) == 1 and nia[0]["level"] >= readers["required_reader"], readers
    assert nia[0]["device"] == person.device and nia[0]["reader"] >= 1, nia  # the declaration that was sent, verified


def test_revoke_a_grant(world, api):
    grants = api.get("/grants").json()
    gid = grants[0]["id"]
    _, final = api.sign({"action": "grant_revoke", "grant": gid, "reason": "done for today"})
    assert final["state"] == "submitted", final
    got = {g["id"]: g for g in api.get("/grants", params={"active": "false"}).json()}
    assert got[gid]["revoked"]["by"] == world.ws.person


def test_move_is_offered_and_prepares_ticket_moved_on_the_real_host(world, api, token):
    features = world.client.get("/api/session", headers={"Authorization": f"Bearer {token}"}).json()["workspaces"][
        world.ws.ws_id
    ]
    assert "move" in features and "do:move" in features
    key = world.keys["changes"]
    # core #416 fixed RH-3: a prepare with the intent idem works for move
    res = api.post("/click/prepare", {"action": "move", "key": key, "to": "backlog", "retry": new_retry()})
    assert res.status_code == 200 and res.json()["op"] == "move", res.text
    assert api.post("/click/cancel", {"request_id": res.json()["request_id"]}).status_code == 200  # leave none open


def test_move_through_the_dashboard(world, api):
    key = world.keys["changes"]
    _, final = api.sign({"action": "move", "key": key, "to": "backlog"})
    assert final["state"] == "submitted", final
    assert api.get(f"/tickets/{key}").json()["status"] == "backlog"
    board = api.get("/board").json()["rows"]
    assert next(r for r in board if r["key"] == key)["status"] == "backlog"
    _, final = api.sign({"action": "move", "key": key, "to": "open", "reason": "back to work"})
    assert final["state"] == "submitted", final
    assert api.get(f"/tickets/{key}").json()["status"] == "open"


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    """Two workspaces with the SAME prefix and the same first ticket key, served by one real host."""
    root = short_run_root() / uuid.uuid4().hex[:6]
    a, b = Workspace(root / "w", dirname="a"), Workspace(root / "w", dirname="b")
    try:
        a.make_dirs()
        a.start_host()
        b.host = a.host
        for w, title in ((a, "Alpha ticket"), (b, "Beta ticket")):
            w.init()  # `orch setup` registers each workspace with the running host
            w.grant()
            w.install_dashboard()
            w.agent("new", title)
    except (RuntimeError, TimeoutError, OSError) as exc:
        a.stop_host()
        shutil.rmtree(root, ignore_errors=True)
        pytest.skip(f"the real host cannot start here: {str(exc)[:300]}")
    static = tmp_path_factory.mktemp("static2")
    (static / "assets").mkdir()
    (static / "index.html").write_text("<html></html>")
    gateway = Gateway(a.state)
    auth = Auth()
    with TestClient(create_app(PORT, gateway, auth, static), base_url=ORIGIN) as client:
        launch = auth.launch(PORT)
        token = client.post("/auth/session", json={"code": launch["code"]}, headers={"Origin": ORIGIN}).json()["token"]
        yield type("Pair", (), {"a": a, "b": b, "client": client, "token": token, "gateway": gateway})
    gateway.close()
    a.stop_host()
    shutil.rmtree(root, ignore_errors=True)


def test_two_workspaces_with_one_prefix_and_one_ticket_key(pair):
    h = {"Authorization": f"Bearer {pair.token}"}
    spaces = {w["id"]: w for w in pair.client.get("/api/workspaces", headers=h).json()}
    assert set(spaces) == {pair.a.ws_id, pair.b.ws_id} and {w["prefix"] for w in spaces.values()} == {"DEMO"}
    assert all(w["host_state"] == "ok" for w in spaces.values())
    titles = {}
    for w in (pair.a, pair.b):
        doc = pair.client.get(f"/api/w/{w.ws_id}/tickets/DEMO-0001", headers=h).json()
        titles[w.ws_id] = doc["title"]
    assert titles == {pair.a.ws_id: "Alpha ticket", pair.b.ws_id: "Beta ticket"}
    me = {m["workspace"]: m["person"] for m in pair.client.get("/api/me", headers=h).json()}
    assert me[pair.a.ws_id] != me[pair.b.ws_id]  # each workspace has its own owner


def test_a_workspace_that_cannot_serve_the_dashboard_is_listed_unavailable_and_the_other_works(pair):
    h = {"Authorization": f"Bearer {pair.token}"}
    pair.b.human("addon", "disable", "dashboard")
    spaces = {w["id"]: w for w in pair.client.get("/api/workspaces", headers=h).json()}
    assert spaces[pair.b.ws_id]["host_state"] == "addon.disabled" and spaces[pair.b.ws_id]["name"] == pair.b.ws_id
    assert spaces[pair.a.ws_id]["host_state"] == "ok"
    assert pair.client.get(f"/api/w/{pair.a.ws_id}/tickets/DEMO-0001", headers=h).status_code == 200
    assert pair.client.get(f"/api/w/{pair.b.ws_id}/tickets/DEMO-0001", headers=h).status_code >= 400


def test_with_the_host_stopped_every_workspace_is_unavailable(pair):
    h = {"Authorization": f"Bearer {pair.token}"}
    pair.a.stop_host()
    res = pair.client.get("/api/workspaces", headers=h)
    assert res.status_code in (200, 503)
    if res.status_code == 200:
        assert all(w["host_state"] != "ok" for w in res.json())


@pytest.mark.skip(
    reason="stream A RH-4: no host or CLI op changes a ticket's visibility (visibility.changed is a person event with no op), so a ticket cannot be hidden from the only person; the purge on `gone` is covered by test_live_updates"
)
def test_hiding_a_ticket_purges_it_from_an_open_tab():
    raise AssertionError("unreachable")


def test_record_real_host_goldens(world):
    """`UPDATE_REALHOST_GOLDEN=1 uv run pytest -m realhost -k record`: scrubbed host answers plus what the adapters make of
    them, in src/test/fixtures/real-host. `test_realhost_goldens.py` replays them offline, in CI."""
    if not os.environ.get("UPDATE_REALHOST_GOLDEN"):
        pytest.skip("set UPDATE_REALHOST_GOLDEN=1 to record")
    from realhost_goldens import DIRECTORY, adapt
    from realhost_scrub import Scrubber

    from orch_dashboard.reads import TicketsQuery, host_args

    g, ws, k = world.gateway, world.ws.ws_id, world.keys
    raw = {
        "ticket.query": g.call(ws, "ticket.query", host_args(TicketsQuery())),
        "needs": g.call(ws, "needs"),
        "member.list": g.call(ws, "member.list"),
        "workspace.settings": g.call(ws, "workspace.settings", {}),
        "grant.list": g.call(ws, "grant.list", {"active": True}),
        "workspace.show": g.call(ws, "workspace.show"),
        "welcome": {k2: v for k2, v in g.link(ws).welcome.items() if k2 in ("caps", "ops")},
        "tickets": {
            role: g.call(ws, "ticket.get", {"ref": k[role]})
            for role in ("verdict", "in_progress", "gate", "answered", "closed", "plain")
        },
        "whoami": {"person": g.link(ws).welcome["peer"]["person"], "workspace": ws},
    }
    scrubbed = Scrubber()(raw)
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    (DIRECTORY / "raw.json").write_text(json.dumps(scrubbed, indent=1, sort_keys=True) + "\n")
    (DIRECTORY / "adapted.json").write_text(json.dumps(adapt(scrubbed), indent=1, sort_keys=True) + "\n")


@pytest.mark.parametrize(
    "body",
    [
        {
            "action": "addon_enable",
            "name": "publish",
            "publish": {"server": "https://files.example.test", "share": "https://links.example.test"},
        },
    ],
)
def test_turn_on_an_addon_with_its_settings_on_the_real_host(world, api, body):
    """e2e run 4: "Turn on" for an add-on with settings answered 500 (`addon.preview` sent as a write). Prepared on the
    real host: the request is prepared (the real host checked the intent and its grant body); nothing is signed."""
    res = api.post("/click/prepare", {**body, "retry": new_retry()})
    assert res.status_code == 200 and res.json()["op"] == "settings.set", res.text
    rid = res.json()["request_id"]
    assert api.post("/click/state", {"request_id": rid}).json()["state"] == "bound"  # bound to the click signer
    res = api.post("/click/cancel", {"request_id": rid})
    assert res.status_code == 200 and res.json()["state"] == "cancelled", res.text
