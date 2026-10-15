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
from tests.relay.bridgekit import T0, World, open_chunk, ulid  # noqa: E402

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

            def run(name: str, meta: dict | bytes, note: str, *, phone=None, **kw) -> None:
                p = phone or w.phone
                env = p.request(w.wk, meta, **kw)
                n = len(w.post.posts)
                w.send(env)
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
        finally:
            w.close()
    doc = {
        "comment": "R2 framing vectors (orch-core 11A.5 'R2 framing'); generated by scripts/gen_r2_framing_vectors.py",
        "suite": wire.SUITE,
        "workspace_id": w.bridge.ws_hex,
        "epoch": w.epochs.cur,
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
            "proto_codes": sorted(bridge.PROTO_CODES),
            "meta_keys": sorted(framing.META_KEYS),
        },
        "constants": {
            "max_answer": framing.MAX_ANSWER,
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
