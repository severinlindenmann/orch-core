"""Shared test helpers for the D11 publish forwarder: a fake host (WSK), bindings, a recording orch-publish."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec

from orch_hub.contract import hub as H
from orch_hub.contract import suite as S
from orch_hub.contract.canon import b64u, cj
from orch_hub.contract.labels import L
from orch_hub.keys import HubKeys

NOW = 1_790_000_000_000
HOST = "127.0.0.1:8103"
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_VEC_PUBLISH = None


def wsk_key(seed_hex: str) -> ec.EllipticCurvePrivateKey:
    """The same derivation as orch-publish's tests: a P-256 key from a vector seed."""
    scalar = int.from_bytes(bytes.fromhex(seed_hex), "big") % (P256_N - 1) + 1
    return ec.derive_private_key(scalar, ec.SECP256R1())


def new_wsk() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def pub_b64(key: ec.EllipticCurvePrivateKey) -> str:
    return b64u(S.public_bytes(key))


def make_hub(tmp: Path) -> HubKeys:
    return HubKeys.init(tmp / "hub", now_ms=lambda: NOW - 1000)


def _signed(key: ec.EllipticCurvePrivateKey, label_key: str, o: dict[str, Any]) -> dict[str, Any]:
    return {"o": o, "sig": b64u(S.sign(key, L[label_key] + cj(o)))}


def binding(wsk, hub: HubKeys, ws: str, seq: int = 1, mode: str = "selective") -> dict[str, Any]:
    return _signed(
        wsk,
        "sig_hub_binding",
        {
            "v": 2, "suite": 2, "hv": 0, "kind": "hub.binding", "workspace_id": ws, "wsk_pub": pub_b64(wsk),
            "hub_id": hub.hub_id, "hk_root_pub": b64u(hub.hk_root_pub), "mode": mode, "binding_seq": seq,
            "valid_from_ms": NOW - 1000, "dev": True,
        },
    )  # fmt: skip


def unbound(wsk, hub: HubKeys, ws: str, seq: int = 1) -> dict[str, Any]:
    return _signed(
        wsk,
        "sig_hub_unbound",
        {
            "v": 2, "suite": 2, "hv": 0, "kind": "hub.unbound", "workspace_id": ws, "hub_id": hub.hub_id,
            "binding_seq": seq, "unbound_ms": NOW, "dev": True,
        },
    )  # fmt: skip


def host_request(
    wsk, hub_id: str, ws: str, *, method="GET", target=None, body=b"", ts=NOW, nonce=None, host=HOST, approval=None
) -> dict[str, Any]:
    """What the host sends over publish_out: v2 headers signed over the hub-path bytes (§11.1)."""
    target = target or f"/v1/ws/{ws}/status"
    nonce = nonce or os.urandom(16)
    pb = H.hub_path_signed_bytes(bytes.fromhex(ws), ts, nonce, method, host, target, body, hub_id, approval)
    headers = {
        "Orch-Workspace": ws,
        "Orch-Ts": str(ts),
        "Orch-Nonce": nonce.hex(),
        "Orch-Signature": b64u(S.sign(wsk, pb)),
    }
    if approval is not None:
        headers["Orch-Owner-Approval"] = b64u(cj(approval))
    return {"method": method, "host": host, "target": target, "headers": headers, "body_hex": body.hex()}


class RecordingSync:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail: Exception | None = None

    def put_hub_card(self, card):
        if self.fail:
            raise self.fail
        self.calls.append(("card", dict(card)))

    def put_op_revoked(self, revoked):
        if self.fail:
            raise self.fail
        self.calls.append(("revoked", dict(revoked)))

    def put_card(self, workspace_id, card, pk_pub):
        if self.fail:
            raise self.fail
        self.calls.append(("ws_card", workspace_id, dict(card), pk_pub))

    def put_binding(self, workspace_id, obj):
        if self.fail:
            raise self.fail
        self.calls.append(("binding", workspace_id, dict(obj)))

    def put_allow(self, workspace_id, obj):
        if self.fail:
            raise self.fail
        self.calls.append(("allow", workspace_id, dict(obj)))


class FakeUpstream:
    """A loopback HTTP server standing in for orch-publish: records requests, answers a scripted response."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status, self.headers, self.body, self.delay = 200, {}, b'{"ok":true}', 0.0
        self.script: list[tuple[int, bytes]] = []  # per-request (status, body) answers, used before the defaults
        up = self

        class Handler(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                up.requests.append(
                    {
                        "method": self.command,
                        "target": self.path,
                        "headers": dict(self.headers.items()),
                        "body": self.rfile.read(n),
                    }
                )
                if up.delay:
                    threading.Event().wait(up.delay)
                status, body = up.script.pop(0) if up.script else (up.status, up.body)
                self.send_response(status)
                for k, v in up.headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_PUT = do_POST = do_DELETE = _do

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(5)


def publish_state(hub: HubKeys, ws: str, wsk, b: dict[str, Any]) -> dict[str, Any]:
    """The state the contract's check_hub_publish reads, as orch-publish would hold it."""
    held = H.verify_card(hub.card(), now_ms=NOW, dev_mode=True, pinned_root=hub.hk_root_pub)
    o = b["o"]
    return {
        "cards": {ws: {"wsk_pub": pub_b64(wsk), "owner_person_id": "00" * 16}},
        "bindings": {ws: {"binding_seq": o["binding_seq"], "status": "bound", "binding": o}},
        "hub_cards": {hub.hub_id: held},
        "nonces": {},
    }


def dump(o: Any) -> str:
    return json.dumps(o, sort_keys=True)
