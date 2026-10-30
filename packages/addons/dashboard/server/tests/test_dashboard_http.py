"""Section 0.8.6 item 7: what this HTTP process does and refuses, one check per rule."""

import pytest
from conftest import ORIGIN, PORT

CSP_DIRECTIVES = {
    "default-src": "'self'",
    "script-src": "'self'",
    "object-src": "'none'",
    "base-uri": "'none'",
    "frame-ancestors": "'none'",
    "form-action": "'self'",
}


def parse(csp):
    return {part.split(" ", 1)[0]: part.split(" ", 1)[1] for part in csp.split("; ")}


@pytest.mark.parametrize("path", ["/", "/assets/main-hash.js", "/api/no-such-route", "/auth/probe?n=x"])
def test_every_response_carries_the_hardening_headers(client, path):
    res = client.get(path)
    directives = parse(res.headers["content-security-policy"])
    for name, value in CSP_DIRECTIVES.items():
        assert directives[name] == value, name
    assert "unsafe-inline" not in res.headers["content-security-policy"] and "unsafe-eval" not in str(directives)
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["referrer-policy"] == "no-referrer"


def test_session_and_launch_responses_are_not_cached_and_set_no_cookie(client, auth):
    res = client.post("/auth/session", json={"code": auth.launch(PORT)["code"]}, headers={"Origin": ORIGIN})
    assert res.headers["cache-control"] == "no-store" and "set-cookie" not in res.headers
    assert (
        client.post("/auth/session", json={"code": "x" * 43}, headers={"Origin": ORIGIN}).headers["cache-control"]
        == "no-store"
    )
    assert client.get("/api/session").headers["cache-control"] == "no-store"


def test_there_is_no_cors(client, token):
    pre = client.options(
        "/api/workspaces", headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "GET"}
    )
    assert not [h for h in pre.headers if h.lower().startswith("access-control-")]
    res = client.get("/api/workspaces", headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.test"})
    assert res.status_code == 403  # a foreign Origin is refused outright, not answered with CORS headers


@pytest.mark.parametrize("origin", [None, "null", "http://localhost:43210", "http://127.0.0.1:1", "https://evil.test"])
def test_a_mutation_needs_the_exact_origin(client, token, stub, origin):
    headers = {"Authorization": f"Bearer {token}"} | ({"Origin": origin} if origin else {})
    res = client.post(f"/api/w/{stub.ws}/click/cancel", json={"request_id": "hr_" + "0" * 26}, headers=headers)
    assert res.status_code == 403 and res.json()["error"]["code"] == "origin.bad"


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none"])
def test_fetch_metadata_is_enforced_when_present(client, token, site):
    headers = {"Authorization": f"Bearer {token}", "Sec-Fetch-Site": site}
    assert client.get("/api/workspaces", headers=headers).status_code == 403
    assert (
        client.post("/auth/session", json={"code": "x" * 43}, headers=headers | {"Origin": ORIGIN}).status_code == 403
    )
    assert client.get("/api/workspaces", headers=headers | {"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_mutations_are_bounded_json(client, token, stub):
    headers = {"Authorization": f"Bearer {token}", "Origin": ORIGIN}
    url = f"/api/w/{stub.ws}/click/view"
    assert client.post(url, content="request_id=1", headers=headers).status_code == 400  # not JSON
    assert client.post(url, content="{}", headers={**headers, "content-type": "text/plain"}).status_code == 400
    assert (
        client.post(url, content=b"{" * 20_000, headers={**headers, "content-type": "application/json"}).status_code
        == 413
    )
    assert (
        client.post(
            "/auth/session", content=b"x" * 2000, headers={**headers, "content-type": "application/json"}
        ).status_code
        == 413
    )


def test_the_bearer_is_only_taken_from_the_authorization_header(client, token):
    assert client.get(f"/api/workspaces?token={token}").status_code == 401
    assert client.get("/api/workspaces", cookies={"orch-session": token, "token": token}).status_code == 401
    assert client.get("/api/workspaces", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_launch_codes_are_single_use(client, auth):
    code = auth.launch(PORT)["code"]
    assert client.post("/auth/session", json={"code": code}, headers={"Origin": ORIGIN}).status_code == 200
    assert client.post("/auth/session", json={"code": code}, headers={"Origin": ORIGIN}).status_code != 200


def test_no_url_prepares_a_request(client, token, stub):
    """A GET never prepares or opens anything: the click routes are POST only."""
    headers = {"Authorization": f"Bearer {token}"}
    for step in ("prepare", "view", "sign", "cancel"):
        res = client.get(f"/api/w/{stub.ws}/click/{step}?request_id=hr_x&op=approve", headers=headers)
        assert res.status_code in (404, 405)


@pytest.mark.parametrize("site", ["none", "same-origin", "same-site", "cross-site", None, "unknown", ""])
@pytest.mark.parametrize("path", ["/", "/w/DEMO/ticket/ACME-1", "/ticket/ACME-1"])
def test_shell_navigation_metadata(client, site, path):
    headers = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    if site is not None:
        headers["Sec-Fetch-Site"] = site
    res = client.get(path, headers=headers)
    allowed = site in (None, "none", "same-origin")
    assert res.status_code == 200
    assert '<script src="/assets/main-hash.js">' in res.text
    assert ('<meta name="orch-nav" content="foreign">' in res.text) == (not allowed)
    baseline = client.get("/")
    for name in (
        "content-security-policy",
        "x-frame-options",
        "x-content-type-options",
        "referrer-policy",
        "cache-control",
    ):
        assert res.headers[name] == baseline.headers[name]


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_cross_site_new_tab_cannot_bootstrap_a_session(client, token, stub, site):
    """The PoC gets a marked shell, but no server session without a launch code."""
    res = client.get(
        f"/w/{stub.ws}/ticket/ACME-1",
        headers={"Sec-Fetch-Site": site, "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"},
    )
    assert res.status_code == 200
    assert '<meta name="orch-nav" content="foreign">' in res.text and token not in res.text
    assert client.get("/api/session", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 401
    assert (
        client.post(
            "/auth/session",
            json={"code": "demo-not-a-real-code".ljust(43, "x")},
            headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        ).status_code
        == 401
    )


def test_file_launcher_foreign_shell_can_exchange_its_fragment_code(client, auth):
    code = auth.launch(PORT)["code"]
    res = client.get(
        f"/#launch={code}&to=%2F",
        headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"},
    )
    assert res.status_code == 200 and '<meta name="orch-nav" content="foreign">' in res.text
    assert code not in res.text
    exchanged = client.post(
        "/auth/session",
        json={"code": code},
        headers={
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        },
    )
    assert exchanged.status_code == 200
    token = exchanged.json()["token"]
    assert client.get("/api/session", headers={"Authorization": f"Bearer {token}"}).status_code == 200


@pytest.mark.parametrize("extra", [{}, {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}])
def test_shell_marks_foreign_even_without_document_metadata(client, extra):
    res = client.get("/", headers={"Sec-Fetch-Site": "same-site", **extra})
    assert res.status_code == 200 and '<meta name="orch-nav" content="foreign">' in res.text


@pytest.mark.parametrize("values", [["none", "none"], ["same-origin", "cross-site"], ["none, same-origin"]])
def test_duplicate_or_joined_metadata_is_foreign(client, values):
    res = client.get("/", headers=[("Sec-Fetch-Site", value) for value in values])
    assert res.status_code == 200 and '<meta name="orch-nav" content="foreign">' in res.text


def test_a_shell_without_a_head_tag_fails_closed_for_foreign_loads(gateway, auth, tmp_path):
    """Opus #444 re-check R-1: if the marker can't be placed, a foreign load must not get an unmarked shell."""
    from fastapi.testclient import TestClient

    from orch_dashboard.app import create_app

    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><html><body>no head</body></html>", encoding="utf-8")
    with TestClient(create_app(PORT, gateway, auth, static), base_url=ORIGIN) as c:
        assert c.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 500
        assert c.get("/", headers={"Sec-Fetch-Site": "none"}).status_code == 200


def test_the_real_built_shell_has_a_literal_head_tag():
    """The Vite build must keep a literal <head> so the foreign marker can be placed (checked where the UI is built)."""
    from pathlib import Path

    built = Path(__file__).resolve().parents[1] / "src/orch_dashboard/static/index.html"
    if not built.is_file():
        pytest.skip("the UI is not built here")
    assert "<head>" in built.read_text(encoding="utf-8")


def test_loading_css_is_served_as_css_with_the_csp(gateway, auth, tmp_path):
    """B12A N6: the boot shell's stylesheet answered 404 JSON, so the blank-page guard never applied."""
    from fastapi.testclient import TestClient

    from orch_dashboard.app import create_app

    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html><head></head></html>", encoding="utf-8")
    (static / "loading.css").write_text(".boot{display:flex}", encoding="utf-8")
    with TestClient(create_app(PORT, gateway, auth, static), base_url=ORIGIN) as c:
        res = c.get("/loading.css")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/css")
        assert "style-src 'self'" in res.headers["content-security-policy"]
        assert res.headers["x-content-type-options"] == "nosniff"
        assert c.get("/other.css").status_code == 404
        (static / "loading.css").unlink()
        assert c.get("/loading.css").status_code == 404  # missing file: the closed 404, never index.html


def test_theme_boot_js_is_served_as_javascript_with_nosniff(gateway, auth, tmp_path):
    """#421: the pre-paint theme script is a root file; a wrong type or no nosniff blocks it under the CSP."""
    from fastapi.testclient import TestClient

    from orch_dashboard.app import create_app

    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html><head></head></html>", encoding="utf-8")
    (static / "theme-boot.js").write_text("(function(){})()", encoding="utf-8")
    with TestClient(create_app(PORT, gateway, auth, static), base_url=ORIGIN) as c:
        res = c.get("/theme-boot.js")
        assert res.status_code == 200
        assert "javascript" in res.headers["content-type"]
        assert res.headers["x-content-type-options"] == "nosniff"
        assert "script-src 'self'" in res.headers["content-security-policy"]


def test_every_root_file_the_built_shell_references_is_served(client):
    """B12A N6 / old 32: each root-level href/src of the built index.html is a route (needs the built UI)."""
    import re
    from pathlib import Path

    built = Path(__file__).resolve().parents[1] / "src/orch_dashboard/static/index.html"
    if not built.is_file():
        pytest.skip("the UI is not built here")
    refs = re.findall(r'(?:href|src)="(/[^"/][^"]*)"', built.read_text(encoding="utf-8"))
    assert refs
    from orch_dashboard.app import ROOT_FILES

    for ref in refs:
        if not ref.startswith("/assets/"):
            assert ref.lstrip("/") in ROOT_FILES, ref
