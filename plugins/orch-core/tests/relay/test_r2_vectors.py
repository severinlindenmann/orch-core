"""The R2 framing vectors (``r2_vectors/r2_framing.json``, made by ``scripts/gen_r2_framing_vectors.py``, never edited
by hand) agree with the code: the tables are the code's, every request opens under ``K_bridge`` and verifies under the
device key, every response chunk verifies under the WSK and opens to the recorded answer, and replaying the requests
in order through a fresh bridge (same seed) gives the same answers. The read results come from the test kit's fake
host: only the framing (meta, flags, chunking, refusal codes and fields) is normative for orch-mobile."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch import crypto
from orch.relay import bridge, framing, submit
from orch.wsproto import wire

from .bridgekit import World, apply_setup, open_chunk

VECTORS = json.loads((Path(__file__).with_name("r2_vectors") / "r2_framing.json").read_text())


def test_the_tables_are_the_codes():
    t = VECTORS["tables"]
    assert t["ops"] == {
        k: {"backend": v.backend, "kind": v.kind, "scope": v.scope, "event": v.event} for k, v in framing.OPS.items()
    }
    assert t["events"] == {k: list(v) for k, v in submit.EVENTS.items()}
    assert t["auth"] == sorted(submit.AUTH)
    assert t["retry"] == dict(framing.RETRY)
    assert t["retry_context"] == {k: dict(v) for k, v in framing.RETRY_CONTEXT.items()}
    assert t["retry_notes"] == dict(framing.RETRY_NOTES)
    assert t["refusal_fields"]["proto"] == {k: sorted(v) for k, v in framing.PROTO_FIELDS.items()}
    assert t["proto_codes"] == sorted(bridge.PROTO_CODES)
    assert t["meta_keys"] == sorted(framing.META_KEYS)
    c = VECTORS["constants"]
    assert c["max_answer"] == framing.MAX_ANSWER and c["chunk_room"] == bridge.CHUNK_ROOM
    assert c["pending_no_record_ms"] == framing.PENDING_NO_RECORD_MS
    assert c["relay_device_response_bytes"] == framing.RELAY_DEVICE_RESPONSE_BYTES
    assert c["busy_allowance"] == bridge.BUSY_ALLOWANCE


def test_round_2_cases_are_present_and_classified():
    names = {c["name"] for c in VECTORS["cases"]}
    assert {
        "answer_write", "ticket_key_not_next", "outcome_get_failed", "outcome_get_partial", "outcome_get_unknown_idem",
        "outcome_get_idem_expired", "outcome_get_pending_admitted", "same_idem_new_rid_while_running",
        "request_with_rd", "answer_over_the_relay_budget", "already_done_on_a_read", "stale_epoch_under_a_burned_key",
    } <= names  # fmt: skip
    for c in VECTORS["cases"]:
        meta = c["answer"][0]["meta"] if c["answer"] else None
        if meta and "refusal" in meta:
            assert c["retry_class"] == framing.retry_class(meta["refusal"])
            if meta["refusal"] not in framing.PROTO_CODES:
                assert meta["retryable"] is (c["retry_class"] in ("A", "B")), c["name"]


@pytest.mark.parametrize("case", VECTORS["cases"], ids=[c["name"] for c in VECTORS["cases"]])
def test_each_case_opens_and_verifies(case):
    wk = {int(e): bytes.fromhex(k) for e, k in VECTORS["wk"].items()}
    wsk_pub = crypto.unb64u(VECTORS["wsk_pub"])
    env = bytes.fromhex(case["request"])
    h, body, _sig = wire.parse_envelope(env, direction=1)
    pt = wire.open_body(wire.k_bridge(wk[h.epoch], h.workspace, h.epoch), env[: wire.HEADER_LEN], body)
    if case["request_meta"] is not None:
        assert wire.parse_plaintext(pt)[0] == case["request_meta"]
    got = []
    for c in case["response"]:
        rh, meta, data = open_chunk(bytes.fromhex(c), wk, wsk_pub)
        assert (rh.rid, rh.device, rh.epoch) == (h.rid, h.device, h.epoch)
        got.append({"idx": rh.seq, "flags": rh.flags, "epoch": rh.epoch, "meta": meta, "data": data.hex()})
    assert got == case["answer"]


def test_replaying_the_requests_gives_the_same_answers(tmp_path):
    w = World(tmp_path, seed=bytes.fromhex(VECTORS["device"]["seed"]))
    try:
        assert w.phone.device == VECTORS["device"]["device_id"]
        for case in VECTORS["cases"]:
            for action in case["setup"]:
                apply_setup(w, action)
            w.clock.ms = case["now_ms"]
            chunks = w.send(bytes.fromhex(case["request"]), run=case["run"])
            answer = []
            for c in chunks:
                h, meta, data = open_chunk(c, w.wk, w.key.public_key)
                answer.append({"idx": h.seq, "flags": h.flags, "epoch": h.epoch, "meta": meta, "data": data.hex()})
            assert answer == case["answer"], case["name"]
    finally:
        w.close()
