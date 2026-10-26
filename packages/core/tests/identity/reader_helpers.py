"""Reader declarations for tests (orch-v2-host §0.8.6 item 9.3): signed by a test ``Person``'s device key."""

from __future__ import annotations

from typing import Any

from orch import canon, crypto
from orch.identity import reader


def decl_o(p: Any, ws: str, *, level: int = 1, via: str = "declare", created_ms: int, surfaces=("log",), **changes):
    o = {
        "v": 1,
        "kind": "reader_decl",
        "suite": 2,
        "workspace_id": ws,
        "device_id": crypto.device_id(p.sig_pub).hex(),
        "dk_hash": crypto.b64u(crypto.dk_hash(p.sig_pub, p.kx_pub)),
        "who": {"person_id": crypto.person_id(p.pk_pub).hex()},
        "reader": level,
        "kat": {"1": {s: reader.KAT_DIGESTS["1"][s] for s in surfaces}} if level >= 1 else {},
        "bind": {"via": via},
        "created_ms": created_ms,
    }
    o.update(changes)
    return o


def sign_decl(p: Any, o: dict[str, Any], key: Any = None) -> dict[str, Any]:
    msg = reader.LABEL + canon.cj_checked(o)
    return {"o": o, "sig": crypto.b64u(crypto.sign(key or p.dk_sig, msg))}


def decl(p: Any, ws: str, **kw: Any) -> dict[str, Any]:
    return sign_decl(p, decl_o(p, ws, **kw))


def malleate(d: dict[str, Any]) -> dict[str, Any]:
    """The same declaration with the other valid ECDSA signature (s -> n - s): another ``rd``, the same ``o``."""
    raw = crypto.unb64u(d["sig"], crypto.SIG_LEN)
    r, s = raw[:32], int.from_bytes(raw[32:], "big")
    return {"o": d["o"], "sig": crypto.b64u(r + (crypto.P256_N - s).to_bytes(32, "big"))}
