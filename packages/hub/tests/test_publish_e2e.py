"""D11 end to end: a host-signed request forwarded by the hub is accepted by a REAL orch-publish while the workspace
is bound to the hub, and refused after unbound or when a header is tampered with.

Starts packages/publish in its own environment (`uv run --locked --project`); the first run syncs that venv.
The contract-level equivalence is also covered in test_publish_forwarder.py.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from publish_helpers import NOW, binding, host_request, make_hub, new_wsk, unbound, wsk_key

from orch_hub.contract.canon import cj
from orch_hub.contract.hub import Refused
from orch_hub.publish import HttpPublishSync, HubPublish, PublishConfig

PUBLISH = Path(__file__).resolve().parents[2] / "publish"
pytestmark = pytest.mark.slow


@pytest.fixture
def pub():
    p = subprocess.Popen(
        [
            "uv",
            "run",
            "--locked",
            "--project",
            str(PUBLISH),
            "python",
            str(Path(__file__).parent / "publish_e2e_server.py"),
        ],
        cwd=PUBLISH,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert p.stdout
        first = p.stdout.readline().split()
        assert first[0] == "PORT", first
        yield p, int(first[1]), first[3]
    finally:
        p.stdin.close()
        p.terminate()  # our own child
        p.wait(10)


@pytest.fixture
def env(tmp_path, pub):
    proc, port, ws = pub
    hub = make_hub(tmp_path)
    wsk = wsk_key(
        json.loads((Path(__file__).resolve().parents[3] / "contracts" / "publish" / "publish_v2.json").read_text())[
            "keys"
        ]["wsk_a.sig"]["seed"]
    )
    host = f"127.0.0.1:{port}"
    fw = HubPublish(hub, HttpPublishSync(f"http://{host}"), PublishConfig(f"http://{host}", host, timeout_s=10))
    return type("E", (), {"hub": hub, "wsk": wsk, "ws": ws, "fw": fw, "host": host, "proc": proc})()


def status(e, **kw):
    out = e.fw.forward(e.ws, host_request(e.wsk, e.hub.hub_id, e.ws, host=e.host, **kw))
    return out["status"], json.loads(bytes.fromhex(out["body_hex"]))


def test_bound_request_is_accepted_then_refused_after_unbound(env):
    e = env
    e.fw.binding(e.ws, binding(e.wsk, e.hub, e.ws, seq=1))
    assert status(e) == (200, {"workspace": e.ws, "status": "active"})
    # a replayed nonce is refused by orch-publish (hub does not add authority)
    n = os.urandom(16)
    assert status(e, nonce=n)[0] == 200
    assert status(e, nonce=n) == (401, {"error": "replay"})
    # the workspace unbinds: the hub learns it and stops locally ...
    ub = unbound(e.wsk, e.hub, e.ws, seq=1)
    e.fw.binding(e.ws, ub)
    with pytest.raises(Refused) as x:
        status(e)
    assert x.value.code == "not_bound"
    # ... and even a hub still holding the old binding is refused BY ORCH-PUBLISH
    e.fw._bindings[e.ws] = binding(e.wsk, e.hub, e.ws, seq=1)
    e.fw._synced[e.ws] = cj(e.fw._bindings[e.ws])  # the hub believes orch-publish still holds it
    assert status(e) == (403, {"error": "not_bound"})


def test_tampered_request_is_refused_by_orch_publish(env):
    e = env
    e.fw.binding(e.ws, binding(e.wsk, e.hub, e.ws))
    good = host_request(e.wsk, e.hub.hub_id, e.ws, host=e.host)
    # target changed after the host signed: its signature no longer verifies
    bad = dict(good, target=f"/v1/ws/{e.ws}/files")
    out = e.fw.forward(e.ws, bad)
    assert out["status"] == 401 and json.loads(bytes.fromhex(out["body_hex"])) == {"error": "bad_signature"}
    # a different timestamp: same
    h = dict(good["headers"], **{"Orch-Ts": str(NOW + 1)})
    out = e.fw.forward(e.ws, dict(good, headers=h))
    assert out["status"] == 401
    # a body changed after signing
    ok = host_request(e.wsk, e.hub.hub_id, e.ws, host=e.host, method="PUT", target=f"/v1/ws/{e.ws}/files/x", body=b"a")
    out = e.fw.forward(e.ws, dict(ok, body_hex=b"b".hex()))
    assert out["status"] == 401


def test_workspace_key_other_than_bound_is_refused(env):
    e = env
    e.fw.binding(e.ws, binding(e.wsk, e.hub, e.ws))
    assert status(e)[0] == 200
    other = new_wsk()
    out = e.fw.forward(e.ws, host_request(other, e.hub.hub_id, e.ws, host=e.host))
    assert out["status"] == 401


def test_binding_by_another_key_is_refused_at_sync(env):
    e = env
    e.fw.binding(e.ws, binding(new_wsk(), e.hub, e.ws))  # swallowed; the forward then reports the refusal
    with pytest.raises(Refused) as x:
        status(e)
    assert x.value.code == "bad_signature"


def test_revoked_op_cert_is_refused_by_orch_publish(env):
    e = env
    e.fw.binding(e.ws, binding(e.wsk, e.hub, e.ws))
    assert status(e)[0] == 200
    old_signer, old_card, hub_id = e.hub.op_signer, e.hub.card(), e.hub.hub_id
    e.hub.revoke_op()
    fw = HubPublish(
        lambda: (old_signer, old_card, hub_id),
        e.fw._sync,
        e.fw._cfg,
        revocations=e.hub.revocations,
    )
    fw.binding(e.ws, binding(e.wsk, e.hub, e.ws))
    out = fw.forward(e.ws, host_request(e.wsk, hub_id, e.ws, host=e.host))
    assert out["status"] in (401, 403)
    assert json.loads(bytes.fromhex(out["body_hex"]))["error"] in ("revoked", "unknown_hub", "bad_hub_signature")


def test_1452_who_and_what_of_a_share_reach_the_admin_list_end_to_end(env, tmp_path):
    """A share PUT carrying label and by_kind/by_name goes through the hub forwarder into a real orch-publish; the hub's
    admin read returns it, and GET /admin/published shows label, by name, agent marker, workspace, until and views."""
    import hashlib

    from webapi_helpers import Web

    from orch_hub.publish import AdminReadClient

    e = env
    e.fw.binding(e.ws, binding(e.wsk, e.hub, e.ws, mode="full"))
    sid = os.urandom(16).hex()
    exp = NOW + 3 * 86_400_000
    meta = {
        "ticket": "TST-0001", "kind": "secret", "expires_ms": exp, "view_limit": 0,
        "token_hash": hashlib.sha256(b"t").hexdigest(),
        "files": [{"path": "a.txt", "size": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}],
        "label": "Q3 report", "by_kind": "agent", "by_name": "Ana",
    }  # fmt: skip
    put = host_request(e.wsk, e.hub.hub_id, e.ws, host=e.host, method="PUT",
                       target=f"/v1/ws/{e.ws}/shares/{sid}", body=json.dumps(meta).encode())  # fmt: skip
    out = e.fw.forward(e.ws, put)
    assert out["status"] == 200, out
    # a staged (uncommitted) share is not a release: commit it so it is listed
    c = host_request(e.wsk, e.hub.hub_id, e.ws, host=e.host, method="PUT",
                     target=f"/v1/ws/{e.ws}/shares/{sid}/files/a.txt", body=b"abc")  # fmt: skip
    assert e.fw.forward(e.ws, c)["status"] == 200
    k = host_request(e.wsk, e.hub.hub_id, e.ws, host=e.host, method="POST", target=f"/v1/ws/{e.ws}/shares/{sid}/commit")
    assert e.fw.forward(e.ws, k)["status"] == 200
    rows = AdminReadClient(f"http://{e.host}", e.hub.op_signer, e.hub.hub_id, now_ms=lambda: NOW).published()
    assert [(r["id"], r["label"], r["by_kind"], r["by_name"]) for r in rows] == [(sid, "Q3 report", "agent", "Ana")]

    (tmp_path / "web").mkdir()
    w = Web(tmp_path / "web")
    try:
        w.reg.add(e.ws, "full", name="Product")

        class Reader:
            def published(self, workspace=None):
                return rows

        w.api.d.published = Reader()
        s, o, _ = w.call("GET", "/admin/published", token=w.login(w.admin))
        assert s == 200
        (link,) = o["links"]
        assert (link["label"], link["by_kind"], link["by_name"], link["workspace"]) == (
            "Q3 report",
            "agent",
            "Ana",
            "Product",
        )
        assert link["expires_ms"] == exp and link["views"] == 0 and link["state"] == "live"
    finally:
        w.close()
