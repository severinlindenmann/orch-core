"""Builds ``contracts/hub/vectors_org_secrets_bundle.json``: the signed ``hub.org_secrets`` bundle (M3, #1431, S-2).

Test code, deterministic (FAKE keys from fixed seeds, the wraps are random-looking FAKE bytes that only have the right shape:
the hub cannot open a wrap, and the bundle verifier does not either). The hub replays every case against
``verify_org_secrets``; the host side (orch-core, slice S-3) must replay the same file and give the same answers.
``uv run python contracts/hub/make_vectors_org_secrets_bundle.py`` rewrites the file."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from orch_hub.contract import suite as S
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u


def sealed(tag: str, n: int = 40) -> str:
    """A wrap-shaped value: a valid P-256 point (the fixed WXK public key) and ``n`` more bytes."""
    return b64u(G.K.pub["wxk_a"] + S.fake_seed("secret-bundle/" + tag)[:1] * n + b"\0" * 16)


def secret(name: str, **kw: Any) -> dict[str, Any]:
    return {
        "id": G.fake16("secret/" + name),
        "name": name,
        "version": 1,
        "expires_ms": G.NOW + 90 * 86_400_000,
        "wxk_version": 1,
        "sealed": sealed(name),
        **kw,
    }


def bundle(
    version: int = 2, secrets: list | None = None, ws: str = G.WS_A, key: str = "hk_op", **kw: Any
) -> dict[str, Any]:
    o = {
        **G.hdr("hub.org_secrets"),
        "hub_id": G.HUB_ID,
        "op_cert_id": G.OP_CERT_ID,
        "workspace_id": ws,
        "version": version,
        "issued_ms": G.NOW - 60_000,
        "secrets": secrets if secrets is not None else [secret("crm-key")],
        "dev": True,
        **kw,
    }
    return G.signed(key, "sig_hub_org_secrets", o)


def build() -> dict[str, Any]:
    ok = bundle()
    a = lambda b, pinned=None, ws=G.WS_A: {"bundle": b, "workspace_id": ws, "pinned": pinned}
    cases = [
        ("accepted", a(ok), {"ok": True}),
        ("two secrets sorted by name", a(bundle(secrets=[secret("a-key"), secret("b-key")])), {"ok": True}),
        ("empty bundle (everything withdrawn) is valid", a(bundle(3, [])), {"ok": True}),
        ("a higher version replaces the pinned one", a(ok, bundle(1)["o"]), {"ok": True}),
        ("the same version and bytes is a no-op", a(ok, ok["o"]), {"ok": True}),
        (
            "the same version with other bytes is a conflict",
            a(ok, bundle(2, [secret("other-key")])["o"]),
            {"error": "bundle_conflict"},
        ),
        ("a lower version is a rollback", a(bundle(1), ok["o"]), {"error": "stale_bundle"}),
        ("the bundle of another workspace", a(bundle(ws=G.WS_B)), {"error": "other_workspace"}),
        ("signed by the wrong key", a(bundle(key="hk_root")), {"error": "bad_signature"}),
        (
            "a skills bundle is not a secrets bundle",
            a(bundle(secrets=[secret("x-key")], kind="hub.org_skills")),
            {"error": "malformed"},
        ),
        (
            "a plaintext member smuggled into an entry",
            a(bundle(secrets=[{**secret("crm-key"), "value": "hunter2hunter2"}])),
            {"error": "malformed"},
        ),
        (
            "a missing member",
            a(bundle(secrets=[{k: v for k, v in secret("crm-key").items() if k != "wxk_version"}])),
            {"error": "malformed"},
        ),
        (
            "a wrap that is too short",
            a(bundle(secrets=[secret("crm-key", sealed=b64u(b"\x04" * 40))])),
            {"error": "malformed"},
        ),
        (
            "a wrap that does not start with a curve point",
            a(bundle(secrets=[secret("crm-key", sealed=b64u(b"\x05" + b"\1" * 100))])),
            {"error": "malformed"},
        ),
        (
            "a wrap that is not base64url",
            a(bundle(secrets=[secret("crm-key", sealed="not base64!")])),
            {"error": "malformed"},
        ),
        ("a bad name", a(bundle(secrets=[secret("Crm")])), {"error": "malformed"}),
        ("a version of zero", a(bundle(secrets=[secret("crm-key", version=0)])), {"error": "malformed"}),
        ("a bad id", a(bundle(secrets=[secret("crm-key", id="zz")])), {"error": "malformed"}),
        ("secrets not sorted by name", a(bundle(secrets=[secret("b-key"), secret("a-key")])), {"error": "malformed"}),
        ("a duplicate name", a(bundle(secrets=[secret("a-key"), secret("a-key")])), {"error": "malformed"}),
    ]
    return {
        "doc": "hub.org_secrets (M3 #1431): replay each case with verify_org_secrets against the card",
        "card": G.card(),
        "now_ms": G.NOW,
        "cases": [{"name": n, "args": args, "expect": e} for n, args, e in cases],
    }


if __name__ == "__main__":
    out = Path(__file__).with_name("vectors_org_secrets_bundle.json")
    out.write_text(json.dumps(build(), indent=1, sort_keys=True) + "\n")
    sys.stdout.write(f"wrote {out}\n")
