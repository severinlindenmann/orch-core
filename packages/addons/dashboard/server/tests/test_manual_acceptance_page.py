"""#740 manual acceptance and verify decisions: the host's request, the server's check and the page's check agree.

Core's in-process host (tests.host.live_helpers) serves a workspace; the dashboard's own app prepares the request as
the one-click path does (its `plan`, a signer-bound `human.prepare` through its real Gateway, `click/view`'s check of
the host's record); the result is then checked by the page's own `requestIsConsistent` (src/api/handoff.ts, run by
node with its types stripped). A drift between the host's `Material` and either the server's or the browser's check
fails here.
"""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import ORIGIN, PORT
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from termkit import grant_dashboard

from orch_dashboard import adapters
from orch_dashboard.app import create_app
from orch_dashboard.auth import Auth
from orch_dashboard.gateway import Gateway
from orch_dashboard.signing import PrepareBody, expected, expected_material, plan

API = Path(__file__).resolve().parents[2] / "src" / "api"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="the page's check runs in node")

RUN = """
import { readFileSync } from 'node:fs'
import { requestIsConsistent } from './handoff.ts'
const { request, now } = JSON.parse(readFileSync(0, 'utf8'))
Date.now = () => now
console.log(JSON.stringify(requestIsConsistent(request)))
"""


@pytest.fixture(scope="module")
def page_check(tmp_path_factory):
    """The page's `requestIsConsistent`, unchanged but for the one relative import node needs spelled with `.ts`."""
    d = tmp_path_factory.mktemp("page")
    (d / "handoff.ts").write_text((API / "handoff.ts").read_text().replace("from './sha256'", "from './sha256.ts'"))
    shutil.copy(API / "sha256.ts", d / "sha256.ts")
    (d / "run.mjs").write_text(RUN)

    def check(request: dict, now_s: float) -> bool:
        out = subprocess.run(
            [NODE, "run.mjs"],
            cwd=d,
            input=json.dumps({"request": request, "now": int(now_s * 1000)}),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    return check


@pytest.fixture
def page(tmp_path, monkeypatch):
    from tests.host.human_helpers import Human
    from tests.host.live_helpers import world

    w = world(tmp_path)
    client = None
    gateway = None
    try:
        grant_dashboard(w)
        monkeypatch.setenv("ORCH_HOST_RUN_DIR", str(w.lab.run))
        monkeypatch.delenv("ORCH_HOST_SOCKET", raising=False)
        # the host runs on the test's fake clock: the server's expiry check reads the same clock
        monkeypatch.setattr(adapters.handoff, "time", SimpleNamespace(time=lambda: float(w.lab.ws.clock[0])))
        gateway = Gateway(Path(w.lab.state))
        auth = Auth()
        static = tmp_path / "static"
        (static / "assets").mkdir(parents=True)
        (static / "index.html").write_text("<html></html>")
        client = TestClient(create_app(PORT, gateway, auth, static), base_url=ORIGIN)
        client.__enter__()
        retries = iter(range(1, 1000))
        owner = w.lab.ws.owner
        signer = {"device": owner.device, "auth": "passphrase"}

        def handoff(body: dict) -> dict:
            action = TypeAdapter(PrepareBody).validate_python(body | {"retry": next(retries)})
            planned = plan(gateway, w.wid, action)
            prepared = client.app.state.human(
                w.wid,
                "human.prepare",
                {"op": planned.op, "args": planned.args, "signer": signer},
                idem=planned.idem(w.wid, action.retry),
                files=planned.files,
            )
            return adapters.handoff.request(
                prepared,
                w.wid,
                expect_op=planned.op,
                expect_key=getattr(action, "key", None),
                expect_events=expected(action, planned.person),
                expect_material=expected_material(action),
            )

        yield SimpleNamespace(w=w, h=Human(w), handoff=handoff, now=lambda: float(w.lab.ws.clock[0]))
    finally:
        if client is not None:
            client.__exit__(None, None, None)
        if gateway is not None:
            gateway.close()
        w.close()
        w.lab.close()


def criterion(page):
    _, key = page.w.ticket()
    page.h.run("ac.add", {"ref": key, "text": "Login shows an error for a wrong password — “é”"})
    return key


def shown(page, page_check, body):
    """The request as the server checked it, then the page's own consistency check (False = "Not shown")."""
    got = page.handoff(body)
    assert got["state"] == "bound", got
    assert page_check(got, page.now()), (body["action"], got["op"], sorted(got["material"]))
    return got


@pytest.mark.parametrize("note", [None, "I tried it with a wrong password"])
def test_accept_withdraw_and_accept_again_are_shown_by_the_page(page, page_check, note):
    key = criterion(page)
    accept = {"action": "accept_criterion", "key": key, "ac": "AC1"} | ({"note": note} if note else {})
    got = shown(page, page_check, accept)
    assert got["op"] == "ac.accept" and "statement" in got["material"]
    page.h.run("ac.accept", {"ref": key, "ac": "AC1"} | ({"note": note} if note else {}))

    got = shown(page, page_check, {"action": "withdraw_acceptance", "key": key, "ac": "AC1", "reason": "recheck"})
    assert got["op"] == "ac.unaccept"
    page.h.run("ac.unaccept", {"ref": key, "ac": "AC1", "reason": "recheck"})

    got = shown(page, page_check, accept)
    assert got["events"][0]["name"] == "accept-AC1-2.md"


def test_mark_task_done_is_shown_by_the_page(page, page_check):
    key = criterion(page)
    page.h.run("task.add", {"ref": key, "text": "Do it", "proves": ["AC1"]})
    assert shown(page, page_check, {"action": "mark_task_done", "key": key, "task": "T1"})["op"] == "task.mark_done"


def verify_ready(page, accepted: bool):
    """A ticket at the verify gate (testing, the owner a reviewer), AC1 accepted manually when `accepted`."""
    from tests.host.test_reads_ticket import agent_ev

    w, h = page.w, page.h
    uid, key = w.ticket("verify me")
    a = h.agent
    a("claim", {"ref": key})
    for sid in ("context", "requirements", "out_of_scope", "plan", "decisions", "verification"):
        a("section.set", {"ref": key, "section": sid, "message": f"the {sid}"})
    a("ac.add", {"ref": key, "text": "it works"})
    a("task.add", {"ref": key, "text": "do it", "verify": "make test", "proves": ["AC1"]})
    h.run("approve", {"ref": key, "gate": "requirements"})
    h.run("approve", {"ref": key, "gate": "plan"})
    if accepted:
        h.run("ac.accept", {"ref": key, "ac": "AC1", "note": "checked by hand"})
    receipt = {"cmd": "make test", "exit": 0, "ms": 5, "repo": None, "commit": None}
    agent_ev(w, uid, "task.done", task="T1", receipt=receipt, text="did it")
    agent_ev(w, uid, "ticket.submitted")
    w.person_event("people.changed", uid, role="reviewers", add=[w.lab.owner], remove=[])
    return key


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("outcome", ["pass", "fail"])
def test_verify_gate_verdicts_are_shown_by_the_page(page, page_check, accepted, outcome):
    """Every verify-gate material carries `manual_acceptances` (empty or not): the page must show it (LOW 1, #784).
    The dashboard decides the verify gate by a verdict only (its `approve` takes requirements, plan or code)."""
    key = verify_ready(page, accepted)
    body = {"action": "verdict", "key": key, "outcome": outcome, "message": "Checked against the spec."}
    got = shown(page, page_check, body)
    rows = got["material"]["manual_acceptances"]
    assert [r["ac"] for r in rows] == (["AC1"] if accepted else [])


def test_the_page_still_refuses_what_does_not_recompute(page, page_check):
    """Admitting the new keys loosens nothing: a changed statement, an extra key or a statement elsewhere is refused."""
    key = criterion(page)
    got = page.handoff({"action": "accept_criterion", "key": key, "ac": "AC1", "note": "n"})
    bad = json.loads(json.dumps(got))
    bad["material"]["statement"] += "x"
    assert not page_check(bad, page.now())
    extra = json.loads(json.dumps(got))
    extra["material"]["extra"] = 1
    assert not page_check(extra, page.now())
    elsewhere = json.loads(json.dumps(got))
    elsewhere["op"] = "log"
    assert not page_check(elsewhere, page.now())
