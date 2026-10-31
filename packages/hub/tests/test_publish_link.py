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


# --- P02 (#1290): the hub vouches for its own workspace at orch-publish (hub.allow) ---------------------------------


def _fw(sync, admission):
    signer = OpSigner(G.K.priv["hk_op"])
    return HubPublish(
        lambda: (signer, G.card(), G.HUB_ID),
        sync,
        PublishConfig("http://127.0.0.1:1", HOST, timeout_s=1),
        admission=admission,
    )


def test_join_sends_a_signed_allow_before_the_card_when_the_hub_admits(make_hub):
    sync = RecordingSync()
    seen = []

    def admission(ws):
        seen.append(ws)
        return G.fake16("owner"), 1

    fw = _fw(sync, admission)
    h = make_hub(publish=fw, downstream=fw)
    assert h.open(first=True).request("join", h.join_body()) == {}
    kinds = [x[0] for x in sync.calls]
    assert kinds.index("allow") < kinds.index("ws_card")  # admission first: publish refuses an unknown workspace
    allow = next(x for x in sync.calls if x[0] == "allow")[2]
    o = allow["o"]
    assert (o["kind"], o["hub_id"], o["workspace_id"], o["owner_person_id"], o["binding_seq"]) == (
        "hub.allow", G.HUB_ID, G.WS_A, G.fake16("owner"), 1,
    )  # fmt: skip
    assert S.verify(
        G.K.pub["hk_op"], unb64u(allow["sig"], 64), H.L["sig_hub_allow"] + H.cj(o)
    )  # signed by HK_op under its own label


def test_no_allow_when_the_hub_does_not_admit_or_has_no_admission(make_hub):
    for admission in (None, lambda ws: None):
        sync = RecordingSync()
        fw = _fw(sync, admission)
        h = make_hub(publish=fw, downstream=fw)
        assert h.open(first=True).request("join", h.join_body()) == {}
        assert "allow" not in [x[0] for x in sync.calls]


def test_a_refused_allow_never_blocks_the_card(make_hub):
    class Refusing(RecordingSync):
        def put_allow(self, workspace_id, obj):
            from orch_hub.publish.sync import PublishSyncError

            raise PublishSyncError("unknown_hub")

    sync = Refusing()
    fw = _fw(sync, lambda ws: (G.fake16("owner"), 1))
    h = make_hub(publish=fw, downstream=fw)
    assert h.open(first=True).request("join", h.join_body()) == {}
    assert [x[0] for x in sync.calls] == ["card", "ws_card", "binding"]  # the old rules still decide at publish
