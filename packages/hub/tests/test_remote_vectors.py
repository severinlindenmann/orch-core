"""Remote access R0 (#1588, protocol.md §10.2c, §8.5): the committed ``vectors_remote.json`` is byte for byte what the
generator builds, and the hub's reference (``orch_hub.contract.remote``, plus ``check_action`` and the custodial
payload check for the "no custodial type can" list) replays EVERY case, failing on a check kind it does not handle
or a required case that is missing. orch-core replays the same file with its own port
(``packages/core/tests/host/test_remote_vectors.py``)."""

from __future__ import annotations

import importlib.util
import itertools
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from test_labels import V2

from orch_hub.contract import hub as H
from orch_hub.contract import phase4 as P4
from orch_hub.contract import remote as R
from orch_hub.contract.canon import cj, unb64u
from orch_hub.contract.labels import CUSTODIAL_PREFIX, HUB_LABELS, V2_REUSED

VEC = Path(__file__).resolve().parents[3] / "contracts" / "hub"
PATH = VEC / "vectors_remote.json"
DOC = json.loads(PATH.read_text())
K = DOC["keys"]
X = {n: bytes.fromhex(v["priv_hex"]) for n, v in K["x25519"].items()}
XP = {n: bytes.fromhex(v["pub_hex"]) for n, v in K["x25519"].items()}
PUB = {n: unb64u(v["pub"], 65) for n, v in K["p256"].items()}
TRANSCRIPTS = {t["name"]: t for t in DOC["transcripts"]}

CHECKS = {
    "prologue",
    "msg1",
    "msg2_build",
    "msg2_read",
    "transport",
    "open",
    "presence_bytes",
    "presence",
    "enroll",
    "wrk_fp",
    "code_bytes",
    "session_static",
    "device_fp",
    "open_msg",
    "device_list",
    "device_lookup",
    "stream_link",
    "frame_cost",
    "gate",
    "custodial_cannot",
}


def test_vector_remote_file_is_generated_byte_for_byte() -> None:
    spec = importlib.util.spec_from_file_location("make_vectors_remote", VEC / "make_vectors_remote.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert json.dumps(mod.build(), indent=1, ensure_ascii=True) + "\n" == PATH.read_text()


def test_vector_remote_keys_rebuild_from_seeds() -> None:
    for name, k in K["p256"].items():
        key = ec.derive_private_key(int(k["scalar_hex"], 16), ec.SECP256R1())
        assert R.S.public_bytes(key) == PUB[name]
    for name, priv in X.items():
        assert R.x25519_pub(priv) == XP[name]


def test_vector_remote_labels_new_and_prefix_free() -> None:
    assert DOC["labels"] == R.REMOTE_LABELS
    remote = list(R.REMOTE_LABELS.values())
    assert all(v.endswith("|") and v.startswith("orch/v2/") for v in remote)
    others = set(HUB_LABELS.values()) | set(V2_REUSED.values()) | set(V2) | {CUSTODIAL_PREFIX}
    assert len(set(remote)) == len(remote) and not set(remote) & others
    for a, b in itertools.permutations(set(remote) | others, 2):
        if a in remote or b in remote:
            assert not b.startswith(a), (a, b)


def test_vector_remote_protocol_doc_names_every_label_and_code() -> None:
    doc = (VEC / "protocol.md").read_text()
    for label in R.REMOTE_LABELS.values():
        assert label.replace("|", "\\|") in doc, label
    for code in {c["expect"]["error"] for c in DOC["cases"] if "error" in c["expect"]} | set(R.CODES):
        assert f"`{code}`" in doc, code
    for op in R.STREAM_OPS:
        assert f"| `{op}` |" in doc, op


def test_vector_remote_noise_name_is_one_block() -> None:
    assert len(R.NOISE_PROTOCOL) == 32 and DOC["constants"]["noise_protocol"] == "Noise_IK_25519_ChaChaPoly_SHA256"


def test_vector_remote_required_cases_present() -> None:
    names = {c["name"] for c in DOC["cases"]}
    assert set(DOC["required"]) <= names
    assert {c["check"] for c in DOC["cases"]} == CHECKS
    # every item of the normative §8.5 list has refusal vectors
    items = {c["args"]["item"] for c in DOC["cases"] if c["check"] == "custodial_cannot"}
    assert items == {"grant", "run", "terminal", "remote"}


def test_vector_remote_custodial_table_has_none_of_the_forbidden_types() -> None:
    """§8.5 "No custodial type can", structurally: the closed table names no grant issue, agent, run, terminal,
    device-add or remote type, so a later row added by mistake fails here before any vector."""
    forbidden_prefixes = (
        "remote.",
        "run.",
        "agent.",
        "terminal.",
        "task.",
        "claim.taken",
        "grant.issued",
        "device.added",
        "device.revoked",
        "keys.",
    )
    for typ, _log in H.CUSTODIAL_TYPES:
        assert not typ.startswith(forbidden_prefixes), typ


def _transcript_cipher(t: dict[str, Any], direction: str, n: int) -> R.CipherState:
    return R.CipherState(k=bytes.fromhex(t["k_i2r_hex" if direction == "i2r" else "k_r2i_hex"]), n=n)


def _run(c: dict[str, Any]) -> dict[str, Any]:
    a = c["args"]
    try:
        match c["check"]:
            case "prologue":
                return {"hex": R.prologue(a["kind"], a["workspace_id"], a["id"], a["stream_id"], a["hub_id"]).hex()}
            case "msg1":
                p = a["prologue"]
                pro = R.prologue(p["kind"], p["workspace_id"], p["id"], p["stream_id"], p["hub_id"])
                hs = R.responder_read_msg1(pro, X[a["wrk"]], bytes.fromhex(a["msg1_hex"]))
                return {"ok": {"rs_hex": hs.rs.hex()}}
            case "msg2_build":
                hs = R.responder_read_msg1(bytes.fromhex(a["prologue_hex"]), X[a["wrk"]], bytes.fromhex(a["msg1_hex"]))
                m2 = R.responder_msg2(hs, X[a["host_e"]], bytes.fromhex(a["challenge_hex"]))
                return {"msg2_hex": m2.hex(), "handshake_hash_hex": hs.hh.hex()}
            case "msg2_read":
                pro = bytes.fromhex(a["prologue_hex"])
                hs, _ = R.initiator_msg1(pro, X[a["s"]], X[a["e"]], bytes.fromhex(a["wrk_pub_hex"]))
                ch = R.initiator_read_msg2(hs, bytes.fromhex(a["msg2_hex"]))
                return {"challenge_hex": ch.hex(), "handshake_hash_hex": hs.hh.hex()}
            case "transport":
                cs = _transcript_cipher(TRANSCRIPTS[a["transcript"]], a["dir"], a["n"])
                return {"pt_hex": cs.decrypt(b"", bytes.fromhex(a["ct_hex"])).hex()}
            case "open":
                n = R.verify_open(
                    a["entry"],
                    a["workspace_id"],
                    a["id"],
                    bytes.fromhex(a["handshake_hash_hex"]),
                    bytes.fromhex(a["challenge_hex"]),
                    a["assertion"],
                    a["held"],
                )
                return {"count": n}
            case "presence_bytes":
                msg = R.presence_bytes(
                    a["workspace_id"],
                    a["device_id"],
                    bytes.fromhex(a["handshake_hash_hex"]),
                    bytes.fromhex(a["nonce_hex"]),
                    [bytes.fromhex(d) for d in a["digests_hex"]],
                )
                return {"hex": msg.hex()}
            case "presence":
                n = R.verify_presence(
                    a["entry"],
                    a["workspace_id"],
                    bytes.fromhex(a["handshake_hash_hex"]),
                    bytes.fromhex(a["nonce_hex"]),
                    [bytes.fromhex(d) for d in a["digests_hex"]],
                    a["assertion"],
                    a["held"],
                    issued_nonces={bytes.fromhex(k): v for k, v in a["issued"].items()},
                    now_ms=a["now_ms"],
                )
                return {"count": n}
            case "enroll":
                pending = R.check_enroll(
                    a["msg"],
                    secret=bytes.fromhex(a["secret_hex"]),
                    workspace_id=a["workspace_id"],
                    code_id=a["code_id"],
                    hh=bytes.fromhex(a["handshake_hash_hex"]),
                    challenge=bytes.fromhex(a["challenge_hex"]),
                    transport_pub=bytes.fromhex(a["transport_pub_hex"]),
                )
                return {"ok": pending}
            case "wrk_fp":
                return {"hex": R.wrk_fp(bytes.fromhex(a["wrk_pub_hex"])).hex()}
            case "code_bytes":
                b = R.code_bytes(a["code_id"], bytes.fromhex(a["secret_hex"]), bytes.fromhex(a["wrk_pub_hex"]))
                return {"hex": b.hex()}
            case "device_fp":
                fp = R.device_fp(
                    bytes.fromhex(a["transport_pub_hex"]), unb64u(a["presence_pub"], 65), a["presence_kind"]
                )
                return {"hex": fp.hex()}
            case "session_static":
                R.check_session_static(a["entry"], bytes.fromhex(a["rs_hex"]))
                return {"ok": True}
            case "open_msg":
                return {"api": R.check_open_msg(a["msg"], tuple(a["host_api"]))}
            case "device_list":
                R.verify_device_list(
                    a["signed"], wsk_pub=PUB[a["wsk"]], workspace_id=a["workspace_id"], held_version=a["held_version"]
                )
                return {"ok": True}
            case "device_lookup":
                return {"ok": R.device_entry(a["list"], a["device_id"], a["now_ms"])["device_id"]}
            case "stream_link":
                o = R.verify_stream_link(
                    a["signed"],
                    direction=a["direction"],
                    hub_id_=a["hub_id"],
                    workspace_id=a["workspace_id"],
                    pub=PUB[a["key"]],
                    now_ms=a["now_ms"],
                )
                return {"ok": o["op"]}
            case "frame_cost":
                return {"cost": R.frame_cost(a["signed"])}
            case "gate":
                R.stream_open_gate(a["body"], a["state"], a["now_ms"])
                return {"ok": True}
            case "custodial_cannot":
                return _custodial(a)
    except H.Refused as e:
        return {"error": e.code}
    raise AssertionError(f"unhandled check {c['check']!r}")


def _custodial(a: dict[str, Any]) -> dict[str, Any]:
    """Both hub-side paths must refuse with the same code: the channel's action, and the delivered event."""
    codes = []
    for step in ("action", "event"):
        try:
            if step == "action":
                P4.check_action(a["action"], role="admin")
            else:
                H.check_custodial_role(a["event"]["type"], a["log"], "admin")
                P4.check_custodial_payload(a["event"], a["log"])
            codes.append(None)
        except H.Refused as e:
            codes.append(e.code)
    assert codes[0] == codes[1], codes
    return {"ok": True} if codes[0] is None else {"error": codes[0]}


@pytest.mark.parametrize("c", DOC["cases"], ids=[c["name"] for c in DOC["cases"]])
def test_vector_remote_case(c: dict[str, Any]) -> None:
    assert _run(c) == c["expect"], c.get("note")


def test_vector_remote_transcripts_replay() -> None:
    """The two full transcripts: msg1 opens at the host, msg2 rebuilds byte for byte, the device reads the challenge,
    the first transport messages open under the split keys and say what the transcript says."""
    for t in DOC["transcripts"]:
        pro = R.prologue(t["kind"], t["workspace_id"], t["id"], t["stream_id"], t["hub_id"])
        assert pro.hex() == t["prologue_hex"]
        ini, m1 = R.initiator_msg1(
            pro,
            X[f"dev_{'a' if t['kind'] == 'session' else 'new'}_s"],
            X[f"dev_{'a' if t['kind'] == 'session' else 'new'}_e"],
            XP["wrk"],
        )
        assert m1.hex() == t["msg1_hex"] and len(m1) == 96
        resp = R.responder_read_msg1(pro, X["wrk"], m1)
        m2 = R.responder_msg2(resp, X["host_e"], bytes.fromhex(t["challenge_hex"]))
        assert m2.hex() == t["msg2_hex"] and len(m2) == 80
        assert R.initiator_read_msg2(ini, m2).hex() == t["challenge_hex"]
        assert ini.hh.hex() == resp.hh.hex() == t["handshake_hash_hex"]
        assert resp.recv is not None and resp.send is not None
        assert resp.recv.decrypt(b"", bytes.fromhex(t["first_hex"])) == cj(t["first_plain"])
        assert ini.recv is not None and ini.recv.decrypt(b"", bytes.fromhex(t["reply_hex"])) == cj(t["reply_plain"])


def test_vector_remote_stream_ops_not_yet_in_runtime_table() -> None:
    """R0 fixes the contract only: the runtime link tables take the stream ops in R2 (host) and R4 (hub), each with its
    handler. Until then the hub's link verifier refuses them (unknown_op), so nothing half-built is reachable."""
    assert not set(R.STREAM_OPS) & set(H.LINK_OPS)
