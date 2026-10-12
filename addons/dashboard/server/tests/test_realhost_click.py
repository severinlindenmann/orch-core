"""Click-to-sign against a REAL host and the REAL signer child (core H5c, orch-v2-host section 0.8.6).

Disposable everything: `realhost_kit` builds a throwaway workspace (explicit ORCH_STATE_DIR/HOME/XDG_CONFIG_HOME under
a short path with `.orch-dev` markers, never `~/.config/orch`). The device key is moved into convenience custody with the
real `orch keys custody convenience` in a pty, and the signer runs core's real code; the only difference from production
is the custody store: `click_child.py` swaps `open_store` for the 0600 file store of the disposable state (core's
documented test seam), so the macOS keychain is never opened. One workspace, one person, no relay, no phone.
"""

import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from conftest import ORIGIN, PORT
from fastapi.testclient import TestClient
from orch.click.client import ClickClient
from realhost_kit import Pty, Workspace, confirm_rules, key_of, ready_requirements, short_run_root

from orch_dashboard.app import create_app
from orch_dashboard.auth import Auth
from orch_dashboard.click_signer import Signer
from orch_dashboard.gateway import Gateway

pytestmark = [
    pytest.mark.realhost,
    pytest.mark.skipif(os.name != "posix", reason="a real host needs unix sockets and a pty"),
]
CHILD = str(Path(__file__).with_name("click_child.py"))
RISK = "Convenience signing: agents running as you, or anything that takes over this dashboard, could sign."
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_idem(n: int) -> str:
    return "a_0" + f"{n:025d}".translate(str.maketrans("0123456789", "0123456789"))[-25:]


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
        run(ws, "host", "convenience", "on", "--workspace", ws.ws_id, "--state", str(ws.state))
        # The real `orch keys custody convenience`, in a pty (it prompts for the passphrase once), with the file store.
        convert = Pty(
            [sys.executable, "-I", CHILD, "cli", "keys", "custody", "convenience", "--workspace", ws.ws_id],
            ws.env(),
            ws.root,
            confirm_rules(),
            90,
        ).run()
        assert convert.code == 0, convert.output[-1500:]
    except (RuntimeError, TimeoutError, OSError, AssertionError) as exc:
        ws.stop_host()
        shutil.rmtree(root, ignore_errors=True)
        pytest.skip(f"the real host cannot start here: {str(exc)[:400]}")
    children = []

    def start(env):
        parent, child = socket.socketpair()
        n = child.fileno()
        own = {**env, "HOME": str(ws.home), "XDG_CONFIG_HOME": str(ws.home / ".config")}
        own["ORCH_HOST_RUN_DIR"], own["ORCH_RUN_DIR"] = str(ws.run), str(ws.run)
        proc = subprocess.Popen(
            [sys.executable, "-I", CHILD, "signer", "--fd", str(n)],
            pass_fds=(n,),
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            env=own,
        )
        child.close()
        children.append(proc)
        return ClickClient(parent, proc)

    os.environ.pop("ORCH_DASHBOARD_CLICK", None)
    static = tmp_path_factory.mktemp("static")
    (static / "assets").mkdir()
    (static / "index.html").write_text("<html></html>")
    old = os.environ.get("ORCH_RUN_DIR")
    os.environ["ORCH_RUN_DIR"] = str(ws.run)
    gateway = Gateway(ws.state, click=Signer(ws.state, start=start))
    auth = Auth()
    with TestClient(create_app(PORT, gateway, auth, static), base_url=ORIGIN) as client:
        launch = client.post("/auth/session", json={"code": auth.launch(PORT)["code"]}, headers={"Origin": ORIGIN})
        token = launch.json()["token"]
        yield type("World", (), {"ws": ws, "keys": keys, "gateway": gateway, "client": client, "token": token})
    gateway.close()
    for proc in children:
        assert proc.poll() is not None  # the signer exits with the server
    if old is None:
        os.environ.pop("ORCH_RUN_DIR", None)
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

    def prepare(self, body):
        self.n += 1
        return self.post("prepare", {**body, "idem": new_idem(int(time.time() * 1000) % 10**9 + self.n)})

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
    assert ws["click"] == {"custody": "convenience", "enabled": True, "available": True}
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


def test_a_word_op_needs_the_word_the_view_shows(world, click):
    policy = {
        "approvers": ["owner"],
        "count": 1,
        "not": [],
        "applies": "all",
        "independent": False,
    }
    got = click.prepare({"action": "policy_set", "gate": "requirements", "policy": policy})
    assert got.status_code == 200, got.text
    assert got.json()["confirm"] == "word"
    rid = got.json()["request_id"]
    view = click.post("view", {"request_id": rid}).json()
    word = view["view"]["confirm"]["word"]
    assert view["view"]["confirm"]["kind"] == "word" and word.endswith("/POLICY") and "~" in word
    time.sleep(1.2)
    for wrong in (None, "policy", word.lower()):
        body = {"request_id": rid, "view_digest": view["view_digest"], **({"word": wrong} if wrong else {})}
        res = click.post("sign", body)
        assert res.status_code == 409 and res.json()["error"]["reason"] == "word", wrong
    ok = click.post("sign", {"request_id": rid, "view_digest": view["view_digest"], "word": word})
    assert ok.status_code == 200, ok.text


def test_cancel_cancels_on_the_host(world, click):
    got = click.prepare(approve(world, "three")).json()
    res = click.post("cancel", {"request_id": got["request_id"]})
    assert res.status_code == 200 and res.json()["state"] == "cancelled"
    assert click.post("view", {"request_id": got["request_id"]}).status_code == 404


def test_the_terminal_path_is_a_separate_handoff_request(world, click):
    body = {**approve(world, "three"), "idem": "a_0" + "1" * 25}
    res = world.client.post(f"/api/w/{world.ws.ws_id}/handoff", json=body, headers=click.headers())
    assert res.status_code == 200 and re.fullmatch(r"[0-9A-Z]{4}-[0-9A-Z]{4}", res.json()["code"])
    world.client.delete(f"/api/w/{world.ws.ws_id}/handoff/{res.json()['id']}", headers=click.headers())


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
    assert res.status_code == 409 and res.json()["error"]["reason"] == "not_prepared_here"  # terminal-only now
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
    token = click.headers()["Authorization"].split()[1]
    op, body, epoch = app.state.clicks.ops[(token, got["request_id"])]
    app.state.clicks.ops[(token, got["request_id"])] = (op, body.model_copy(update={"key": world.keys["two"], "gate": "plan"}), epoch)
    res = click.post("view", {"request_id": got["request_id"]})
    assert res.status_code == 409 and res.json()["error"]["reason"] == "check_failed"
    assert click.post("view", {"request_id": got["request_id"]}).status_code == 404  # cancelled and forgotten
