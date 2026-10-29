"""M4 (#1357, S0): the contract of Published, shared links and apps, the host's copy (orch-hub
``contracts/hub/protocol.md`` sections 11.4 to 11.9).

The host builds what the owner approves and what orch-publish checks: the slug and reach rules, the CSP of a view (to
show the review), the password record (the host makes it), the version meta and the commit/rollback/start body that
binds it by ``meta_hash``, and the hash the owner's approval names on BOTH paths (through a hub and the direct v2 12.1
path). The same rules as ``orch_hub.contract.published`` and ``pubsrv.published``, implemented again;
``contracts/hub/vectors_published.json`` proves the three equal (``tests/hub/test_published_vectors.py``).
No key, no clock, no I/O: signing the approval is slice S4.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import re
from collections.abc import Mapping
from typing import Any

from orch.canon.jcs import JcsError, loads_strict
from orch.canon.jcs import dumps as _jcs
from orch.crypto.encoding import EncodingError, b64u, unb64u

from . import publish as _pub
from .custodial_shape import text_ok

LABEL_APP_META = b"orch/v2/app-meta|"
LABEL_APP_REACH = b"orch/v2/app-reach|"
LABEL_REQUEST_DIRECT = b"orch/v2/publish-request|"
LABELS = {
    "sig_hub_release_narrow": b"orch/v2/hub-release-narrow|",
    "sig_hub_app_pass": b"orch/v2/hub-app-pass|",
    "h_publish_request_direct": LABEL_REQUEST_DIRECT,
    "h_app_meta": LABEL_APP_META,
    "h_app_reach": LABEL_APP_REACH,
}


def _dec(x: Any, n: int) -> bytes | None:
    try:
        return unb64u(x, n)
    except EncodingError:
        return None


def _enc(b: bytes) -> str:
    return b64u(b)


def _sha(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def _cj(o: Any) -> bytes:
    return _jcs(o)


# --- the pure rules (identical in orch-hub, orch-publish and orch-core; the shared vectors prove it) ---------------

MAX_SAFE = 2**53 - 1
DAY_MS = 86_400_000
_HEX32 = re.compile(r"[0-9a-f]{32}")

# slugs (Opus condition 11): a slug is never freed, and these names are routes of the app server
SLUG = re.compile(r"[a-z0-9-]{3,40}")
RESERVED_SLUGS = frozenset({"s", "a", "v", "api", "pass", "rows", "v1", "v2"})


def slug_problem(slug: Any) -> str | None:
    if type(slug) is not str or SLUG.fullmatch(slug) is None:
        return "slug"
    return "reserved_slug" if slug in RESERVED_SLUGS else None


# reach origins (C7, Opus condition 10)
LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet", ".private")
REACH_MAX = 8
ORIGIN_MAX = 120
_HOST_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


def _host_of(origin_or_host: str) -> str:
    """The host of an origin (any scheme) or a bare host; cookies do not separate ports, so hosts are compared."""
    rest = re.sub(r"^[a-z]+://", "", str(origin_or_host).lower())
    return rest.partition("/")[0].partition(":")[0]


def origin_problem(origin: Any, forbidden: Any = ()) -> str | None:
    """None when ``origin`` may be a reach entry, else a short code. ``forbidden`` are origins or hosts of the hub and
    of orch-publish itself; the comparison is by host. Canonical form only (lower case, no default port), so two
    spellings of one origin cannot both be approved."""
    if type(origin) is not str or not origin or len(origin) > ORIGIN_MAX or not origin.isascii():
        return "origin"
    if not origin.isprintable() or " " in origin:
        return "origin"
    if "*" in origin:
        return "wildcard"
    if not origin.startswith("https://"):
        return "https_only"  # http:, data:, javascript:, blob:, ws:, file: and a bare host all land here
    rest = origin[len("https://") :]
    if "@" in rest:
        return "userinfo"
    if any(c in rest for c in "/?#\\"):
        return "path"
    if rest.startswith("["):
        return "ip_literal"
    host, sep, port = rest.partition(":")
    if sep and (re.fullmatch(r"[1-9][0-9]{0,4}", port) is None or not 1 <= int(port) <= 65535 or port == "443"):
        return "origin"
    labels = host.split(".")
    if not host or len(host) > 253 or any(_HOST_LABEL.fullmatch(x) is None for x in labels):
        return "origin"
    if labels[-1][0].isdigit():
        return "ip_literal"  # 127.0.0.1, 127.1, 0x7f.0.0.1, 2130706433: no spelling of a number is a name
    if len(labels) < 2 or host == "localhost" or ("." + host).endswith(LOCAL_SUFFIXES):
        return "not_remote"
    return "forbidden_origin" if host in {_host_of(f) for f in forbidden} else None


def reach_problem(reach: Any, forbidden: Any = ()) -> str | None:
    """``reach``: strictly sorted, duplicate-free list of canonical origins, at most :data:`REACH_MAX`."""
    if type(reach) is not list or len(reach) > REACH_MAX:
        return "reach"
    for o in reach:
        bad = origin_problem(o, forbidden)
        if bad is not None:
            return bad
    return None if all(a < b for a, b in itertools.pairwise(reach)) else "reach_order"


_BASE = re.compile(r"https?://(\[[0-9a-f:]{2,39}\]|[a-z0-9.-]{1,253})(:[0-9]{1,5})?/a/[a-z0-9-]{3,40}/v/[0-9a-f]{32}/")


def csp(base: Any, reach: Any, forbidden: Any = ()) -> str:
    """The Content-Security-Policy of one app view (C7). ``base`` is the configured share origin plus the view path
    (``http://203.0.113.7:8081/a/my-app/v/<view id>/``), never read from a Host header; it is exactly
    ``scheme://host[:port]/a/<slug>/v/<view id>/`` (no dot segments, port 1..65535). ``forbidden`` as in reach_problem. ``reach`` is the version's approved origin list. Raises ValueError."""
    m = _BASE.fullmatch(base) if type(base) is str else None
    if m is None or (m.group(2) is not None and not 1 <= int(m.group(2)[1:]) <= 65535):
        raise ValueError("base")
    bad = reach_problem(reach, forbidden)
    if bad is not None:
        raise ValueError(bad)
    connect = " ".join([base, *reach])
    return (
        "sandbox allow-scripts allow-forms allow-popups allow-downloads; default-src 'none'; "
        f"script-src {base} 'unsafe-inline'; style-src {base} 'unsafe-inline'; img-src {base}; font-src {base}; "
        f"connect-src {connect}; form-action {base}; base-uri 'none'; frame-ancestors 'none'"
    )


def csp_outcome(base: Any, reach: Any, forbidden: Any = ()) -> dict[str, Any]:
    try:
        return {"ok": csp(base, reach, forbidden)}
    except ValueError as e:
        return {"error": str(e)}


# password record (C5, Opus condition 8): made by the host, checked by orch-publish; nothing else is a record
PW_N, PW_R, PW_P, PW_SALT, PW_DK = 32768, 8, 1, 16, 32
PW_MAXMEM = 128 * PW_N * PW_R * 2
PW_INPUT_MAX = 256


def password_record_problem(rec: Any) -> str | None:
    if type(rec) is not dict or set(rec) != {"alg", "n", "r", "p", "salt", "dk"}:
        return "malformed"
    if rec["alg"] != "scrypt" or any(type(rec[k]) is not int for k in ("n", "r", "p")):
        return "malformed"
    if (rec["n"], rec["r"], rec["p"]) != (PW_N, PW_R, PW_P):
        return "malformed"  # exactly these parameters: a weaker record is refused, a heavier one is a memory attack
    if _dec(rec["salt"], PW_SALT) is None or _dec(rec["dk"], PW_DK) is None:
        return "malformed"
    return None


def make_password_record(password: str, salt: bytes) -> dict[str, Any]:
    """The host draws ``salt`` (16 random bytes) and shows the generated password once; only this record leaves."""
    if type(password) is not str or not password or len(password.encode("utf-8")) > PW_INPUT_MAX:
        raise ValueError("password")
    if len(salt) != PW_SALT:
        raise ValueError("salt")
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=PW_N, r=PW_R, p=PW_P, dklen=PW_DK, maxmem=PW_MAXMEM)
    return {"alg": "scrypt", "n": PW_N, "r": PW_R, "p": PW_P, "salt": _enc(salt), "dk": _enc(dk)}


def password_ok(rec: Any, password: Any) -> bool:
    """True when ``password`` opens ``rec``. A caller rate-limits and takes a KDF slot BEFORE calling this (cond. 8)."""
    if password_record_problem(rec) is not None:
        return False
    if type(password) is not str or not password or len(password.encode("utf-8", "replace")) > PW_INPUT_MAX:
        return False
    try:
        raw = password.encode("utf-8")
    except UnicodeEncodeError:
        return False
    salt, want = _dec(rec["salt"], PW_SALT), _dec(rec["dk"], PW_DK)
    got = hashlib.scrypt(raw, salt=salt, n=PW_N, r=PW_R, p=PW_P, dklen=PW_DK, maxmem=PW_MAXMEM)
    return hmac.compare_digest(got, want)


# the version meta and what a commit binds (C3, Opus condition 3)
KINDS = ("page", "app", "form")
ACCESS = ("public", "password", "company", "group")
FILES_MAX = 500
FILE_SIZE_MAX = 50_000_000
_FILE_SEG = re.compile(r"[A-Za-z0-9_~+@-][A-Za-z0-9._~+@-]*")
_AUDIENCE_GROUP = re.compile(r"group:[a-z0-9][a-z0-9_-]{0,39}")
_AUDIENCE_WS = re.compile(r"ws:[0-9a-f]{32}")
_TICKET = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
META_KEYS = frozenset({"slug", "name", "kind", "access", "audience", "pw", "reach", "files", "label", "ticket"})


def audience_problem(access: Any, audience: Any) -> str | None:
    """public and password: no audience. company: exactly ``company``. group: ``group:<hub group>`` or ``ws:<id>``."""
    if access in ("public", "password"):
        return None if audience is None else "audience"
    if access == "company":
        return None if audience == "company" else "audience"
    if access == "group":
        ok = type(audience) is str and (_AUDIENCE_GROUP.fullmatch(audience) or _AUDIENCE_WS.fullmatch(audience))
        return None if ok else "audience"
    return "access"


def file_path_problem(path: Any) -> bool:
    if type(path) is not str or not path or len(path) > 200 or not path.isascii():
        return True
    return any(_FILE_SEG.fullmatch(seg) is None for seg in path.split("/"))


def files_problem(files: Any) -> str | None:
    """Sorted by path, unique, relative paths without ``.``/``..``/dot-files, sizes and sha256 (hex), ``index.html``."""
    if type(files) is not list or not 1 <= len(files) <= FILES_MAX:
        return "files"
    for f in files:
        if type(f) is not dict or set(f) != {"path", "size", "sha256"}:
            return "files"
        if file_path_problem(f["path"]) or type(f["size"]) is not int or not 0 <= f["size"] <= FILE_SIZE_MAX:
            return "files"
        if type(f["sha256"]) is not str or _HEX64.fullmatch(f["sha256"]) is None:
            return "files"
    paths = [f["path"] for f in files]
    if not all(a < b for a, b in itertools.pairwise(paths)):
        return "files_order"
    return None if "index.html" in paths else "no_index"


def meta_problem(meta: Any, forbidden: Any = ()) -> str | None:
    """The stored meta of one app version. ``forbidden``: the hub's and orch-publish's own origins/hosts."""
    if type(meta) is not dict or set(meta) != META_KEYS:
        return "malformed"
    if (bad := slug_problem(meta["slug"])) is not None:
        return bad
    if not text_ok(meta["name"], 60) or "\n" in meta["name"]:
        return "name"
    if meta["kind"] not in KINDS:
        return "kind"
    if meta["access"] not in ACCESS:
        return "access"
    if (bad := audience_problem(meta["access"], meta["audience"])) is not None:
        return bad
    if (meta["access"] == "password") != (meta["pw"] is not None) or (
        meta["pw"] is not None and password_record_problem(meta["pw"]) is not None
    ):
        return "password"
    if (bad := reach_problem(meta["reach"], forbidden)) is not None:
        return bad
    if (bad := files_problem(meta["files"])) is not None:
        return bad
    if not text_ok(meta["label"], 80) or "\n" in meta["label"]:
        return "label"
    if meta["ticket"] is not None and (type(meta["ticket"]) is not str or _TICKET.fullmatch(meta["ticket"]) is None):
        return "ticket"
    return None


def meta_hash(meta: Mapping[str, Any]) -> str:
    """``b64u(H("orch/v2/app-meta|" || cj(meta)))`` over the whole stored meta (slug, name, kind, access, audience,
    the password record, reach, the files list with their sha256, label, ticket): any change is another hash."""
    return _enc(_sha(LABEL_APP_META + _cj(dict(meta))))


def reach_hash(reach: list[str]) -> str:
    return _enc(_sha(LABEL_APP_REACH + _cj(list(reach))))


OPS = ("commit", "rollback", "start")
BODY_KEYS = frozenset({"op", "app_id", "version", "meta_hash", "reach_hash", "access", "audience"})


def commit_body_problem(body: Any) -> str | None:
    """The body of ``commit``, ``rollback`` and ``start``: the one request the owner's approval names (by its hash)."""
    if type(body) is not dict or set(body) != BODY_KEYS:
        return "malformed"
    if body["op"] not in OPS or type(body["app_id"]) is not str or _HEX32.fullmatch(body["app_id"]) is None:
        return "malformed"
    if type(body["version"]) is not int or not 1 <= body["version"] <= 2**31 - 1:
        return "malformed"
    if _dec(body["meta_hash"], 32) is None or _dec(body["reach_hash"], 32) is None:
        return "malformed"
    if body["access"] not in ACCESS or audience_problem(body["access"], body["audience"]) is not None:
        return "malformed"
    return None


def release_body(op: str, app_id: str, version: int, meta: Mapping[str, Any]) -> dict[str, Any]:
    """The body a host sends (and the owner approves) for ``op`` on ``version`` whose stored meta is ``meta``."""
    return {
        "op": op,
        "app_id": app_id,
        "version": version,
        "meta_hash": meta_hash(meta),
        "reach_hash": reach_hash(meta["reach"]),
        "access": meta["access"],
        "audience": meta["audience"],
    }


def check_release(
    body: Any, meta: Any, stored_files: Mapping[str, tuple[int, str]] | None = None, forbidden: Any = ()
) -> str | None:
    """What orch-publish does with a commit, a rollback or a start before it serves a byte (condition 3): the body is
    well formed; the stored meta is valid; the body's ``meta_hash``, ``reach_hash``, ``access`` and ``audience`` are
    exactly those of the stored meta (``meta_mismatch``); and the files it holds are exactly the meta's list, with
    every size and sha256 (``file_mismatch``). ``stored_files`` maps path -> (size, sha256 hex) as publish recomputed
    them from the bytes on disk; None (nothing stored yet) is ``file_mismatch``: meta alone serves nothing."""
    if commit_body_problem(body) is not None:
        return "malformed"
    if (bad := meta_problem(meta, forbidden)) is not None:
        return bad
    if (
        body["meta_hash"] != meta_hash(meta)
        or body["reach_hash"] != reach_hash(meta["reach"])
        or body["access"] != meta["access"]
        or body["audience"] != meta["audience"]
    ):
        return "meta_mismatch"
    if stored_files is None:
        return "file_mismatch"
    want = {f["path"]: (f["size"], f["sha256"]) for f in meta["files"]}
    return None if dict(stored_files) == want else "file_mismatch"


# which requests exist at orch-publish and which of them need the owner's approval (Opus condition 15)
_WS = r"/v1/ws/([0-9a-f]{32})"
_ID = r"([0-9a-f]{32})"
APP_ROUTES: tuple[tuple[str, str, str, bool], ...] = (
    ("PUT", _WS + "/apps/" + _ID + r"/versions/([1-9][0-9]{0,9})", "app.meta", False),
    ("PUT", _WS + "/apps/" + _ID + r"/versions/([1-9][0-9]{0,9})/files/(.+)", "app.file", False),
    ("POST", _WS + "/apps/" + _ID + "/commit", "app.commit", True),
    ("POST", _WS + "/apps/" + _ID + "/rollback", "app.rollback", True),
    ("POST", _WS + "/apps/" + _ID + "/start", "app.start", True),
    ("POST", _WS + "/apps/" + _ID + "/stop", "app.stop", False),
    ("DELETE", _WS + "/apps/" + _ID, "app.remove", False),
    ("POST", _WS + "/shares/" + _ID + "/extend", "share.extend", False),
    ("POST", _WS + "/shares/" + _ID + "/password", "share.password", False),
)


def app_route(method: Any, path: Any) -> dict[str, Any] | None:
    """Classify an app or link route: ``{op, approval, ws, id}`` or None. ``path`` is the target without its query;
    anything with a query, ``%``, a dot segment or a double slash is not a route (no second spelling of a route)."""
    if type(method) is not str or type(path) is not str or not path.isascii():
        return None
    if "?" in path or "%" in path or "//" in path or any(s in (".", "..") for s in path.split("/")):
        return None
    for m, rx, op, approval in APP_ROUTES:
        hit = re.fullmatch(rx, path) if method == m else None
        if hit is not None:
            return {"op": op, "approval": approval, "ws": hit.group(1), "id": hit.group(2)}
    return None


EXTEND_MAX_DAYS = 30


def extend_problem(body: Any) -> str | None:
    """``{"days": 1..30}``; the new expiry is ``now + days`` (never longer than 30 days from now, condition 14)."""
    if type(body) is not dict or set(body) != {"days"} or type(body["days"]) is not int:
        return "malformed"
    return None if 1 <= body["days"] <= EXTEND_MAX_DAYS else "days"


def extend_expiry(now_ms: int, body: Any) -> int:
    if extend_problem(body) is not None:
        raise ValueError("extend")
    return now_ms + body["days"] * DAY_MS


# --- the owner's approval: what it names, on both paths ---------------------------------------------------------------


def app_request_hash(
    ws: bytes, ts_ms: int, nonce: bytes, method: str, host: str, target: str, body: bytes, hub_id: str | None
) -> bytes:
    """What the owner's approval names. Through a hub (``hub_id`` set): section 11.3 (:func:`orch.hubclient.publish
    .request_hash`). On the direct v2 12.1 path (``hub_id`` None): ``H("orch/v2/publish-request|" || the same 12.1
    fields)`` with no hub part, so an approval for one path never verifies on the other."""
    if hub_id is not None:
        return _pub.request_hash(ws, ts_ms, nonce, method, host, target, body, hub_id)
    return _sha(LABEL_REQUEST_DIRECT + _pub._fields(ws, ts_ms, nonce, method, host, target, body))


def approval_request_problem(req: Mapping[str, Any]) -> str | None:
    """The part of the approval step the host can judge before anybody signs (section 11.4): the route takes an approval
    and has no query, the body is a valid release body whose ``op`` and ``app_id`` are the route's (``malformed``), and
    the workspace is the route's (``other_workspace``). ``req`` = {method, target, body (bytes), ws (hex)}. The
    certificate chain and the signature are orch-publish's to verify; the host only builds them."""
    target = req["target"]
    route = app_route(req["method"], target.partition("?")[0])
    if route is None or not route["approval"] or target != target.partition("?")[0]:
        return "malformed"
    try:
        body = loads_strict(req["body"])
    except (JcsError, EncodingError):
        return "malformed"
    if commit_body_problem(body) is not None or "app." + body["op"] != route["op"] or body["app_id"] != route["id"]:
        return "malformed"
    return "other_workspace" if route["ws"] != req["ws"] else None
