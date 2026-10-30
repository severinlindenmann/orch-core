"""Builds ``contracts/hub/vectors_hub_closing.json``: the signed ``hub.closing`` notice (#1463, protocol.md §9.7) and the
removal proof digest.

Test code, deterministic (FAKE keys from fixed seeds). The hub replays every case against ``verify_closing``; orch-core
(``orch.hubclient.closing``) replays the SAME ``cases`` list and must give the same answers. ``digest_cases`` are the
console side (hub tests, and the browser's ``channel.js`` in the build step); core never reads them.
``uv run python contracts/hub/make_vectors_hub_closing.py`` rewrites the file."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from orch_hub.contract import closing as C
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import cj

REMOVAL_ID = G.fake16("removal_1")
BSEQ = 3  # the workspace's current binding_seq in every case
ISSUED = G.NOW - 60_000
DAY = G.DAY
HEX_NONCE = G.fake16("remove_nonce")


def notice(label: str = "sig_hub_closing", key: str = "hk_op", **kw: Any) -> dict[str, Any]:
    o = C.closing_object(
        hub_id=G.HUB_ID,
        op_cert_id=G.OP_CERT_ID,
        workspace_id=G.WS_A,
        binding_seq=BSEQ,
        removal_id=REMOVAL_ID,
        issued_ms=ISSUED,
        dev=True,
    )
    o = {**o, **kw}
    for k, v in list(o.items()):
        if v is G.DROP:
            del o[k]
    return G.signed(key, label, o)


def case(
    name: str,
    bundle: dict[str, Any],
    expect: dict[str, Any],
    *,
    ws: str = G.WS_A,
    bseq: int = BSEQ,
    now: int = G.NOW,
    dev_mode: bool = True,
    revoked: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "args": {
            "notice": bundle,
            "workspace_id": ws,
            "binding_seq": bseq,
            "now_ms": now,
            "dev_mode": dev_mode,
            "revoked_op_certs": revoked or [],
        },
        "expect": expect,
    }


def cases() -> list[dict[str, Any]]:
    good = notice()
    ok = {"ok": True}
    bad = lambda code: {"error": code}
    tampered = {"o": {**good["o"], "ends_ms": good["o"]["ends_ms"] + DAY}, "sig": good["sig"]}
    return [
        case("valid notice", good, ok),
        case("valid at the last millisecond", good, ok, now=good["o"]["ends_ms"] - 1),
        case("wrong label (signed as a snapshot)", notice(label="sig_hub_snapshot"), bad("bad_signature")),
        case("wrong label (signed as company skills)", notice(label="sig_hub_org_skills"), bad("bad_signature")),
        case("wrong key (the root key, not the op key)", notice(key="hk_root"), bad("bad_signature")),
        case("signed by another hub's keys", notice(key="hk_op_rogue"), bad("bad_signature")),
        case("a tampered field (ends_ms moved)", tampered, bad("bad_signature")),
        case(
            "a tampered field (removal_id swapped)",
            {"o": {**good["o"], "removal_id": G.fake16("removal_2")}, "sig": good["sig"]},
            bad("bad_signature"),
        ),
        case("an extra field", notice(reason="closing"), bad("malformed")),
        case("a missing field (removal_id)", notice(removal_id=G.DROP), bad("malformed")),
        case("a missing field (binding_seq)", notice(binding_seq=G.DROP), bad("malformed")),
        case("a missing field (dev)", notice(dev=G.DROP), bad("malformed")),
        case("removal_id is not 16 bytes of hex", notice(removal_id="abc"), bad("malformed")),
        case("binding_seq is zero", notice(binding_seq=0), bad("malformed")),
        case("binding_seq is a string", notice(binding_seq="3"), bad("malformed")),
        case("another hub (signed by this hub's op key)", notice(hub_id=G.ROGUE_HUB_ID), bad("other_hub")),
        case("another workspace (the notice names B)", notice(workspace_id=G.WS_B), bad("other_workspace")),
        case("the verifier is workspace B, the notice names A", good, bad("other_workspace"), ws=G.WS_B),
        case("a stale binding_seq (the workspace rejoined)", good, bad("stale_binding"), bseq=BSEQ + 1),
        case("a binding_seq of a binding not yet seen", notice(binding_seq=BSEQ + 1), bad("stale_binding")),
        case("another op cert id", notice(op_cert_id=G.OP_CERT_NEXT), bad("stale_op_cert")),
        case("the op cert is revoked", good, bad("revoked"), revoked=[G.OP_CERT_ID]),
        case("dev notice at a real verifier", good, bad("dev_refused"), dev_mode=False),
        case("real notice at a dev verifier", notice(dev=False), bad("dev_mismatch")),
        case("issued more than 300 s ahead", good, bad("cert_invalid"), now=ISSUED - 300_001),
        case("issued exactly 300 s ahead", good, ok, now=ISSUED - 300_000),
        case("ends exactly at issued", notice(ends_ms=ISSUED), bad("cert_invalid")),
        case("ends before issued", notice(ends_ms=ISSUED - 1), bad("cert_invalid")),
        case("lives 7 days", notice(ends_ms=ISSUED + 7 * DAY), ok),
        case("lives more than 7 days", notice(ends_ms=ISSUED + 7 * DAY + 1), bad("cert_invalid")),
        case("expired: now is ends_ms", good, bad("cert_expired"), now=good["o"]["ends_ms"]),
        case("expired: long after", good, bad("cert_expired"), now=good["o"]["ends_ms"] + 30 * DAY),
        case("not an object", [], bad("malformed")),
        case("no signature", {"o": good["o"]}, bad("malformed")),
        case("another kind under the same label", notice(kind="hub.snapshot"), bad("malformed")),
    ]


def digest_cases() -> list[dict[str, Any]]:
    """Console side: ``digest = SHA-256("orch/v2/hub-remove|" || cj(ctx))``. The page recomputes it from ``ctx``."""
    base = {"hub_id": G.HUB_ID, "nonce": HEX_NONCE, "exp_ms": G.NOW + 300_000, "user_id": G.U_ALICE}
    out = []
    for name, kw in (
        ("the context", {}),
        ("another nonce", {"nonce": G.fake16("remove_nonce_2")}),
        ("another user", {"user_id": G.U_BOB}),
        ("another expiry", {"exp_ms": G.NOW + 300_001}),
        ("another hub", {"hub_id": G.ROGUE_HUB_ID}),
    ):
        ctx = C.remove_context(**{**base, **kw})
        out.append(
            {
                "name": name,
                "ctx": ctx,
                "signed_hex": (b"orch/v2/hub-remove|" + cj(ctx)).hex(),
                "digest_hex": C.remove_digest(ctx).hex(),
            }
        )
    return out


def build() -> dict[str, Any]:
    return {
        "contract": "orch.hub-closing/1",
        "note": "protocol.md 9.7. `cases` are replayed by orch-hub (verify_closing) and orch-core (orch.hubclient.closing); "
        "`digest_cases` by the hub and the console only. Fake keys, deterministic.",
        "now_ms": G.NOW,
        "card": G.card(),
        "cases": cases(),
        "digest_cases": digest_cases(),
    }


if __name__ == "__main__":
    out = Path(__file__).with_name("vectors_hub_closing.json")
    out.write_text(json.dumps(build(), indent=1, sort_keys=True) + "\n")
    sys.stdout.write(f"wrote {out}\n")
