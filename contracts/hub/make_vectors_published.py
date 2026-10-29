"""Builds ``contracts/hub/vectors_published.json``: the contract of Published, shared links and apps (M4, #1357, S0;
protocol.md sections 11.4-11.9, Opus design conditions 1-3, 7, 8, 10, 11, 15, 19).

Test code, deterministic (FAKE keys from fixed seeds, a fixed clock). orch-hub, orch-publish and orch-core each replay
EVERY section against their own copy of the rules; ``uv run python contracts/hub/make_vectors_published.py``
rewrites the file (the hub package's ``tests/test_published_vectors.py`` checks the committed file equals the build)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from orch_hub.contract import published as P
from orch_hub.contract import suite as S
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u, cj

NOW = G.NOW
HUB = G.HUB_ID
WS_A, WS_B = G.WS_A, G.WS_B
APP = G.fake16("app_1")
APP_2 = G.fake16("app_2")
SHARE = G.fake16("share_1")
SHARE_B = G.fake16("share_b")
HOST = G.PUB_HOST
SALT = S.fake_seed("published/pw_salt")[:16]
PASSWORD = "river-lamp-orbit-tulip-seven"
FORBIDDEN = ["https://hub.example.com", "https://publish.example.com:8443"]
REQUIRED = {
    "approval": [
        "app_commit_hub_path",
        "app_commit_direct_path_no_hub",
        "app_commit_without_approval",
        "rollback_needs_approval",
        "start_needs_approval",
        "approval_for_company_used_for_public",
        "approval_mismatched_reach_hash",
        "approval_other_manifest",
    ],
    "narrow": ["narrow_start_refused", "narrow_other_workspace", "narrow_selective_refused", "narrow_replay"],
    "pass": ["pass_wrong_app", "pass_expired", "pass_reused_nonce", "pass_other_hub", "pass_audience_mismatch"],
}


def n32(label: str) -> str:
    return b64u(S.fake_seed("published/" + label))


def file_of(path: str, data: bytes) -> dict[str, Any]:
    return {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


FILES_DATA = {"app.js": b"console.log(1)", "index.html": b"<h1>FAQ</h1><script src=app.js></script>"}
FILES = [file_of(p, d) for p, d in sorted(FILES_DATA.items())]
STORED = {f["path"]: [f["size"], f["sha256"]] for f in FILES}
RECORD = P.make_password_record(PASSWORD, SALT)


def meta(**kw: Any) -> dict[str, Any]:
    m = {
        "slug": "team-faq",
        "name": "Team FAQ",
        "kind": "app",
        "access": "company",
        "audience": "company",
        "pw": None,
        "reach": ["https://api.example.com"],
        "files": FILES,
        "label": "Team FAQ, first version",
        "ticket": G.TICKET,
        **kw,
    }
    return {k: v for k, v in m.items() if v is not G.DROP}


def drop(m: dict[str, Any], key: str) -> dict[str, Any]:
    return {k: v for k, v in m.items() if k != key}


def named(rows: list[tuple[Any, ...]], *keys: str) -> list[dict[str, Any]]:
    return [dict(zip(("name", *keys), r, strict=True)) for r in rows]


# ---- pure rules -----------------------------------------------------------------------------------------------------


def slug_cases() -> list[dict[str, Any]]:
    rows = [
        ("plain", "team-faq", None),
        ("minimum length 3", "abc", None),
        ("maximum length 40", "a" * 40, None),
        ("digits and hyphens", "2026-q4-plan", None),
        ("two characters", "ab", "slug"),
        ("leading hyphen", "-abc", "slug"),
        ("trailing hyphen", "abc-", "slug"),
        ("only hyphens", "---", "slug"),
        ("hyphen inside", "a-c", None),
        ("forty-one characters", "a" * 41, "slug"),
        ("upper case", "Team-faq", "slug"),
        ("underscore", "team_faq", "slug"),
        ("dot", "team.faq", "slug"),
        ("slash", "team/faq", "slug"),
        ("space", "team faq", "slug"),
        ("non-ascii", "täam", "slug"),
        ("empty", "", "slug"),
        ("not a string", 7, "slug"),
        ("reserved api", "api", "reserved_slug"),
        ("reserved pass", "pass", "reserved_slug"),
        ("reserved rows", "rows", "reserved_slug"),
        ("reserved v1 is too short to be a slug", "v1", "slug"),
        ("reserved single letters are too short", "s", "slug"),
        ("a name that only starts like a reserved one", "api-docs", None),
    ]
    return named(rows, "slug", "expect")


def origin_cases() -> list[dict[str, Any]]:
    rows = [
        ("plain https origin", "https://api.example.com", None),
        ("with a port", "https://api.example.com:8443", None),
        ("a punycode name", "https://xn--bcher-kva.example.net", None),
        ("a number inside a name", "https://api2.example.com", None),
        ("http", "http://api.example.com", "https_only"),
        ("a bare host", "api.example.com", "https_only"),
        ("data url", "data:text/html,hi", "https_only"),
        ("javascript url", "javascript:alert(1)", "https_only"),
        ("blob url", "blob:https://api.example.com/x", "https_only"),
        ("websocket", "wss://api.example.com", "https_only"),
        ("wildcard", "*", "wildcard"),
        ("wildcard host", "https://*.example.com", "wildcard"),
        ("wildcard scheme", "*://api.example.com", "wildcard"),
        ("a path", "https://api.example.com/v1", "path"),
        ("a trailing slash", "https://api.example.com/", "path"),
        ("a query", "https://api.example.com?x=1", "path"),
        ("a fragment", "https://api.example.com#x", "path"),
        ("a backslash", "https://api.example.com\\x", "path"),
        ("userinfo", "https://user:pw@api.example.com", "userinfo"),
        ("userinfo without a password", "https://user@api.example.com", "userinfo"),
        ("userinfo that hides the host", "https://api.example.com@evil.example", "userinfo"),
        ("ipv4 literal", "https://203.0.113.7", "ip_literal"),
        ("loopback literal", "https://127.0.0.1", "ip_literal"),
        ("private literal", "https://10.0.0.5", "ip_literal"),
        ("short loopback 127.1", "https://127.1", "ip_literal"),
        ("hex loopback", "https://0x7f.0.0.1", "ip_literal"),
        ("decimal loopback", "https://2130706433", "ip_literal"),
        ("ipv6 loopback", "https://[::1]", "ip_literal"),
        ("ipv6 mapped private", "https://[::ffff:10.0.0.1]:8443", "ip_literal"),
        ("localhost", "https://localhost", "not_remote"),
        ("a one-word host", "https://intranet", "not_remote"),
        ("suffix .local", "https://printer.local", "not_remote"),
        ("suffix .internal", "https://tools.internal", "not_remote"),
        ("suffix .lan", "https://nas.lan", "not_remote"),
        ("suffix .home", "https://router.home", "not_remote"),
        ("suffix .corp", "https://crm.corp", "not_remote"),
        ("subdomain of localhost", "https://app.localhost", "not_remote"),
        ("suffix .home.arpa", "https://nas.home.arpa", "not_remote"),
        ("suffix .test", "https://api.test", "not_remote"),
        ("suffix .invalid", "https://api.invalid", "not_remote"),
        ("suffix .example", "https://api.example", "not_remote"),
        ("suffix .onion", "https://abc.onion", "not_remote"),
        ("suffix .alt", "https://api.alt", "not_remote"),
        ("upper case", "https://API.example.com", "origin"),
        ("the default port spelled out", "https://api.example.com:443", "origin"),
        ("port zero", "https://api.example.com:0", "origin"),
        ("port too big", "https://api.example.com:65536", "origin"),
        ("port with a leading zero", "https://api.example.com:08443", "origin"),
        ("an empty port", "https://api.example.com:", "origin"),
        ("a space", "https://api.example.com x", "origin"),
        ("a control character", "https://api.example.com\x00", "origin"),
        ("non-ascii host", "https://bücher.example", "origin"),
        ("empty label", "https://api..example.com", "origin"),
        ("empty", "", "origin"),
        ("not a string", None, "origin"),
        ("the hub's own origin", "https://hub.example.com", "forbidden_origin"),
        ("the hub's host on another port", "https://hub.example.com:9443", "forbidden_origin"),
        ("publish's own origin", "https://publish.example.com:8443", "forbidden_origin"),
        ("publish's host on the default port", "https://publish.example.com", "forbidden_origin"),
    ]
    return [{"name": n, "origin": o, "forbidden": FORBIDDEN, "expect": e} for n, o, e in rows]


def reach_cases() -> list[dict[str, Any]]:
    a, b = "https://api.example.com", "https://cdn.example.com"
    rows = [
        ("empty reach", [], None),
        ("one origin", [a], None),
        ("two origins sorted", [a, b], None),
        ("two origins not sorted", [b, a], "reach_order"),
        ("a duplicate", [a, a], "reach_order"),
        ("a bad origin among good ones", [a, "https://x.example.com/p"], "path"),
        ("a wildcard", ["*"], "wildcard"),
        ("the hub origin", ["https://hub.example.com"], "forbidden_origin"),
        ("too many (9)", [f"https://h{i}.example.com" for i in range(9)], "reach"),
        ("eight is the limit", [f"https://h{i}.example.com" for i in range(8)], None),
        ("not a list", "https://api.example.com", "reach"),
        ("a tuple-like string list entry that is not a string", [7], "origin"),
    ]
    return [{"name": n, "reach": r, "forbidden": FORBIDDEN, "expect": e} for n, r, e in rows]


BASE = "http://203.0.113.7:8081/a/team-faq/v/" + "ab" * 16 + "/"


def csp_for(base: str, reach: list[str]) -> str:
    b = base
    return (
        "sandbox allow-scripts allow-forms allow-popups allow-downloads; default-src 'none'; "
        f"script-src {b} 'unsafe-inline'; style-src {b} 'unsafe-inline'; img-src {b}; font-src {b}; "
        f"connect-src {' '.join([b, *reach])}; form-action {b}; base-uri 'none'; frame-ancestors 'none'"
    )


def csp_cases() -> list[dict[str, Any]]:
    a, b = "https://api.example.com", "https://cdn.example.com:8443"
    rows = [
        ("empty reach: the app reaches only itself", BASE, [], {"ok": csp_for(BASE, [])}),
        ("one https origin", BASE, [a], {"ok": csp_for(BASE, [a])}),
        ("two origins", BASE, [a, b], {"ok": csp_for(BASE, [a, b])}),
        (
            "an https share origin with a domain",
            "https://apps.example.com/a/team-faq/v/" + "cd" * 16 + "/",
            [],
            {"ok": csp_for("https://apps.example.com/a/team-faq/v/" + "cd" * 16 + "/", [])},
        ),
        ("a wildcard", BASE, ["*"], {"error": "wildcard"}),
        ("a path", BASE, ["https://api.example.com/v1"], {"error": "path"}),
        ("data:", BASE, ["data:"], {"error": "https_only"}),
        ("javascript:", BASE, ["javascript:alert(1)"], {"error": "https_only"}),
        ("userinfo", BASE, ["https://u:p@api.example.com"], {"error": "userinfo"}),
        ("a loopback address", BASE, ["https://127.0.0.1"], {"error": "ip_literal"}),
        ("a source that would end the directive", BASE, ["https://a.example.com; script-src *"], {"error": "origin"}),
        ("a base that would end the directive", "http://203.0.113.7:8081/a/x;script-src */", [], {"error": "base"}),
        ("a base without the closing slash", "http://203.0.113.7:8081/a/team-faq/v/ab", [], {"error": "base"}),
        ("a base with a quote", "http://203.0.113.7:8081/a/'x'/", [], {"error": "base"}),
        ("a base with a space", "http://203.0.113.7:8081/a/x y/", [], {"error": "base"}),
        ("a base that is not a string", None, [], {"error": "base"}),
        ("csp_root_base", "http://203.0.113.7:8081/", [], {"error": "base"}),
        ("csp_dotdot_base", "http://203.0.113.7:8081/a/../", [], {"error": "base"}),
        ("csp_bad_port", "http://203.0.113.7:99999/a/team-faq/v/" + "ab" * 16 + "/", [], {"error": "base"}),
        ("csp_port_zero", "http://203.0.113.7:0/a/team-faq/v/" + "ab" * 16 + "/", [], {"error": "base"}),
        ("csp_hub_origin_in_reach", BASE, ["https://hub.example.com"], {"error": "forbidden_origin"}),
        ("csp_publish_origin_in_reach", BASE, ["https://publish.example.com:8443"], {"error": "forbidden_origin"}),
    ]
    return [{"name": n, "base": b_, "reach": r, "forbidden": FORBIDDEN, "expect": e} for n, b_, r, e in rows]


def password_record_cases() -> list[dict[str, Any]]:
    r = RECORD
    rows = [
        ("a valid record", r, None),
        ("n below 32768", {**r, "n": 16384}, "malformed"),
        ("n above 32768", {**r, "n": 65536}, "malformed"),
        ("r other than 8", {**r, "r": 4}, "malformed"),
        ("p other than 1", {**r, "p": 2}, "malformed"),
        ("a 15-byte salt", {**r, "salt": b64u(SALT[:15])}, "malformed"),
        ("a 17-byte salt", {**r, "salt": b64u(SALT + b"x")}, "malformed"),
        ("a 31-byte dk", {**r, "dk": b64u(b"k" * 31)}, "malformed"),
        ("a 33-byte dk", {**r, "dk": b64u(b"k" * 33)}, "malformed"),
        ("another algorithm", {**r, "alg": "argon2id"}, "malformed"),
        ("an extra member", {**r, "pepper": "x"}, "malformed"),
        ("a missing member", drop(r, "dk"), "malformed"),
        ("n as a string", {**r, "n": "32768"}, "malformed"),
        ("n as a bool", {**r, "n": True}, "malformed"),
        ("a padded salt", {**r, "salt": r["salt"] + "=="}, "malformed"),
        ("not an object", [r], "malformed"),
        ("null", None, "malformed"),
    ]
    return named(rows, "record", "expect")


def password_verify_cases() -> list[dict[str, Any]]:
    other_salt = S.fake_seed("published/pw_salt_2")[:16]
    other = P.make_password_record(PASSWORD, other_salt)
    rows = [
        ("the right password", RECORD, PASSWORD, True),
        ("a wrong password", RECORD, PASSWORD + "x", False),
        (
            "the same password with another salt does not open this record",
            {**RECORD, "salt": other["salt"]},
            PASSWORD,
            False,
        ),
        ("the record of another salt opens with the same password", other, PASSWORD, True),
        ("an empty password", RECORD, "", False),
        ("a 257-byte password is refused before any KDF", RECORD, "x" * 257, False),
        ("not a string", RECORD, 12345, False),
        ("a weak record never verifies", {**RECORD, "n": 1024}, PASSWORD, False),
        ("a lone surrogate", RECORD, "\ud800", False),
    ]
    return named(rows, "record", "password", "expect")


def meta_cases() -> list[dict[str, Any]]:
    m = meta()
    rows = [
        ("a valid company app", m, None),
        ("a valid public page", meta(kind="page", access="public", audience=None), None),
        ("a valid password form", meta(kind="form", access="password", audience=None, pw=RECORD), None),
        ("a valid group app by hub group", meta(access="group", audience="group:marketing"), None),
        ("a valid group app by workspace", meta(access="group", audience="ws:" + WS_A), None),
        ("no reach", meta(reach=[]), None),
        ("no ticket", meta(ticket=None), None),
        ("an extra member", {**m, "env": {}}, "malformed"),
        ("a missing member", drop(m, "reach"), "malformed"),
        ("a reserved slug", meta(slug="api"), "reserved_slug"),
        ("a bad slug", meta(slug="Team FAQ"), "slug"),
        ("an empty name", meta(name=""), "name"),
        ("a name with a newline", meta(name="a\nb"), "name"),
        ("a name with a bidi override", meta(name="Team" + chr(0x202E) + "QAF"), "name"),
        ("a name over 60 bytes", meta(name="n" * 61), "name"),
        ("an unknown kind", meta(kind="server"), "kind"),
        ("an unknown access", meta(access="secret"), "access"),
        ("public with an audience", meta(access="public", audience="company"), "audience"),
        ("company with another audience", meta(audience="group:marketing"), "audience"),
        ("group without an audience", meta(access="group", audience=None), "audience"),
        ("group with a bad audience", meta(access="group", audience="everyone"), "audience"),
        ("password access without a record", meta(access="password", audience=None, pw=None), "password"),
        ("a record on a non-password app", meta(pw=RECORD), "password"),
        ("a weak record", meta(access="password", audience=None, pw={**RECORD, "n": 1024}), "password"),
        ("reach with a wildcard", meta(reach=["*"]), "wildcard"),
        ("reach with the hub's origin", meta(reach=["https://hub.example.com"]), "forbidden_origin"),
        ("reach with publish's origin", meta(reach=["https://publish.example.com:8443"]), "forbidden_origin"),
        ("reach not sorted", meta(reach=["https://b.example.com", "https://a.example.com"]), "reach_order"),
        ("no files", meta(files=[]), "files"),
        ("no index.html", meta(files=[FILES[0]]), "no_index"),
        ("files not sorted", meta(files=list(reversed(FILES))), "files_order"),
        ("a duplicate file", meta(files=[FILES[0], FILES[0], FILES[1]]), "files_order"),
        ("a path with ..", meta(files=[FILES[0], file_of("../x", b"x"), FILES[1]]), "files"),
        ("an absolute path", meta(files=[file_of("/etc/passwd", b"x"), FILES[1]]), "files"),
        ("a dot file", meta(files=[file_of(".env", b"x"), FILES[0], FILES[1]]), "files"),
        ("a backslash path", meta(files=[FILES[0], file_of("a\\b", b"x"), FILES[1]]), "files"),
        ("an empty segment", meta(files=[file_of("a//b", b"x"), FILES[0], FILES[1]]), "files"),
        ("two paths equal ignoring case", meta(files=[file_of("Index.html", b"x"), *FILES]), "files_order"),
        ("a bad sha256", meta(files=[{**FILES[0], "sha256": "AB" * 32}, FILES[1]]), "files"),
        ("a negative size", meta(files=[{**FILES[0], "size": -1}, FILES[1]]), "files"),
        ("a file member too many", meta(files=[{**FILES[0], "mode": 511}, FILES[1]]), "files"),
        ("an empty label", meta(label=""), "label"),
        ("a bad ticket", meta(ticket="not-a-ticket"), "ticket"),
    ]
    return [{"name": n, "meta": x, "forbidden": FORBIDDEN, "expect": e} for n, x, e in rows]


def meta_hash_cases() -> list[dict[str, Any]]:
    """The base meta and one variant per field: each has its own hash, so a commit binds every one of them."""
    base = meta()
    variants = [
        ("base", base),
        ("slug", meta(slug="team-faq-2")),
        ("name", meta(name="Team FAQ 2")),
        ("kind", meta(kind="page")),
        ("access", meta(access="group", audience="group:marketing")),
        ("audience", meta(access="group", audience="ws:" + WS_A)),
        ("pw record", meta(access="password", audience=None, pw=RECORD)),
        ("reach", meta(reach=[])),
        ("a file changed", meta(files=[FILES[0], file_of("index.html", b"<h1>other</h1>")])),
        ("a file added", meta(files=[file_of("a.css", b"x"), *FILES])),
        ("label", meta(label="Team FAQ, second version")),
        ("ticket", meta(ticket=None)),
    ]
    return [{"name": n, "meta": m, "hash": P.meta_hash(m), "reach_hash": P.reach_hash(m["reach"])} for n, m in variants]


def commit_cases() -> list[dict[str, Any]]:
    m = meta()

    def body(op: str = "commit", mm: dict[str, Any] = m, **kw: Any) -> dict[str, Any]:
        return {**P.release_body(op, APP, 1, mm), **kw}

    def case(name: str, b: Any, mm: Any, files: Any, expect: str | None) -> dict[str, Any]:
        return {"name": name, "body": b, "meta": mm, "stored_files": files, "forbidden": FORBIDDEN, "expect": expect}

    changed_file = [FILES[0], file_of("index.html", b"<h1>evil</h1>")]
    pub = meta(access="public", audience=None)
    return [
        case("commit_ok", body(), m, STORED, None),
        case("rollback_ok", body("rollback"), m, STORED, None),
        case("start_ok", body("start"), m, STORED, None),
        case("commit_before_any_file_is_stored", body(), m, None, "file_mismatch"),
        case(
            "commit_meta_changed_after_approval_label",
            body(),
            meta(label="changed after the owner looked"),
            STORED,
            "meta_mismatch",
        ),
        case(
            "commit_reach_widened_after_approval",
            body(),
            meta(reach=["https://api.example.com", "https://evil.example.net"]),
            STORED,
            "meta_mismatch",
        ),
        case("commit_access_widened_after_approval", body(), pub, STORED, "meta_mismatch"),
        case(
            "commit_body_claims_public_for_a_company_app",
            body(access="public", audience=None),
            m,
            STORED,
            "meta_mismatch",
        ),
        case(
            "commit_audience_changed", body(), meta(access="group", audience="group:marketing"), STORED, "meta_mismatch"
        ),
        case(
            "commit_password_record_swapped",
            body(mm=meta(access="password", audience=None, pw=RECORD)),
            meta(access="password", audience=None, pw={**RECORD, "dk": b64u(b"k" * 32)}),
            STORED,
            "meta_mismatch",
        ),
        case("commit_slug_changed", body(), meta(slug="other-app"), STORED, "meta_mismatch"),
        case(
            "commit_files_list_changed",
            body(),
            meta(files=changed_file),
            {p: v for p, v in STORED.items()},
            "meta_mismatch",
        ),
        case(
            "commit_file_bytes_changed",
            body(),
            m,
            {**STORED, "index.html": [STORED["index.html"][0], "0" * 64]},
            "file_mismatch",
        ),
        case("commit_file_size_changed", body(), m, {**STORED, "app.js": [1, STORED["app.js"][1]]}, "file_mismatch"),
        case("commit_file_missing", body(), m, {"index.html": STORED["index.html"]}, "file_mismatch"),
        case("commit_file_extra", body(), m, {**STORED, "evil.js": [1, "1" * 64]}, "file_mismatch"),
        case(
            "commit_hub_origin_in_reach",
            body(mm=meta(reach=["https://hub.example.com"])),
            meta(reach=["https://hub.example.com"]),
            STORED,
            "forbidden_origin",
        ),
        case("commit_body_extra_member", {**body(), "files": []}, m, STORED, "malformed"),
        case("commit_body_unknown_op", body(op="publish"), m, STORED, "malformed"),
        case("commit_body_version_zero", body(version=0), m, STORED, "malformed"),
        case("commit_body_bad_app_id", body(app_id="x"), m, STORED, "malformed"),
        case("commit_body_not_an_object", [body()], m, STORED, "malformed"),
        case("commit_invalid_meta", body(), meta(slug="api"), STORED, "reserved_slug"),
    ]


def route_cases() -> list[dict[str, Any]]:
    w = "/v1/ws/" + WS_A
    rows = [
        ("meta put", "PUT", f"{w}/apps/{APP}/versions/3", {"op": "app.meta", "approval": False}),
        ("file put", "PUT", f"{w}/apps/{APP}/versions/3/files/css/site.css", {"op": "app.file", "approval": False}),
        ("commit", "POST", f"{w}/apps/{APP}/commit", {"op": "app.commit", "approval": True}),
        ("rollback", "POST", f"{w}/apps/{APP}/rollback", {"op": "app.rollback", "approval": True}),
        ("start", "POST", f"{w}/apps/{APP}/start", {"op": "app.start", "approval": True}),
        ("stop", "POST", f"{w}/apps/{APP}/stop", {"op": "app.stop", "approval": False}),
        ("remove", "DELETE", f"{w}/apps/{APP}", {"op": "app.remove", "approval": False}),
        ("extend", "POST", f"{w}/shares/{SHARE}/extend", {"op": "share.extend", "approval": False}),
        ("password", "POST", f"{w}/shares/{SHARE}/password", {"op": "share.password", "approval": False}),
        ("commit with GET", "GET", f"{w}/apps/{APP}/commit", None),
        ("commit with PUT", "PUT", f"{w}/apps/{APP}/commit", None),
        ("a query on commit", "POST", f"{w}/apps/{APP}/commit?x=1", None),
        ("a double slash", "POST", f"{w}//apps/{APP}/commit", None),
        ("a dot segment", "POST", f"{w}/apps/{APP}/../{APP}/commit", None),
        ("a percent escape", "POST", f"{w}/apps/{APP}/comm%69t", None),
        ("a trailing slash", "POST", f"{w}/apps/{APP}/commit/", None),
        ("upper-case hex in the app id", "POST", f"{w}/apps/{APP.upper()}/commit", None),
        ("version zero", "PUT", f"{w}/apps/{APP}/versions/0", None),
        ("an unknown app op", "POST", f"{w}/apps/{APP}/publish", None),
        ("a route of the old share api", "POST", f"{w}/shares/{SHARE}/commit", None),
    ]
    out = []
    for n, m, p, e in rows:
        want = None if e is None else {**e, "ws": WS_A, "id": APP if "/apps/" in p else SHARE}
        out.append({"name": n, "method": m, "path": p, "expect": want})
    return out


def extend_cases() -> list[dict[str, Any]]:
    rows = [
        ("one day", {"days": 1}, {"ok": NOW + 86_400_000}),
        ("seven days", {"days": 7}, {"ok": NOW + 7 * 86_400_000}),
        ("thirty days is the limit", {"days": 30}, {"ok": NOW + 30 * 86_400_000}),
        ("thirty-one days", {"days": 31}, {"error": "days"}),
        ("zero days", {"days": 0}, {"error": "days"}),
        ("negative", {"days": -3}, {"error": "days"}),
        ("a string", {"days": "7"}, {"error": "malformed"}),
        ("a float", {"days": 7.5}, {"error": "malformed"}),
        ("a bool", {"days": True}, {"error": "malformed"}),
        ("an extra member", {"days": 7, "until": 1}, {"error": "malformed"}),
        ("no member", {}, {"error": "malformed"}),
        ("not an object", [7], {"error": "malformed"}),
    ]
    return [{"name": n, "body": b, "now_ms": NOW, "expect": e} for n, b, e in rows]


# ---- C1: rows --------------------------------------------------------------------------------------------------------

LINK_ROW = {
    "workspace_id": WS_A,
    "id": SHARE,
    "type": "link",
    "kind": "secret",
    "state": "live",
    "created_ms": NOW - G.DAY,
    "expires_ms": NOW + G.DAY,
    "bytes": 12_345,
    "label": "Q3 numbers for the board",
    "ticket": G.TICKET,
    "by_kind": "agent",
    "by_name": "invoice-agent",
    "views": 3,
    "has_password": False,
}
APP_ROW = {
    "workspace_id": WS_A,
    "id": APP,
    "type": "app",
    "kind": "app",
    "state": "running",
    "created_ms": NOW - G.DAY,
    "updated_ms": NOW - 3_600_000,
    "bytes": 9_001,
    "label": "Team FAQ, first version",
    "ticket": G.TICKET,
    "by_kind": "person",
    "by_name": "Alice",
    "slug": "team-faq",
    "name": "Team FAQ",
    "access": "company",
    "has_password": False,
    "version": 2,
    "visits_30d": 41,
}


def row_cases() -> list[dict[str, Any]]:
    def bad(r: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return {**r, **kw}

    rows = [
        ("row_link_ok", LINK_ROW, None),
        ("row_app_ok", APP_ROW, None),
        ("row_app_password_ok", bad(APP_ROW, access="password", has_password=True), None),
        ("row_link_with_nulls_ok", bad(LINK_ROW, ticket=None, by_name=None, expires_ms=None), None),
        ("row_link_with_token", bad(LINK_ROW, token="abc"), "malformed"),
        ("row_link_with_token_hash", bad(LINK_ROW, token_hash="a" * 64), "malformed"),
        (
            "row_link_with_fragment_link",
            bad(LINK_ROW, url="http://203.0.113.7:8081/s/" + SHARE + "#t=abc"),
            "malformed",
        ),
        ("row_link_with_public_url", bad(LINK_ROW, public_url=None), "malformed"),
        ("row_app_with_password_record", bad(APP_ROW, pw=RECORD), "malformed"),
        ("row_app_with_a_path", bad(APP_ROW, path="/var/lib/publish/apps/x"), "malformed"),
        ("row_app_with_content", bad(APP_ROW, content="<h1>x</h1>"), "malformed"),
        ("row_app_with_files", bad(APP_ROW, files=FILES), "malformed"),
        ("row_app_with_reach", bad(APP_ROW, reach=[]), "malformed"),
        ("row_has_password_a_string", bad(LINK_ROW, has_password="yes"), "malformed"),
        ("row_has_password_a_record", bad(LINK_ROW, has_password=RECORD), "malformed"),
        ("row_password_app_says_no_password", bad(APP_ROW, access="password", has_password=False), "malformed"),
        ("row_company_app_says_password", bad(APP_ROW, has_password=True), "malformed"),
        ("row_unknown_type", bad(LINK_ROW, type="release"), "malformed"),
        ("row_link_kind_of_an_app", bad(LINK_ROW, kind="app"), "malformed"),
        ("row_app_kind_of_a_link", bad(APP_ROW, kind="secret"), "malformed"),
        ("row_unknown_state", bad(APP_ROW, state="live"), "malformed"),
        ("row_label_with_bidi", bad(LINK_ROW, label="Q3" + chr(0x202E) + "stnemucod"), "malformed"),
        ("row_label_with_a_newline", bad(LINK_ROW, label="a\nb"), "malformed"),
        ("row_by_name_with_a_zero_width_space", bad(LINK_ROW, by_name="al" + chr(0x200B) + "ice"), "malformed"),
        ("row_by_kind_a_bot", bad(LINK_ROW, by_kind="bot"), "malformed"),
        ("row_negative_views", bad(LINK_ROW, views=-1), "malformed"),
        ("row_bad_ticket", bad(LINK_ROW, ticket="TKT-1"), "malformed"),
        ("row_missing_a_member", drop(LINK_ROW, "views"), "malformed"),
        ("row_bad_slug", bad(APP_ROW, slug="API"), "malformed"),
        ("row_not_an_object", [LINK_ROW], "malformed"),
        ("row_type_is_a_list", bad(LINK_ROW, type=["link"]), "malformed"),
    ]
    return named(rows, "row", "expect")


def row_scope_cases() -> list[dict[str, Any]]:
    other = {**LINK_ROW, "workspace_id": WS_B, "id": SHARE_B}
    rows = [
        ("rows_of_a_full_mode_workspace", [LINK_ROW, APP_ROW], [WS_A], {"ok": 2}),
        ("rows_of_a_selective_workspace_are_refused", [LINK_ROW, other], [WS_A], {"error": "not_bound"}),
        ("rows_with_nothing_in_scope", [LINK_ROW], [], {"error": "not_bound"}),
        ("no_rows", [], [], {"ok": 0}),
        ("a_bad_row_is_refused_first", [{**LINK_ROW, "token": "x"}], [WS_A], {"error": "malformed"}),
        ("rows_not_a_list", {"rows": []}, [WS_A], {"error": "malformed"}),
    ]
    return named(rows, "rows", "scope", "expect")


# ---- C3: approvals on both paths ------------------------------------------------------------------------------------


def req_of(
    op: str,
    b: dict[str, Any],
    *,
    hub: str | None = HUB,
    ts: int = NOW,
    nonce: bytes = G.NONCE,
    ws: str = WS_A,
    app: str = APP,
    target: str | None = None,
) -> dict[str, Any]:
    return {
        "method": "POST",
        "host": HOST,
        "target": target or f"/v1/ws/{ws}/apps/{app}/{op}",
        "body_hex": cj(b).hex(),
        "ws": ws,
        "ts_ms": ts,
        "nonce_hex": nonce.hex(),
        "hub": hub,
    }


def approval_for(
    req: dict[str, Any],
    *,
    access: str,
    hub: Any = "same",
    pk: str = "pk_alice",
    dk: str = "dk_alice_mac",
    ws: str | None = None,
    request_hash: bytes | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    hub_in = req["hub"] if hub == "same" else hub
    rh = request_hash or P.app_request_hash(
        bytes.fromhex(req["ws"]),
        req["ts_ms"],
        bytes.fromhex(req["nonce_hex"]),
        req["method"],
        req["host"],
        req["target"],
        bytes.fromhex(req["body_hex"]),
        req["hub"],
    )
    o = {
        **G.hdr("hub.publish_approval"),
        "workspace_id": ws or req["ws"],
        "hub_id": hub_in,
        "device_id": G.H.device_id(G.K.pub[dk + ".sig"]),
        "request_hash": b64u(rh),
        "access": access,
    }
    s = G.signed(key or dk + ".sig", "sig_hub_publish_approval", o)
    return {"o": s["o"], "sig": s["sig"], "device_cert": G.device_cert(pk, dk), "pk_pub": G.K.b(pk)}


def approval_cases() -> list[dict[str, Any]]:
    m = meta()
    pub = meta(access="public", audience=None)
    cases: list[dict[str, Any]] = []

    def add(name: str, req: dict[str, Any], approval: Any, expect: dict[str, Any], **kw: Any) -> None:
        cases.append(
            {
                "name": name,
                "args": {
                    "now_ms": NOW,
                    "owner_person_id": G.P_ALICE,
                    "revoked_devices": [],
                    "request": req,
                    "approval": approval,
                    **kw,
                },
                "expect": expect,
            }
        )

    def ok(b: dict[str, Any], who: str = G.P_ALICE) -> dict[str, Any]:
        return {
            "ok": {
                "op": b["op"],
                "app_id": APP,
                "access": b["access"],
                "audience": b["audience"],
                "approved_by_person": who,
            }
        }

    for op in P.OPS:
        b = P.release_body(op, APP, 1, m)
        r = req_of(op, b)
        a = approval_for(r, access="company")
        label = {"commit": "app_commit_hub_path", "rollback": "app_rollback_hub_path", "start": "app_start_hub_path"}[
            op
        ]
        add(label, r, a, ok(b))
        add(
            {
                "commit": "app_commit_without_approval",
                "rollback": "rollback_needs_approval",
                "start": "start_needs_approval",
            }[op],
            r,
            None,
            {"error": "approval_required"},
        )
        rd = req_of(op, b, hub=None)
        add(
            f"app_{op}_direct_path_no_hub" if op != "commit" else "app_commit_direct_path_no_hub",
            rd,
            approval_for(rd, access="company"),
            ok(b),
        )
        add(f"app_{op}_direct_path_without_approval", rd, None, {"error": "approval_required"})
    bp = P.release_body("commit", APP, 1, pub)
    rp = req_of("commit", bp)
    add("app_commit_public_hub_path", rp, approval_for(rp, access="public"), ok(bp))
    rpd = req_of("commit", bp, hub=None)
    add("no_hub_publishes_a_public_app", rpd, approval_for(rpd, access="public"), ok(bp))
    bc = P.release_body("commit", APP, 1, m)
    rc = req_of("commit", bc)
    add("approval_for_company_used_for_public", rp, approval_for(rp, access="company"), {"error": "bad_approval"})
    bg = P.release_body("commit", APP, 1, meta(access="group", audience="group:marketing"))
    rg = req_of("commit", bg)
    add("app_commit_group", rg, approval_for(rg, access="group"), ok(bg))
    add("approval_for_public_used_for_group", rg, approval_for(rg, access="public"), {"error": "bad_approval"})
    # the owner approved reach A; the commit that arrives names reach B (same everything else)
    bad_reach = P.release_body("commit", APP, 1, meta(reach=["https://api.example.com", "https://evil.example.net"]))
    add(
        "approval_mismatched_reach_hash",
        req_of("commit", bad_reach),
        approval_for(rc, access="company"),
        {"error": "bad_approval"},
    )
    add(
        "approval_other_manifest",
        req_of("commit", P.release_body("commit", APP, 1, meta(label="swapped files"))),
        approval_for(rc, access="company"),
        {"error": "bad_approval"},
    )
    other_aud = P.release_body("commit", APP, 1, meta(access="group", audience="group:sales"))
    add(
        "approval_other_audience",
        req_of("commit", other_aud),
        approval_for(rg, access="group"),
        {"error": "bad_approval"},
    )
    add(
        "approval_other_version",
        req_of("commit", P.release_body("commit", APP, 2, m)),
        approval_for(rc, access="company"),
        {"error": "bad_approval"},
    )
    add(
        "approval_commit_used_for_rollback",
        req_of("rollback", P.release_body("rollback", APP, 1, m)),
        approval_for(rc, access="company"),
        {"error": "bad_approval"},
    )
    add(
        "approval_replayed_on_another_nonce",
        req_of("commit", bc, nonce=S.fake_seed("published/nonce2")[:16]),
        approval_for(rc, access="company"),
        {"error": "bad_approval"},
    )
    add(
        "approval_other_timestamp",
        req_of("commit", bc, ts=NOW + 1000),
        approval_for(rc, access="company"),
        {"error": "bad_approval"},
    )
    add(
        "approval_for_another_app",
        req_of("commit", bc, app=APP_2),
        approval_for(rc, access="company"),
        {"error": "malformed"},
    )
    # path-agnostic but never interchangeable
    rd = req_of("commit", bc, hub=None)
    add("direct_approval_on_hub_path", rc, approval_for(rd, access="company", hub=None), {"error": "other_hub"})
    add("hub_approval_on_direct_path", rd, approval_for(rc, access="company"), {"error": "other_hub"})
    add(
        "direct_approval_with_the_hub_path_request_hash",
        rd,
        approval_for(
            rd,
            access="company",
            request_hash=P.app_request_hash(
                bytes.fromhex(WS_A), NOW, G.NONCE, "POST", HOST, rd["target"], bytes.fromhex(rd["body_hex"]), HUB
            ),
        ),
        {"error": "bad_approval"},
    )
    add(
        "approval_through_another_hub",
        rc,
        approval_for(rc, access="company", hub=G.ROGUE_HUB_ID),
        {"error": "other_hub"},
    )
    add("approval_of_another_workspace", rc, approval_for(rc, access="company", ws=WS_B), {"error": "other_workspace"})
    add(
        "approval_of_another_person",
        rc,
        approval_for(rc, access="company", pk="pk_mallory", dk="dk_mallory"),
        {"error": "other_person"},
    )
    add(
        "approval_of_a_revoked_device",
        rc,
        approval_for(rc, access="company"),
        {"error": "revoked"},
        revoked_devices=[G.H.device_id(G.K.pub["dk_alice_mac.sig"])],
    )
    add(
        "approval_signed_by_another_key",
        rc,
        approval_for(rc, access="company", key="dk_mallory.sig"),
        {"error": "bad_approval"},
    )
    # the request itself
    add(
        "body_op_differs_from_the_route",
        req_of("start", bc),
        approval_for(rc, access="company"),
        {"error": "malformed"},
    )
    add(
        "body_with_an_unknown_access",
        req_of("commit", {**bc, "access": "secret"}),
        approval_for(rc, access="company"),
        {"error": "malformed"},
    )
    add(
        "body_not_json",
        {**rc, "body_hex": b"not json".hex()},
        approval_for(rc, access="company"),
        {"error": "malformed"},
    )
    stop = req_of("stop", bc)
    add("an approval on a route that takes none", stop, approval_for(stop, access="company"), {"error": "malformed"})
    add(
        "a query on the signed route",
        {**rc, "target": rc["target"] + "?x=1"},
        approval_for(rc, access="company"),
        {"error": "malformed"},
    )
    return cases


# ---- C2 and C6: what the hub signs ------------------------------------------------------------------------------------

BIND_FULL = G.binding(WS_A, "wsk_a", 1, "hk_root", mode="full")
BIND_SELECTIVE = G.binding(WS_A, "wsk_a", 1, "hk_root", mode="selective")
BIND_ROGUE = G.binding(WS_A, "wsk_a", 1, "hk_root_rogue", mode="full")
BIND_B = G.binding(WS_B, "wsk_b", 1, "hk_root", mode="full")
UNBOUND = G.unbound(WS_A, "wsk_a", 1)


def state(bindings: dict[str, list[Any]] | None = None, **kw: Any) -> dict[str, Any]:
    b = {WS_A: [{"binding": BIND_FULL}], WS_B: [{"binding": BIND_B}]} if bindings is None else bindings
    return {
        "now_ms": NOW,
        "dev_mode": True,
        "hub_card": G.card(),
        "cards": {WS_A: {"wsk_pub": G.K.b("wsk_a")}, WS_B: {"wsk_pub": G.K.b("wsk_b")}},
        "bindings": b,
        "binding_wsk": {WS_A: G.K.b("wsk_a"), WS_B: G.K.b("wsk_b")},
        **kw,
    }


def narrow(
    target: str = "share",
    id_: str | None = None,
    action: str | None = None,
    *,
    ws: str = WS_A,
    hub: str = HUB,
    ts: int = NOW,
    nonce: str | None = None,
    key: str = "hk_op",
    label: str = "sig_hub_release_narrow",
    **kw: Any,
) -> dict[str, Any]:
    o = {
        **G.hdr("hub.release_narrow"),
        "hub_id": hub,
        "workspace_id": ws,
        "target": target,
        "id": id_ or (SHARE if target == "share" else APP),
        "action": action or {"share": "revoke", "app": "stop"}[target],
        "ts_ms": ts,
        "nonce": nonce or n32("narrow1"),
        **kw,
    }
    o = {k: v for k, v in o.items() if v is not G.DROP}
    return G.signed(key, label, o)


def narrow_cases() -> list[dict[str, Any]]:
    owners = {"share:" + SHARE: WS_A, "app:" + APP: WS_A, "share:" + SHARE_B: WS_B, "app:" + APP_2: WS_B}
    cases: list[dict[str, Any]] = []

    def add(name: str, signed: Any, expect: dict[str, Any], **kw: Any) -> None:
        args = {**state(), "owners": owners, "seen_nonces": [], "signed": signed, **kw}
        cases.append({"name": name, "args": args, "expect": expect})

    def ok(t: str) -> dict[str, Any]:
        return {
            "ok": {
                "target": t,
                "id": SHARE if t == "share" else APP,
                "action": "revoke" if t == "share" else "stop",
                "workspace_id": WS_A,
                "hub": HUB,
            }
        }

    add("narrow_revoke_a_share", narrow("share"), ok("share"))
    add("narrow_stop_an_app", narrow("app"), ok("app"))
    add("narrow_start_refused", narrow("app", action="start"), {"error": "malformed"})
    add("narrow_extend_refused", narrow("share", action="extend"), {"error": "malformed"})
    add("narrow_remove_refused", narrow("app", action="remove"), {"error": "malformed"})
    add("narrow_stop_a_share_refused", narrow("share", action="stop"), {"error": "malformed"})
    add("narrow_revoke_an_app_refused", narrow("app", action="revoke"), {"error": "malformed"})
    add("narrow_other_workspace", narrow("share", SHARE_B), {"error": "other_workspace"})
    add("narrow_other_workspace_app", narrow("app", APP_2), {"error": "other_workspace"})
    add("narrow_unknown_target", narrow("share", G.fake16("nothing")), {"error": "not_found"})
    add(
        "narrow_selective_refused",
        narrow("share"),
        {"error": "not_bound"},
        bindings={WS_A: [{"binding": BIND_SELECTIVE}]},
    )
    add("narrow_replay", narrow("share"), {"error": "replay"}, seen_nonces=[n32("narrow1")])
    add(
        "narrow_unbound_workspace",
        narrow("share"),
        {"error": "not_bound"},
        bindings={WS_A: [{"binding": BIND_FULL}, {"unbound": UNBOUND}]},
    )
    add("narrow_no_binding", narrow("share"), {"error": "not_bound"}, bindings={})
    add(
        "narrow_binding_under_an_old_wsk",
        narrow("share"),
        {"error": "not_bound"},
        cards={WS_A: {"wsk_pub": G.K.b("wsk_b")}, WS_B: {"wsk_pub": G.K.b("wsk_b")}},
    )
    add(
        "narrow_workspace_bound_to_another_hub",
        narrow("share"),
        {"error": "other_hub"},
        bindings={WS_A: [{"binding": BIND_ROGUE}]},
    )
    add("narrow_stale_timestamp", narrow("share", ts=NOW - 121_000), {"error": "stale_timestamp"})
    add("narrow_future_timestamp", narrow("share", ts=NOW + 121_000), {"error": "stale_timestamp"})
    add("narrow_window_edge", narrow("share", ts=NOW - 120_000), ok("share"))
    add("narrow_unknown_hub", narrow("share", hub=G.ROGUE_HUB_ID), {"error": "unknown_hub"})
    add("narrow_signed_by_another_hub_key", narrow("share", key="hk_op_rogue"), {"error": "bad_hub_signature"})
    add(
        "narrow_signed_under_the_publish_label",
        narrow("share", label="sig_hub_publish"),
        {"error": "bad_hub_signature"},
    )
    add("narrow_signed_under_the_pass_label", narrow("share", label="sig_hub_app_pass"), {"error": "bad_hub_signature"})
    add("narrow_signed_by_the_workspace", narrow("share", key="wsk_a"), {"error": "bad_hub_signature"})
    s = narrow("share")
    add(
        "narrow_changed_after_signing",
        {"o": {**s["o"], "id": SHARE_B}, "sig": s["sig"]},
        {"error": "bad_hub_signature"},
    )
    add("narrow_extra_member", narrow("share", dev=True), {"error": "malformed"})
    add("narrow_missing_member", narrow("share", nonce=G.DROP), {"error": "malformed"})
    add("narrow_bad_id", narrow("share", id_="not-hex"), {"error": "malformed"})
    add("narrow_short_nonce", narrow("share", nonce=b64u(b"short")), {"error": "malformed"})
    add("narrow_wrong_suite", narrow("share", suite=3), {"error": "wrong_suite"})
    add("narrow_wrong_kind", {"o": {**s["o"], "kind": "hub.snapshot"}, "sig": s["sig"]}, {"error": "malformed"})
    add("narrow_not_an_object", [s], {"error": "malformed"})
    return cases


def pass_obj(
    *,
    app: str = APP,
    ws: str = WS_A,
    hub: str = HUB,
    aud: str = "company",
    nonce: str | None = None,
    exp: int = NOW + 30_000,
    key: str = "hk_op",
    label: str = "sig_hub_app_pass",
    **kw: Any,
) -> dict[str, Any]:
    o = {
        **G.hdr("hub.app_pass"),
        "hub_id": hub,
        "workspace_id": ws,
        "app_id": app,
        "audience": aud,
        "nonce": nonce or n32("pass1"),
        "exp_ms": exp,
        **kw,
    }
    return G.signed(key, label, o)


def pass_cases() -> list[dict[str, Any]]:
    appr = {"id": APP, "workspace_id": WS_A, "access": "company", "audience": "company"}
    issued = {n32("pass1"): {"app_id": APP, "until_ms": NOW + 60_000}}
    cases: list[dict[str, Any]] = []

    def add(name: str, signed: Any, expect: dict[str, Any], **kw: Any) -> None:
        args = {
            **state(bindings={WS_A: [{"binding": BIND_SELECTIVE}], WS_B: [{"binding": BIND_B}]}),
            "app": appr,
            "issued": issued,
            "spent": [],
            "signed": signed,
            **kw,
        }
        cases.append({"name": name, "args": args, "expect": expect})

    okc = {"ok": {"app_id": APP, "nonce": n32("pass1"), "audience": "company"}}
    add("pass_company_app", pass_obj(), okc)
    add("pass_full_mode_workspace", pass_obj(), okc, bindings={WS_A: [{"binding": BIND_FULL}]})
    grp = {**appr, "access": "group", "audience": "group:marketing"}
    add(
        "pass_group_app", pass_obj(aud="group:marketing"), {"ok": {**okc["ok"], "audience": "group:marketing"}}, app=grp
    )
    wsa = {**appr, "access": "group", "audience": "ws:" + WS_A}
    add("pass_workspace_audience", pass_obj(aud="ws:" + WS_A), {"ok": {**okc["ok"], "audience": "ws:" + WS_A}}, app=wsa)
    add("pass_exactly_sixty_seconds", pass_obj(exp=NOW + 60_000), okc)
    add("pass_wrong_app", pass_obj(app=APP_2), {"error": "not_found"})
    add(
        "pass_nonce_issued_for_another_app",
        pass_obj(),
        {"error": "bad_nonce"},
        issued={n32("pass1"): {"app_id": APP_2, "until_ms": NOW + 60_000}},
    )
    add("pass_expired", pass_obj(exp=NOW - 1), {"error": "expired"})
    add("pass_expired_exactly_now", pass_obj(exp=NOW), {"error": "expired"})
    add("pass_nonce_expired", pass_obj(), {"error": "expired"}, issued={n32("pass1"): {"app_id": APP, "until_ms": NOW}})
    add("pass_longer_than_sixty_seconds", pass_obj(exp=NOW + 60_001), {"error": "pass_too_long"})
    add("pass_reused_nonce", pass_obj(), {"error": "replay"}, spent=[n32("pass1")])
    add("pass_unknown_nonce", pass_obj(nonce=n32("other")), {"error": "bad_nonce"})
    add("pass_other_hub", pass_obj(hub=G.ROGUE_HUB_ID), {"error": "unknown_hub"})
    add(
        "pass_workspace_bound_to_another_hub",
        pass_obj(),
        {"error": "other_hub"},
        bindings={WS_A: [{"binding": BIND_ROGUE}]},
    )
    add("pass_audience_mismatch", pass_obj(aud="group:sales"), {"error": "audience_mismatch"}, app=grp)
    add("pass_company_pass_for_a_group_app", pass_obj(aud="company"), {"error": "audience_mismatch"}, app=grp)
    add("pass_group_pass_for_a_company_app", pass_obj(aud="group:marketing"), {"error": "audience_mismatch"})
    add(
        "pass_for_a_public_app",
        pass_obj(),
        {"error": "not_company_app"},
        app={**appr, "access": "public", "audience": None},
    )
    add(
        "pass_for_a_password_app",
        pass_obj(),
        {"error": "not_company_app"},
        app={**appr, "access": "password", "audience": None},
    )
    add("pass_other_workspace", pass_obj(ws=WS_B), {"error": "other_workspace"})
    add("pass_no_binding", pass_obj(), {"error": "not_bound"}, bindings={})
    add(
        "pass_unbound",
        pass_obj(),
        {"error": "not_bound"},
        bindings={WS_A: [{"binding": BIND_SELECTIVE}, {"unbound": UNBOUND}]},
    )
    add("pass_signed_by_another_hub_key", pass_obj(key="hk_op_rogue"), {"error": "bad_hub_signature"})
    add("pass_signed_under_the_narrow_label", pass_obj(label="sig_hub_release_narrow"), {"error": "bad_hub_signature"})
    add("pass_with_a_user_id", pass_obj(user_id=G.U_ALICE), {"error": "malformed"})
    add("pass_with_a_person_id", pass_obj(person_id=G.P_ALICE), {"error": "malformed"})
    add("pass_bad_audience", pass_obj(aud="everyone"), {"error": "malformed"})
    add(
        "pass_missing_exp",
        {"o": {k: v for k, v in pass_obj()["o"].items() if k != "exp_ms"}, "sig": pass_obj()["sig"]},
        {"error": "malformed"},
    )
    add("pass_wrong_suite", pass_obj(suite=3), {"error": "wrong_suite"})
    add("pass_app_not_found", pass_obj(), {"error": "not_found"}, app=None)
    return cases


# ---- the file ---------------------------------------------------------------------------------------------------------


def labels() -> dict[str, str]:
    from orch_hub.contract.labels import HUB_LABELS

    keys = ("sig_hub_release_narrow", "sig_hub_app_pass", "h_publish_request_direct", "h_app_meta", "h_app_reach")
    return {k: HUB_LABELS[k] for k in keys}


def build() -> dict[str, Any]:
    doc = {
        "doc": "Published, shared links and apps (M4 #1357 S0): replay every section with your own copy of the rules",
        "card": G.card(),
        "now_ms": NOW,
        "ids": {
            "hub": HUB,
            "ws_a": WS_A,
            "ws_b": WS_B,
            "app": APP,
            "app_2": APP_2,
            "share": SHARE,
            "owner_person": G.P_ALICE,
        },
        "labels": labels(),
        "forbidden_origins": FORBIDDEN,
        "required": REQUIRED,
        "slug_cases": slug_cases(),
        "origin_cases": origin_cases(),
        "reach_cases": reach_cases(),
        "csp_cases": csp_cases(),
        "password_record_cases": password_record_cases(),
        "password_verify_cases": password_verify_cases(),
        "meta_cases": meta_cases(),
        "meta_hash_cases": meta_hash_cases(),
        "commit_cases": commit_cases(),
        "route_cases": route_cases(),
        "extend_cases": extend_cases(),
        "row_cases": row_cases(),
        "row_scope_cases": row_scope_cases(),
        "approval_cases": approval_cases(),
        "narrow_cases": narrow_cases(),
        "pass_cases": pass_cases(),
    }
    return doc


if __name__ == "__main__":
    out = Path(__file__).with_name("vectors_published.json")
    out.write_text(json.dumps(build(), indent=1, sort_keys=True) + "\n")
    sys.stdout.write(f"wrote {out}\n")
