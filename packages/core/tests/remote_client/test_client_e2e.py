"""R5 T1: the REAL remote client (``orch.remoteclient``) against the real R4 hub and the real R3 host, in process.

The harness is the R4 one (``tests/host/test_remote_hub_e2e.py``: real ``LinkServer`` / ``StreamRouter`` /
``RemoteEndpoint`` and the real host session behind a TLS stream link). Only the dashboard behind the host's adapter is
played by a responder thread, and the presence key is a test-only software signer injected through the backend seam."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from orch.canon.jcs import dumps, loads_strict
from orch.crypto.encoding import b64u, unb64u
from orch.remoteclient import Client, ClientError, Store
from orch.remoteclient import presence as P
from orch.remoteclient.hubconn import dial
from tests.host import remote_port as RP
from tests.host.dispatch_helpers import ok
from tests.host.remote_world import frame
from tests.host.test_remote_hub_e2e import HUB_SRC, Dashboard, _teardown, _world
from tests.host.test_remote_ops import call
from tests.remote_client.fakes import SoftPresence

pytestmark = pytest.mark.skipif(not (HUB_SRC / "orch_hub" / "remote").is_dir(), reason="packages/hub not present")
KINDS = {"HEAD": 1, "BODY": 2, "END": 3, "RESET": 4, "WS_OPEN": 5}


@pytest.fixture
def world(human, served, hub):
    w = _world(human, served, hub, "selective")
    try:
        yield w
    finally:
        _teardown(w)


class Responder(threading.Thread):
    """The dashboard behind the host's adapter: answers app frames with ``handler(method, path, body)``."""

    def __init__(self, dash: Dashboard, handler) -> None:
        super().__init__(daemon=True)
        self.dash, self.handler, self.stop, self.seen = dash, handler, False, []
        self.partial: dict[tuple[str, int], dict[str, Any]] = {}

    def run(self) -> None:
        out: list[bytes] = []
        sid_out = None
        while not self.stop:
            try:
                msgs = self.dash.attach(out, sid_out) if out else self.dash.attach()
            except Exception:  # noqa: BLE001
                return
            out, sid_out = [], None
            for m in msgs:
                if m["t"] != "frame":
                    continue
                sid, pt = m["stream_id"], unb64u(m["bytes"])
                kind, i, p = pt[0], int.from_bytes(pt[1:5], "big"), pt[5:]
                if kind == 1:
                    self.partial[(sid, i)] = {"head": loads_strict(p), "body": b""}
                elif kind == 2 and (sid, i) in self.partial:
                    self.partial[(sid, i)]["body"] += p
                elif kind == 3 and (sid, i) in self.partial:
                    r = self.partial.pop((sid, i))
                    self.seen.append((r["head"]["method"], r["head"]["path"], r["body"]))
                    status, body = self.handler(r["head"]["method"], r["head"]["path"], r["body"])
                    sid_out = sid
                    out += [
                        frame("HEAD", i, dumps({"headers": [["content-type", "application/json"]], "status": status})),
                        frame("BODY", i, body),
                        frame("END", i),
                    ]
            if not out:
                time.sleep(0.01)


def make_client(world, hub, tmp_path, *, presence=None, batch_window=0.2) -> tuple[Client, SoftPresence, Store]:
    pres = presence or SoftPresence()
    pres.key, pres.pub = world.dev.key, world.dev.ppub  # the world's device: its presence key
    st = Store(tmp_path / "rc")
    st.set_hub(origin="http://hub.test", link=hub.addr, hub_id=hub.hub_id, spki=hub.spki)
    st.set_token(hub.token("ann"), 2**50)
    st.put_transport_key(world.ws, world.dev.s)
    st.set_presence("platform", "a" * 32, world.dev.ppub)
    st.add_workspace(
        world.ws,
        {
            "device_id": world.did, "wrk_pub": b64u(world.wrk_pub), "hub_id": hub.hub_id, "presence_kind": "platform",
            "presence_ref": "a" * 32, "transport_pub": b64u(RP.xpub(world.dev.s)), "added_ms": 1,
        },
    )  # fmt: skip
    c = Client(st, presence=pres, connector=lambda h, p, s: dial(h, p, s), batch_window=batch_window)
    return c, pres, st


def start_responder(world, handler) -> Responder:
    r = Responder(world.dash, handler)
    r.start()
    return r


def basic(method, path, body):
    if path == "/api/workspaces":
        return 200, dumps({"workspaces": [{"id": "x", "name": "Other"}]})
    return 200, dumps({"ok": True, "path": path})


def test_rail_lists_the_workspace_with_the_hubs_name_labelled(world, hub, tmp_path):
    c, _, st = make_client(world, hub, tmp_path)
    rows = c.workspaces()
    assert [r["workspace_id"] for r in rows] == [world.ws]
    assert rows[0]["added"] is True and rows[0]["name_from_hub"] is True and rows[0]["online"] is True


def test_session_opens_and_reads_end_to_end(world, hub, tmp_path):
    c, pres, st = make_client(world, hub, tmp_path)
    r = start_responder(world, basic)
    try:
        s = c.open(world.ws)
        assert s.api == 1 and [type(p) for p in pres.prompts] == [P.Prompt] * len(pres.prompts)
        assert "Open workspace" in pres.prompts[0].reason  # no name known yet: short id, never the hub's rail name
        resp = s.request("GET", f"/api/w/{world.ws}/runs", "", [("authorization", "Bearer x"), ("accept", "*/*")])
        assert resp.status == 200 and loads_strict(resp.read())["ok"] is True
        method, path, _ = r.seen[-1]
        assert (method, path) == ("GET", f"/api/w/{world.ws}/runs")
        s.close()
    finally:
        r.stop = True


def test_hub_that_is_not_the_pinned_one_is_refused(world, hub, tmp_path):
    c, _, st = make_client(world, hub, tmp_path)
    st.set_hub(origin="http://hub.test", link=hub.addr, hub_id="ab" * 16, spki=hub.spki)
    with pytest.raises(ClientError) as e:
        c.open(world.ws)
    assert e.value.code == "remote_client.hub_mismatch"
    st.set_hub(origin="http://hub.test", link=hub.addr, hub_id=hub.hub_id, spki=b64u(bytes(32)))
    with pytest.raises(ClientError) as e:
        c.workspaces()
    assert e.value.code == "remote_client.hub_key_changed"


def test_wrong_workspace_key_fails_the_handshake(world, hub, tmp_path):
    c, _, st = make_client(world, hub, tmp_path)
    e = dict(st.workspace(world.ws))
    e["wrk_pub"] = b64u(RP.xpub(bytes([9]) * 32))
    st.add_workspace(world.ws, e)
    with pytest.raises(ClientError) as ex:
        c.open(world.ws)
    assert ex.value.code == "handshake"


def test_cancelled_presence_opens_nothing(world, hub, tmp_path):
    pres = SoftPresence()
    pres.cancel = True
    c, _, _ = make_client(world, hub, tmp_path, presence=pres)
    with pytest.raises(ClientError) as e:
        c.open(world.ws)
    assert e.value.code == "remote_client.cancelled"
    assert world.log("remote.session_opened") == []


def test_version_range_without_overlap_shows_remote_version(world, hub, tmp_path):
    c, _, _ = make_client(world, hub, tmp_path)
    c.api = (2, 2)
    with pytest.raises(ClientError) as e:
        c.open(world.ws)
    assert e.value.code == "remote.version" and "Update orch" in e.value.message


def test_action_needs_presence_digest_recomputed_then_replayed(world, hub, tmp_path):
    c, pres, st = make_client(world, hub, tmp_path)
    ws = world.ws
    path = f"/api/w/{ws}/agents/start"
    tmpl = "/api/w/{ws}/agents/start"
    body = dumps({"model": "sonnet"})
    want = "sha256:" + P.route_digest(ws, "POST", tmpl, path, "", body).hex()
    state = {"taken": False, "presence": None}

    def handler(method, p, b):
        if p == "/api/workspaces":
            return 200, dumps({"workspaces": [{"id": ws, "name": "Acme"}]})
        if p.endswith("/remote/presence/nonce"):
            return 200, dumps({"nonce": b64u(b"n" * 32)})
        if p.endswith("/remote/presence"):
            state["presence"] = loads_strict(b)
            state["taken"] = True
            return 200, dumps({"ok": True, "n": 1})
        if p == path and not state["taken"]:
            err = {"code": "remote.presence_required", "message": "x", "retryable": False, "view_digest": want,
                   "template": tmpl}  # fmt: skip
            return 428, dumps({"ok": False, "error": err})
        return 200, dumps({"started": True})

    r = start_responder(world, handler)
    try:
        s = c.open(ws)
        assert s.name == "Acme" and st.workspace(ws)["name"] == "Acme"  # the name came end to end
        resp = s.request("POST", path, "", [("content-type", "application/json")], body)
        assert resp.status == 200 and loads_strict(resp.read()) == {"started": True}
        sent = state["presence"]
        assert sent["digests"] == [want]
        msg = P.presence_bytes(ws, world.did, s.ch.hh, b"n" * 32, [bytes.fromhex(want[7:])])
        assert RP.assertion_ok(b64u(world.dev.ppub), "platform", msg, sent["assertion"], 0) == 0
        assert pres.prompts[-1].reason == "Confirm 1 action in Acme" and "Acme" not in pres.prompts[0].reason
        assert [x[:2] for x in r.seen if x[1] == path] == [("POST", path)] * 2  # first try and the replay
        s.close()
    finally:
        r.stop = True


def test_digest_mismatch_means_no_prompt_and_a_403(world, hub, tmp_path):
    c, pres, _ = make_client(world, hub, tmp_path)
    ws = world.ws
    path = f"/api/w/{ws}/agents/start"

    def handler(method, p, b):
        if p == path:
            err = {"code": "remote.presence_required", "message": "x", "retryable": False,
                   "view_digest": "sha256:" + "0" * 64, "template": "/api/w/{ws}/agents/start"}  # fmt: skip
            return 428, dumps({"ok": False, "error": err})
        return 200, b"{}"

    r = start_responder(world, handler)
    try:
        s = c.open(ws)
        n = len(pres.prompts)
        resp = s.request("POST", path, "", [], b"{}")
        assert resp.status == 403 and b"not_confirmed" in resp.read()
        assert len(pres.prompts) == n  # no prompt for a digest the client cannot reproduce
        s.close()
    finally:
        r.stop = True


def test_template_not_matching_the_path_means_no_prompt(world, hub, tmp_path):
    c, pres, _ = make_client(world, hub, tmp_path)
    ws = world.ws
    path = f"/api/w/{ws}/agents/start"
    other = "/api/w/{ws}/workdir/delete"
    d = "sha256:" + P.route_digest(ws, "POST", other, path, "", b"{}").hex()

    def handler(method, p, b):
        err = {
            "code": "remote.presence_required",
            "message": "x",
            "retryable": False,
            "view_digest": d,
            "template": other,
        }
        return (428, dumps({"ok": False, "error": err})) if p == path else (200, b"{}")

    r = start_responder(world, handler)
    try:
        s = c.open(ws)
        n = len(pres.prompts)
        assert s.request("POST", path, "", [], b"{}").status == 403 and len(pres.prompts) == n
        s.close()
    finally:
        r.stop = True


def test_seventeen_actions_make_two_prompts_sorted_unique(world, hub, tmp_path):
    c, pres, _ = make_client(world, hub, tmp_path, batch_window=0.5)
    ws = world.ws
    taken: set[str] = set()
    batches: list[list[str]] = []

    def route(i):
        return f"/api/w/{ws}/runs/r_{i:02d}/attach"

    tmpl = "/api/w/{ws}/runs/{run}/attach"

    def handler(method, p, b):
        if p.endswith("/remote/presence/nonce"):
            return 200, dumps({"nonce": b64u(b"n" * 32)})
        if p.endswith("/remote/presence"):
            d = loads_strict(b)["digests"]
            assert d == sorted(set(d))
            batches.append(d)
            taken.update(d)
            return 200, dumps({"ok": True})
        want = "sha256:" + P.route_digest(ws, "POST", tmpl, p, "", b"").hex()
        if want in taken:
            return 200, b"{}"
        err = {
            "code": "remote.presence_required",
            "message": "x",
            "retryable": False,
            "view_digest": want,
            "template": tmpl,
        }
        return 428, dumps({"ok": False, "error": err})

    r = start_responder(world, handler)
    try:
        s = c.open(ws)
        results = []

        def go(i):
            try:
                results.append(s.request("POST", route(i), "", [], b"").status)
            except ClientError as e:
                results.append(e.code)

        ts = [threading.Thread(target=go, args=(i,)) for i in range(17)]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        assert s.closed is None, s.closed
        assert results.count(200) == 17, results
        assert all(len(b) <= 16 for b in batches) and sum(len(b) for b in batches) == 17
        s.close()
    finally:
        r.stop = True


def test_enrollment_end_to_end_six_words_then_added(world, hub, human, served, tmp_path):
    pres = SoftPresence()
    st = Store(tmp_path / "rc2")
    st.set_hub(origin="http://hub.test", link=hub.addr, hub_id=hub.hub_id, spki=hub.spki)
    st.set_token(hub.token("ann"), 2**50)
    c = Client(st, presence=pres, connector=lambda h, p, s: dial(h, p, s))
    code = ok(call(served, "remote.code"))["data"]
    assert c.enroll is not None
    words: list[str] = []
    result: dict[str, Any] = {}

    def run():
        try:
            result["r"] = c.enroll(code["code"], "Test laptop", words.append)
        except ClientError as e:  # pragma: no cover
            result["err"] = e.code

    t = threading.Thread(target=run)
    t.start()
    end = time.time() + 20
    pend = []
    while not pend and time.time() < end:
        pend = ok(call(served, "remote.pending"))["data"]["pending"]
        time.sleep(0.05)
    assert pend, result
    deadline = time.time() + 5
    while not words and time.time() < deadline:
        time.sleep(0.05)
    assert words == [pend[0]["words"]] and len(words[0].split()) == 6  # the same six words the owner sees
    human.run("remote.device_confirm", {"pending": pend[0]["pending"], "level_cap": "work", "days": 30})
    t.join(20)
    assert "err" not in result, result
    got = result["r"]
    assert got["workspace_id"] == world.ws and got["device_id"].startswith("rd_")
    e = st.workspace(world.ws)
    assert e["device_id"] == got["device_id"] and st.transport_key(world.ws) is not None
    assert pres.signed and pres.signed[0].startswith(b"orch/v2/remote-open|")
    assert code["code"] not in str(e)  # the code is never stored


def test_browser_code_and_wrong_wrk_are_refused_before_any_hub_call(tmp_path):
    from orch.remoteclient.enroll import parse_code

    with pytest.raises(ClientError) as e:
        parse_code("orch-rb1.abc")
    assert e.value.code == "remote_client.browser_code"
    ws = "a" * 32
    body = b"\x01" * 48
    with pytest.raises(ClientError) as e:
        parse_code(f"orch-rc1.{ws}.{b64u(body)}.{b64u(b'w' * 32)}")
    assert e.value.code == "remote_client.wrk_mismatch"
    with pytest.raises(ClientError):
        parse_code("orch-rc1.nothex.AA.AA")
