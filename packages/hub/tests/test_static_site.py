"""The console origin serves only the allow-listed files plus /api/v1 JSON, with hard headers (design-a8 I7, A8-2/4/8)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from webapi_helpers import Web

from orch_hub.config import HubConfig
from orch_hub.contract import hub as H
from orch_hub.contract import suite as S
from orch_hub.contract.canon import unb64u
from orch_hub.webapi import api as A
from orch_hub.webapi.static import CSP, FILES, WEB_DIR

HOST = "127.0.0.1:9000"


@pytest.fixture
def w(tmp_path):
    web = Web(tmp_path)
    yield web
    web.close()


def get(w, path, method="GET", host=HOST, headers=None):
    h = {"host": host, **(headers or {})}
    s, hdrs, body = w.api.handle(method, path, h, b"", "")
    return s, dict(hdrs), body


def test_every_allow_listed_file_is_served_with_the_hard_headers(w):
    assert set(FILES) == {
        "/",
        "/c",
        "/app.css",
        "/app.js",
        "/channel.js",
        "/cj.js",
        "/render.js",
        "/home.js",
        "/inbox.js",
        "/route.js",
        "/admin.js",
        "/switch.js",
        "/ui.js",
        "/services.js",
        "/workspaces.js",
    }
    for path, (name, ctype) in FILES.items():
        s, h, body = get(w, path)
        assert s == 200 and body == (WEB_DIR / name).read_bytes(), path
        assert h["Content-Type"] == ctype
        assert h["Content-Security-Policy"] == CSP
        assert h["X-Content-Type-Options"] == "nosniff" and h["Referrer-Policy"] == "no-referrer"
        assert h["Cross-Origin-Resource-Policy"] == "same-origin" and h["Cross-Origin-Opener-Policy"] == "same-origin"
        assert h["Cache-Control"] == ("no-store" if path in ("/", "/c") else "no-cache")
        assert "Strict-Transport-Security" in h  # the test hub has secure cookies
    assert FILES["/app.js"][1] == "text/javascript; charset=utf-8"


def test_the_csp_has_no_unsafe_source_and_the_pieces_the_note_names():
    for d in (
        "default-src 'none'",
        "script-src 'self'",
        "worker-src 'none'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "connect-src 'self'",
        "require-trusted-types-for 'script'",
        "trusted-types 'none'",
    ):
        assert d in CSP.split("; "), d
    assert "unsafe" not in CSP and "*" not in CSP and "http" not in CSP and "data:" not in CSP


def test_hsts_only_when_the_cookie_is_secure(tmp_path):
    import dataclasses

    w = Web(tmp_path)
    try:
        loop = A.WebApi(dataclasses.replace(w.api.d, secure_cookies=False))
        _, h, _ = loop.handle("GET", "/", {"host": HOST}, b"", "")
        assert "Strict-Transport-Security" not in dict(h)
    finally:
        w.close()


def test_nothing_else_is_served_on_any_path_and_every_other_answer_is_json(w):
    for path in (
        "/app.js/",
        "/App.js",
        "/index.html",
        "/../etc/passwd",
        "/%2e%2e/x",
        "/app.js%00",
        "//app.js",
        "/web/app.js",
        "/static/app.js",
        "/favicon.ico",
        "/robots.txt",
        "/api",
        "/api/",
        "/api/v1",
        "/api/v1/",
        "/api/v2/session",
        "/wiki",
        "/addons/x",
        "/uploads/a.html",
        "/channel.js.map",
    ):
        s, h, body = get(w, path)
        assert s == 404 and h["Content-Type"] == "application/json" and h["X-Content-Type-Options"] == "nosniff", path
        assert json.loads(body) == {"error": "unknown_op"}
    for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
        s, h, body = get(w, "/", method=m)
        assert s == 404 and h["Content-Type"] == "application/json", m


def test_every_api_route_answers_application_json(w):
    for rt in A.ROUTES:
        path = re.sub(r"\{(\w+)\}", "a" * 32, rt.path)
        _s, hdrs, body = w.api.handle(rt.method, path, {"host": HOST}, b"", "")
        h = dict(hdrs)
        assert h["Content-Type"] == "application/json" and h["X-Content-Type-Options"] == "nosniff", rt.path
        json.loads(body)


def test_static_files_pass_the_same_host_check_as_the_api(w):
    for host in ("evil.example", "", "127.0.0.1:9001", "127.0.0.1:9000.evil.example"):
        s, _, body = get(w, "/", host=host)
        assert (s, json.loads(body)) == (403, {"error": "not_allowed"}), host
        s, _, body = get(w, "/app.js", host=host)
        assert s == 403


def test_head_is_allowed_and_a_query_string_does_not_widen_the_set(w):
    assert get(w, "/", method="HEAD")[0] == 200
    assert get(w, "/app.js?v=1")[0] == 200


def test_the_web_dir_holds_exactly_the_served_files_and_the_lint_roots():
    on_disk = {p.name for p in WEB_DIR.iterdir() if p.is_file()}
    assert on_disk == {n for n, _ in FILES.values()}  # nothing in the package that is not on the list


def test_the_html_loads_only_its_own_modules_and_has_no_inline_code():
    html = (WEB_DIR / "index.html").read_text()
    assert re.findall(r"<script[^>]*>", html) == ['<script type="module" src="/app.js">']
    assert "style=" not in html and "<style" not in html and not re.search(r"\son\w+=", html)
    assert "http://" not in html and "https://" not in html


# --- startup refusal (A8-4) ----------------------------------------------------------------------------------------


def test_behind_the_edge_the_origin_is_exactly_https_host():
    ok = {
        "dev": True,
        "listen": "127.0.0.1:0",
        "web_listen": "127.0.0.1:0",
        "behind_edge": True,
        "trusted_peers": frozenset({"10.0.0.1"}),
    }
    HubConfig(**ok, allowed_origins=frozenset({"https://hub.example"}))
    HubConfig(**ok, allowed_origins=frozenset({"https://hub.example:8443"}))
    for bad in (
        frozenset(),
        frozenset({"http://hub.example"}),
        frozenset({"https://hub.example/"}),
        frozenset({"https://hub.example/x"}),
        frozenset({"https://u@hub.example"}),
        frozenset({"hub.example"}),
        frozenset({"https://hub.example", "http://x.example"}),
        frozenset({"https://"}),
    ):
        with pytest.raises(ValueError):
            HubConfig(**ok, allowed_origins=bad)


# --- the browser's own signatures verify under the hub's verifier (cross-language) ----------------------------------

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_webcrypto_proof_and_registration_verify_under_the_hubs_verifier():
    out = subprocess.run(
        [NODE, str(Path(__file__).parent / "js" / "emit_proof.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    r = json.loads(out.stdout)
    key = unb64u(r["key_pub"], S.PUB_LEN)
    assert H.channel_id(key) == r["channel_id"]  # the browser derives the same channel id
    assert S.verify(key, unb64u(r["proof"], S.SIG_LEN), H.channel_proof_bytes(bytes.fromhex(r["digest"])))
    from orch_hub.contract.canon import cj

    reg = r["reg"]
    assert S.verify(key, unb64u(reg["sig"], S.SIG_LEN), H.L["sig_hub_channel_register"] + cj(reg["o"]))


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_node_unit_tests_run_green():
    files = sorted(str(p) for p in (Path(__file__).parent / "js").glob("*.test.mjs"))
    r = subprocess.run([NODE, "--test", *files], check=False, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
