"""R5: the client's Noise initiator, prologue, presence bytes and route digest replayed against the contract vectors."""

from __future__ import annotations

import pytest

from orch.model import remote as model_remote
from orch.remoteclient import noise_init, presence
from orch.remoteclient.codes import ClientError
from tests.remote_client.vectors import DOC, cases, priv, pub


@pytest.mark.parametrize("c", cases("prologue"), ids=lambda c: c["name"])
def test_prologue(c):
    a = c["args"]
    try:
        got = noise_init.prologue(a["kind"], a["workspace_id"], a["id"], a["stream_id"], a["hub_id"]).hex()
    except ClientError as e:
        assert c["expect"] == {"error": e.code}
    else:
        assert c["expect"] == {"hex": got}


@pytest.mark.parametrize("c", cases("msg1"), ids=lambda c: c["name"])
def test_msg1_built_by_initiator_equals_vector(c):
    a = c["args"]
    if "ok" not in c["expect"] or "msg1_hex" not in a:
        pytest.skip("responder-side case")
    pro = noise_init.prologue(**{"kind": a["prologue"]["kind"], "workspace_id": a["prologue"]["workspace_id"],
                                 "ident": a["prologue"]["id"], "stream_id": a["prologue"]["stream_id"],
                                 "hub_id": a["prologue"]["hub_id"]})
    msg1 = bytes.fromhex(a["msg1_hex"])
    e_pub = msg1[:32]
    for s_name in ("dev_a_s", "dev_b_s", "dev_new_s"):
        for e_name in ("dev_a_e", "dev_b_e", "dev_new_e"):
            if pub(e_name) == e_pub and bytes.fromhex(c["expect"]["ok"]["rs_hex"]) == pub(s_name):
                ini = noise_init.Initiator(pro, priv(s_name), pub("wrk"), lambda n, k=priv(e_name): k)
                assert ini.msg1() == msg1
                return
    pytest.fail("no key pair for the vector")


@pytest.mark.parametrize("c", cases("msg2_read"), ids=lambda c: c["name"])
def test_msg2_read(c):
    a = c["args"]
    ini = noise_init.Initiator(bytes.fromhex(a["prologue_hex"]), priv(a["s"]), bytes.fromhex(a["wrk_pub_hex"]),
                               lambda n: priv(a["e"]))
    ini.msg1()
    try:
        challenge, tp = ini.read_msg2(bytes.fromhex(a["msg2_hex"]))
    except ClientError as e:
        assert c["expect"] == {"error": e.code}
    else:
        assert challenge.hex() == c["expect"]["challenge_hex"] and tp.hh.hex() == c["expect"]["handshake_hash_hex"]


@pytest.mark.parametrize("t", DOC["transcripts"], ids=lambda t: t["name"])
def test_transcript_from_the_initiators_side(t):
    import json

    from orch.canon.jcs import dumps

    msg1 = bytes.fromhex(t["msg1_hex"])
    s = next(n for n in ("dev_a_s", "dev_b_s", "dev_new_s") if _opens(t, n, msg1))
    e = next(n for n in ("dev_a_e", "dev_b_e", "dev_new_e") if pub(n) == msg1[:32])
    ini = noise_init.Initiator(bytes.fromhex(t["prologue_hex"]), priv(s), pub("wrk"), lambda n: priv(e))
    assert ini.msg1() == msg1
    challenge, tp = ini.read_msg2(bytes.fromhex(t["msg2_hex"]))
    assert challenge.hex() == t["challenge_hex"] and tp.hh.hex() == t["handshake_hash_hex"]
    assert tp.seal(dumps(t["first_plain"])).hex() == t["first_hex"]
    assert json.loads(tp.open(bytes.fromhex(t["reply_hex"]))) == t["reply_plain"]


def _opens(t, s_name, msg1):
    ini = noise_init.Initiator(bytes.fromhex(t["prologue_hex"]), priv(s_name), pub("wrk"), lambda n: bytes.fromhex(
        next(v["priv_hex"] for v in DOC["keys"]["x25519"].values() if bytes.fromhex(v["pub_hex"]) == msg1[:32])))
    return ini.msg1() == msg1


def test_initiator_refuses_wrong_sizes_and_reuse():
    ini = noise_init.Initiator(b"p", bytes(range(32)), pub("wrk"))
    with pytest.raises(ClientError):
        ini.read_msg2(bytes(80))  # before msg1
    ini.msg1()
    with pytest.raises(ClientError):
        ini.msg1()
    with pytest.raises(ClientError) as e:
        ini.read_msg2(bytes(79))
    assert e.value.code == "malformed"


def test_presence_bytes_vector():
    for c in cases("presence_bytes"):
        a = c["args"]
        got = presence.presence_bytes(a["workspace_id"], a["device_id"], bytes.fromhex(a["handshake_hash_hex"]),
                                      bytes.fromhex(a["nonce_hex"]), [bytes.fromhex(d) for d in a["digests_hex"]])
        assert got.hex() == c["expect"]["hex"]
        assert presence.check_signable(got) == "presence"


@pytest.mark.parametrize("c", cases("route_digest"), ids=lambda c: c["name"])
def test_route_digest(c):
    a = c["args"]
    body = bytes.fromhex(a["body_hex"]) if "body_hex" in a else b""
    try:
        got = presence.route_digest(a["workspace_id"], a["method"], a["template"], a["path"], a["query"], body)
    except ClientError:
        assert "error" in c["expect"]
        return
    assert "digest_hex" in c["expect"] or "bytes_hex" in c["expect"] or "hex" in c["expect"], c["expect"]
    if "digest_hex" in c["expect"]:
        assert got.hex() == c["expect"]["digest_hex"]


@pytest.mark.parametrize("c", cases("device_fp"), ids=lambda c: c["name"])
def test_device_fp(c):
    from orch.crypto.encoding import unb64u

    a = c["args"]
    fp = model_remote.device_fp(a["presence_kind"], bytes.fromhex(a["transport_pub_hex"]), unb64u(a["presence_pub"], 65))
    assert fp.hex() == c["expect"]["hex"]
