"""Builds ``contracts/hub/vectors_org_skills.json``: the signed ``hub.org_skills`` bundle (M3, #1430, Opus conditions 1 and 4).

Test code, deterministic (FAKE keys from fixed seeds). The hub replays every case against ``verify_org_skills``; the host
side (orch-core, the M3 host adoption slice) must replay the same file and give the same answers.
``uv run python contracts/hub/make_vectors_org_skills.py`` rewrites the file."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from orch_hub.contract import vectorgen as G
from orch_hub.contract.org_bundles import mcp_digest, skill_digest

RLO = chr(0x202E)  # built, so no bidi character sits in this file
TEXT = "Write invoice numbers as INV-2026-0001."


def skill(name: str, text: str = TEXT, **kw: Any) -> dict[str, Any]:
    return {"name": name, "text": text, "digest": skill_digest(name, text), **kw}


def bundle(
    version: int = 2, skills: list | None = None, ws: str = G.WS_A, key: str = "hk_op", **kw: Any
) -> dict[str, Any]:
    o = {
        **G.hdr("hub.org_skills"),
        "hub_id": G.HUB_ID,
        "op_cert_id": G.OP_CERT_ID,
        "workspace_id": ws,
        "version": version,
        "issued_ms": G.NOW - 60_000,
        "skills": skills if skills is not None else [skill("invoices")],
        "dev": True,
        **kw,
    }
    return G.signed(key, "sig_hub_org_skills", o)


#: one case per class of text a person cannot see (or that is not text): refused, never stripped
HIDDEN = {
    "bidi override": 0x202E,
    "zero width space": 0x200B,
    "variation selector": 0xFE0F,
    "variation selector supplement": 0xE0100,
    "tag character": 0xE0041,
    "hangul filler": 0x3164,
    "halfwidth hangul filler": 0xFFA0,
    "line separator": 0x2028,
    "paragraph separator": 0x2029,
    "braille blank": 0x2800,
    "soft hyphen": 0x00AD,
    "private use": 0xE000,
    "control": 0x1B,
    "carriage return": 0x0D,
    "byte order mark": 0xFEFF,
}


def text_cases() -> list[dict[str, Any]]:
    cases = [
        {"name": n, "skill_name": "invoices", "text": "ok" + chr(c) + "text", "expect": "hidden_characters"}
        for n, c in HIDDEN.items()
    ]
    cases.append(
        {
            "name": "combining mark after a digit",
            "skill_name": "invoices",
            "text": "1" + chr(0x0301),
            "expect": "hidden_characters",
        }
    )
    for n, name, text, expect in (
        ("plain text", "invoices", TEXT, None),
        (
            "accents, other scripts, symbols",
            "invoices",
            "Rechnung \u00fcber 100 \u20ac \u65e5\u672c\u8a9e \U0001f600",
            None,
        ),
        ("newline and tab", "invoices", "a\tb\nc", None),
        ("reserved name", "orch-x", TEXT, "reserved_name"),
        ("bad name", "Invoices", TEXT, "name"),
        ("empty", "invoices", " \n", "empty"),
        ("too long", "invoices", "x" * 2401, "too_long"),
        ("front matter", "invoices", "---\nx", "front_matter"),
    ):
        cases.append({"name": n, "skill_name": name, "text": text, "expect": expect})
    return cases


URL = "https://tools.example.com/mcp"


def server(name: str = "crm", **kw: Any) -> dict[str, Any]:
    e = {"name": name, "url": URL, "auth": "none", "secret": None, "description": "Our CRM", **kw}
    return {**e, "digest": mcp_digest(e)}


def mcp_bundle(
    version: int = 2, servers: list | None = None, ws: str = G.WS_A, key: str = "hk_op", **kw: Any
) -> dict[str, Any]:
    o = {
        **G.hdr("hub.mcp_servers"),
        "hub_id": G.HUB_ID,
        "op_cert_id": G.OP_CERT_ID,
        "workspace_id": ws,
        "version": version,
        "issued_ms": G.NOW - 60_000,
        "servers": servers if servers is not None else [server()],
        "dev": True,
        **kw,
    }
    return G.signed(key, "sig_hub_mcp_servers", o)


def mcp_cases() -> list[dict[str, Any]]:
    """Server entries (the host replays them with its own ``mcp_problem``) and signed bundles (both sides)."""
    entries = [
        ("remote https server", {}, None),
        ("a port and a path", {"url": "https://tools.example.com:8443/a/b"}, None),
        ("a company secret by name", {"auth": "hub_secret", "secret": "crm-key"}, None),
        ("http is refused", {"url": "http://tools.example.com/mcp"}, "https_only"),
        ("a command is not an address", {"url": "npx -y some-server"}, "https_only"),
        ("a key in the query", {"url": URL + "?key=abc"}, "url"),
        ("a fragment", {"url": URL + "#x"}, "url"),
        ("credentials in the address", {"url": "https://user:pw@tools.example.com/"}, "url"),
        ("a space", {"url": "https://tools.example.com/a b"}, "url"),
        ("loopback name", {"url": "https://localhost/mcp"}, "not_remote"),
        ("loopback address", {"url": "https://127.0.0.1/mcp"}, "not_remote"),
        ("loopback v6", {"url": "https://[::1]/mcp"}, "not_remote"),
        ("cloud metadata address", {"url": "https://169.254.169.254/x"}, "not_remote"),
        ("a one-word host", {"url": "https://intranet/mcp"}, "not_remote"),
        ("reserved name", {"name": "orch-tools"}, "reserved_name"),
        ("sign-in needs a secret name", {"auth": "hub_secret"}, "secret"),
        ("a secret name without a sign-in", {"secret": "crm-key"}, "secret"),
        ("unknown sign-in", {"auth": "oauth"}, "auth"),
        ("hidden character in the description", {"description": "ok" + chr(0xE0041)}, "description"),
        ("a newline in the description", {"description": "a\nb"}, "description"),
    ]
    out = [
        {
            "name": n,
            "entry": {"name": "crm", "url": URL, "auth": "none", "secret": None, "description": "Our CRM", **kw},
            "expect": e,
        }
        for n, kw, e in entries
    ]
    out.append({"name": "no room for a command", "entry": {"name": "crm", "command": "npx"}, "expect": "shape"})
    return out


def mcp_bundle_cases() -> list[dict[str, Any]]:
    ok = mcp_bundle()
    cases = [
        ("accepted", {"bundle": ok, "workspace_id": G.WS_A, "pinned": None}, {"ok": True}),
        (
            "a lower version is a rollback",
            {"bundle": mcp_bundle(1), "workspace_id": G.WS_A, "pinned": ok["o"]},
            {"error": "stale_bundle"},
        ),
        (
            "the bundle of another workspace",
            {"bundle": mcp_bundle(ws=G.WS_B), "workspace_id": G.WS_A, "pinned": None},
            {"error": "other_workspace"},
        ),
        (
            "signed by the wrong key",
            {"bundle": mcp_bundle(key="hk_root"), "workspace_id": G.WS_A, "pinned": None},
            {"error": "bad_signature"},
        ),
        (
            "a local command smuggled into an entry",
            {"bundle": mcp_bundle(servers=[{**server(), "command": "npx"}]), "workspace_id": G.WS_A, "pinned": None},
            {"error": "malformed"},
        ),
        (
            "an http address",
            {
                "bundle": mcp_bundle(servers=[server(url="http://x.example.com/")]),
                "workspace_id": G.WS_A,
                "pinned": None,
            },
            {"error": "malformed"},
        ),
        (
            "a skills bundle is not a servers bundle",
            {"bundle": bundle(), "workspace_id": G.WS_A, "pinned": None},
            {"error": "malformed"},
        ),
    ]
    return [{"name": n, "args": a, "expect": e} for n, a, e in cases]


def build() -> dict[str, Any]:
    ok = bundle()
    cases = [
        ("accepted", {"bundle": ok, "workspace_id": G.WS_A, "pinned": None}, {"ok": True}),
        (
            "empty bundle (everything withdrawn) is valid",
            {"bundle": bundle(3, []), "workspace_id": G.WS_A, "pinned": None},
            {"ok": True},
        ),
        (
            "a higher version replaces the pinned one",
            {"bundle": ok, "workspace_id": G.WS_A, "pinned": bundle(1)["o"]},
            {"ok": True},
        ),
        (
            "the same version and bytes is a no-op",
            {"bundle": ok, "workspace_id": G.WS_A, "pinned": ok["o"]},
            {"ok": True},
        ),
        (
            "the same version with other bytes is a conflict",
            {"bundle": ok, "workspace_id": G.WS_A, "pinned": bundle(2, [skill("support", "Greet people.")])["o"]},
            {"error": "bundle_conflict"},
        ),
        (
            "a lower version is a rollback",
            {"bundle": bundle(1), "workspace_id": G.WS_A, "pinned": ok["o"]},
            {"error": "stale_bundle"},
        ),
        (
            "the bundle of another workspace",
            {"bundle": bundle(ws=G.WS_B), "workspace_id": G.WS_A, "pinned": None},
            {"error": "other_workspace"},
        ),
        (
            "signed by the wrong key",
            {"bundle": bundle(key="hk_root"), "workspace_id": G.WS_A, "pinned": None},
            {"error": "bad_signature"},
        ),
        (
            "content changed after signing",
            {"bundle": {"o": {**ok["o"], "skills": []}, "sig": ok["sig"]}, "workspace_id": G.WS_A, "pinned": None},
            {"error": "bad_signature"},
        ),
        (
            "a name starting with orch shadows a builtin skill",
            {"bundle": bundle(skills=[skill("orch-tickets")]), "workspace_id": G.WS_A, "pinned": None},
            {"error": "malformed"},
        ),
        (
            "a bidi control character in the text",
            {"bundle": bundle(skills=[skill("invoices", "ok" + RLO)]), "workspace_id": G.WS_A, "pinned": None},
            {"error": "malformed"},
        ),
        *[
            (
                f"{n} in the text",
                {"bundle": bundle(skills=[skill("invoices", "ok" + chr(c))]), "workspace_id": G.WS_A, "pinned": None},
                {"error": "malformed"},
            )
            for n, c in HIDDEN.items()
        ],
        (
            "front matter",
            {
                "bundle": bundle(skills=[skill("invoices", "---\nname: x\n---\nbody")]),
                "workspace_id": G.WS_A,
                "pinned": None,
            },
            {"error": "malformed"},
        ),
        (
            "a text over 2400 characters",
            {"bundle": bundle(skills=[skill("invoices", "x" * 2401)]), "workspace_id": G.WS_A, "pinned": None},
            {"error": "malformed"},
        ),
        (
            "a digest that does not match the text",
            {
                "bundle": bundle(skills=[skill("invoices", digest="sha256:" + "0" * 64)]),
                "workspace_id": G.WS_A,
                "pinned": None,
            },
            {"error": "malformed"},
        ),
        (
            "skills not sorted by name",
            {"bundle": bundle(skills=[skill("support"), skill("invoices")]), "workspace_id": G.WS_A, "pinned": None},
            {"error": "malformed"},
        ),
    ]
    return {
        "doc": "hub.org_skills (M3 #1430): replay each case with verify_org_skills against the card",
        "card": G.card(),
        "now_ms": G.NOW,
        "text_cases": text_cases(),
        "mcp_cases": mcp_cases(),
        "mcp_bundle_cases": mcp_bundle_cases(),
        "cases": [{"name": n, "args": a, "expect": e} for n, a, e in cases],
    }


if __name__ == "__main__":
    out = Path(__file__).with_name("vectors_org_skills.json")
    out.write_text(json.dumps(build(), indent=1, sort_keys=True) + "\n")
    sys.stdout.write(f"wrote {out}\n")
