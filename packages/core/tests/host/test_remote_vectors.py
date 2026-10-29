"""Remote access R0 (#1588): core (the workspace host, and the remote client on the person's device) replays EVERY case
of the shared ``contracts/hub/vectors_remote.json`` with its own port (``remote_port.py``, written apart from the hub's
reference), and the "no custodial type can" list (hub protocol §8.5) against core's real custodial checks. A check kind
this file does not know, or a missing required case, fails."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from orch.canon import LABELS as CANON_LABELS
from orch.canon.jcs import dumps
from orch.crypto import labels as crypto_labels
from orch.crypto.encoding import unb64u
from orch.hubclient import custodial as C
from orch.hubclient.custodial_shape import check_custodial_payload
from orch.hubclient.errors import HubRefused
from tests.host import remote_port as RP

CONTRACT = Path(__file__).resolve().parents[4] / "contracts" / "hub"
DOC: dict[str, Any] = json.loads((CONTRACT / "vectors_remote.json").read_text())
X = {n: bytes.fromhex(v["priv_hex"]) for n, v in DOC["keys"]["x25519"].items()}
PUB = {n: unb64u(v["pub"], 65) for n, v in DOC["keys"]["p256"].items()}
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


def test_vector_remote_host_knows_every_check_and_required_case() -> None:
    assert {c["check"] for c in DOC["cases"]} == CHECKS
    assert set(DOC["required"]) <= {c["name"] for c in DOC["cases"]}
    assert {c["args"]["item"] for c in DOC["cases"] if c["check"] == "custodial_cannot"} == {
        "grant",
        "run",
        "terminal",
        "remote",
    }


def test_vector_remote_host_labels_and_constants() -> None:
    assert {k: v.decode() for k, v in RP.LABEL.items()} == {
        "open": DOC["labels"]["sig_remote_open"],
        "presence": DOC["labels"]["sig_remote_presence"],
        "devices": DOC["labels"]["sig_remote_devices"],
        "enroll": DOC["labels"]["mac_remote_enroll"],
        "prologue": DOC["labels"]["h_remote_prologue"],
        "wrk": DOC["labels"]["h_remote_wrk"],
        "fp": DOC["labels"]["h_remote_device_fp"],
    }
    k = DOC["constants"]
    assert (k["noise_protocol"].encode(), k["frame_max"], k["frame_min_cost"]) == (RP.PROTOCOL, 65535, 1024)
    assert tuple(k["codes"]) == RP.CODES and tuple(k["close_reasons"]) == RP.REASONS
    assert k["limits"] == {
        "streams_per_device": 4,
        "streams_per_workspace": 16,
        "handshakes_per_min": 10,
        "enrolls_per_min": 3,
    }


def test_vector_remote_labels_prefix_free_with_core_tables() -> None:
    """The remote labels are new and prefix-free with every live core label (crypto, planned and canon)."""
    remote = set(DOC["labels"].values())
    live = {v for v in {**crypto_labels.LABELS, **crypto_labels.PLANNED_LABELS}.values() if v.endswith("|")}
    live |= {v for v in CANON_LABELS.values() if isinstance(v, str) and v.endswith("|")}
    assert not remote & live
    for a, b in itertools.permutations(remote | live, 2):
        if a in remote or b in remote:
            assert not b.startswith(a), (a, b)


def test_vector_remote_core_custodial_table_has_no_forbidden_type() -> None:
    """§8.5 "No custodial type can", structurally on core's closed table."""
    bad = (
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
    assert not [t for t, _ in C.CUSTODIAL_TYPES if t.startswith(bad)]


def _cipher(t: dict[str, Any], d: str, n: int) -> RP.Cipher:
    return RP.Cipher(bytes.fromhex(t["k_i2r_hex" if d == "i2r" else "k_r2i_hex"]), n)


def _custodial(a: dict[str, Any]) -> None:
    ev = a["event"]
    C.check_custodial_role(ev["type"], a["log"], "admin")
    check_custodial_payload(ev["type"], ev, frozenset(C.HUB_FIELDS))


def _run(c: dict[str, Any]) -> dict[str, Any]:
    a = c["args"]
    h = bytes.fromhex
    try:
        match c["check"]:
            case "prologue":
                return {"hex": RP.prologue(a["kind"], a["workspace_id"], a["id"], a["stream_id"], a["hub_id"]).hex()}
            case "msg1":
                p = a["prologue"]
                pro = RP.prologue(p["kind"], p["workspace_id"], p["id"], p["stream_id"], p["hub_id"])
                _, _, rs = RP.responder1(pro, X[a["wrk"]], h(a["msg1_hex"]))
                return {"ok": {"rs_hex": rs.hex()}}
            case "msg2_build":
                n, re_, rs = RP.responder1(h(a["prologue_hex"]), X[a["wrk"]], h(a["msg1_hex"]))
                m2 = RP.responder2(n, re_, rs, X[a["host_e"]], h(a["challenge_hex"]))
                return {"msg2_hex": m2.hex(), "handshake_hash_hex": n.hh.hex()}
            case "msg2_read":
                n, _ = RP.initiator1(h(a["prologue_hex"]), X[a["s"]], X[a["e"]], h(a["wrk_pub_hex"]))
                ch = RP.initiator2(n, h(a["msg2_hex"]))
                return {"challenge_hex": ch.hex(), "handshake_hash_hex": n.hh.hex()}
            case "transport":
                return {
                    "pt_hex": _cipher(TRANSCRIPTS[a["transcript"]], a["dir"], a["n"]).open(b"", h(a["ct_hex"])).hex()
                }
            case "open":
                e = a["entry"]
                msg = RP.open_bytes(a["workspace_id"], a["id"], h(a["handshake_hash_hex"]), h(a["challenge_hex"]))
                return {"count": RP.assertion_ok(e["presence_pub"], e["presence_kind"], msg, a["assertion"], a["held"])}
            case "presence_bytes":
                msg = RP.presence_bytes(
                    a["workspace_id"],
                    a["device_id"],
                    h(a["handshake_hash_hex"]),
                    h(a["nonce_hex"]),
                    [h(d) for d in a["digests_hex"]],
                )
                return {"hex": msg.hex()}
            case "presence":
                return {"count": RP.check_presence(a)}
            case "enroll":
                return {"ok": RP.check_enroll(a)}
            case "wrk_fp":
                return {"hex": RP.wrk_fp(h(a["wrk_pub_hex"])).hex()}
            case "code_bytes":
                return {"hex": RP.code_bytes(a["code_id"], h(a["secret_hex"]), h(a["wrk_pub_hex"])).hex()}
            case "device_fp":
                fp = RP.device_fp(h(a["transport_pub_hex"]), unb64u(a["presence_pub"], 65), a["presence_kind"])
                return {"hex": fp.hex()}
            case "session_static":
                RP.session_static(a["entry"], h(a["rs_hex"]))
                return {"ok": True}
            case "open_msg":
                return {"api": RP.check_open_msg(a["msg"], a["host_api"])}
            case "device_list":
                RP.check_device_list(a["signed"], PUB[a["wsk"]], a["workspace_id"], a["held_version"])
                return {"ok": True}
            case "device_lookup":
                return {"ok": RP.lookup(a["list"], a["device_id"], a["now_ms"])["device_id"]}
            case "stream_link":
                return {"ok": RP.check_stream_link(a, PUB[a["key"]])}
            case "frame_cost":
                return {"cost": RP.frame_cost(a["signed"])}
            case "gate":
                RP.gate(a["body"], a["state"], a["now_ms"])
                return {"ok": True}
            case "custodial_cannot":
                _custodial(a)
                return {"ok": True}
    except HubRefused as e:
        return {"error": e.code}
    raise AssertionError(f"unhandled check {c['check']!r}")


@pytest.mark.parametrize("c", DOC["cases"], ids=[c["name"] for c in DOC["cases"]])
def test_vector_remote_host_case(c: dict[str, Any]) -> None:
    assert _run(c) == c["expect"], c.get("note")


def test_vector_remote_host_transcripts() -> None:
    """The full transcripts, rebuilt by core alone: the host side opens message 1 and writes message 2 byte for byte,
    the device side writes message 1 and reads the challenge, and both transport directions open under the split."""
    for t in DOC["transcripts"]:
        dev = "dev_a" if t["kind"] == "session" else "dev_new"
        pro = RP.prologue(t["kind"], t["workspace_id"], t["id"], t["stream_id"], t["hub_id"])
        assert pro.hex() == t["prologue_hex"]
        ini, m1 = RP.initiator1(pro, X[f"{dev}_s"], X[f"{dev}_e"], RP.xpub(X["wrk"]))
        assert m1.hex() == t["msg1_hex"]
        resp, re_, rs = RP.responder1(pro, X["wrk"], m1)
        assert rs == RP.xpub(X[f"{dev}_s"])
        m2 = RP.responder2(resp, re_, rs, X["host_e"], bytes.fromhex(t["challenge_hex"]))
        assert m2.hex() == t["msg2_hex"]
        assert RP.initiator2(ini, m2).hex() == t["challenge_hex"]
        assert ini.hh == resp.hh and resp.hh.hex() == t["handshake_hash_hex"]
        assert ini.c_i2r.seal(b"", dumps(t["first_plain"])).hex() == t["first_hex"]
        assert resp.c_r2i.seal(b"", dumps(t["reply_plain"])).hex() == t["reply_hex"]
