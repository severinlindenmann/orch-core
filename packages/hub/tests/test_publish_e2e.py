"""D11 end to end: a host-signed request forwarded by the hub is accepted by a REAL orch-publish while the workspace
is bound to the hub, and refused after unbound or when a header is tampered with.

Needs an orch-publish checkout with its dependencies: set ORCH_PUBLISH_SRC to it (its .venv/bin/python is used).
Without it the test is skipped; the contract-level equivalence is covered in test_publish_forwarder.py.
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

SRC = os.environ.get("ORCH_PUBLISH_SRC")
pytestmark = [
    pytest.mark.skipif(
        not SRC or not (Path(SRC or "x") / ".venv/bin/python").exists(), reason="ORCH_PUBLISH_SRC not set"
    ),
    pytest.mark.slow,
]


@pytest.fixture
def pub():
    p = subprocess.Popen(
        [str(Path(SRC) / ".venv/bin/python"), str(Path(__file__).parent / "publish_e2e_server.py")],
        cwd=SRC,
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
    wsk = wsk_key(json.loads((Path(SRC) / "tests/vectors/publish_v2.json").read_text())["keys"]["wsk_a.sig"]["seed"])
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
