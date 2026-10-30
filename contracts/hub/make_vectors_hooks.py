"""Builds ``contracts/hub/vectors_hooks.json``: the public webhook inbox (protocol.md section 10.11, orch-core#1285).

Test code, deterministic, FAKE secrets only. The hub replays every case against ``orch_hub.contract.hooks``
(``packages/hub/tests/test_hooks_vectors.py``); orch-core replays every case against its own hand-written port
(``packages/core/tests/hub/test_hooks_vectors.py``). Both fail on an unknown case kind.

``uv run python ../../contracts/hub/make_vectors_hooks.py`` (from packages/hub) rewrites the file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from orch_hub.contract import hooks as HK


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def seed(label: str) -> bytes:
    return hashlib.sha256(b"FAKE orch v2 test vector: hooks/" + label.encode()).digest()


TOKEN = "hh_" + b64u(seed("token"))
SECRET = b64u(seed("secret"))
OTHER = b64u(seed("other-secret"))
TH = hashlib.sha256(TOKEN.encode()).hexdigest()
TH2 = hashlib.sha256(("hh_" + b64u(seed("token2"))).encode()).hexdigest()
NOW = 1_800_000_000
BODY = b'{"action":"completed","check_suite":{"conclusion":"success"}}'


def put(**kw: Any) -> dict[str, Any]:
    b = {"token_hash": TH, "followup": "FU-12", "source": "github", "secret": SECRET, "expires_ms": 1_800_086_400_000}
    b.update(kw)
    return {k: v for k, v in b.items() if v is not None}


def ring(**kw: Any) -> dict[str, Any]:
    r = {"seq": 1, "token_hash": TH, "source": "github", "kind": "hook.rang", "received_at": 1_800_000_000_000}
    r.update(kw)
    return r


def main() -> None:
    cases: list[dict[str, Any]] = []

    def case(name: str, kind: str, **kw: Any) -> None:
        cases.append({"name": name, "check": kind, **kw})

    # token and hash
    case("token_hash", "token_hash", token=TOKEN, expect=TH)
    for name, tok in (
        ("token_short", TOKEN[:-1]),
        ("token_long", TOKEN + "A"),
        ("token_no_prefix", TOKEN[3:]),
        ("token_old_hk", "hk_" + "0" * 26),
        ("token_pad", TOKEN[:-1] + "="),
        ("token_space", TOKEN[:-1] + " "),
    ):
        case(name, "token_hash", token=tok, expect=None)
    # request bodies
    for name, op, body, ok in (
        ("put_ok", "hook_put", put(), True),
        ("put_replaces_ok", "hook_put", put(replaces=TH2), True),
        ("put_replaces_self", "hook_put", put(replaces=TH), False),
        ("put_source_databricks", "hook_put", put(source="databricks"), True),
        ("put_source_other", "hook_put", put(source="gitlab"), False),
        ("put_hash_upper", "hook_put", put(token_hash=TH.upper()), False),
        ("put_hash_short", "hook_put", put(token_hash=TH[:-1]), False),
        ("put_secret_short", "hook_put", put(secret=SECRET[:-1]), False),
        ("put_followup_slash", "hook_put", put(followup="FU/12"), False),
        ("put_followup_empty", "hook_put", put(followup=""), False),
        ("put_followup_65", "hook_put", put(followup="A" * 65), False),
        ("put_followup_64", "hook_put", put(followup="A" * 64), True),
        ("put_expires_zero", "hook_put", put(expires_ms=0), False),
        ("put_expires_bool", "hook_put", put(expires_ms=True), False),
        ("put_extra_key", "hook_put", {**put(), "body": "x"}, False),
        ("put_missing_secret", "hook_put", put(secret=None), False),
        ("revoke_ok", "hook_revoke", {"token_hash": TH}, True),
        ("revoke_extra", "hook_revoke", {"token_hash": TH, "x": 1}, False),
        ("pull_ok", "hook_pull", {"since": 0}, True),
        ("pull_negative", "hook_pull", {"since": -1}, False),
        ("ack_ok", "hook_ack", {"seq": 3}, True),
        ("ack_zero", "hook_ack", {"seq": 0}, False),
    ):
        case(name, "request", op=op, body=body, expect=ok)
    # replies
    for name, op, body, ok in (
        ("put_reply_ok", "hook_put", {"url_base": "https://hub.example.com/h/"}, True),
        ("put_reply_local_ok", "hook_put", {"url_base": "http://127.0.0.1:8790/h/"}, True),
        ("put_reply_no_slash", "hook_put", {"url_base": "https://hub.example.com/h"}, False),
        ("put_reply_other_path", "hook_put", {"url_base": "https://hub.example.com/x/h/"}, False),
        ("put_reply_empty", "hook_put", {}, False),
        ("pull_reply_ok", "hook_pull", {"rings": [ring(seq=1), ring(seq=2, source="databricks")], "head": 2}, True),
        ("pull_reply_empty", "hook_pull", {"rings": [], "head": 0}, True),
        ("pull_reply_kind", "hook_pull", {"rings": [ring(kind="hook.failed")], "head": 1}, False),
        ("pull_reply_source", "hook_pull", {"rings": [ring(source="slack")], "head": 1}, False),
        ("pull_reply_text", "hook_pull", {"rings": [{**ring(), "followup": "FU-12"}], "head": 1}, False),
        ("pull_reply_unsorted", "hook_pull", {"rings": [ring(seq=2), ring(seq=1)], "head": 2}, False),
        ("pull_reply_dup_seq", "hook_pull", {"rings": [ring(seq=1), ring(seq=1)], "head": 1}, False),
        ("pull_reply_51", "hook_pull", {"rings": [ring(seq=i) for i in range(1, 52)], "head": 51}, False),
        ("ack_reply_ok", "hook_ack", {"acked": 3}, True),
        ("ack_reply_extra", "hook_ack", {"acked": 3, "x": 1}, False),
    ):
        case(name, "reply", op=op, body=body, expect=ok)
    # signatures
    gh = "sha256=" + hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    case("github_ok", "github", secret=SECRET, body_hex=BODY.hex(), header=gh, expect=True)
    case("github_wrong_secret", "github", secret=OTHER, body_hex=BODY.hex(), header=gh, expect=False)
    case("github_body_changed", "github", secret=SECRET, body_hex=(BODY + b" ").hex(), header=gh, expect=False)
    case("github_no_prefix", "github", secret=SECRET, body_hex=BODY.hex(), header=gh[7:], expect=False)
    case("github_upper", "github", secret=SECRET, body_hex=BODY.hex(), header=gh.upper(), expect=False)
    case("github_sha1", "github", secret=SECRET, body_hex=BODY.hex(), header="sha1=" + gh[7:47], expect=False)
    case("github_missing", "github", secret=SECRET, body_hex=BODY.hex(), header=None, expect=False)
    case(
        "github_empty_body_ok",
        "github",
        secret=SECRET,
        body_hex="",
        header="sha256=" + hmac.new(SECRET.encode(), b"", hashlib.sha256).hexdigest(),
        expect=True,
    )
    t = NOW - 10
    gen = f"t={t},v1=" + hmac.new(SECRET.encode(), str(t).encode() + b"." + BODY, hashlib.sha256).hexdigest()
    case("generic_ok", "generic", secret=SECRET, body_hex=BODY.hex(), header=gen, now_s=NOW, expect=True)
    case("generic_wrong_secret", "generic", secret=OTHER, body_hex=BODY.hex(), header=gen, now_s=NOW, expect=False)
    case(
        "generic_stale",
        "generic",
        secret=SECRET,
        body_hex=BODY.hex(),
        header=gen,
        now_s=NOW + 301 - 10 + 10,
        expect=False,
    )
    case("generic_edge", "generic", secret=SECRET, body_hex=BODY.hex(), header=gen, now_s=t + 300, expect=True)
    case("generic_future", "generic", secret=SECRET, body_hex=BODY.hex(), header=gen, now_s=t - 301, expect=False)
    case(
        "generic_t_replaced",
        "generic",
        secret=SECRET,
        body_hex=BODY.hex(),
        header=gen.replace(f"t={t}", f"t={t + 1}"),
        now_s=NOW,
        expect=False,
    )
    case(
        "generic_body_changed",
        "generic",
        secret=SECRET,
        body_hex=(BODY + b" ").hex(),
        header=gen,
        now_s=NOW,
        expect=False,
    )
    case("generic_garbage", "generic", secret=SECRET, body_hex=BODY.hex(), header="t=1", now_s=NOW, expect=False)
    ok64 = base64.b64encode(f"orch:{SECRET}".encode()).decode()
    for name, hdr, ok in (
        ("basic_ok", "Basic " + ok64, True),
        ("basic_wrong_user", "Basic " + base64.b64encode(f"admin:{SECRET}".encode()).decode(), False),
        ("basic_wrong_pass", "Basic " + base64.b64encode(f"orch:{OTHER}".encode()).decode(), False),
        ("basic_both_wrong", "Basic " + base64.b64encode(b"x:y").decode(), False),
        ("basic_no_colon", "Basic " + base64.b64encode(b"orch").decode(), False),
        ("basic_not_base64", "Basic !!!", False),
        ("basic_bearer", "Bearer " + ok64, False),
        ("basic_lower_scheme", "basic " + ok64, False),
        ("basic_missing", None, False),
    ):
        case(name, "basic", secret=SECRET, header=hdr, expect=ok)
    doc = {
        "about": "FAKE secrets, test-only. The public webhook inbox (protocol.md 10.11, orch-core#1285).",
        "constants": {
            "sources": list(HK.SOURCES),
            "kinds": list(HK.KINDS),
            "basic_user": HK.BASIC_USER,
            "generic_window_s": HK.GENERIC_WINDOW_S,
            "body_cap": HK.BODY_CAP,
            "max_hooks": HK.MAX_HOOKS_PER_WORKSPACE,
            "max_expiry_ms": HK.MAX_EXPIRY_MS,
            "pull_max": HK.PULL_MAX,
        },
        "cases": cases,
    }
    out = Path(__file__).with_name("vectors_hooks.json")
    out.write_text(json.dumps(doc, indent=1) + "\n")
    print(f"{out.name}: {len(cases)} cases")


if __name__ == "__main__":
    main()
