"""Reference checker for ``orch.market/1`` (docs/architecture/orch-v2-marketplace.md), test code only.

It is the smallest thing that can answer every vector in ``vectors_market.json``: the schema, the rules of section 5 in
their order, and the two lockfile rules. It is not the installer. G2/G3 build that and must give the same answers.
"""

from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from jsonschema import Draft202012Validator

from orch.addons.manifest import _core_key
from orch.addons.package import package_digest
from orch.canon import dumps
from orch.crypto.encoding import EncodingError, unb64u

SCHEMA_PATH = Path(__file__).parents[2] / "src/orch/addons/schemas/market.v1.json"
ENTRY_LABEL = b"orch/market/1/entry|"


class Refuse(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@lru_cache(maxsize=1)
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


# ------------------------------------------------------------------------------------------------ signatures


def entry_message(entry: dict[str, Any], market: str) -> bytes:
    """What a publisher signs: the label and ``cj`` of the entry (``publisher`` reduced to ``{key}``) together with the
    URL of the marketplace it is published in, so a signed entry cannot be replayed into another marketplace."""
    e = copy.deepcopy(entry)
    e["publisher"] = {"key": entry["publisher"]["key"]}
    return ENTRY_LABEL + dumps({"entry": e, "market": market})


def sign_entry(entry: dict[str, Any], private_key: ec.EllipticCurvePrivateKey, market: str) -> str:
    from orch.crypto.encoding import b64u

    der = private_key.sign(entry_message(entry, market), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def verify_entry(entry: dict[str, Any], market: str) -> bool:
    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(entry["publisher"]["key"], 65))
        raw = unb64u(entry["publisher"]["sig"], 64)
        r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        n = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
        if not (1 <= r < n and 1 <= s < n):
            return False
        key.verify(encode_dss_signature(r, s), entry_message(entry, market), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, EncodingError, ValueError):
        return False


# ------------------------------------------------------------------------------------------------ the index


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def parse_index(index: Any) -> dict[str, Any]:
    """An index given as text (strict parse: duplicate keys refused) or as an already parsed object."""
    if isinstance(index, str):
        try:
            index = json.loads(index, object_pairs_hook=_strict_pairs, parse_float=_refuse, parse_constant=_refuse)
        except ValueError:
            raise Refuse("market.malformed") from None
    return index


def _refuse(_: str) -> Any:
    raise ValueError("float or constant")


def check_index(index: Any) -> dict[str, Any]:
    """Section 3: parse, the pin and digest pre-checks, the schema, the semantic rules. Returns the parsed index."""
    doc = parse_index(index)
    if not isinstance(doc, dict) or not isinstance(doc.get("addons"), list):
        raise Refuse("market.malformed")
    for e in doc["addons"]:
        if isinstance(e, dict):
            if not isinstance(e.get("commit"), str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", e["commit"]):
                raise Refuse("market.unpinned")
            if not isinstance(e.get("package_sha256"), str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", e["package_sha256"]
            ):
                raise Refuse("market.bad_digest_format")
    if next(Draft202012Validator(schema()).iter_errors(doc), None) is not None:
        raise Refuse("market.malformed")
    ids = [e["id"] for e in doc["addons"]]
    if len(set(ids)) != len(ids):
        raise Refuse("market.duplicate_id")
    for e in doc["addons"]:
        if e["capabilities"] != sorted(e["capabilities"]):
            raise Refuse("market.malformed")
    return doc


# ------------------------------------------------------------------------------------------------ lockfiles

_PY_LINE = re.compile(r"(?P<req>\S.*?)(?P<hashes>(?: +--hash=sha256:[0-9a-f]{64})+)")
NODE_HOSTS = ("registry.npmjs.org",)  # the registry hosts a node lockfile may name (per ecosystem, pinned in core)


def _py_requirement_ok(req: str) -> bool:
    """A real PEP 508 parse: one name, one exact ``==`` pin, an optional environment marker, nothing else. Options
    (``--index-url``…) cannot hide in a marker because the whole text must parse as a requirement."""
    from packaging.requirements import InvalidRequirement, Requirement

    try:
        r = Requirement(req)
    except InvalidRequirement:
        return False
    specs = list(r.specifier)
    return r.url is None and len(specs) == 1 and specs[0].operator == "==" and "*" not in specs[0].version


def _node_entry_ok(p: Any) -> bool:
    if not isinstance(p, dict) or p.get("link"):
        return False
    resolved, integrity = p.get("resolved"), p.get("integrity")
    if not isinstance(resolved, str) or not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        return False
    m = re.fullmatch(r"https://([a-z0-9.-]+)/[A-Za-z0-9@._~/-]+", resolved)
    return m is not None and m[1] in NODE_HOSTS


def check_lock(kind: str, text: str) -> None:
    """Section 8: every entry pinned and hashed, nothing that resolves or fetches by itself. Raises ``lock_invalid``.
    Install scripts are not judged here: lock metadata is the publisher's claim. The installer turns scripts off."""
    if kind == "python-requirements":
        logical = re.sub(r" *\\\n *", " ", text)
        lines = [ln.strip() for ln in logical.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        ok = bool(lines)
        for ln in lines:
            m = _PY_LINE.fullmatch(ln)
            ok = ok and m is not None and _py_requirement_ok(m["req"])
        if not ok:
            raise Refuse("market.lock_invalid")
    elif kind == "node-package-lock":
        try:
            lock = json.loads(text, object_pairs_hook=_strict_pairs)
            pkgs = lock["packages"]
            root = pkgs[""]
            ok = lock["lockfileVersion"] == 3 and isinstance(pkgs, dict) and len(pkgs) > 1 and isinstance(root, dict)
            # the root entry is the add-on itself: it is not fetched, so it must not claim a source or a link,
            # and everything it depends on must be a locked entry
            ok = ok and not ({"resolved", "integrity", "link", "inBundle"} & set(root))
            for field in ("dependencies", "optionalDependencies", "peerDependencies"):
                for dep in root.get(field, {}):
                    ok = ok and f"node_modules/{dep}" in pkgs
            for key, p in pkgs.items():
                if key != "":
                    ok = ok and key.startswith("node_modules/") and _node_entry_ok(p)
        except (ValueError, KeyError, TypeError, AttributeError):
            ok = False
        if not ok:
            raise Refuse("market.lock_invalid")
    else:  # the schema only lets the two kinds through
        raise Refuse("market.malformed")


# ------------------------------------------------------------------------------------------------ network targets


def check_address(ip: str) -> None:
    """Section 5.1: the address a connection is about to use, after DNS resolution (and again after any redirect)."""
    import ipaddress

    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        raise Refuse("market.address_refused") from None
    if getattr(a, "ipv4_mapped", None) is not None:
        a = a.ipv4_mapped
    if not a.is_global or a.is_multicast:
        raise Refuse("market.address_refused")


def _semver(v: str) -> tuple[int, int, int]:
    a, b, c = v.split(".")
    return int(a), int(b), int(c)


# ------------------------------------------------------------------------------------------------ install


def install(a: dict[str, Any]) -> dict[str, Any]:
    """The checks of section 5 in order. ``a`` = ``{index, id, files, ctx}``. Returns the facts the review shows."""
    ctx = a["ctx"]
    if ctx["actor"] != "human":  # step 0, before anything is read
        raise Refuse("market.actor_refused")
    doc = check_index(a["index"])
    repo = ctx["market_repo"]
    if doc["source"]["git"] != repo:
        raise Refuse("market.source_mismatch")
    if ctx["last_version"] is not None and doc["version"] < ctx["last_version"]:
        raise Refuse("market.rollback")
    official = repo == ctx["official"]["repo"]
    if not official and not ctx["isolation"]:
        raise Refuse("market.community_unisolated")
    hub = ctx["hub"]
    if hub is not None:
        if repo in hub["block"]:
            raise Refuse("market.blocked")
        if repo not in hub["allow"]:
            raise Refuse("market.not_allowed")
    entry = next((e for e in doc["addons"] if e["id"] == a["id"]), None)
    if entry is None:
        raise Refuse("market.unknown_addon")
    host = lambda u: u.split("/")[2]  # noqa: E731  (schema guarantees https://host/…)
    if host(entry["source"]["git"]) != host(repo) and host(entry["source"]["git"]) not in ctx["git_hosts"]:
        raise Refuse("market.host_not_allowed")
    if not official and entry["id"] in ctx["builtin"]:
        raise Refuse("market.reserved_id")
    prior = ctx["installed"].get(entry["id"])
    if prior is not None and prior["origin"] != repo:
        raise Refuse("market.origin_changed")
    floor = [v for v in (prior and prior["version"], ctx["seen"].get(f"{repo}|{entry['id']}")) if v]
    if any(_semver(entry["version"]) < _semver(v) for v in floor):
        raise Refuse("market.rollback")
    pub = entry.get("publisher")
    if official and (pub is None or pub["key"] != ctx["official"]["key"]):
        raise Refuse("market.official_unsigned")
    if pub is not None and not verify_entry(entry, repo):
        raise Refuse("market.bad_signature")
    known = ctx["known_keys"].get(f"{repo}|{entry['id']}")
    if known is not None and (pub is None or pub["key"] != known):
        raise Refuse("market.publisher_key_changed")
    tofu = "unsigned" if pub is None else ("known" if known is not None else "new")
    ceiling = None if hub is None else hub["ceiling"]
    if ceiling is not None and not set(entry["capabilities"]) <= set(ceiling):
        raise Refuse("market.capability_ceiling")
    need, have = _core_key(entry["min_orch"]), _core_key(ctx["core_version"])
    if need is None or have is None or have < need:
        raise Refuse("market.min_orch")
    files = {p: t.encode("utf-8") for p, t in a["files"].items()}
    if "orch-addon.json" not in files:
        raise Refuse("market.package_invalid")
    if package_digest(files) != entry["package_sha256"]:
        raise Refuse("market.digest_mismatch")
    try:
        m = json.loads(files["orch-addon.json"], object_pairs_hook=_strict_pairs)
        same = (
            m["schema"] == "orch.addon/3"
            and m["name"] == entry["id"]
            and m["version"] == entry["version"]
            and sorted(m.get("capabilities", [])) == entry["capabilities"]
            and m.get("min_core") == entry["min_orch"]
        )
    except (ValueError, KeyError, TypeError):
        same = False
    if not same:
        raise Refuse("market.manifest_mismatch")
    rt = entry.get("runtime")
    if rt is not None:
        if rt["lock"] not in a["files"]:
            raise Refuse("market.lock_missing")
        check_lock(rt["kind"], a["files"][rt["lock"]])
    return {
        "id": entry["id"],
        "version": entry["version"],
        "package_sha256": entry["package_sha256"],
        "class": "official" if official else "other",
        "tofu": tofu,
    }


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    """The vector's answer: ``{"ok": …}`` or ``{"refuse": code}``."""
    try:
        if case["check"] == "index":
            doc = check_index(case["args"]["index"])
            return {"ok": {"addons": [e["id"] for e in doc["addons"]]}}
        if case["check"] == "install":
            return {"ok": install(case["args"])}
        if case["check"] == "address":
            check_address(case["args"]["ip"])
            return {"ok": {}}
        if case["check"] == "lock":
            check_lock(case["args"]["kind"], case["args"]["text"])
            return {"ok": {}}
    except Refuse as r:
        return {"refuse": r.code}
    raise AssertionError(f"unknown check {case['check']!r}")
