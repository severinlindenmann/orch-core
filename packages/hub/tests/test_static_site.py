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
        "/oidc/callback",
        "/oidc.js",
        "/favicon.svg",
        "/app.css",
        "/app.js",
        "/channel.js",
        "/cj.js",
        "/render.js",
        "/connect.js",
        "/home.js",
        "/inbox.js",
        "/route.js",
        "/account.js",
        "/admin.js",
        "/changes.js",
        "/admin_reach.js",
        "/activity.js",
        "/switch.js",
        "/ui.js",
        "/feel.js",
        "/serverguide.js",
        "/services.js",
        "/workspaces.js",
        "/phones.js",
        "/service_actions.js",
        "/handoffs.js",
        "/invite.js",
        "/qr.js",
        "/totp.js",
        "/seal.js",
    }
    for path, (name, ctype) in FILES.items():
        s, h, body = get(w, path)
        disk = (WEB_DIR / name).read_bytes()
        assert s == 200 and (body == disk or (path in ("/", "/oidc/callback") and b'rel="modulepreload"' in body)), path
        assert h["Content-Type"] == ctype
        assert h["Content-Security-Policy"] == CSP
        assert h["X-Content-Type-Options"] == "nosniff" and h["Referrer-Policy"] == "no-referrer"
        assert h["Cross-Origin-Resource-Policy"] == "same-origin" and h["Cross-Origin-Opener-Policy"] == "same-origin"
        assert h["Cache-Control"] == "no-cache" and h["ETag"].startswith('"')  # revalidated every time, never stale
        assert "Strict-Transport-Security" in h  # the test hub has secure cookies
    assert FILES["/app.js"][1] == "text/javascript; charset=utf-8"


def test_the_etag_gives_a_304_and_changes_with_the_bytes(w):
    for path in ("/", "/app.js", "/app.css", "/pages.js"):
        s, h, body = get(w, path)
        tag = h["ETag"]
        assert s == 200 and body
        s2, h2, body2 = get(w, path, headers={"if-none-match": tag})
        assert (s2, body2) == (304, b"") and h2["ETag"] == tag, path
        assert h2["Content-Security-Policy"] == CSP and h2["Cache-Control"] == "no-cache"
        assert get(w, path, headers={"if-none-match": f'W/{tag}, "other"'})[0] == 304
        assert get(w, path, headers={"if-none-match": "*"})[0] == 304
        assert get(w, path, headers={"if-none-match": '"stale"'})[0] == 200
    a = get(w, "/app.js")[1]["ETag"]
    assert a != get(w, "/app.css")[1]["ETag"]


def test_the_etag_comes_from_the_file_bytes_so_an_update_is_seen(tmp_path):
    import shutil

    from orch_hub.webapi.static import StaticSite

    root = tmp_path / "web"
    shutil.copytree(WEB_DIR, root)
    before = StaticSite(hsts=False, root=root).get("/app.js")[1]
    before = dict(before)["ETag"]
    (root / "app.js").write_bytes((root / "app.js").read_bytes() + b"\n// changed\n")
    site = StaticSite(hsts=False, root=root)
    assert dict(site.get("/app.js")[1])["ETag"] != before
    assert site.get("/app.js", {"if-none-match": before})[0] == 200  # the old ETag no longer matches


def test_gzip_only_when_the_client_accepts_it(w):
    import gzip

    s, h, plain = get(w, "/app.js")
    assert s == 200 and "Content-Encoding" not in h and plain == (WEB_DIR / "app.js").read_bytes()
    assert h["Vary"] == "Accept-Encoding"
    s, hz, z = get(w, "/app.js", headers={"accept-encoding": "br, gzip;q=0.8"})
    assert s == 200 and hz["Content-Encoding"] == "gzip" and hz["Vary"] == "Accept-Encoding"
    assert gzip.decompress(z) == plain and len(z) < len(plain)
    assert hz["ETag"] != h["ETag"]  # the two bodies are two representations
    assert get(w, "/app.js", headers={"accept-encoding": "gzip", "if-none-match": hz["ETag"]})[0] == 304
    assert get(w, "/app.js", headers={"accept-encoding": "gzip;q=0"})[1].get("Content-Encoding") is None
    assert get(w, "/app.js", headers={"accept-encoding": "identity"})[1].get("Content-Encoding") is None
    # the security headers ride on the gzip answer too
    assert hz["Content-Security-Policy"] == CSP and hz["X-Content-Type-Options"] == "nosniff"


def test_the_shell_preloads_every_static_import_of_app_js(w):
    s, _, html = get(w, "/")
    links = re.findall(r'<link rel="modulepreload" href="([^"]+)">', html.decode())
    assert s == 200 and links[0] == "/app.js" and "/pages.js" in links
    served = set(FILES) | {"/pages.js"} | {"/pages/" + n for n in w.api._site.pages}
    # every module app.js imports statically (and the page modules behind /pages.js) is in the list, and nothing else
    app = (WEB_DIR / "app.js").read_text()
    for spec in re.findall(r'from "\./([a-z_]+\.js)"', app):
        assert "/" + spec in links, spec
    for n in w.api._site.pages:
        assert "/pages/" + n in links, n
    assert set(links) <= served and len(links) == len(set(links))
    # the other entry of the same shell and the connect page: the oidc return carries the list too
    assert b"modulepreload" in get(w, "/oidc/callback")[2]


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


def test_the_tab_icon_is_served_and_linked_and_favicon_ico_stays_unknown(w):
    s, h, body = get(w, "/favicon.svg")
    assert (
        s == 200
        and h["Content-Type"] == "image/svg+xml"
        and h["Content-Security-Policy"] == CSP
        and b"<script" not in body
    )
    for page in ("index.html", "connect.html"):
        assert '<link rel="icon" href="/favicon.svg" type="image/svg+xml">' in (WEB_DIR / page).read_text()
    assert get(w, "/favicon.ico")[0] == 404


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


# --- the pages/ folder (registry): one file per page, served by glob, nothing hand-kept --------------------------------------


def test_every_page_module_is_served_and_the_generated_list_names_exactly_those(w):
    from orch_hub.webapi.static import PAGE_FILE, page_files

    names = page_files()
    assert names and all(PAGE_FILE.fullmatch(n) for n in names)
    on_disk = sorted(
        p.name for p in (WEB_DIR / "pages").iterdir() if not p.name.startswith(".") and p.name != "__pycache__"
    )
    assert (
        names == on_disk
    )  # a file in the folder is served; nothing is served from the folder that is not a file in it
    for n in names:
        s, h, body = get(w, "/pages/" + n)
        assert s == 200 and body == (WEB_DIR / "pages" / n).read_bytes() and h["Content-Security-Policy"] == CSP
    s, h, body = get(w, "/pages.js")
    assert s == 200 and h["Content-Type"].startswith("text/javascript")
    text = body.decode()
    listed = sorted(__import__("re").findall(r'from "\./pages/([^"]+)"', text))
    assert listed == names and text.count("import ") == len(names)
    for bad in ("/pages/nope.js", "/pages/../app.js", "/pages/", "/pages"):
        assert get(w, bad)[0] != 200 or bad == "/pages.js"


def test_a_file_in_pages_that_is_not_a_page_module_name_fails_the_build(tmp_path):
    import shutil

    import pytest

    from orch_hub.webapi.static import StaticSite

    root = tmp_path / "web"
    shutil.copytree(WEB_DIR, root)
    for odd in ("Bad.js", "x.txt", "a_b.js"):
        (root / "pages" / odd).write_text("x")
        with pytest.raises(ValueError):
            StaticSite(hsts=False, root=root)
        (root / "pages" / odd).unlink()
    (root / "pages" / "sub").mkdir()
    with pytest.raises(ValueError):
        StaticSite(hsts=False, root=root)
    (root / "pages" / "sub").rmdir()
    assert StaticSite(hsts=False, root=root).pages == sorted(p.name for p in (root / "pages").iterdir())


def test_the_generated_pages_module_loads_and_lists_every_page(tmp_path):
    """The real module graph: /pages.js as served plus the page files, imported by node (as the browser does)."""
    import json
    import shutil
    import subprocess

    from orch_hub.webapi.static import page_files, pages_module

    shutil.copytree(WEB_DIR, tmp_path / "web")
    (tmp_path / "web" / "pages.js").write_bytes(pages_module(page_files()))
    out = subprocess.run(
        ["node", "-e", "import('./pages.js').then(m => console.log(JSON.stringify(m.PAGES.map(p => p.path))))"],
        cwd=tmp_path / "web",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert json.loads(out) == [n[:-3] for n in page_files()]


def test_dotfiles_and_pycache_in_pages_are_ignored_not_served(tmp_path):
    import shutil

    from orch_hub.webapi.static import StaticSite

    root = tmp_path / "web"
    shutil.copytree(WEB_DIR, root)
    base = StaticSite(hsts=False, root=root)
    (root / "pages" / ".DS_Store").write_text("x")
    (root / "pages" / ".hidden.js").write_text("export const page = {}")
    (root / "pages" / "__pycache__").mkdir()
    (root / "pages" / "__pycache__" / "x.pyc").write_text("x")
    site = StaticSite(hsts=False, root=root)  # no failure at start
    assert site.pages == base.pages
    for path in ("/pages/.DS_Store", "/pages/.hidden.js", "/pages/__pycache__"):
        assert site.get(path) is None
    assert b".hidden" not in site.get("/pages.js")[2]
