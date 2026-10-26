"""Extract contracts/publish/publish_v2.json (suite 2) from contracts/relay/vectors_v2.json.

Usage: python scripts/extract_publish_vectors.py   (from anywhere; paths are relative to the repository root)
"""

import hashlib
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
raw = (ROOT / "contracts" / "relay" / "vectors_v2.json").read_bytes()
src = json.loads(raw)
v = src["suites"]["2"]
out = {
    "comment": "orch v2 publish-request + card vectors, suite 2 (P-256). Extracted verbatim from orch-relay"
    " tests/vectors_v2.json (protocol-v2 sections 7.1, 12.1). All keys are FAKE. orch-core's publish"
    " client (P4) checks parity against THIS file. Regenerate: scripts/extract_publish_vectors.py.",
    "source": {
        "repo": "orch-relay",
        "path": "contracts/relay/vectors_v2.json",
        "sha256": hashlib.sha256(raw).hexdigest(),
    },
    "suite": 2,
    "ids": v["ids"],
    "keys": {k: v["keys"][k] for k in ("person_alice.sig", "wsk_a.sig", "wsk_b.sig")},
    "publish": v["publish"],
    "card": {
        "card": v["card"]["card"],
        "card_seq_2": v["card"]["card_seq_2"],
        "cases": v["card"]["cases"],
    },
}
(ROOT / "contracts" / "publish" / "publish_v2.json").write_text(
    json.dumps(out, indent=1, sort_keys=True)
)
