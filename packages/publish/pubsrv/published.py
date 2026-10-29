"""M4 (#1357, S0): the contract of Published, shared links and apps, orch-publish's copy (contracts/hub/protocol.md
sections 11.4-11.9).

The same rules as ``orch_hub.contract.published`` (the hub's reference), implemented again on top of the vendored
``relay_proto`` and ``hub_path``; nothing here imports the orch_hub or orch_core packages. They are proved equal by
``contracts/hub/vectors_published.json``, which ``tests/test_published_vectors.py`` replays section by section. Verifiers
raise :class:`pubsrv.hub_path.Refused`; the routes that use them are slices S1 to S5.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from . import hub_path as H
from . import relay_proto as proto

LABEL_APP_META = H.LABELS["h_app_meta"]
LABEL_APP_REACH = H.LABELS["h_app_reach"]
LABEL_REQUEST_DIRECT = H.LABELS["h_publish_request_direct"]

_BAD_CATS = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})
# Default_Ignorable_Code_Point (Unicode 16.0): blank or invisible characters that are not Cf. Unlike the hub's copy this
# one does not insist on runtime tables of exactly 16.0.0 (publish runs on older Pythons): a character the runtime does
# not know is category Cn and is refused, so an older runtime only ever refuses MORE.
_DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
    (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)  # fmt: skip


def _char_ok(c: str) -> bool:
    cp = ord(c)
    return unicodedata.category(c) not in _BAD_CATS and not any(
        lo <= cp <= hi for lo, hi in _DEFAULT_IGNORABLE
    )


def text_ok(x: Any, n: int) -> bool:
    """One line of display text: 1..n UTF-8 bytes, no leading or trailing white space, no refused character (§8.5)."""
    if type(x) is not str or not x:
        return False
    try:
        if len(x.encode("utf-8")) > n:
            return False
    except UnicodeEncodeError:
        return False
    return x == x.strip() and all(_char_ok(c) for c in x)


_id32 = H._hex16  # 16 bytes as 32 lower-case hex digits


def _dec(x: Any, n: int) -> bytes | None:
    try:
        return proto.unb64u(x, n)
    except (ValueError, TypeError):
        return None


def _enc(b: bytes) -> str:
    return proto.b64u(b)


def _sha(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def _cj(o: Any) -> bytes:
    return proto.cj(o)


# --- the pure rules (identical in orch-hub, orch-publish and orch-core; the shared vectors prove it) ---------------

MAX_SAFE = 2**53 - 1
DAY_MS = 86_400_000
_HEX32 = re.compile(r"[0-9a-f]{32}")

# slugs (Opus condition 11): a slug is never freed, and these names are routes of the app server
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{1,38}[a-z0-9]")
RESERVED_SLUGS = frozenset({"s", "a", "v", "api", "pass", "rows", "v1", "v2"})


def slug_problem(slug: Any) -> str | None:
    if type(slug) is not str or SLUG.fullmatch(slug) is None:
        return "slug"
    return "reserved_slug" if slug in RESERVED_SLUGS else None


# reach origins (C7, Opus condition 10)
LOCAL_SUFFIXES = (
    ".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet", ".private",
    ".home.arpa", ".test", ".invalid", ".example", ".onion", ".alt",
)  # fmt: skip
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
    if sep and (
        re.fullmatch(r"[1-9][0-9]{0,4}", port) is None
        or not 1 <= int(port) <= 65535
        or port == "443"
    ):
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


_BASE = re.compile(
    r"https?://(\[[0-9a-f:]{2,39}\]|[a-z0-9.-]{1,253})(:[1-9][0-9]{0,4})?/a/[a-z0-9][a-z0-9-]{1,38}[a-z0-9]/v/[0-9a-f]{32}/"
)


def csp(base: Any, reach: Any, forbidden: Any) -> str:
    """The Content-Security-Policy of one app view (C7). ``base`` is the configured share origin plus the view path,
    never read from a Host header: exactly ``scheme://host[:port]/a/<slug>/v/<view id>/`` (no dot segments,
    port 1..65535). ``reach`` is the approved origin list, ``forbidden`` as in reach_problem. Raises ValueError."""
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


def csp_outcome(base: Any, reach: Any, forbidden: Any) -> dict[str, Any]:
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
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=PW_N, r=PW_R, p=PW_P, dklen=PW_DK, maxmem=PW_MAXMEM
    )
    return {"alg": "scrypt", "n": PW_N, "r": PW_R, "p": PW_P, "salt": _enc(salt), "dk": _enc(dk)}


def password_ok(rec: Any, password: Any) -> bool:
    """True when ``password`` opens ``rec``. A caller rate-limits and takes a KDF slot BEFORE calling this (cond. 8)."""
    if password_record_problem(rec) is not None:
        return False
    if (
        type(password) is not str
        or not password
        or len(password.encode("utf-8", "replace")) > PW_INPUT_MAX
    ):
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
META_KEYS = frozenset(
    {"slug", "name", "kind", "access", "audience", "pw", "reach", "files", "label", "ticket"}
)


def audience_problem(access: Any, audience: Any) -> str | None:
    """public and password: no audience. company: exactly ``company``. group: ``group:<hub group>`` or ``ws:<id>``."""
    if access in ("public", "password"):
        return None if audience is None else "audience"
    if access == "company":
        return None if audience == "company" else "audience"
    if access == "group":
        ok = type(audience) is str and (
            _AUDIENCE_GROUP.fullmatch(audience) or _AUDIENCE_WS.fullmatch(audience)
        )
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
        if (
            file_path_problem(f["path"])
            or type(f["size"]) is not int
            or not 0 <= f["size"] <= FILE_SIZE_MAX
        ):
            return "files"
        if type(f["sha256"]) is not str or _HEX64.fullmatch(f["sha256"]) is None:
            return "files"
    paths = [f["path"] for f in files]
    if len({p.casefold() for p in paths}) != len(paths):
        return "files_order"  # two paths equal ignoring case would be one file on a case-insensitive disk
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
    if meta["ticket"] is not None and (
        type(meta["ticket"]) is not str or _TICKET.fullmatch(meta["ticket"]) is None
    ):
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
    if (
        body["op"] not in OPS
        or type(body["app_id"]) is not str
        or _HEX32.fullmatch(body["app_id"]) is None
    ):
        return "malformed"
    if type(body["version"]) is not int or not 1 <= body["version"] <= 2**31 - 1:
        return "malformed"
    if _dec(body["meta_hash"], 32) is None or _dec(body["reach_hash"], 32) is None:
        return "malformed"
    if (
        body["access"] not in ACCESS
        or audience_problem(body["access"], body["audience"]) is not None
    ):
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
    body: Any,
    meta: Any,
    stored_files: Mapping[str, tuple[int, str]] | None = None,
    forbidden: Any = (),
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


# --- C1: the rows an admin read returns (never a token, a password record, a path or a byte of content) --------------

_LINK_ROW = {
    "workspace_id": H._hex16,
    "id": _id32,
    "type": H._enum("link"),
    "kind": H._enum("secret", "once"),
    "state": H._enum("live", "expired", "revoked"),
    "created_ms": H._int_min(0),
    "expires_ms": lambda x: x is None or H._int_min(0)(x),
    "bytes": H._int_min(0),
    "label": lambda x: text_ok(x, 80),
    "ticket": lambda x: x is None or (type(x) is str and _TICKET.fullmatch(x) is not None),
    "by_kind": H._enum("person", "agent"),
    "by_name": lambda x: x is None or text_ok(x, 60),
    "views": H._int_min(0),
    "has_password": H._bool,
}
_APP_ROW = {
    "workspace_id": H._hex16,
    "id": _id32,
    "type": H._enum("app"),
    "kind": H._enum(*KINDS),
    "state": H._enum("running", "stopped", "removed"),
    "created_ms": H._int_min(0),
    "updated_ms": H._int_min(0),
    "bytes": H._int_min(0),
    "label": lambda x: text_ok(x, 80),
    "ticket": lambda x: x is None or (type(x) is str and _TICKET.fullmatch(x) is not None),
    "by_kind": H._enum("person", "agent"),
    "by_name": lambda x: x is None or text_ok(x, 60),
    "slug": lambda x: slug_problem(x) is None,
    "name": lambda x: text_ok(x, 60),
    "access": H._enum(*ACCESS),
    "has_password": H._bool,
    "version": H._int_min(1),
    "visits_30d": H._int_min(0),
}
ROW_SHAPES = {"link": _LINK_ROW, "app": _APP_ROW}


def check_published_row(row: Any) -> dict[str, Any]:
    """The only shapes of a row of the company list: exactly these fields, ``has_password`` a bool, and no other
    member (so no token, hash, record, path, content or URL can ride along). ``malformed`` otherwise."""
    shape = (
        ROW_SHAPES.get(row.get("type"))
        if type(row) is dict and type(row.get("type")) is str
        else None
    )
    if shape is None or set(row) != set(shape) or not all(ok(row[k]) for k, ok in shape.items()):
        raise H.Refused("malformed")
    if row["type"] == "app" and (row["access"] == "password") != row["has_password"]:
        raise H.Refused("malformed")
    return row


def check_published_rows(rows: Any, scope: Any) -> list[dict[str, Any]]:
    """Every row is checked and must belong to the scope (full-mode workspaces bound to the calling hub); a row of a
    workspace outside it, a selective one included, is never emitted (``not_bound``)."""
    if type(rows) is not list:
        raise H.Refused("malformed")
    for r in rows:
        check_published_row(r)
        if r["workspace_id"] not in scope:
            raise H.Refused("not_bound")
    return rows


# --- C3: the owner's approval on both paths -------------------------------------------------------------------------


def app_request_hash(
    ws: bytes,
    ts_ms: int,
    nonce: bytes,
    method: str,
    host: str,
    target: str,
    body: bytes,
    hub_id: str | None,
) -> bytes:
    """What the owner's approval names. Through a hub (``hub_id`` set): section 11.3, unchanged. On the direct v2
    12.1 path (``hub_id`` None): ``H("orch/v2/publish-request|" || the same 12.1 fields)`` with no hub part, so an
    approval for one path never verifies on the other."""
    if hub_id is not None:
        return H.publish_request_hash(ws, ts_ms, nonce, method, host, target, body, hub_id)
    return _sha(
        LABEL_REQUEST_DIRECT + H._request_fields(ws, ts_ms, nonce, method, host, target, body)
    )


def check_app_approval(
    req: Mapping[str, Any],
    approval: Any,
    *,
    owner_person_id: str,
    now_ms: int,
    revoked_devices: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Step 9 for an app request on either path. ``req`` = {method, host, target (path + query), body (bytes), ws (hex),
    ts_ms, nonce (bytes), hub (hex, or None on the direct path)}. A commit, a rollback and a start ALWAYS need the
    owner's approval, whatever the access (``approval_required``); the approval must name this exact request and
    grant exactly the body's ``access``. Returns {op, app_id, access, audience, approved_by_person, device_id, body}."""
    route = app_route(req["method"], req["target"].partition("?")[0])
    if route is None or not route["approval"] or req["target"] != req["target"].partition("?")[0]:
        raise H.Refused("malformed")
    try:
        body = proto.parse_json(req["body"])
    except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        raise H.Refused("malformed") from None
    if (
        commit_body_problem(body) is not None
        or "app." + body["op"] != route["op"]
        or body["app_id"] != route["id"]
    ):
        raise H.Refused("malformed")
    if route["ws"] != req["ws"]:
        raise H.Refused("other_workspace")
    if approval is None:
        raise H.Refused("approval_required")
    o = H.verify_approval(
        approval,
        req["ws"],
        req["hub"],
        owner_person_id,
        app_request_hash(
            proto.unhex(req["ws"], 16),
            req["ts_ms"],
            req["nonce"],
            req["method"],
            req["host"],
            req["target"],
            req["body"],
            req["hub"],
        ),
        body["access"],
        now_ms,
        revoked_devices,
    )
    return {
        "op": body["op"],
        "app_id": body["app_id"],
        "access": body["access"],
        "audience": body["audience"],
        "approved_by_person": proto.identifier("person", proto.unb64u(approval["pk_pub"], 65)),
        "device_id": o["device_id"],
        "body": body,  # the one parsed body: the caller runs check_release on exactly this (M4 S3)
    }


# --- C2 and C6: what the hub signs at orch-publish --------------------------------------------------------------------

NARROW_WINDOW_MS = 120_000
PASS_MAX_MS = 60_000
NARROW_ACTION = {"share": "revoke", "app": "stop"}


def _bound(
    ws: str,
    hub: str,
    held: Mapping[str, Any],
    cards: Mapping[str, Any],
    bindings: Mapping[str, Any],
    full: bool,
):
    """The workspace has a binding to this hub under its card's CURRENT WSK; ``full`` also requires full mode."""
    card, b = cards.get(ws), bindings.get(ws)
    if (
        card is None
        or b is None
        or b["status"] != "bound"
        or b["binding"]["wsk_pub"] != card["wsk_pub"]
    ):
        raise H.Refused("not_bound")
    if b["binding"]["hub_id"] != hub or b["binding"]["hk_root_pub"] != proto.b64u(
        held["hk_root_pub"]
    ):
        raise H.Refused("other_hub")
    if full and b["binding"]["mode"] != "full":
        raise H.Refused(
            "not_bound"
        )  # a selective workspace shares no details and takes no hub narrowing


_KIND_LABEL = {"hub.release_narrow": "sig_hub_release_narrow", "hub.app_pass": "sig_hub_app_pass"}


def _hk_op_signed(signed: Any, kind: str, held: Any, hub: str) -> None:
    """``held`` is the verified hub card the caller found for the object's ``hub_id`` (None when it has none)."""
    if held is None or held["card"]["hub_id"] != hub:
        raise H.Refused("unknown_hub")
    try:
        sig = proto.unb64u(signed["sig"], 64)
    except (ValueError, TypeError):
        raise H.Refused("malformed") from None
    if not proto.SUITE.verify(
        held["hk_op_pub"], sig, H.LABELS[_KIND_LABEL[kind]] + proto.cj(signed["o"])
    ):
        raise H.Refused("bad_hub_signature")


def check_release_narrow(
    signed: Any,
    *,
    held: Any,
    cards: Mapping[str, Any],
    bindings: Mapping[str, Any],
    owners: Mapping[str, str],
    nonces: Any,
    now_ms: int,
) -> dict[str, Any]:
    """orch-publish's check of ``PUT /v2/hub/narrow`` (section 11.6), in order: syntax (envelope, fields, only
    revoke for a share and stop for an app) -> window -> the held hub card for ``hub_id`` (``held``, verified again for
    this request, or None) -> the HK_op signature -> nonce (``nonces.record(hub, nonce, now_ms)``) -> a binding of the
    workspace to this hub, full mode, under the CURRENT WSK -> the target belongs to ``workspace_id`` (``owners``:
    ``"share:<id>"`` / ``"app:<id>"`` -> workspace). Returns the narrowing to apply."""
    o = H._envelope(signed, "hub.release_narrow")
    H._fields(o, "hub.release_narrow")
    if NARROW_ACTION[o["target"]] != o["action"]:
        raise H.Refused("malformed")
    if abs(now_ms - o["ts_ms"]) > NARROW_WINDOW_MS:
        raise H.Refused("stale_timestamp", server_ms=now_ms)
    _hk_op_signed(signed, "hub.release_narrow", held, o["hub_id"])
    if not nonces.record(o["hub_id"], o["nonce"], now_ms):
        raise H.Refused("replay")
    _bound(o["workspace_id"], o["hub_id"], held, cards, bindings, full=True)
    owner = owners.get(o["target"] + ":" + o["id"])
    if owner is None:
        raise H.Refused("not_found")
    if owner != o["workspace_id"]:
        raise H.Refused("other_workspace")
    return {
        "target": o["target"],
        "id": o["id"],
        "action": o["action"],
        "workspace_id": o["workspace_id"],
        "hub": o["hub_id"],
    }


def check_app_pass(
    signed: Any,
    *,
    held: Any,
    cards: Mapping[str, Any],
    bindings: Mapping[str, Any],
    app: Mapping[str, Any] | None,
    issued: Mapping[str, Mapping[str, Any]],
    spent: frozenset[str],
    now_ms: int,
) -> dict[str, Any]:
    """orch-publish's check of the hub's pass into a company or group app (section 11.8), in order: syntax -> the held
    hub card -> the HK_op signature -> the app's workspace is bound to this hub -> the app exists, in that workspace
    -> it is a company or group app (``not_company_app``) -> ``audience`` equals the stored audience
    (``audience_mismatch``) -> not expired (``expired``) and at most 60 s long (``pass_too_long``) -> the nonce was
    issued by publish for this app (``bad_nonce``), is not expired, and unspent (``replay``). The caller spends the
    nonce in the same transaction. ``app`` = {id, workspace_id, access, audience}; ``issued`` = {nonce: {app_id,
    until_ms}}."""
    o = H._envelope(signed, "hub.app_pass")
    H._fields(o, "hub.app_pass")
    _hk_op_signed(signed, "hub.app_pass", held, o["hub_id"])
    _bound(o["workspace_id"], o["hub_id"], held, cards, bindings, full=False)
    if app is None or app["id"] != o["app_id"]:
        raise H.Refused("not_found")
    if app["workspace_id"] != o["workspace_id"]:
        raise H.Refused("other_workspace")
    if app["access"] not in ("company", "group"):
        raise H.Refused("not_company_app")
    if o["audience"] != app["audience"]:
        raise H.Refused("audience_mismatch")
    if o["exp_ms"] <= now_ms:
        raise H.Refused("expired")
    if o["exp_ms"] - now_ms > PASS_MAX_MS:
        raise H.Refused("pass_too_long")
    n = issued.get(o["nonce"])
    if o["nonce"] in spent:
        raise H.Refused("replay")
    if n is None or n["app_id"] != o["app_id"]:
        raise H.Refused("bad_nonce")
    if n["until_ms"] <= now_ms:
        raise H.Refused("expired")
    return {"app_id": o["app_id"], "nonce": o["nonce"], "audience": o["audience"]}
