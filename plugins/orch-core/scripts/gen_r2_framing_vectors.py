#!/usr/bin/env python3
"""Generate ``tests/relay/r2_vectors/r2_framing.json``: the R2 framing sheet as data, for orch-mobile (orch-f1).

Run from ``plugins/orch-core``: ``uv run python scripts/gen_r2_framing_vectors.py``. Never edit the output by hand;
``tests/relay/test_r2_vectors.py`` checks that the file is consistent with the code (the tables are equal, every
request opens and verifies, the host answers it as recorded, every response chunk verifies and opens).

Keys are test-only and derived from a fixed seed. ECDSA signatures and the 16-byte salts are random, so a re-run
gives other bytes for the same content; the checks are semantic.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orch import crypto  # noqa: E402
from orch.relay import bridge, framing, submit, wire  # noqa: E402
from tests.relay.bridgekit import T0, World, apply_setup, open_chunk, ulid  # noqa: E402

OUT = ROOT / "tests" / "relay" / "r2_vectors" / "r2_framing.json"
SEED = b"r2-framing-vectors-v1"


def main() -> None:
    cases = []
    with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as tmp:
        w = World(Path(tmp).resolve(), seed=SEED)
        try:
            stranger_seed = SEED + b"-stranger"
            from tests.relay.bridgekit import Phone

            stranger = Phone(seed=stranger_seed)

            def run(name: str, meta: dict | bytes, note: str, *, phone=None, setup=(), execute=True, **kw) -> None:
                for action in setup:
                    apply_setup(w, action)
                p = phone or w.phone
                kw.setdefault("ts_ms", w.clock.ms)
                env = p.request(w.wk, meta, **kw)
                n = len(w.post.posts)
                w.send(env, run=execute)
                chunks = [e for _, r, e in w.post.posts[n:] if r == env[40:56].hex()]
                answer = []
                for c in chunks:
                    h, m, d = open_chunk(c, w.wk, w.key.public_key)
                    answer.append({"idx": h.seq, "flags": h.flags, "epoch": h.epoch, "meta": m, "data": d.hex()})
                cases.append({
                    "name": name, "note": note, "now_ms": w.clock.ms,
                    "device": p.device, "request_meta": meta if isinstance(meta, dict) else None,
                    "request_plaintext": None if isinstance(meta, dict) else meta.hex(),
                    "request": env.hex(), "response": [c.hex() for c in chunks], "answer": answer,
                    "setup": list(setup), "run": execute,
                    "retry_class": framing.retry_class(answer[0]["meta"]["refusal"]) if answer and "refusal" in
                    answer[0]["meta"] else None,
                })  # fmt: skip

            run("read_whoami", {"op": "whoami", "args": {}}, "a read: status ok, the Result in data")
            m = w.new_ticket(title="From the phone")
            run("new_ticket", m, "a write: relay.submit of one device-signed ticket.created; idem = event id")
            run("new_ticket_again_new_rid", m, "the same idem under a new rid: the stored outcome, duplicate true")
            run("outcome_get_committed", {"op": "outcome.get", "args": {"idem": m["idem"]}}, "outcome.get")
            run("outcome_get_pending", {"op": "outcome.get", "args": {"idem": ulid(T0)}},
                "no record yet, inside ULID time + 1200 s: pending")  # fmt: skip
            run("forbidden_op_http", {"op": "http", "method": "GET", "path": "/v2/snapshot", "headers": {}},
                "an op outside the closed table: forbidden_scope (checked before the other meta keys)")  # fmt: skip
            run("auth_key", {"op": "whoami", "args": {}, "auth": {}}, "an auth key: req.invalid")
            run("grant_shaped", {"op": "ticket.get", "args": {"ref": "gr_" + "0" * 26 + "." + "A" * 43}},
                "a grant-shaped string anywhere: grant.secret_in_args")  # fmt: skip
            m2 = w.new_ticket()
            del m2["idem"]
            run("write_without_idem", m2, "idem.required")
            m3 = w.new_ticket(idem=ulid(T0))
            run("idem_not_the_event_id", m3, "idem.invalid")
            m4 = w.new_ticket()
            m4["args"]["events"][0] = w.phone.event(m4["args"]["log"], "ticket.created", eid=ulid(T0 - 900_001),
                                                    key="DEMO-9", ticket_type="task", title="old",
                                                    owner="p_" + w.phone.person)  # fmt: skip
            m4["idem"] = m4["args"]["events"][0]["id"]
            run("event_expired", m4, "the event id's ULID time outside [now - 900 s, now + 300 s]: event.expired")
            run("idem_on_a_read", {"op": "whoami", "args": {}, "idem": ulid(T0)}, "an idem on a read: req.invalid")
            run("stale_epoch", {"op": "whoami", "args": {}}, "an old epoch whose key is held: sealed under it",
                epoch=1)  # fmt: skip
            run("stale_timestamp", {"op": "whoami", "args": {}}, "outside ±300 s", ts_ms=T0 - 300_001)
            run("seq_used", {"op": "whoami", "args": {}}, "a used seq: stale_sequence with high", seq=1)
            run("malformed_meta", b"\x00\x00\x00\x02{}", "a meta without op: malformed, seq not used")
            run("not_member", {"op": "whoami", "args": {}}, "a device not in the member list", phone=stranger)
            run("bad_signature", {"op": "whoami", "args": {}}, "signed by another key",
                signer=stranger.dk)  # fmt: skip
            run("chunked_answer", {"op": "ticket.text", "args": {"size": 300_000}},
                "an answer over one chunk: chunk 0 meta {status: ok}, later {}, LAST on the final one")  # fmt: skip
            # -- round 2 (orch-f1's asks) --
            ans = w.phone.event(m["args"]["log"], "question.answered", eid=ulid(T0), question="q1",
                                hash="sha256:" + "0" * 64, option="yes")  # fmt: skip
            run("answer_write", {"op": "answer", "args": {"log": m["args"]["log"], "events": [ans]}, "idem": ans["id"]},
                "an answer write: relay.submit of one device-signed question.answered (scope decide)")  # fmt: skip
            run(
                "ticket_key_not_next",
                w.new_ticket(key="DEMO-TAKEN"),
                "the key is not the next free key: C-auto, rebuild with next_key (advisory) under a NEW event id",
            )
            failed = w.new_ticket(title="refuse me")
            run("write_failed_ticket_exists", failed, "a write refused by the host: ticket.exists, D")
            run("outcome_get_failed", {"op": "outcome.get", "args": {"idem": failed["idem"]}},
                "outcome.get of a failed write: data.state failed with its error")  # fmt: skip
            pid = ulid(T0)
            run("outcome_get_partial", {"op": "outcome.get", "args": {"idem": pid}},
                "outcome.get of a partial record: seqs are events that LANDED, never 'not run'",
                setup=[{"do": "outcome", "idem": pid, "step": "partial", "error": {"code": "invalid.input"},
                        "seqs": [7]}])  # fmt: skip
            run("outcome_get_unknown_idem", {"op": "outcome.get", "args": {"idem": ulid(T0 - 1_200_001)}},
                "no record and past the event id's ULID time + 1200 s: outcome.unknown_idem, C-review")  # fmt: skip
            tid = ulid(T0)
            run("outcome_get_idem_expired", {"op": "outcome.get", "args": {"idem": tid}},
                "a tombstoned idem: idem.expired, B (read the log)",
                setup=[{"do": "outcome", "idem": tid, "step": "tombstone"}])  # fmt: skip
            held = w.new_ticket()
            run("write_admitted_not_yet_run", held, "admitted (T1), still running: no answer yet", execute=False)
            run("outcome_get_pending_admitted", {"op": "outcome.get", "args": {"idem": held["idem"]}},
                "outcome.get of an idem this device has admitted: pending")  # fmt: skip
            run(
                "same_idem_new_rid_while_running",
                held,
                "the same signed event + idem on a NEW rid while it runs: refusal pending (B), never a second run",
            )
            run("request_with_rd", {"op": "whoami", "args": {}, "rd": "sha256:" + "ab" * 32},
                "rd (H12d) is accepted and carried into the identity; never a qualification in v1.7")  # fmt: skip
            run("answer_over_the_relay_budget", {"op": "ticket.text", "args": {"size": framing.MAX_ANSWER}},
                "an answer whose chunk envelopes would exceed the relay's 2 MiB per-device budget: frame.too_large "
                "(the 4 MiB-class answer of round 1 is refused up front, never stored)")  # fmt: skip
            run("already_done_on_a_read", {"op": "whoami", "args": {}},
                "stamped before the host (re)started: already_done {status: unknown}; on a read: re-issue, new rid",
                setup=[{"do": "clock", "ms": 1000}, {"do": "restart"}], ts_ms=T0 + 500)  # fmt: skip
            run(
                "stale_epoch_under_a_burned_key",
                {"op": "whoami", "args": {}},
                "a request under a burned epoch above the committed one: ONLY a signed stale_epoch under that key, "
                "naming the pending epoch, else EHW + 1 (C5, proto 8.5 G2b)",
                setup=[{"do": "burn"}],
                epoch=3,
            )
        finally:
            w.close()
    doc = {
        "comment": "R2 framing vectors (orch-core 11A.5 'R2 framing'); generated by scripts/gen_r2_framing_vectors.py",
        "suite": wire.SUITE,
        "workspace_id": w.bridge.ws_hex,
        "epoch": w.epochs.cur,
        "burned": sorted(w.epochs.burned),
        "wk": {str(e): k.hex() for e, k in w.epochs.keys.items()},
        "wsk_pub": crypto.b64u(w.key.public_key),
        "device": {
            "device_id": w.phone.device,
            "person_id": w.phone.person,
            "seed": SEED.hex(),
            "dk_sig_pub": crypto.b64u(w.phone.dk_pub),
            "cert": w.phone.cert,
            "scopes": list(w.phone.member().scopes),
        },  # fmt: skip
        "tables": {
            "ops": {
                k: {"backend": v.backend, "kind": v.kind, "scope": v.scope, "event": v.event}
                for k, v in framing.OPS.items()
            },  # fmt: skip
            "events": {k: list(v) for k, v in submit.EVENTS.items()},
            "auth": sorted(submit.AUTH),
            "retry": dict(sorted(framing.RETRY.items())),
            "retry_context": {k: dict(v) for k, v in sorted(framing.RETRY_CONTEXT.items())},
            "retry_notes": dict(sorted(framing.RETRY_NOTES.items())),
            "refusal_fields": {
                "proto": {k: sorted(v) for k, v in sorted(framing.PROTO_FIELDS.items())},
                "host": {
                    "always": ["retryable"],
                    "seqs": "only for a write whose events LANDED (failed/partial)",
                    "next_key": ["ticket.key_not_next"],
                },
            },  # fmt: skip
            "proto_codes": sorted(bridge.PROTO_CODES),
            "meta_keys": sorted(framing.META_KEYS),
        },
        "constants": {
            "max_answer": framing.MAX_ANSWER,
            "relay_device_response_bytes": framing.RELAY_DEVICE_RESPONSE_BYTES,
            "busy_allowance": bridge.BUSY_ALLOWANCE,
            "device_chunk_bytes": bridge.DEVICE_CHUNK_BYTES,
            "stuck_backoff_ms": list(bridge.STUCK_BACKOFF_MS),
            "chunk_room": bridge.CHUNK_ROOM,
            "window_ms": bridge.WINDOW_MS,
            "pending_no_record_ms": framing.PENDING_NO_RECORD_MS,
            "fresh_past_ms": framing.FRESH_PAST_MS,
            "fresh_future_ms": framing.FRESH_FUTURE_MS,
            "budget": bridge.BUDGET,
            "per_device": bridge.PER_DEVICE,
            "replay_every_ms": bridge.REPLAY_EVERY_MS,
            "replay_max": bridge.REPLAY_MAX,
        },  # fmt: skip
        "cases": cases,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(cases)} cases")


if __name__ == "__main__":
    main()
