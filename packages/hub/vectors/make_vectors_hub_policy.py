"""Builds ``vectors/vectors_hub_policy.json``: the ``orch.hub-policy/1`` contract (D14, orch-core#927, design-d14.md).

Test code. Holds the reference validator (``check_policy``) and the reference evaluation (``evaluate``) that the hub
and orch-core (G7, #928) must agree with case by case. Run ``uv run python vectors/make_vectors_hub_policy.py`` to
rewrite the file; the output is deterministic (FAKE keys from fixed seeds, no signatures)."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from orch_hub.contract.canon import cj

OFFICIAL_URL = "https://github.com/severinlindenmann/orch-marketplace"  # orch-core orch.market.constants
CAPABILITIES = (
    "choose_model",
    "credential",
    "git_push",
    "network",
    "pty",
    "serve_http",
    "spawn_agent",
)  # core addons.manifest
CASE_INSENSITIVE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")
MAX_MARKETS, MAX_KEYS, MAX_IDS = 16, 2, 256  # these bound cj(policy) below 48 KiB (case policy_max_size)
HASH_LABEL = b"orch/v2/hub-market-policy|"
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

GIT_URL = re.compile(
    r"(?!.*\.git\Z)https://(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){1,5}[a-z]{2,24}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._~-]{0,99}){1,6}\Z"
)
ID = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
SEMVER = r"(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})"
BLOCK_ITEM = re.compile(r"[a-z][a-z0-9-]{0,39}(?:@" + SEMVER + r")?\Z")
B64U_KEY = re.compile(r"[A-Za-z0-9_-]{86}[AEIMQUYcgkosw048]\Z")


class Malformed(Exception):
    pass


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fake_key(name: str) -> str:
    seed = hashlib.sha256(b"FAKE orch v2 test vector: hub/" + name.encode()).digest()
    k = ec.derive_private_key(int.from_bytes(seed, "big") % (N - 1) + 1, ec.SECP256R1())
    return b64u(k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))


def canonical_url(u: Any) -> bool:
    if not isinstance(u, str) or not GIT_URL.match(u):
        return False
    host = u.split("/")[2]
    return not (host in CASE_INSENSITIVE_HOSTS and u != u.lower())


def valid_key(k: Any) -> bool:
    if not isinstance(k, str) or not B64U_KEY.match(k):
        return False
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), base64.urlsafe_b64decode(k + "="))
    except ValueError:
        return False
    return True


def _sorted_unique(xs: Any, lo: int, hi: int, ok: Any) -> list:
    if not isinstance(xs, list) or not lo <= len(xs) <= hi or not all(ok(x) for x in xs):
        raise Malformed
    if xs != sorted(set(xs)):
        raise Malformed
    return xs


def check_policy(p: Any) -> dict:
    """design-d14 §2. Returns the policy or raises Malformed (the whole object; no partial reading)."""
    if not isinstance(p, dict) or set(p) != {"markets", "addons", "ceiling"}:
        raise Malformed
    ms = p["markets"]
    if not isinstance(ms, list) or len(ms) > MAX_MARKETS:
        raise Malformed
    for m in ms:
        if not isinstance(m, dict) or set(m) != {"url", "class", "keys"} or not canonical_url(m["url"]):
            raise Malformed
        if m["class"] == "official":
            if m["url"] != OFFICIAL_URL or m["keys"] != []:
                raise Malformed
        elif m["class"] == "company":
            if m["url"] == OFFICIAL_URL:
                raise Malformed
            _sorted_unique(m["keys"], 1, MAX_KEYS, valid_key)
        else:
            raise Malformed
    urls = [m["url"] for m in ms]
    if urls != sorted(set(urls)):
        raise Malformed
    a = p["addons"]
    if not isinstance(a, dict) or set(a) != {"allow", "block"}:
        raise Malformed
    if a["allow"] is not None:
        _sorted_unique(a["allow"], 0, MAX_IDS, lambda x: isinstance(x, str) and bool(ID.match(x)))
    _sorted_unique(a["block"], 0, MAX_IDS, lambda x: isinstance(x, str) and bool(BLOCK_ITEM.match(x)))
    if p["ceiling"] is not None:
        _sorted_unique(p["ceiling"], 0, len(CAPABILITIES), lambda x: x in CAPABILITIES)
    return p


def policy_hash(p: dict) -> str:
    return b64u(hashlib.sha256(HASH_LABEL + cj(p)).digest())


class Refuse(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def evaluate(policy: Any, req: dict) -> None:
    """design-d14 §4, the hub-policy parts only, in order. ``policy`` None = unreadable (link down, no snapshot).
    ``req.stage``: install (P0, steps 2, 4, 6, 7, 8), grant (6, 8 on the grant event), run (block + ceiling)."""
    stage = req["stage"]
    if stage == "run":
        try:
            p = check_policy(policy)
        except Malformed:
            raise Refuse("market.not_allowed") from None
        # market_url "" = a built-in (no origin); a marketplace add-on's origin must still be listed
        if req["market_url"] and all(m["url"] != req["market_url"] for m in p["markets"]):
            raise Refuse("market.not_allowed")
        _block(p, req)
        _ceiling(p, req)
        return
    try:
        p = check_policy(policy)
    except Malformed:
        raise Refuse("market.not_allowed") from None
    m = next((m for m in p["markets"] if m["url"] == req["market_url"]), None)
    if m is None:  # P0
        raise Refuse("market.not_allowed")
    company = m["class"] == "company"
    if stage == "install" and company and req["index_key"] not in m["keys"]:  # step 2
        raise Refuse("market.index_unsigned")
    # step 4: an allowed official or hub-pinned company market passes without isolation (V3-D32)
    _block(p, req)  # step 6
    allow = p["addons"]["allow"]
    if allow is not None and req["id"] not in allow:
        raise Refuse("market.addon_not_allowed")
    if stage == "install" and company and req["publisher_key"] not in m["keys"]:  # step 7 (then the signature)
        raise Refuse("market.publisher_unpinned")
    _ceiling(p, req)  # step 8 / grant event


def _block(p: dict, req: dict) -> None:
    b = p["addons"]["block"]
    if req["id"] in b or f"{req['id']}@{req['version']}" in b:
        raise Refuse("market.blocked")


def _ceiling(p: dict, req: dict) -> None:
    c = p["ceiling"]
    if c is not None and not set(req["capabilities"]) <= set(c):
        raise Refuse("market.capability_ceiling")


# --- cases ------------------------------------------------------------------------------------------------------------

K1, K2, K3 = fake_key("policy-company-1"), fake_key("policy-company-2"), fake_key("policy-company-3")
KX = fake_key("policy-attacker")
COMPANY = "https://git.acme.example/tools/orch-market"
OTHER = "https://github.com/someone/market"
OFF = {"url": OFFICIAL_URL, "class": "official", "keys": []}


def comp(keys: list[str] | None = None, url: str = COMPANY) -> dict:
    return {"url": url, "class": "company", "keys": sorted(keys or [K1])}


def pol(markets: list | None = None, allow: Any = None, block: list | None = None, ceiling: Any = None) -> dict:
    ms = [OFF] if markets is None else markets
    return {"markets": ms, "addons": {"allow": allow, "block": block or []}, "ceiling": ceiling}


DEFAULT = pol()


def policy_cases() -> list[tuple[str, Any]]:
    big = pol(allow=[f"a{i:03d}" for i in range(256)])
    over = copy.deepcopy(big)
    over["addons"]["allow"] = [f"a{i:03d}" for i in range(257)]
    many = pol(
        markets=sorted([comp(url=f"https://git.acme.example/m{i:02d}") for i in range(17)], key=lambda m: m["url"])
    )
    sixteen = pol(
        markets=sorted([comp(url=f"https://git.acme.example/m{i:02d}") for i in range(16)], key=lambda m: m["url"])
    )
    huge = pol(
        markets=sorted(
            [comp(sorted([K1, K2]), url=f"https://git.acme.example/{'t' * 90}/m{i:02d}") for i in range(16)],
            key=lambda m: m["url"],
        ),
        allow=sorted(f"a{i:03d}{'x' * 36}" for i in range(256)),
        block=sorted(f"b{i:03d}{'x' * 36}@1000.1000.1000" for i in range(256)),
        ceiling=list(CAPABILITIES),
    )
    assert len(cj(huge)) < 48 * 1024
    two = sorted([K1, K2])
    unsorted_m = pol(markets=[comp(), OFF] if COMPANY > OFFICIAL_URL else [OFF, comp()])
    if unsorted_m["markets"] == sorted(unsorted_m["markets"], key=lambda m: m["url"]):
        unsorted_m["markets"].reverse()
    bad_point = K1[:-2] + ("A" if K1[-2] != "A" else "B") + K1[-1]
    out: list[tuple[str, Any]] = [
        ("policy_default", DEFAULT),
        ("policy_no_markets", pol(markets=[])),
        ("policy_company_one_key", pol(markets=sorted([OFF, comp()], key=lambda m: m["url"]))),
        ("policy_company_two_keys", pol(markets=[comp(two)])),
        ("policy_ceiling_empty", pol(ceiling=[])),
        ("policy_ceiling_all", pol(ceiling=list(CAPABILITIES))),
        ("policy_allow_empty", pol(allow=[])),
        ("policy_allow_list", pol(allow=["github", "landing"])),
        ("policy_block_id_and_version", pol(block=["foo", "landing@1.2.0"])),
        ("policy_16_markets", sixteen),
        ("policy_256_allow", big),
        ("policy_null", None),
        ("policy_not_object", []),
        ("policy_unknown_top_key", {**DEFAULT, "extra": 1}),
        ("policy_missing_ceiling", {k: v for k, v in DEFAULT.items() if k != "ceiling"}),
        ("policy_old_spec_shape", {"allow": [OFFICIAL_URL], "block": [], "ceiling": None}),
        ("policy_market_unknown_key", pol(markets=[{**OFF, "name": "official"}])),
        ("policy_market_missing_keys", pol(markets=[{"url": OFFICIAL_URL, "class": "official"}])),
        ("policy_addons_unknown_key", {**DEFAULT, "addons": {"allow": None, "block": [], "pin": []}}),
        ("policy_addons_missing_block", {**DEFAULT, "addons": {"allow": None}}),
        ("policy_markets_unsorted", unsorted_m),
        ("policy_markets_duplicate_url", pol(markets=[comp(), comp([K2])])),
        ("policy_url_dot_git", pol(markets=[comp(url=COMPANY + ".git")])),
        ("policy_url_trailing_slash", pol(markets=[comp(url=COMPANY + "/")])),
        ("policy_url_http", pol(markets=[comp(url=COMPANY.replace("https", "http"))])),
        ("policy_url_port", pol(markets=[comp(url="https://git.acme.example:8443/tools/m")])),
        ("policy_url_upper_github_path", pol(markets=[comp(url="https://github.com/ACME/market")])),
        ("policy_url_upper_host", pol(markets=[comp(url="https://Git.acme.example/tools/m")])),
        ("policy_url_ip_literal", pol(markets=[comp(url="https://10.0.0.1/tools/m")])),
        ("policy_url_query", pol(markets=[comp(url=COMPANY + "?x=1")])),
        ("policy_official_with_keys", pol(markets=[{**OFF, "keys": [K1]}])),
        ("policy_official_url_as_company", pol(markets=[comp(url=OFFICIAL_URL)])),
        ("policy_other_url_as_official", pol(markets=[{"url": OTHER, "class": "official", "keys": []}])),
        ("policy_unknown_class", pol(markets=[{"url": OTHER, "class": "community", "keys": [K1]}])),
        ("policy_company_no_keys", pol(markets=[{"url": COMPANY, "class": "company", "keys": []}])),
        (
            "policy_company_three_keys",
            pol(markets=[{"url": COMPANY, "class": "company", "keys": sorted([K1, K2, K3])}]),
        ),
        (
            "policy_company_keys_unsorted",
            pol(markets=[{"url": COMPANY, "class": "company", "keys": sorted(two, reverse=True)}]),
        ),
        ("policy_company_keys_duplicate", pol(markets=[{"url": COMPANY, "class": "company", "keys": [K1, K1]}])),
        ("policy_key_padded", pol(markets=[{"url": COMPANY, "class": "company", "keys": [K1 + "="]}])),
        ("policy_key_short", pol(markets=[{"url": COMPANY, "class": "company", "keys": [K1[:-4]]}])),
        ("policy_key_not_on_curve", pol(markets=[{"url": COMPANY, "class": "company", "keys": [bad_point]}])),
        ("policy_key_std_alphabet", pol(markets=[{"url": COMPANY, "class": "company", "keys": ["+" + K1[1:]]}])),
        ("policy_unknown_capability", pol(ceiling=["network", "root"])),
        ("policy_ceiling_unsorted", pol(ceiling=["pty", "network"])),
        ("policy_ceiling_duplicate", pol(ceiling=["network", "network"])),
        ("policy_ceiling_string", pol(ceiling="network")),
        ("policy_allow_unsorted", pol(allow=["landing", "github"])),
        ("policy_allow_bad_id", pol(allow=["Github"])),
        ("policy_allow_with_version", pol(allow=["github@1.0.0"])),
        ("policy_block_bad_semver", pol(block=["foo@1.0"])),
        ("policy_block_prerelease", pol(block=["foo@1.0.0-rc1"])),
        ("policy_block_unsorted", pol(block=["zed", "foo"])),
        ("policy_block_null", {**DEFAULT, "addons": {"allow": None, "block": None}}),
        ("policy_17_markets", many),
        ("policy_257_allow", over),
        ("policy_max_size", huge),
    ]
    return out


def eval_cases() -> list[tuple[str, Any, dict]]:
    def r(
        stage: str = "install",
        url: str = OFFICIAL_URL,
        id: str = "github",
        version: str = "1.2.0",
        caps: list | None = None,
        index_key: str | None = None,
        publisher_key: str | None = None,
    ) -> dict:
        return {
            "stage": stage,
            "market_url": url,
            "id": id,
            "version": version,
            "capabilities": caps or [],
            "index_key": index_key,
            "publisher_key": publisher_key,
        }

    cpol = pol(markets=sorted([OFF, comp(sorted([K1, K2]))], key=lambda m: m["url"]))
    ok_c = {"index_key": K1, "publisher_key": K2}
    return [
        ("eval_default_official_ok", DEFAULT, r(caps=["network"])),
        ("eval_policy_unreadable", None, r()),
        ("eval_policy_malformed", {**DEFAULT, "extra": 1}, r()),
        ("eval_official_not_listed", pol(markets=[]), r()),
        ("eval_company_not_listed", DEFAULT, r(url=COMPANY, **ok_c)),
        ("eval_other_url_never_allowed", cpol, r(url=OTHER, **ok_c)),
        ("eval_company_ok", cpol, r(url=COMPANY, id="acme-tool", **ok_c)),
        ("eval_company_index_unsigned", cpol, r(url=COMPANY, index_key=None, publisher_key=K1)),
        ("eval_company_index_other_key", cpol, r(url=COMPANY, index_key=KX, publisher_key=K1)),
        ("eval_company_publisher_unsigned", cpol, r(url=COMPANY, index_key=K1, publisher_key=None)),
        ("eval_company_publisher_other_key", cpol, r(url=COMPANY, index_key=K1, publisher_key=KX)),
        ("eval_blocked_id", pol(block=["github"]), r()),
        ("eval_blocked_id_version", pol(block=["github@1.2.0"]), r()),
        ("eval_block_other_version_ok", pol(block=["github@1.1.0"]), r()),
        ("eval_block_prefix_not_match", pol(block=["git"]), r()),
        ("eval_allow_listed_ok", pol(allow=["github"]), r()),
        ("eval_allow_not_listed", pol(allow=["landing"]), r()),
        ("eval_allow_empty", pol(allow=[]), r()),
        ("eval_ceiling_ok", pol(ceiling=["network", "pty"]), r(caps=["network"])),
        ("eval_ceiling_exceeded", pol(ceiling=["pty"]), r(caps=["network", "pty"])),
        ("eval_ceiling_empty_no_caps_ok", pol(ceiling=[]), r(caps=[])),
        ("eval_ceiling_empty_refuses_any", pol(ceiling=[]), r(caps=["pty"])),
        ("eval_order_blocked_before_ceiling", pol(block=["github"], ceiling=[]), r(caps=["network"])),
        ("eval_order_not_allowed_before_blocked", pol(markets=[], block=["github"]), r()),
        (
            "eval_order_unpinned_before_ceiling",
            {**cpol, "ceiling": []},
            r(url=COMPANY, index_key=K1, publisher_key=KX, caps=["network"]),
        ),
        (
            "eval_order_allow_before_unpinned",
            {**cpol, "addons": {"allow": ["x"], "block": []}},
            r(url=COMPANY, index_key=K1, publisher_key=KX),
        ),
        ("eval_grant_ceiling_rechecked", pol(ceiling=["pty"]), r(stage="grant", caps=["network"])),
        ("eval_grant_blocked_rechecked", pol(block=["github"]), r(stage="grant")),
        ("eval_run_blocked_builtin", pol(block=["terminals"]), r(stage="run", url="", id="terminals", version="0.3.0")),
        ("eval_run_over_ceiling", pol(ceiling=[]), r(stage="run", url="", id="terminals", caps=["pty"])),
        ("eval_run_unblocked_ok", DEFAULT, r(stage="run", url="", id="terminals", caps=["pty"])),
        ("eval_run_policy_unreadable", None, r(stage="run", url="", id="terminals")),
        ("eval_run_company_market_removed", DEFAULT, r(stage="run", url=COMPANY, id="acme-tool")),
        ("eval_run_company_market_listed_ok", cpol, r(stage="run", url=COMPANY, id="acme-tool")),
        ("eval_run_official_market_removed", pol(markets=[]), r(stage="run", id="github")),
    ]


def build() -> dict:
    cases: list[dict] = []
    for name, p in policy_cases():
        try:
            check_policy(p)
            exp: dict = {"ok": True, "policy_hash": policy_hash(p)}
        except Malformed:
            exp = {"error": "malformed"}
        cases.append({"name": name, "check": "policy", "args": {"policy": p}, "expect": exp})
    for name, p, req in eval_cases():
        try:
            evaluate(p, req)
            exp = {"ok": True}
        except Refuse as e:
            exp = {"error": e.code}
        cases.append({"name": name, "check": "evaluate", "args": {"policy": p, "req": req}, "expect": exp})
    return {
        "format": "orch.hub-policy/1 vectors",
        "comment": "D14 orch-core#927, design-d14.md. FAKE keys (seed = SHA-256('FAKE orch v2 test vector: hub/' "
        "|| name)). check=policy: the validator (ok + policy_hash, or malformed). check=evaluate: the hub-policy "
        "steps of the install flow; policy null = unreadable; index_key/publisher_key = the key whose signature "
        "verified (null = none); signature checks themselves are the marketplace vectors' job.",
        "official_url": OFFICIAL_URL,
        "capabilities": list(CAPABILITIES),
        "hash_label": HASH_LABEL.decode(),
        "limits": {"markets": MAX_MARKETS, "keys": MAX_KEYS, "ids": MAX_IDS},
        "keys": {"company_1": K1, "company_2": K2, "company_3": K3, "attacker": KX},
        "default_policy": DEFAULT,
        "cases": cases,
    }


if __name__ == "__main__":
    out = Path(__file__).with_name("vectors_hub_policy.json")
    doc = build()
    out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"{len(doc['cases'])} cases -> {out}", file=sys.stderr)
