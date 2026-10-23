"""R6c against the relay corpus: every ``bridge.host_cases`` ``pair_*`` and ``pair_status_*`` case (proto §8.3, §8.6
steps 1 to 3) through :meth:`PairEngine.receive`, with the case's state as offer records. The answer the engine POSTs
is opened under ``K_pair``, checked for the WSK signature and compared field for field with the vector (refusals:
``LAST|REFUSAL`` and ``{refusal, wsk_pub[, host_ms]}``; answers: ``LAST`` and the exact answer)."""

from __future__ import annotations

import pytest

from orch import crypto
from orch.identity import certreq, pairing
from orch.wsproto import wire

from . import corpus
from .helpers import WS, WSK_PUB
from .pairkit import OWNER, Kit

CASES = [c for c in corpus.S2["bridge"]["host_cases"] if c["name"].startswith("pair")]
_STATE = {"pending": "pending", "cert_pending": "cert_pending", "approved": "approved", "rejected": "rejected"}


def test_the_corpus_pair_cases_are_all_here():
    assert len(CASES) == 22


def _seed(kit: Kit, st: dict) -> None:
    assert st["workspace"] == WS and crypto.unb64u(st["wsk_pub"]) == WSK_PUB and st["owner_person_id"] == OWNER
    kit.host.revoked_ids = set(st["revoked"])
    held_by_offer = {h["offer_id"]: (dev, h) for dev, h in st["pending_pairs"].items()}
    for oid, offer in st["offers"].items():
        rec = {
            "v": 1, "offer_id": oid, "state": "open", "secret": crypto.b64u(bytes.fromhex(offer["secret"])),
            "sas_nonce": offer["sas_nonce"], "scopes": ["look", "decide"], "created_ms": offer["expires_ms"] - 600_000,
            "expires_ms": offer["expires_ms"], "phone_deadline_ms": None, "consumed": None, "path": None,
            "sas_tries": 0, "request": None, "challenge": None, "rejection": None, "cert": None, "intent": None,
            "event_id": None, "publication": None, "card_seq": None, "baseline": None, "terminal_ms": None,
            "second_key": False, "primary_label": None, "reason": None, "device_put": False, "addition_posted": False,
        }  # fmt: skip
        if oid in held_by_offer:
            dev, h = held_by_offer[oid]
            rec["consumed"] = {
                "device_id": dev, "dk_sig_pub": h["dk_sig_pub"], "dk_kx_pub": h["dk_kx_pub"], "attempt": h["attempt"],
                "label": h["label"], "cert": h["cert"], "decl": None, "decl_who": None,
            }  # fmt: skip
            rec["path"] = "7a" if h["cert_state"] == "present" else "7b"
            rec["state"] = _STATE[h["state"]]
            rec["phone_deadline_ms"] = offer["expires_ms"] + 600_000
            if h["state"] == "rejected":
                rec["terminal_ms"] = offer["expires_ms"] - 600_000
            if h["state"] == "cert_pending":
                ch = h.get("challenge")
                created = ch["o"]["expires_ms"] - 600_000 if ch else h["cert_pending_since"]
                o = certreq.cert_request_o(
                    request_id=ch["o"]["request_id"] if ch else "11" * 16, workspace_id=WS, person_id=OWNER,
                    primary_device_id=corpus.IDS["device_primary"],
                    primary_kx_pub=crypto.public_bytes(crypto.generate_private_key()), offer_id=oid,
                    attempt=h["attempt"], dk_sig_pub=crypto.unb64u(h["dk_sig_pub"]),
                    dk_kx_pub=crypto.unb64u(h["dk_kx_pub"]), label=h["label"], scopes_max=["look", "decide"],
                    created_ms=created,
                )  # fmt: skip
                rec["request"] = {"request_id": o["request_id"], "signed": {"o": o, "sig": "x"},
                                  "expires_ms": o["expires_ms"], "posted": True}  # fmt: skip
                rec["primary_label"] = h["primary_label"]
                rec["challenge"] = ch
            if h["state"] == "approved":
                rec["cert"], rec["scopes"] = h["cert"], h["scopes"]
                rec["publication"] = {"list_seq": h["list_seq"], "epoch": st["epoch"], "list_hash": None}
                rec["card_seq"], rec["baseline"] = h["card_seq"], h.get("baseline")
        kit.store.put(rec)


def _open(kit: Kit, env: bytes) -> tuple[dict, int]:
    h, body, sig = wire.parse_envelope(env, direction=2)
    assert h.epoch == 0 and h.seq == 0 and h.direction == 2
    rec = kit.store.get(h.stream.hex())
    k = wire.k_pair(crypto.unb64u(rec["secret"]), h.workspace, h.stream)
    meta, data = wire.parse_plaintext(wire.open_body(k, env[: wire.HEADER_LEN], body))
    assert data == b"" and crypto.verify(WSK_PUB, sig, wire.bridge_signed_bytes(env[: wire.HEADER_LEN], body))
    return meta, h.flags


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_pair_host_case(tmp_path, case):
    kit = Kit(tmp_path)
    try:
        _seed(kit, case["state"])
        for step in case.get("steps") or [case]:
            kit.clock.ms = step["now_ms"]
            env = bytes.fromhex(step["envelope"])
            before = len(kit.relay.answers)
            got = kit.engine.receive(env, step["mailbox_id"])
            exp = step["expect"]
            if exp["result"] == "drop":
                assert got.startswith("drop:"), got
                assert len(kit.relay.answers) == before
                continue
            assert len(kit.relay.answers) == before + 1, got
            meta, flags = _open(kit, kit.relay.answers[-1][2])
            if exp["result"] == "refuse":
                assert got == f"refuse:{exp['code']}"
                assert flags == wire.FLAG_LAST | wire.FLAG_REFUSAL
                want = {k: v for k, v in exp.items() if k not in ("result", "code")}
                assert meta == {"refusal": exp["code"], **want}
                continue
            assert flags == wire.FLAG_LAST
            assert meta == exp["answer"], (meta, exp["answer"])
            if "sas" in exp:  # a consumption: the record holds the cleaned label and the path; the SAS is the vector's
                h = wire.parse_header(env[: wire.HEADER_LEN])
                rec = kit.store.get(h.stream.hex())
                c = rec["consumed"]
                assert c["label"] == exp["label"]
                assert rec["path"] == ("7a" if exp["cert_state"] == "present" else "7b")
                code = pairing.sas(WS, rec["offer_id"], crypto.unb64u(c["dk_sig_pub"]), crypto.unb64u(c["dk_kx_pub"]),
                                   WSK_PUB, crypto.unb64u(rec["sas_nonce"]))  # fmt: skip
                assert code == exp["sas"]
                assert rec["state"] == "pending"
    finally:
        kit.close()
