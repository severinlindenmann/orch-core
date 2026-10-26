"""D11 over the real link: publish_out through a real LinkServer (TLS, signed frames) into HubPublish and on to a
loopback orch-publish double. HubPublish is also the link's Downstream, so the join's binding reaches orch-publish."""

from __future__ import annotations

import pytest
from publish_helpers import HOST, FakeUpstream, RecordingSync, host_request

from orch_hub.contract import hub as H
from orch_hub.contract import suite as S
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import unb64u
from orch_hub.contract.keystore import OpSigner
from orch_hub.link import LinkError
from orch_hub.publish import HubPublish, PublishConfig


@pytest.fixture
def up():
    u = FakeUpstream()
    yield u
    u.close()


def test_publish_out_over_the_link_is_forwarded_with_the_transport_signature(make_hub, up):
    sync = RecordingSync()
    signer = OpSigner(G.K.priv["hk_op"])
    fw = HubPublish(lambda: (signer, G.card(), G.HUB_ID), sync, PublishConfig(up.url, HOST, timeout_s=3))
    h = make_hub(publish=fw, downstream=fw)
    c = h.open(first=True)
    assert c.request("join", h.join_body()) == {}
    # the join's card (admission) and binding reached the sync seam, the hub's own card first for the binding
    assert [x[0] for x in sync.calls] == ["ws_card", "card", "binding"]

    up.status, up.body = 201, b"made"
    req = host_request(G.K.priv["wsk_a"], G.HUB_ID, G.WS_A, method="PUT", target="/v1/ws/x/files/a", body=b"data")
    assert c.request("publish_out", {"request": req}) == {"status": 201, "body_hex": b"made".hex()}

    (seen,) = up.requests
    hdr = {k.lower(): v for k, v in seen["headers"].items()}
    assert seen["body"] == b"data" and hdr["orch-hub"] == G.HUB_ID
    pb = H.hub_path_signed_bytes(
        bytes.fromhex(G.WS_A),
        int(req["headers"]["Orch-Ts"]),
        bytes.fromhex(req["headers"]["Orch-Nonce"]),
        "PUT",
        HOST,
        "/v1/ws/x/files/a",
        b"data",
        G.HUB_ID,
        None,
    )
    assert S.verify(G.K.pub["hk_op"], unb64u(hdr["orch-hub-signature"], 64), H.hub_publish_bytes(G.HUB_ID, pb))

    # malformed shapes come back as link errors, nothing reaches orch-publish
    bad = dict(req, extra=1)
    with pytest.raises(LinkError) as e:
        c.request("publish_out", {"request": bad})
    assert e.value.code == "malformed" and len(up.requests) == 1

    # the workspace leaves: the unbound reaches the sync seam and publishing stops
    c.request("binding_put", {"object": G.unbound()})
    assert sync.calls[-1][0] == "binding" and sync.calls[-1][2]["o"]["kind"] == "hub.unbound"
    with pytest.raises(LinkError) as e:
        c.request("publish_out", {"request": req})
    assert e.value.code == "not_bound" and len(up.requests) == 1


def test_a_join_succeeds_even_when_publish_does_not_admit_the_workspace_yet(make_hub, up):
    from orch_hub.publish import PublishSyncError

    sync = RecordingSync()
    sync.fail = PublishSyncError("unknown_workspace")
    signer = OpSigner(G.K.priv["hk_op"])
    fw = HubPublish(lambda: (signer, G.card(), G.HUB_ID), sync, PublishConfig(up.url, HOST, timeout_s=3))
    h = make_hub(publish=fw, downstream=fw)
    c = h.open(first=True)
    assert c.request("join", h.join_body()) == {}  # the invite is consumed; publish.register tells the owner later
