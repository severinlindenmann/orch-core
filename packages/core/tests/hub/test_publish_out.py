"""V3 C7: the hub-path publish request, built by core (hub contract section 11).

The bytes are pinned by the contract's vectors (the same ones orch-publish's ``hub_path`` verifier is proved against);
the signer, the transport and the approval hook are checked against the fake hub. With ``ORCH_PUBLISH_SRC`` set to an
orch-publish checkout the forwarded request is also run through its real verifier."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from orch.canon.jcs import dumps as cj
from orch.crypto import suite as S
from orch.crypto.encoding import b64u, unb64u
from orch.hubclient import HubLink, HubRefused
from orch.hubclient import publish as hp
from orch.wsproto.pubsign import PublishContext, publish_signed_bytes
from orch.wsproto.sign import SignerRefused

from . import contract as H
from .fake import BareLink, FakeHub, vector_join_body
from .vec import V, private_key, pub

VEC = V["publish"]
REQ = VEC["request"]
WS = V["ids"]["workspace_a"]
HUB = V["ids"]["hub"]
ORIGIN = "https://pub.dev.severin.io"
TARGET = f"/v1/ws/{WS}/shares/s1"


def _vector_inputs():
    h = REQ["headers"]
    return dict(
        ws=bytes.fromhex(h["Orch-Workspace"]), ts_ms=int(h["Orch-Ts"]), nonce=bytes.fromhex(h["Orch-Nonce"]),
        method=REQ["method"], host=REQ["host"], target=REQ["target"], body=bytes.fromhex(REQ["body_hex"]),
    )  # fmt: skip


def _approval():
    return H.parse(unb64u(REQ["headers"]["Orch-Owner-Approval"]))


def test_the_signed_bytes_are_the_vectors_bytes():
    a = _approval()
    assert hp.approval_hash(a).hex() == VEC["approval_hash_hex"]
    assert hp.approval_hash(None) == bytes(32)
    got = hp.hub_path_signed_bytes(**_vector_inputs(), hub_id=HUB, approval=a)
    assert got.hex() == VEC["wsk_signed_hex"]
    assert S.verify(pub("wsk_a"), unb64u(REQ["headers"]["Orch-Signature"], 64), got)


def test_the_request_hash_is_the_vectors_hash():
    assert b64u(hp.request_hash(**_vector_inputs(), hub_id=HUB)) == VEC["request_hash"]
    assert a_request_hash_in_the_vector_approval() == VEC["request_hash"]


def a_request_hash_in_the_vector_approval():
    return _approval()["o"]["request_hash"]


def test_a_hub_path_signature_is_not_a_direct_signature():
    i = _vector_inputs()
    direct = publish_signed_bytes(i["ws"], i["ts_ms"], i["nonce"], i["method"], i["host"], i["target"], i["body"])
    hub = hp.hub_path_signed_bytes(**i, hub_id=HUB, approval=None)
    assert hub.startswith(b"orch/v2/publish-hub|") and direct.startswith(b"orch/v2/publish|")
    assert not S.verify(pub("wsk_a"), S.sign(private_key("wsk_a"), hub), direct)


def _signer(*, approver=None, clock=lambda: 1_790_000_000_000, rand=None):
    key = private_key("wsk_a")
    return hp.HubPublishSigner(
        public_key=pub("wsk_a"), sign=lambda p: S.sign(key, p), ctx=PublishContext(WS, ORIGIN), hub_id=HUB,
        approver=approver, clock_ms=clock, rand=rand,
    )  # fmt: skip


def _verify(sp, body, approval=None):
    h = sp.headers
    signed = hp.hub_path_signed_bytes(
        bytes.fromhex(h["Orch-Workspace"]), int(h["Orch-Ts"]), bytes.fromhex(h["Orch-Nonce"]), sp.method, sp.host,
        sp.target, body, HUB, approval,
    )  # fmt: skip
    return S.verify(pub("wsk_a"), unb64u(h["Orch-Signature"], 64), signed)


def test_the_signer_signs_the_hub_path_bytes_with_the_hub_header_and_no_approval():
    sp = _signer().request("PUT", TARGET, b"{}")
    assert sp.host == "pub.dev.severin.io"
    # contract 11.1: the host sends exactly these four (+ the approval for public access); the hub adds its own two
    assert {k.lower() for k in sp.headers} == {"orch-workspace", "orch-ts", "orch-nonce", "orch-signature"}
    assert _verify(sp, b"{}")
    assert not _verify(sp, b"{ }")  # the body is covered


def test_each_request_has_its_own_nonce_and_the_signer_refuses_another_workspace_or_host():
    s = _signer()
    assert (
        s.request("GET", f"/v1/ws/{WS}/shares").headers["Orch-Nonce"]
        != s.request("GET", f"/v1/ws/{WS}/shares").headers["Orch-Nonce"]
    )
    for bad in (f"/v1/ws/{V['ids']['workspace_b']}/shares", f"/v1/ws/{WS}/../x", "/v1/ws/"):
        with pytest.raises(SignerRefused):
            s.request("GET", bad)
    with pytest.raises(SignerRefused):
        s.request("PATCH", TARGET)
    s2 = _signer(rand=lambda n: b"\x01" * n)
    s2.request("GET", TARGET)
    with pytest.raises(SignerRefused, match="nonce reuse"):
        s2.request("GET", TARGET)  # a broken RNG cannot sign twice under one nonce


@pytest.fixture
def public_route(monkeypatch):
    monkeypatch.setattr(hp, "PUBLIC_ROUTES", (("PUT", r"/v1/ws/[0-9a-f]{32}/shares/[^/]+"),))


def test_a_public_route_without_an_owner_approval_is_refused(public_route):
    with pytest.raises(SignerRefused, match="approval_required"):
        _signer().request("PUT", TARGET, b"{}")
    with pytest.raises(SignerRefused, match="approval_required"):
        _signer(approver=lambda rh: None).request("PUT", TARGET, b"{}")
    assert _signer().request("GET", f"/v1/ws/{WS}/shares")  # other routes are unchanged


def test_the_approval_names_this_exact_request_and_is_inside_the_signature(public_route):
    seen = []

    def approver(rh):
        seen.append(rh)
        return _approval()

    sp = _signer(approver=approver).request("PUT", TARGET, b"{}")
    approval = H.parse(unb64u(sp.headers["Orch-Owner-Approval"]))
    assert approval == _approval() and sp.headers["Orch-Owner-Approval"] == b64u(cj(approval))
    want = hp.request_hash(
        bytes.fromhex(WS), int(sp.headers["Orch-Ts"]), bytes.fromhex(sp.headers["Orch-Nonce"]), "PUT", sp.host, TARGET,
        b"{}", HUB,
    )  # fmt: skip
    assert seen == [b64u(want)]  # the approver saw the hash of this request, ts and nonce included
    assert _verify(sp, b"{}", approval)
    assert not _verify(sp, b"{}", None)  # stripping the approval breaks the signature
    other = {**approval, "o": {**approval["o"], "access": "x"}}
    assert not _verify(sp, b"{}", other)  # so does swapping it


# -- over the fake hub --------------------------------------------------------------------------------------------


@pytest.fixture
def joined_link():
    hub = FakeHub()
    key = private_key("wsk_a")
    sign = lambda p: S.sign(key, p)  # noqa: E731
    link = HubLink(
        hub, hub_id=hub.hub_id, workspace_id=WS, hk_op_pub=hub.hk_op_pub,
        sign=lambda p: sign(p), mode="selective", now_ms=lambda: hub.now_ms,
    )  # fmt: skip
    link.call("join", vector_join_body())
    return hub, link


def _client(hub, link, responder=None):
    from orch.publishclient.client import PublishClient

    if responder:
        hub.responder = responder
    return PublishClient(_signer(clock=lambda: hub.now_ms), transport=hp.HubPublishTransport(link))


def test_a_request_goes_through_publish_out_and_the_reply_comes_back(joined_link):
    hub, link = joined_link
    hub.responder = lambda fwd: {"status": 200, "body_hex": b'{"result":"stored"}'.hex()}
    got = _client(hub, link).request("PUT", TARGET, b'{"a":1}')
    assert (got.status, got.body) == (200, b'{"result":"stored"}')
    (fwd,) = hub.forwarded
    r = fwd.request
    assert (r["method"], r["host"], r["target"]) == ("PUT", "pub.dev.severin.io", TARGET)
    assert bytes.fromhex(r["body_hex"]) == b'{"a":1}'
    assert r["headers"]["Orch-Hub"] == HUB and r["headers"]["Orch-Hub-Signature"]  # added by the hub
    assert [c[1] for c in hub.calls if c[1] == "publish_out"] == ["publish_out"]


def test_the_forwarded_request_verifies_under_orch_publishs_verifier(joined_link):
    src = os.environ.get("ORCH_PUBLISH_SRC")
    if not src:
        pytest.skip("ORCH_PUBLISH_SRC is not set (the vector bytes above are the always-on check)")
    hub, link = joined_link
    _client(hub, link).request("PUT", TARGET, b'{"a":1}')
    sys.path.insert(0, src)
    try:
        from pubsrv import hub_path as hpath
        from pubsrv import relay_proto as proto
    finally:
        sys.path.remove(src)
    r = hub.forwarded[-1].request
    wsk = b64u(pub("wsk_a"))
    binding = {"status": "bound", "binding": {"wsk_pub": wsk, "hub_id": HUB, "hk_root_pub": b64u(hub.hk_root_pub)}}

    class Nonces:
        def record(self, ws, nonce, now):
            return True

    st = hpath.State(
        {WS: {"wsk_pub": wsk, "owner_person_id": V["ids"]["person_alice"]}}, {WS: binding},
        lambda hub_hex: {"hk_root_pub": hub.hk_root_pub, "hk_op_pub": hub.hk_op_pub},
    )  # fmt: skip
    req = {
        "method": r["method"], "host": r["host"], "target": r["target"], "body": bytes.fromhex(r["body_hex"]),
        "headers": {k.lower(): v for k, v in r["headers"].items()},
    }  # fmt: skip
    assert proto and hpath.check_hub_publish(req, st, Nonces(), hub.now_ms, True)["workspace"] == WS


def test_a_hub_that_cannot_be_reached_is_hub_unreachable_and_nothing_else_is_tried():
    class Down(BareLink):
        sent = 0

        def send(self, message):
            Down.sent += 1
            raise ConnectionRefusedError("down")

    key = private_key("wsk_a")
    link = HubLink(
        Down(), hub_id=HUB, workspace_id=WS, hk_op_pub=pub("hk_op"), sign=lambda p: S.sign(key, p),
        mode="selective", now_ms=lambda: V["now_ms"],
    )  # fmt: skip
    from orch.publishclient.client import PublishClient

    client = PublishClient(_signer(), transport=hp.HubPublishTransport(link), sleep=lambda s: None)
    for method in ("PUT", "GET", "POST"):  # even idempotent requests are not retried into a direct path
        with pytest.raises(HubRefused) as exc:
            client.request(method, TARGET)
        assert exc.value.code == "hub_unreachable"
    assert Down.sent == 3


def test_a_hub_refusal_is_not_hidden(joined_link):
    hub, link = joined_link
    hub.workspaces[WS].binding = {**hub.workspaces[WS].binding, "status": "unbound"}  # the workspace left
    with pytest.raises(HubRefused) as exc:
        _client(hub, link).request("GET", f"/v1/ws/{WS}/shares")
    assert exc.value.code == "not_bound"


def test_a_request_over_the_link_frame_is_refused_before_anything_is_sent(joined_link):
    hub, link = joined_link
    with pytest.raises(HubRefused) as exc:
        _client(hub, link).request("PUT", TARGET, b"x" * 600_000)
    assert exc.value.code == "too_large" and hub.forwarded == []


def test_hub_file_max_is_one_frame_less_the_overhead_and_halved_for_hex():
    # the hub's link frame cap (orch_hub.link.framing.MAX_FRAME) is 1_500_000; core never imports the hub, so it is here
    assert hp.HUB_PART == 384 * 1024
    assert hp.HUB_PART < hp.HUB_FILE_MAX
    assert hp.HUB_FILE_MAX == (hp.REQUEST_MAX - hp.REQUEST_OVERHEAD) // 2
    assert hp.HUB_FILE_MAX * 2 + hp.REQUEST_OVERHEAD <= 1_500_000
    assert hp.HUB_FILE_MAX * 2 + hp.REQUEST_OVERHEAD <= hp.REQUEST_MAX


def test_the_transport_still_bounds_individual_frames(joined_link):
    hub, link = joined_link
    client = _client(hub, link)
    with pytest.raises(HubRefused) as exc:
        client.request("PUT", TARGET, b"x" * (hp.HUB_FILE_MAX + 1 + hp.REQUEST_OVERHEAD))
    assert exc.value.code == "too_large" and hub.forwarded == []
    client.request("PUT", TARGET, b"x" * hp.HUB_FILE_MAX)  # the real headers and target fit the allowance
    assert len(hub.forwarded) == 1


def test_a_malformed_reply_is_refused(joined_link):
    hub, link = joined_link
    t = hp.HubPublishTransport(type("L", (), {"call": lambda self, op, body: {"status": "200", "body_hex": ""}})())
    with pytest.raises(HubRefused):
        t("https", "h", "GET", TARGET, {}, b"", 1.0)


# -- cross-repo: the request as the REAL hub and the REAL orch-publish take it (#981) ------------------------------

HUB_SRC = os.environ.get("ORCH_HUB_SRC", "")
needs_hub = pytest.mark.skipif(
    not (HUB_SRC and (Path(HUB_SRC) / "src" / "orch_hub" / "publish").is_dir() and shutil.which("uv")),
    reason="ORCH_HUB_SRC not set to an orch-hub checkout with orch_hub.publish",
)


def _through_the_hub(request):
    out = subprocess.run(
        ["uv", "run", "--project", HUB_SRC, "python", str(Path(__file__).with_name("hub_forward_proc.py")), HUB_SRC],
        input=json.dumps({"request": request}), capture_output=True, text=True, timeout=120, check=True,
    )  # fmt: skip
    return json.loads(out.stdout.strip().splitlines()[-1])


def _host_request(hub, link):
    """What the host sent over the link, built by the real signer and transport (the fake hub recorded it with the
    hub's two headers added; they come off again)."""
    _client(hub, link).request("PUT", TARGET, b'{"a":1}')
    sent = hub.forwarded[-1].request
    return sent | {"headers": {k: v for k, v in sent["headers"].items() if k not in ("Orch-Hub", "Orch-Hub-Signature")}}


@needs_hub
def test_the_real_hub_takes_the_host_request_and_the_real_publish_takes_the_forward(joined_link):
    hub, link = joined_link
    request = _host_request(hub, link)
    assert {k.lower() for k in request["headers"]} == {"orch-workspace", "orch-ts", "orch-nonce", "orch-signature"}
    got = _through_the_hub(request)
    assert "forwarded" in got, got  # the hub's own validator accepts it (this refused it with `malformed` in #981)
    fwd = got["forwarded"]
    assert fwd["headers"]["Orch-Hub"] == HUB and fwd["headers"]["Orch-Hub-Signature"]
    src = os.environ.get("ORCH_PUBLISH_SRC")
    if not src:
        return
    sys.path.insert(0, src)
    try:
        from pubsrv import hub_path as hpath
    finally:
        sys.path.remove(src)
    wsk = b64u(pub("wsk_a"))
    st = hpath.State(
        {WS: {"wsk_pub": wsk, "owner_person_id": V["ids"]["person_alice"]}},
        {WS: {"status": "bound", "binding": {"wsk_pub": wsk, "hub_id": HUB, "hk_root_pub": b64u(pub("hk_root"))}}},
        lambda hub_hex: {"hk_root_pub": pub("hk_root"), "hk_op_pub": pub("hk_op")},
    )  # fmt: skip

    class Nonces:
        def record(self, ws, nonce, now):
            return True

    req = {
        "method": fwd["method"], "host": fwd["host"], "target": fwd["target"], "body": bytes.fromhex(fwd["body_hex"]),
        "headers": {k.lower(): v for k, v in fwd["headers"].items()},
    }  # fmt: skip
    assert hpath.check_hub_publish(req, st, Nonces(), hub.now_ms, True)["workspace"] == WS


@needs_hub
def test_the_real_hub_refuses_a_hub_header_from_the_host(joined_link):
    hub, link = joined_link
    request = _host_request(hub, link)
    request["headers"]["Orch-Hub"] = HUB  # the shape #981 sent
    assert _through_the_hub(request) == {"refused": "malformed"}
