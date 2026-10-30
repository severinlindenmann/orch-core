"""R5 T3: the two shapes the client core GUESSED (T1) are checked against the real dashboard handlers' output.

* ``GET /api/workspaces``: how the client reads a workspace's OWN name through the end-to-end session;
* ``POST /api/w/{ws}/click/view``: how the client recomputes a click-to-sign review's digest (it never trusts the
  workspace's ``view_digest``; a presence prompt is only drawn for a digest the client derived from the view itself).
"""

from __future__ import annotations

import sys

import pytest
from orch.remoteclient import Client, Response, Session, Store
from remote_client_kit import FakeSession
from test_realhost_agents import aw  # noqa: F401  (the real-host fixture: core's host, signer and the dashboard app)


def response(status: int, body: bytes) -> Response:
    chunks = [body]
    return Response(
        status, [("content-type", "application/json")], lambda t: chunks.pop(0) if chunks else None, lambda: None
    )


def test_the_workspace_name_is_read_from_the_real_workspaces_answer(client, token, stub, tmp_path):
    real = client.get("/api/workspaces", headers={"Authorization": f"Bearer {token}"})
    assert real.status_code == 200
    rows = real.json()
    assert isinstance(rows, list) and rows[0]["id"] == stub.ws and isinstance(rows[0]["name"], str)  # the shape
    store = Store(tmp_path / "rc")
    store.add_workspace(stub.ws, {"device_id": "rd_" + "1" * 32, "presence_ref": "ref-1", "presence_kind": "platform"})
    sess = FakeSession(stub.ws, name=None)
    sess.request = lambda *a, **k: response(200, real.content)  # exactly the bytes the handler sent
    Client(store)._learn_name(sess)  # noqa: SLF001
    assert sess.name == rows[0]["name"] == "Acme"
    assert store.workspace(stub.ws)["name"] == "Acme"  # kept, so prompts of later sessions use it from the start


@pytest.mark.realhost
@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")
def test_the_click_view_digest_is_recomputed_from_the_real_reply(aw):  # noqa: F811
    p = aw.post("/click/prepare", {"action": "runner_grant", "hours": 8, "scope": "workable"})
    assert p.status_code == 200, p.text
    assert "view" not in p.json()  # prepare names the request only; the review comes from click/view
    v = aw.post("/click/view", {"request_id": p.json()["request_id"]})
    assert v.status_code == 200, v.text
    reply = v.json()
    assert set(reply) >= {"view", "view_digest"} and reply["view_digest"].startswith("sha256:")
    s = Session(None, workspace_id=aw.w.wid, device_id="rd_" + "1" * 32, api=1, assert_=lambda m, p: {})  # not started
    mine = s._record_view_bytes(v.content)  # noqa: SLF001 - the bytes the page would have received
    assert mine == reply["view_digest"]  # the client's own SHA-256 over the view equals the signer's
    assert s._known_view(mine)  # noqa: SLF001
    # the signer's headline is what the prompt line says, not anything the workspace sends separately
    assert s._views[mine][1] == F_plain(reply["view"]["headline"])  # noqa: SLF001
    # a tampered view does not match the digest the workspace names
    bad = dict(reply, view=dict(reply["view"], headline="Pay the attacker"))
    import json

    assert s._record_view_bytes(json.dumps(bad).encode()) != reply["view_digest"]  # noqa: SLF001


def F_plain(text: str) -> str:  # noqa: N802
    from orch.remoteclient.session import _plain_line

    return _plain_line(text)
