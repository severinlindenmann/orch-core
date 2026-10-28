"""Click-to-sign against a REAL host and the REAL signer child (core H5c, orch-v2-host section 0.8.6).

Disposable everything: `realhost_kit` builds a throwaway workspace (explicit ORCH_STATE_DIR/HOME/XDG_CONFIG_HOME under
a short path with `.orch-dev` markers, never `~/.config/orch`). The device key is moved into convenience custody with the
real `orch keys custody convenience` in a pty, and the signer runs core's real code; the only difference from production
is the custody store: `click_child.py` swaps `open_store` for the 0600 file store of the disposable state (core's
documented test seam), so the macOS keychain is never opened. One workspace, one person, no relay, no phone.
"""

import itertools
import os
import re
import shutil
import subprocess
import time
import uuid

import pytest
from conftest import ORIGIN, PORT
from fastapi.testclient import TestClient
from realhost_kit import Workspace, key_of, ready_requirements, short_run_root

from orch_dashboard.app import create_app
from orch_dashboard.auth import Auth
from orch_dashboard.click_signer import Signer
from orch_dashboard.gateway import Gateway

pytestmark = [
    pytest.mark.realhost,
    pytest.mark.skipif(os.name != "posix", reason="a real host needs unix sockets and a pty"),
]
RISK = "Convenience signing: agents running as you, or anything that takes over this dashboard, could sign."
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
RETRIES = itertools.count(1)  # one counter for the whole module: every "fresh" prepare is its own intent


def seeding(ws: Workspace) -> dict:
    keys = {}
    for name, title in (("one", "Export billing CSV"), ("two", "Retry failed webhooks"), ("three", "Audit log")):
        k = keys[name] = key_of(ws.agent("new", title, "--type", "feature", "--priority", "medium"))
        ws.agent("ac", "add", "It works", "--ref", k)
        ready_requirements(ws, k, f"{title}: do the described thing.")
    return keys


def run(ws: Workspace, *args: str) -> str:
    done = subprocess.run(ws.orch(*args), env=ws.env(), cwd=ws.root, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout + done.stderr


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = short_run_root() / uuid.uuid4().hex[:6]
    ws = Workspace(root / "w")
    try:
        keys = ws.bring_up(seeding)
        # The real `orch host convenience on`, in a pty: since #1304 it moves the key into the One click key file in
        # one step (the passphrase is typed once on the terminal) and then switches the flag on.
        ws.convenience_on()
    except (RuntimeError, TimeoutError, OSError, AssertionError) as exc:
        ws.stop_host()
        shutil.rmtree(root, ignore_errors=True)
        pytest.skip(f"the real host cannot start here: {str(exc)[:400]}")
    children = []
    start = ws.click_start(children)

    os.environ.pop("ORCH_DASHBOARD_CLICK", None)
    static = tmp_path_factory.mktemp("static")
    (static / "assets").mkdir()
    (static / "index.html").write_text("<html></html>")
    old = os.environ.get("ORCH_HOST_RUN_DIR")
    os.environ["ORCH_HOST_RUN_DIR"] = str(ws.run)
    gateway = Gateway(ws.state, click=Signer(ws.state, start=start))
    auth = Auth()
    with TestClient(create_app(PORT, gateway, auth, static), base_url=ORIGIN) as client:
        launch = client.post("/auth/session", json={"code": auth.launch(PORT)["code"]}, headers={"Origin": ORIGIN})
        token = launch.json()["token"]
        yield type(
            "World", (), {"ws": ws, "keys": keys, "gateway": gateway, "client": client, "token": token, "auth": auth}
        )
    gateway.close()
    for proc in children:
        assert proc.poll() is not None  # the signer exits with the server
    if old is None:
        os.environ.pop("ORCH_HOST_RUN_DIR", None)
    else:
        os.environ["ORCH_HOST_RUN_DIR"] = old
    ws.stop_host()
    shutil.rmtree(root, ignore_errors=True)


class Click:
    def __init__(self, world):
        self.w, self.n = world, 0

    def headers(self, write=True):
        return {"Authorization": f"Bearer {self.w.token}", **({"Origin": ORIGIN} if write else {})}

    def url(self, step):
        return f"/api/w/{self.w.ws.ws_id}/click/{step}"

    def post(self, step, body):
        return self.w.client.post(self.url(step), json=body, headers=self.headers())

    def prepare(self, body, fresh=True):
        """`fresh`: an explicit "do it again" (a new retry counter), so tests that repeat an action stay independent.
        `fresh=False` is the plain intent: the same action again is the same request."""
        return self.post("prepare", {**body, "retry": next(RETRIES)} if fresh else body)

    def workspace(self):
        res = self.w.client.get("/api/workspaces", headers=self.headers(False))
        return next(w for w in res.json() if w["id"] == self.w.ws.ws_id)

    def ticket(self, key):
        return self.w.client.get(f"/api/w/{self.w.ws.ws_id}/tickets/{key}", headers=self.headers(False)).json()


@pytest.fixture
def click(world, monkeypatch):
    monkeypatch.delenv("ORCH_DASHBOARD_CLICK", raising=False)  # the suite's default is "no signer child"
    return Click(world)


def approve(world, which="one", gate="requirements"):
    return {"action": "approve", "key": world.keys[which], "gate": gate}


def test_the_workspace_card_and_the_flag(world, click):
    ws = click.workspace()
    assert "click" in ws["features"], (ws["click"], ws["features"])
    card = dict(ws["click"])
    assert re.fullmatch(r"d_[0-9a-f]{32}", card.pop("device"))  # the validated device key (#578), never a raw value
    # B6 (b238af89): the card says when the One click risk notice was acknowledged; enabling One click records it.
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", card.pop("risk_ack"))
    assert card == {"custody": "convenience", "enabled": True, "available": True, "idle_hours": 12}
    card = world.client.get(click.url("status"), headers=click.headers(False)).json()
    assert card["custody"] == "convenience"


def test_prepare_view_and_sign_end_to_end(world, click):
    prepared = click.prepare(approve(world))
    assert prepared.status_code == 200, prepared.text
    got = prepared.json()
    assert got["op"] == "approve" and got["confirm"] == "click" and got["request_id"].startswith("hr_")
    view = click.post("view", {"request_id": got["request_id"]})
    assert view.status_code == 200, view.text
    body = view.json()
    shown = body["view"]
    assert shown["risk"] == RISK and shown["op"] == "approve" and shown["auth"] == "convenience"
    assert shown["workspace"]["prefix"] == "DEMO" and re.fullmatch(r"[0-9a-f]{12}", shown["fingerprint"])
    assert shown["confirm"] == {"kind": "click"} and shown["lines"] and all(isinstance(x, str) for x in shown["lines"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", body["view_digest"])
    # Signed at once: the signer's 1 s guard refuses (a refusal, not a cancel).
    soon = click.post("sign", {"request_id": got["request_id"], "view_digest": body["view_digest"]})
    assert soon.status_code == 409 and soon.json()["error"]["code"] == "human.click_refused"
    assert soon.json()["error"]["reason"] == "too_soon"
    time.sleep(1.2)
    done = click.post("sign", {"request_id": got["request_id"], "view_digest": body["view_digest"]})
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "submitted"
    gates = click.ticket(world.keys["one"])["gates"]
    assert gates["requirements"]["state"] == "approved"


def test_a_view_that_is_not_the_one_built_is_refused(world, click):
    got = click.prepare(approve(world, "two")).json()
    view = click.post("view", {"request_id": got["request_id"]}).json()
    time.sleep(1.2)
    bad = click.post("sign", {"request_id": got["request_id"], "view_digest": "sha256:" + "0" * 64})
    assert bad.status_code == 409 and bad.json()["error"]["reason"] == "view_changed"
    # Refusals never cancel: the right digest still signs it.
    ok = click.post("sign", {"request_id": got["request_id"], "view_digest": view["view_digest"]})
    assert ok.status_code == 200, ok.text


def test_a_policy_op_signs_on_one_click_and_takes_no_word(world, click):
    policy = {
        "approvers": ["owner"],
        "count": 1,
        "not": [],
        "applies": "all",
        "independent": False,
    }
    got = click.prepare({"action": "policy_set", "gate": "requirements", "policy": policy})
    assert got.status_code == 200, got.text
    assert got.json()["confirm"] == "click"
    rid = got.json()["request_id"]
    view = click.post("view", {"request_id": rid}).json()
    assert view["view"]["confirm"] == {"kind": "click"}
    time.sleep(1.2)
    typed = click.post("sign", {"request_id": rid, "view_digest": view["view_digest"], "word": "POLICY"})
    assert typed.status_code == 400 or typed.status_code == 422  # since contract 3: there is no word field
    assert view["view"]["risk_lines"] == [] and view["view"]["headline"]  # contract 4: policy is not a marked op
    ok = click.post("sign", {"request_id": rid, "view_digest": view["view_digest"]})
    assert ok.status_code == 200, ok.text


def _signed(world, click, body):
    """prepare, view and sign one action through the dashboard server; the View as shown."""
    got = click.prepare(body)
    assert got.status_code == 200, got.text
    assert got.json()["confirm"] == "click"
    rid = got.json()["request_id"]
    view = click.post("view", {"request_id": rid})
    assert view.status_code == 200, view.text
    time.sleep(1.2)
    done = click.post("sign", {"request_id": rid, "view_digest": view.json()["view_digest"]})
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "submitted"
    return view.json()["view"]


def test_a_new_schedule_signs_on_one_click(world, click):
    """Run 13 (owner e2e, blocking): Schedules > New schedule, a once-run 1.5 h ahead with the host's default run
    hours, is shown and signed on one click. The View keeps the convenience-signing notice in `risk` (the server's
    check) and carries the schedule's own risk as a line of the review."""
    from datetime import UTC, datetime

    from orch.ops.schedule import RISK as SCHEDULE_RISK

    _signed(world, click, {"action": "addon_grant", "name": "schedules"})
    at = datetime.fromtimestamp((int(time.time()) + 5400) // 60 * 60, UTC).strftime("%Y-%m-%d %H:%M")
    body = {"action": "schedule_add", "key": world.keys["three"], "brief": "Write the report", "at": at, "tz": "UTC"}
    shown = _signed(world, click, body)
    assert shown["op"] == "schedule.add" and shown["risk"] == RISK and SCHEDULE_RISK in shown["lines"]
    listed = world.client.get(f"/api/w/{world.ws.ws_id}/schedules", headers=click.headers(False))
    assert listed.status_code == 200, listed.text
    page = listed.json()
    assert page["min_lead_s"] == 3600  # a real (DEMO) workspace: the form offers nothing earlier than an hour ahead
    assert [(r["ticket"], r["brief"], r["state"], r["run_hours"]) for r in page["rows"]] == [
        (world.keys["three"], "Write the report", "live", 8)
    ]


def test_cancel_cancels_on_the_host(world, click):
    got = click.prepare(approve(world, "three")).json()
    res = click.post("cancel", {"request_id": got["request_id"]})
    assert res.status_code == 200 and res.json()["state"] == "cancelled"
    assert click.post("view", {"request_id": got["request_id"]}).status_code == 404


def test_with_the_flag_off_the_host_refuses_and_the_card_says_so(world, click):
    ws = world.ws
    run(ws, "host", "convenience", "off", "--workspace", ws.ws_id, "--state", str(ws.state))
    try:
        res = click.prepare(approve(world, "three"))
        assert res.status_code == 409, res.text
        assert res.json()["error"]["code"] == "sign.convenience_unavailable"
        assert res.json()["error"]["reason"] == "disabled"
        card = click.workspace()
        assert "click" not in card["features"] and card["click"]["enabled"] is False
    finally:
        run(ws, "host", "convenience", "on", "--workspace", ws.ws_id, "--state", str(ws.state))
    assert "click" in click.workspace()["features"]


def test_a_signer_that_dies_is_restarted_and_its_open_approvals_are_gone(world, click):
    got = click.prepare(approve(world, "three")).json()
    old = world.gateway.click.client.proc
    pid = old.pid
    old.kill()  # this signer's own PID, nothing else
    old.wait(5)
    world.gateway.click.last_spawn -= 10  # the respawn guard is for tight loops, not for this test
    res = click.post("view", {"request_id": got["request_id"]})
    assert res.status_code == 409 and res.json()["error"]["reason"] == "not_prepared_here"  # new signer
    assert world.gateway.click.client.proc.pid != pid and world.gateway.click.restarts == 1
    # And a new request works with the new signer.
    assert click.prepare(approve(world, "three")).status_code == 200


def test_the_signer_holds_no_inherited_descriptors_of_ours(world, click):
    """The signer child has the channel only: no listening socket, no host connection of this process."""
    pid = world.gateway.click.client.proc.pid
    out = subprocess.run(["lsof", "-p", str(pid), "-Fn", "-Ft"], capture_output=True, text=True).stdout
    assert str(PORT) not in out


def test_a_view_is_refused_when_the_hosts_record_is_not_what_was_asked_for(world, click):
    """The hand-off's gate on the host's own record (read with human.get through our link): another ticket is refused."""
    got = click.prepare(approve(world, "three")).json()
    app = world.client.app
    ws, op, body, epoch = app.state.clicks.get(got["request_id"])
    app.state.clicks.add(
        ws, got["request_id"], op, body.model_copy(update={"key": world.keys["two"], "gate": "plan"}), epoch
    )
    res = click.post("view", {"request_id": got["request_id"]})
    assert res.status_code == 409 and res.json()["error"]["reason"] == "check_failed"
    assert click.post("view", {"request_id": got["request_id"]}).status_code == 404  # cancelled and forgotten


# -- one action intent -> at most one request -> at most one signature, against the REAL host and signer ---------------


def _second_session(world):
    from conftest import ORIGIN as origin

    launch = world.auth.launch(PORT)
    res = world.client.post("/auth/session", json={"code": launch["code"]}, headers={"Origin": origin})
    return {"Authorization": f"Bearer {res.json()['token']}", "Origin": origin}


def test_the_same_intent_is_the_same_request_for_every_tab_and_retry(world, click):
    """Two prepares with the same intent idem return the same request_id: the signer forwards it and the host honours it."""
    body = {"action": "comment", "key": world.keys["three"], "message": f"intent probe {time.time()}"}
    one = click.prepare(body, fresh=False)
    assert one.status_code == 200, one.text
    again = click.prepare(body, fresh=False)  # a retry, or a reopened dialog after a lost answer
    other = world.client.post(click.url("prepare"), json=body, headers=_second_session(world))  # a second tab
    assert again.json()["request_id"] == other.json()["request_id"] == one.json()["request_id"]
    # Both sessions of the person may view it (it is theirs, not the tab's).
    view = world.client.post(
        click.url("view"), json={"request_id": one.json()["request_id"]}, headers=_second_session(world)
    )
    assert view.status_code == 200, view.text
    click.post("cancel", {"request_id": one.json()["request_id"]})


def test_signed_once_then_already_done_and_only_an_explicit_click_repeats_it(world, click):
    body = {"action": "comment", "key": world.keys["three"], "message": f"sign once {time.time()}"}
    made = click.prepare(body, fresh=False).json()
    view = click.post("view", {"request_id": made["request_id"]}).json()
    time.sleep(1.2)
    signed = click.post("sign", {"request_id": made["request_id"], "view_digest": view["view_digest"]})
    assert signed.status_code == 200, signed.text
    # The same action again, from this tab or another: the host returns its terminal state - no second request.
    for headers in (click.headers(), _second_session(world)):
        res = world.client.post(click.url("prepare"), json=body, headers=headers)
        err = res.json()["error"]
        assert res.status_code == 409 and err["code"] == "intent.done" and err["state"] == "submitted", res.text
    # A second signature of the same request is refused or answered with the recorded result, never a new event.
    again = click.post("sign", {"request_id": made["request_id"], "view_digest": view["view_digest"]})
    assert again.status_code in (200, 404, 409), again.text
    # "Do it again" is a new intent: a new request, signed on purpose.
    more = click.prepare({**body, "retry": 1}, fresh=False)
    assert more.status_code == 200 and more.json()["request_id"] != made["request_id"]
    click.post("cancel", {"request_id": more.json()["request_id"]})


def test_a_cancelled_attempt_stays_ended_and_pressing_again_is_a_fresh_request_signed_only_on_purpose(world, click):
    """#794 (12a19e82, merge train #974): the server steps past an attempt that ended unsigned, so pressing the action
    again is one fresh request instead of `intent.ended`. The cancelled request is never signed afterwards, and the
    fresh one adds nothing to the log until the person views and signs it."""
    body = {"action": "comment", "key": world.keys["three"], "message": f"cancel then press again {time.time()}"}
    before = _head(click, world)
    made = click.prepare(body, fresh=False).json()
    view = click.post("view", {"request_id": made["request_id"]}).json()
    assert click.post("cancel", {"request_id": made["request_id"]}).status_code == 200
    time.sleep(1.2)
    late = click.post("sign", {"request_id": made["request_id"], "view_digest": view["view_digest"]})
    assert late.status_code in (404, 409), late.text  # the cancelled attempt stays cancelled
    again = click.prepare(body, fresh=False)
    assert again.status_code == 200 and again.json()["request_id"] != made["request_id"], again.text
    twin = click.prepare(body, fresh=False)  # a double click or a second tab: the same open request
    assert twin.status_code == 200 and twin.json()["request_id"] == again.json()["request_id"], twin.text
    assert _head(click, world) == before  # nothing was signed
    click.post("cancel", {"request_id": again.json()["request_id"]})


# -- host idem and the signer's durable record, together (core #416 idem, #431 record) ---------------------------------


def _head(click, world) -> int:
    return click.ticket(world.keys["three"])["head"]["seq"]


def test_two_tabs_same_intent_one_request_one_signature_and_a_repeat_adds_no_event(world, click):
    body = {"action": "comment", "key": world.keys["three"], "message": f"both layers {time.time()}"}
    one = click.prepare(body, fresh=False).json()
    second = _second_session(world)
    two = world.client.post(click.url("prepare"), json=body, headers=second).json()
    assert one["request_id"] == two["request_id"]  # (a) the host's idem: one request for both tabs
    rid = one["request_id"]
    view = click.post("view", {"request_id": rid}).json()
    time.sleep(1.2)
    before = _head(click, world)
    first = click.post("sign", {"request_id": rid, "view_digest": view["view_digest"]})
    assert first.status_code == 200 and first.json()["duplicate"] is False, first.text
    after = _head(click, world)
    assert after == before + 1  # exactly one event
    # (b) the other tab signs the same request with the same View: the recorded result, no new event
    again = world.client.post(
        click.url("sign"), json={"request_id": rid, "view_digest": view["view_digest"]}, headers=second
    )
    assert again.status_code == 200 and again.json()["duplicate"] is True, again.text
    assert _head(click, world) == after


def test_a_different_view_for_an_already_signed_request_is_already_signed(world, click):
    body = {"action": "comment", "key": world.keys["three"], "message": f"other view {time.time()}"}
    rid = click.prepare(body, fresh=False).json()["request_id"]
    view = click.post("view", {"request_id": rid}).json()
    time.sleep(1.2)
    assert click.post("sign", {"request_id": rid, "view_digest": view["view_digest"]}).status_code == 200
    after = _head(click, world)
    other = click.post("sign", {"request_id": rid, "view_digest": "sha256:" + "1" * 64})  # (c)
    err = other.json()["error"]
    assert other.status_code == 409 and err["code"] == "human.click_refused" and err["reason"] == "already_signed", (
        other.text
    )
    assert _head(click, world) == after
    # The server did not cancel or forget anything because of it: the request's state still reads submitted.
    assert click.post("state", {"request_id": rid}).json()["state"] == "submitted"
