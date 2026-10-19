"""Drives the reference phone against a running ``dev-pair up --expect-code-from <root>/codes`` (B1 recipe).

    ORCH_RELAY_DIR=<relay at the SOURCE pin> uv run scripts/dev-pair up --root ~/orch-dev/.dp1 --relay local \
        --sim-convenience --expect-code-from ~/orch-dev/.dp1/codes &
    uv run python -m tests.relay.devpair_phone ~/orch-dev/.dp1      # from plugins/orch-core
    uv run scripts/dev-pair down --root ~/orch-dev/.dp1

It waits for ``<root>/pair.json``, pairs (the SAS and ``cert_code`` go to the FIFO), joins, and does a column-R
``whoami`` and ``ticket.query``. Test tree only."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .phone import DevPhone, HttpNet, write_code


def main(root_arg: str) -> int:
    root = Path(root_arg).expanduser()
    end = time.time() + 600
    while not (root / "pair.json").exists():
        if time.time() > end:
            print("no pair.json", file=sys.stderr)
            return 1
        time.sleep(0.5)
    time.sleep(0.5)
    pj = json.loads((root / "pair.json").read_text())
    fifo = root / "codes"
    phone = DevPhone(pj["link"], label="dev-pair driver phone")
    net = HttpNet(pj["origin"], pj["workspace_id"])

    def codes(offer: str, phase: str, code: str) -> None:
        print("code:", phase, flush=True)
        write_code(fifo, offer, phase, code)

    ap = phone.pair(net, codes=codes, deadline_s=300, poll_s=0.5)
    print("approved", ap.scopes, flush=True)
    end = time.time() + 120
    while True:
        try:
            phone.join(net)
            break
        except Exception as exc:  # noqa: BLE001 - the membership is published a moment after approval
            if time.time() > end:
                raise
            print("join retry:", exc, flush=True)
            time.sleep(1)
    meta, data = phone.whoami(net)
    print("whoami", meta, json.dumps(data["data"]["relay"]), flush=True)
    meta, data = phone.call(net, {"op": "ticket.query", "args": {}})
    print("ticket.query", meta, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
