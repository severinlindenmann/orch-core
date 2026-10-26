"""Run by the cross-repo test under orch-hub's own environment (``ORCH_HUB_SRC``): reads ``{"request": ...}`` (a
``publish_out`` request body as the host built it) on stdin, runs it through the hub's REAL request validator
(``orch_hub.publish.request.parse``), checks the host's WSK signature with the hub's own bytes, adds the hub's two
headers exactly as ``orch_hub.publish.forwarder`` does (vector keys, all FAKE) and prints one JSON line:
``{"forwarded": request}`` or ``{"refused": code}``. Not a test."""

import json
import sys
from pathlib import Path

hub_src = Path(sys.argv[1])
sys.path[:0] = [str(hub_src / "src")]

from orch_hub.contract import hub as H  # noqa: E402
from orch_hub.contract import suite as S  # noqa: E402
from orch_hub.contract import vectorgen as G  # noqa: E402
from orch_hub.contract.canon import b64u  # noqa: E402
from orch_hub.contract.hub import Refused  # noqa: E402
from orch_hub.publish import request as R  # noqa: E402

request = json.load(sys.stdin)["request"]
hub_id = H.hub_id(G.K.pub["hk_root"])
try:
    p = R.parse(request, max_body=8 * 1024 * 1024)
    pbytes = H.hub_path_signed_bytes(
        bytes.fromhex(p.workspace_id), p.ts_ms, p.nonce, p.method, p.host, p.target, p.body, hub_id, p.approval
    )
    if not S.verify(G.K.pub["wsk_a"], p.wsk_sig, pbytes):
        raise Refused("bad_signature")
    headers = dict(p.headers)
    headers["Orch-Hub"] = hub_id  # the forwarder's own step: the hub adds these two and nothing else
    headers["Orch-Hub-Signature"] = b64u(S.sign(G.K.priv["hk_op"], H.hub_publish_bytes(hub_id, pbytes)))
    print(json.dumps({"forwarded": {**request, "headers": headers}}))
except Refused as exc:
    print(json.dumps({"refused": exc.code}))
