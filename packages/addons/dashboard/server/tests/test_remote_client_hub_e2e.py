"""R5 T4: the REAL remote client (core ``Client`` + the loopback ``Controller`` and its listeners) against the real R4
hub and the real R3 host, in process, driven only through the loopback HTTP API a browser would use.

Only the dashboard behind the host's adapter is played (a responder thread, as in the core e2e) and the presence key
is the tests-only software signer behind the backend seam. Flow: connect with a connect link (pinned) -> list -> add
this computer (six words, the owner confirms at the workspace) -> open -> the page comes from the local build -> a
presence-gated action shows Confirm with the workspace's OWN name -> End."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from orch.canon.jcs import dumps, loads_strict
from orch.crypto.encoding import b64u
from orch.hubclient import connectlink
from orch.remoteclient import Client, Store
from orch.remoteclient import presence as P
from orch.remoteclient.hubconn import dial
from remote_client_kit import make_static, raw

from orch_dashboard.remote_client import Controller, GatedPresence, StaticSite
from tests.host.dispatch_helpers import ok
from tests.host.human_helpers import human, lab, served  # noqa: F401
from tests.host.test_remote_hub_e2e import HUB_SRC, dev_workspace, hub, hub_kw  # noqa: F401
from tests.host.test_remote_ops import call
from tests.remote_client.fakes import SoftPresence
from tests.remote_client.test_client_e2e import Responder, world  # noqa: F401

pytestmark = pytest.mark.skipif(not (HUB_SRC / "orch_hub" / "remote").is_dir(), reason="packages/hub not present")
OWN_NAME = "Billing Service"
SESSION_PAGE = (
    '<html><head><meta charset="utf-8"><title>Mission Control</title></head><body>'
    '<div id="root">DASHBOARD</div></body></html>'
)


def redirect_target(uri: str) -> str:
    text = Path(unquote(urlparse(uri).path)).read_text()
    return json.loads(text.split("location.replace(")[1].split(");")[0])


class Origin_:
    """What a browser does against one loopback origin: redeem the launch code once, then call with the bearer."""

    def __init__(self, url: str) -> None:
        base, code = url.split("/#launch=")
        self.port = int(base.rsplit(":", 1)[1])
        self.origin = base
        st, _, body = raw(
            self.port, "POST", "/auth/session",
            headers={"Origin": self.origin, "Content-Type": "application/json"},
            body=json.dumps({"code": code}).encode(),
        )  # fmt: skip
        assert st == 200
        self.token = json.loads(body)["token"]

    def call(self, method: str, path: str, doc=None, *, timeout: float = 30.0):
        h = {"Authorization": f"Bearer {self.token}", "Origin": self.origin, "Content-Type": "application/json"}
        st, hs, b = raw(
            self.port,
            method,
            path,
            headers=h,
            body=None if doc is None else json.dumps(doc, separators=(",", ":")).encode(),
            timeout=timeout,
        )
        return st, hs, b


def fake_hub_http(rig):
    """The hub's web API (card and sign-in) as the harness has it: the card of its keys, a token of ``ann``."""

    def http(method, url, body, headers):
        if url.endswith("/api/v1/hub/card"):
            return 200, dumps({"card": rig.card})
        if url.endswith("/api/v1/session"):
            return 200, dumps({"token": rig.token("ann"), "expires_ms": 2**50})
        return 404, b""

    return http


@pytest.fixture
def stack(world, hub, human, served, tmp_path, monkeypatch):  # noqa: F811
    soft = SoftPresence()
    gate = GatedPresence(soft, wait_s=30)
    client = Client(
        Store(tmp_path / "rc"), presence=gate, http=fake_hub_http(hub), dev_mode=True,
        connector=lambda h, p, s: dial(h, p, s), batch_window=0.2,
    )  # fmt: skip
    link = SimpleLink()
    monkeypatch.setattr(
        connectlink, "resolve", lambda text, **k: {"endpoint": hub.addr, "hub_card": {"tls_spki_sha256": hub.spki}}
    )
    monkeypatch.setattr(connectlink, "parse_link", lambda t: link)
    opened: list[str] = []
    static = StaticSite.load(_static(tmp_path))
    ctl = Controller(
        client, static, gate=gate, opener=opened.append, computer_name="Test Mac", launch_dir=tmp_path / "launch"
    )
    ctl.start_home()
    ctl.open_home()
    home = Origin_(redirect_target(opened.pop(0)))
    ctl.card = hub.card  # noqa: B010
    try:
        yield SimpleNamespace_(
            ctl=ctl, home=home, opened=opened, soft=soft, client=client, world=world, hub=hub, world_human=human
        )
    finally:
        ctl.stop()


class SimpleLink:
    def __init__(self) -> None:
        self.origin = "http://127.0.0.1:9"


def _static(tmp_path: Path) -> Path:
    real = os.environ.get("ORCH_R5_STATIC")  # the browser run: the real built UI (dist) instead of the stand-in
    if real:
        return Path(real)
    root = make_static(tmp_path / "static")
    (root / "index.html").write_text(SESSION_PAGE)
    return root


class SimpleNamespace_:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


class Person:
    """The person at the home page: confirms every prompt the client shows (and remembers what it said)."""

    def __init__(self, home: Origin_, answer: bool = True) -> None:
        self.home, self.answer, self.seen, self.stop = home, answer, [], False
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()

    def run(self) -> None:
        while not self.stop:
            try:
                st = json.loads(self.home.call("GET", "/api/remote-client/state")[2])
            except Exception:  # noqa: BLE001
                return
            p = st.get("prompt")
            if p and p["id"] not in [s["id"] for s in self.seen]:
                self.seen.append(p)
                self.home.call("POST", "/api/remote-client/answer", {"id": p["id"], "approve": self.answer})
            time.sleep(0.05)


def api(o: Origin_, method: str, path: str, doc=None):
    st, _, b = o.call(method, "/api/remote-client" + path, doc)
    return st, json.loads(b)


def test_connect_list_add_open_confirm_end(stack, served, human):  # noqa: F811
    s, w = stack, stack.world
    person = Person(s.home)
    try:
        # connect with a connect link (pinned): the card's own key is the pin, the sign-in follows
        assert api(
            s.home,
            "POST",
            "/connect",
            {"link": "https://hub.test/c#x", "user": "ann", "password": "pw", "code": "123456"},
        ) == (200, {"ok": True})
        assert s.client.store.hub()["spki"] == s.hub.spki
        assert api(s.home, "GET", "/state")[1]["signed_in"] is True

        # list: not added yet, the hub's word for the name, labelled as such
        (row,) = api(s.home, "GET", "/workspaces")[1]["workspaces"]
        assert row["workspace_id"] == w.ws and row["added"] is False and row["name_from_hub"] is True

        # add this computer: six words equal the owner's, then the owner confirms at the workspace
        code = ok(call(served, "remote.code"))["data"]
        st, job = api(s.home, "POST", "/enroll", {"code": code["code"]})
        assert st == 200
        end = time.time() + 20
        pend = []
        while not pend and time.time() < end:
            pend = ok(call(served, "remote.pending"))["data"]["pending"]
            time.sleep(0.05)
        assert pend
        words = []
        while len(words) != 6 and time.time() < end:
            words = api(s.home, "GET", f"/enroll/{job['id']}")[1]["words"]
            time.sleep(0.05)
        assert " ".join(words) == pend[0]["words"]
        human.run("remote.device_confirm", {"pending": pend[0]["pending"], "level_cap": "work", "days": 30})
        status = {}
        while status.get("status") != "added" and time.time() < end:
            status = api(s.home, "GET", f"/enroll/{job['id']}")[1]
            time.sleep(0.05)
        assert status["status"] == "added", status
        assert api(s.home, "GET", "/workspaces")[1]["workspaces"][0]["added"] is True
        assert code["code"] not in str(s.client.store.workspace(w.ws))

        # open: the dashboard behind the host answers; the page is the LOCAL build
        path = f"/api/w/{w.ws}/agents/start"
        tmpl = "/api/w/{ws}/agents/start"
        body = dumps({"model": "sonnet"})
        want = "sha256:" + P.route_digest(w.ws, "POST", tmpl, path, "", body).hex()
        done = {"taken": False}

        def handler(method, p, b):
            if p == "/api/workspaces":
                return 200, dumps({"workspaces": [{"id": w.ws, "name": OWN_NAME}]})
            if p == "/api/session":
                return 200, dumps({"login": "remote-device", "features": ["board"], "workspaces": {}})
            if p.endswith("/remote/presence/nonce"):
                return 200, dumps({"nonce": b64u(b"n" * 32)})
            if p.endswith("/remote/presence"):
                done["taken"] = True
                return 200, dumps({"ok": True, "n": 1})
            if p == path and not done["taken"]:
                err = {
                    "code": "remote.presence_required",
                    "message": "x",
                    "retryable": False,
                    "view_digest": want,
                    "template": tmpl,
                }
                return 428, dumps({"ok": False, "error": err})
            return 200, dumps({"started": True})

        r = Responder(w.dash, handler)
        r.start()
        try:
            assert api(s.home, "POST", "/open", {"workspace_id": w.ws})[0] == 200
            assert len(s.opened) == 1 and s.opened[0].startswith("file://") and "launch=" not in s.opened[0]
            sess = Origin_(redirect_target(s.opened[0]))
            assert sess.port != s.home.port
            st, hs, page = sess.call("GET", "/")
            assert st == 200 and b"Mission Control" in page  # from the installed build, not the workspace
            st, _, b = sess.call("GET", "/api/session")
            assert st == 200 and loads_strict(b)["login"] == "remote-device"
            assert api(s.home, "GET", "/state")[1]["open"] == [w.ws]

            # a presence-gated action: the confirm names the workspace by ITS OWN name, then the action goes through
            st, _, b = sess.call("POST", path, {"model": "sonnet"})
            assert st == 200 and loads_strict(b) == {"started": True}
            confirm = person.seen[-1]
            assert confirm["workspace"] == OWN_NAME and confirm["lines"]
            assert s.client.store.workspace(w.ws)["name"] == OWN_NAME
        finally:
            r.stop = True

        # End: the origin goes away
        assert api(s.home, "POST", "/end", {"workspace_id": w.ws})[0] == 200
        assert api(s.home, "GET", "/state")[1]["open"] == []
        with pytest.raises(OSError):
            raw(sess.port, "GET", "/", timeout=2)
    finally:
        person.stop = True
