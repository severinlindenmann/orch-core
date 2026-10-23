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
INDEX_LABEL = b"orch/market/1/index|"


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


def index_message(doc: dict[str, Any]) -> bytes:
    """What the official key signs: the URL, the index version, the expiry, every (id, version, digest) and the
    revoked list. Nothing else in the file is covered (titles and summaries are display text)."""
    body = {
        "market": doc["source"]["git"],
        "version": doc["version"],
        "expires_at": doc.get("expires_at"),
        "entries": [[e["id"], e["version"], e["package_sha256"]] for e in sorted(doc["addons"], key=lambda e: e["id"])],
        "revoked": sorted(doc.get("revoked", [])),
    }
    return INDEX_LABEL + dumps(body)


def sign_index(doc: dict[str, Any], private_key: ec.EllipticCurvePrivateKey, key_b64: str) -> None:
    from orch.crypto.encoding import b64u

    der = private_key.sign(index_message(doc), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    doc["signature"] = {"key": key_b64, "sig": b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}


def _verify(key_b64: str, sig_b64: str, message: bytes) -> bool:
    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(key_b64, 65))
        raw = unb64u(sig_b64, 64)
        r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        n = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
        if not (1 <= r < n and 1 <= s < n):
            return False
        key.verify(encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, EncodingError, ValueError):
        return False


def verify_index(doc: dict[str, Any]) -> bool:
    sig = doc.get("signature")
    return sig is not None and _verify(sig["key"], sig["sig"], index_message(doc))


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


CASE_INSENSITIVE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")


def is_canonical_url(url: str) -> bool:
    """Section 4.1: the one form of a marketplace or repository URL that is a trust key. The schema covers the shape
    (https, lower-case ASCII host, no port, credentials, query, fragment, trailing slash or ``.git``); this adds the
    lower-case path on hosts known to ignore case, where ``ACME/m`` and ``acme/m`` are one repository."""
    host = url.split("/")[2]
    return not (host in CASE_INSENSITIVE_HOSTS and url != url.lower())


CAPABILITIES = ("git_push", "network", "pty", "serve_http", "spawn_agent")


def hub_policy_ok(hub: Any) -> bool:
    """Section 9: ``orch.hub-policy/1`` is exactly ``{allow: [url], block: [url], ceiling: [cap] | null}``, URLs in
    canonical form. Anything else counts as an empty allow list."""

    def urls(x: Any) -> bool:
        return isinstance(x, list) and all(
            isinstance(u, str) and re.fullmatch(r"https://[a-z0-9.-]+(?:/[^/\s]+)+", u) and is_canonical_url(u)
            for u in x
        )

    ceiling = hub.get("ceiling") if isinstance(hub, dict) else 0
    return (
        isinstance(hub, dict)
        and set(hub) == {"allow", "block", "ceiling"}
        and urls(hub["allow"])
        and urls(hub["block"])
        and (ceiling is None or (isinstance(ceiling, list) and all(c in CAPABILITIES for c in ceiling)))
    )


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
    if not all(is_canonical_url(u) for u in [doc["source"]["git"], *(e["source"]["git"] for e in doc["addons"])]):
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


_NPM_NAME = r"(?:@[a-z0-9~-][a-z0-9._~-]*/)?[a-z0-9~-][a-z0-9._~-]*"
_NODE_KEY = re.compile(rf"node_modules/{_NPM_NAME}(?:/node_modules/{_NPM_NAME})*")
_NODE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]{1,40})?(?:\+[0-9A-Za-z.-]{1,40})?")
_SHA512 = re.compile(r"sha512-[A-Za-z0-9+/]{86}==")


def _node_entry_ok(key: str, p: Any) -> bool:
    """A locked package: the key is a plain ``node_modules/…`` chain of package names, ``version`` is a version, the
    tarball URL is exactly the registry's URL for that name and version (so what the review shows is what installs),
    and ``integrity`` is exactly one full sha512 digest."""
    if not isinstance(p, dict) or p.get("link") or _NODE_KEY.fullmatch(key) is None:
        return False
    name = key.rsplit("node_modules/", 1)[1]
    version, resolved, integrity = p.get("version"), p.get("resolved"), p.get("integrity")
    if not isinstance(version, str) or not _NODE_VERSION.fullmatch(version):
        return False
    base = name.rsplit("/", 1)[-1]
    return (
        resolved in [f"https://{h}/{name}/-/{base}-{version}.tgz" for h in NODE_HOSTS]
        and isinstance(integrity, str)
        and _SHA512.fullmatch(integrity) is not None
    )


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
            ok = (
                type(lock["lockfileVersion"]) is int
                and lock["lockfileVersion"] == 3
                and isinstance(pkgs, dict)
                and len(pkgs) > 1
                and isinstance(root, dict)
            )
            # the root entry is the add-on itself: it is not fetched, so it must not claim a source or a link,
            # and everything it depends on must be a locked entry
            ok = ok and not ({"resolved", "integrity", "link", "inBundle"} & set(root))
            for field in ("dependencies", "optionalDependencies", "peerDependencies"):
                for dep in root.get(field, {}):
                    ok = ok and f"node_modules/{dep}" in pkgs
            for key, p in pkgs.items():
                if key != "":
                    ok = ok and _node_entry_ok(key, p)
        except (ValueError, KeyError, TypeError, AttributeError):
            ok = False
        if not ok:
            raise Refuse("market.lock_invalid")
    else:  # the schema only lets the two kinds through
        raise Refuse("market.malformed")


# ------------------------------------------------------------------------------------------------ the tree listing


def check_tree(entries: list[dict[str, Any]]) -> None:
    """Section 5.1.1, before any blob is read or written: the ``git ls-tree -r -z -l`` listing of the package
    directory must already satisfy every §8.1 rule, so a hostile tree never reaches the disk.
    Raises ``package_invalid``."""
    import unicodedata

    from orch.addons.package import MANIFEST_FILE, MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES, PackageError, check_path

    seen: dict[str, str] = {}
    dirs: set[str] = set()
    total = 0
    try:
        for e in entries:
            if e["mode"] not in ("100644", "100755"):  # links (120000), submodules (160000), anything else
                raise PackageError("mode")
            path = e["path"]
            check_path(path)
            if not isinstance(e["size"], int) or isinstance(e["size"], bool) or not 0 <= e["size"] <= MAX_FILE_BYTES:
                raise PackageError("size")
            total += e["size"]
            key = unicodedata.normalize("NFC", path).casefold()
            if key in seen:
                raise PackageError("two names equal when case is ignored")
            seen[key] = path
            parts = path.split("/")
            dirs.update("/".join(parts[:i]).casefold() for i in range(1, len(parts)))
        if dirs & set(seen):  # a name that is both a file and a directory
            raise PackageError("file and directory")
        if (
            len(seen) + len(dirs) > MAX_FILES
            or total > MAX_TOTAL_BYTES
            or MANIFEST_FILE not in {p for p in seen.values()}
        ):
            raise PackageError("limits")
    except (PackageError, KeyError, TypeError):
        raise Refuse("market.package_invalid") from None


# ------------------------------------------------------------------------------------------------ environments

_AUTO_RUN = re.compile(r"(?:sitecustomize|usercustomize)(?:\..*)?|.*\.pth", re.I)


def check_env_paths(paths: list[str]) -> None:
    """Section 8.2: the files a dependency install would put into the add-on's environment root. Nothing may escape
    the root, and nothing at the root may run by itself when an interpreter starts (``.pth`` files, ``sitecustomize``,
    ``usercustomize``). Raises ``env_failed``."""
    for p in paths:
        parts = p.split("/")
        if p.startswith("/") or "" in parts or "." in parts or ".." in parts or "\\" in p or "\x00" in p:
            raise Refuse("market.env_failed")
        if _AUTO_RUN.fullmatch(parts[0]):
            raise Refuse("market.env_failed")


def tree_hash(entries: list[list[Any]]) -> str:
    """``sha256:`` over ``<sha256>  <mode 4 octal digits>  <path>\\n`` lines in byte order of the path: the environment
    stamp. A changed, added, removed or re-moded file changes it."""
    import hashlib

    lines = [f"{sha}  {mode:04o}  {path}\n" for path, sha, mode in sorted(entries, key=lambda e: e[0].encode())]
    return "sha256:" + hashlib.sha256("".join(lines).encode()).hexdigest()


def check_env_stamp(stamp: dict[str, Any], package_digest_: str, actual: list[list[Any]]) -> None:
    """Section 8.2, before every run: the stamp names this package digest and the tree on disk hashes to the stamp.
    Raises ``env_changed``."""
    if stamp.get("digest") != package_digest_ or stamp.get("tree") != tree_hash(actual):
        raise Refuse("market.env_changed")


# ------------------------------------------------------------------------------------------------ network targets


def check_address(ip: str) -> None:
    """Section 5.1: the address a connection is about to use, after DNS resolution (and again after any redirect).

    IPv4 must be global unicast. IPv6 is an allow list: only 2000::/3, minus the ranges that embed or translate IPv4
    or are special (Teredo and the 2001::/23 block, 6to4 2002::/16, documentation 3fff::/20). So NAT64 (64:ff9b::/96,
    64:ff9b:1::/48), IPv4-compatible (::/96), SIIT (::ffff:0:0/96) and site-local (fec0::/10) are refused without
    decoding. IPv4-mapped (::ffff:a.b.c.d) is decoded and judged as the IPv4 address it carries."""
    import ipaddress

    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        raise Refuse("market.address_refused") from None
    if isinstance(a, ipaddress.IPv6Address):
        if a.ipv4_mapped is not None:
            a = a.ipv4_mapped
        else:
            net = ipaddress.ip_network
            ok = a in net("2000::/3") and not any(
                a in net(n) for n in ("2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20")
            )
            if not ok or not a.is_global:
                raise Refuse("market.address_refused")
            return
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
    repo = ctx["market_repo"]
    if (
        not is_canonical_url(repo)
        or not re.fullmatch(r"https://[a-z0-9.-]+(?:/[^/\s]+)+", repo)
        or repo.endswith(".git")
    ):
        raise Refuse("market.malformed")  # `market add` refuses a non-canonical URL instead of normalising it
    doc = check_index(a["index"])
    if doc["source"]["git"] != repo:
        raise Refuse("market.source_mismatch")
    # the signature is checked before anything is taken from the index: a version, an id list or a revocation that is
    # not from a verified official index is never recorded (L2M-1)
    isig = doc.get("signature")
    if repo == ctx["official"]["repo"]:
        if isig is None or isig["key"] != ctx["official"]["key"] or "expires_at" not in doc or not verify_index(doc):
            raise Refuse("market.index_unsigned")
    elif isig is not None and not verify_index(doc):
        raise Refuse("market.index_unsigned")
    if ctx["last_version"] is not None and doc["version"] < ctx["last_version"]:
        raise Refuse("market.rollback")
    official = repo == ctx["official"]["repo"]
    if not official and not ctx["isolation"]:
        raise Refuse("market.community_unisolated")
    hub = ctx["hub"]
    if hub is not None:
        if not hub_policy_ok(hub):
            raise Refuse("market.not_allowed")
        if repo in hub["block"]:
            raise Refuse("market.blocked")
        if repo not in hub["allow"]:
            raise Refuse("market.not_allowed")
    entry = next((e for e in doc["addons"] if e["id"] == a["id"]), None)
    if entry is None:
        raise Refuse("market.unknown_addon")
    if f"{entry['id']}@{entry['version']}" in doc.get("revoked", []):
        raise Refuse("market.revoked")
    host = lambda u: u.split("/")[2]  # noqa: E731  (schema guarantees https://host/…)
    if host(entry["source"]["git"]) != host(repo) and host(entry["source"]["git"]) not in ctx["git_hosts"]:
        raise Refuse("market.host_not_allowed")
    # reserved: core ships the built-in ids and the official ids, so the rule holds with no cached official index.
    # An official entry may use a built-in id only if core lists it as moved to the marketplace (G6).
    taken = official and entry["id"] in ctx["builtin"] and entry["id"] not in ctx["moved"]
    claimed = not official and (entry["id"] in ctx["builtin"] or entry["id"] in ctx["official_ids"])
    if taken or claimed:
        raise Refuse("market.reserved_id")
    prior = ctx["installed"].get(entry["id"])
    if prior is not None and prior["origin"] != repo:
        raise Refuse("market.origin_changed")
    if (
        prior is not None
        and prior["version"] == entry["version"]
        and prior.get("package_sha256") != entry["package_sha256"]
    ):
        raise Refuse("market.version_reused")  # the same version number must be the same bytes
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
            and m.get("runtime") == entry.get("runtime")  # the manifest decides; the index copy must agree (L2M-8)
        )
    except (ValueError, KeyError, TypeError):
        same = False
    if not same:
        raise Refuse("market.manifest_mismatch")
    rt = m.get("runtime")  # step 15 reads the *granted manifest*, never the index
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
        if case["check"] == "tree":
            check_tree(case["args"]["entries"])
            return {"ok": {}}
        if case["check"] == "env_paths":
            check_env_paths(case["args"]["paths"])
            return {"ok": {}}
        if case["check"] == "env_stamp":
            a = case["args"]
            check_env_stamp(a["stamp"], a["package_sha256"], a["actual"])
            return {"ok": {}}
        if case["check"] == "address":
            check_address(case["args"]["ip"])
            return {"ok": {}}
        if case["check"] == "lock":
            check_lock(case["args"]["kind"], case["args"]["text"])
            return {"ok": {}}
    except Refuse as r:
        return {"refuse": r.code}
    raise AssertionError(f"unknown check {case['check']!r}")
