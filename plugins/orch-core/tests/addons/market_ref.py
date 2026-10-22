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


def entry_message(entry: dict[str, Any]) -> bytes:
    """What a publisher signs: the label and ``cj`` of the entry with ``publisher`` reduced to ``{key}``."""
    e = copy.deepcopy(entry)
    e["publisher"] = {"key": entry["publisher"]["key"]}
    return ENTRY_LABEL + dumps(e)


def sign_entry(entry: dict[str, Any], private_key: ec.EllipticCurvePrivateKey) -> str:
    from orch.crypto.encoding import b64u

    der = private_key.sign(entry_message(entry), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def verify_entry(entry: dict[str, Any]) -> bool:
    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(entry["publisher"]["key"], 65))
        raw = unb64u(entry["publisher"]["sig"], 64)
        r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        n = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
        if not (1 <= r < n and 1 <= s < n):
            return False
        key.verify(encode_dss_signature(r, s), entry_message(entry), ec.ECDSA(hashes.SHA256()))
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
            if not isinstance(e.get("package_sha256"), str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", e["package_sha256"]):
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

_PY_LINE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9.!+_-]+(?: *; *[^#\\]+?)?(?: +--hash=sha256:[0-9a-f]{64})+"
)


def check_lock(kind: str, text: str) -> None:
    """Section 8: every entry pinned and hashed, nothing that resolves or fetches by itself. Raises ``lock_invalid``."""
    if kind == "python-requirements":
        logical = re.sub(r" *\\\n *", " ", text)
        lines = [ln.strip() for ln in logical.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        if not lines or not all(_PY_LINE.fullmatch(ln) for ln in lines):
            raise Refuse("market.lock_invalid")
    elif kind == "node-package-lock":
        try:
            lock = json.loads(text, object_pairs_hook=_strict_pairs)
            pkgs = lock["packages"]
            ok = lock["lockfileVersion"] == 3 and isinstance(pkgs, dict) and len(pkgs) > 1
            for key, p in pkgs.items():
                if key == "":
                    continue
                ok = ok and p.get("resolved", "").startswith("https://") and p.get("integrity", "").startswith("sha512-")
                ok = ok and not p.get("link") and "hasInstallScript" not in p
        except (ValueError, KeyError, TypeError, AttributeError):
            ok = False
        if not ok:
            raise Refuse("market.lock_invalid")
    else:  # the schema only lets the two kinds through
        raise Refuse("market.malformed")


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
    if not official and entry["id"] in ctx["builtin"]:
        raise Refuse("market.reserved_id")
    prior = ctx["installed"].get(entry["id"])
    if prior is not None and prior["origin"] != repo:
        raise Refuse("market.origin_changed")
    pub = entry.get("publisher")
    if official and (pub is None or pub["key"] != ctx["official"]["key"]):
        raise Refuse("market.official_unsigned")
    if pub is not None and not verify_entry(entry):
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
        if case["check"] == "lock":
            check_lock(case["args"]["kind"], case["args"]["text"])
            return {"ok": {}}
    except Refuse as r:
        return {"refuse": r.code}
    raise AssertionError(f"unknown check {case['check']!r}")
