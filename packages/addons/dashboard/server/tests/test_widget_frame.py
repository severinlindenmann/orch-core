"""Widgets task 7: the locked `/widget-frame/` route, its headers, `widgets.catalog` and the narrowed parent CSP."""

import base64
import hashlib
import re

import pytest
from conftest import ORIGIN, PORT

from orch_dashboard import widget_frame
from orch_dashboard.gateway import Gateway

IFRAME = {"Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "same-origin"}
LINE = widget_frame.TEMPLATES[("line-chart", 1)]
FRAME_ANCESTORS = f"frame-ancestors http://127.0.0.1:{PORT}"


@pytest.fixture
def widgets_on(monkeypatch):
    state = {"on": True}
    real = Gateway.addon_on.__func__

    def addon_on(cls, link, name):
        return state["on"] if name == "widgets" else real(cls, link, name)

    monkeypatch.setattr(Gateway, "addon_on", classmethod(addon_on))
    return state


def b64(text):
    return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


def csp_of(res):
    values = res.headers.get_list("content-security-policy")
    assert len(values) == 1, "exactly one CSP header (two would both apply)"
    return values[0]


def directives(csp):
    return {part.split(" ", 1)[0]: (part.split(" ", 1) + [""])[1] for part in csp.split("; ")}


def test_template_digests_are_recomputed_from_the_shipped_bytes():
    # TODO(widgets task 8): the TS twin (`templateDigest` in widgetTemplates.ts) must produce these same values.
    files = sorted(widget_frame._TEMPLATES_DIR.glob("*@*.html"))
    assert len(files) == len(widget_frame.TEMPLATES) == len(widget_frame.META) == 6
    for f in files:
        name, _, version = f.stem.partition("@")
        t = widget_frame.TEMPLATES[(name, int(version))]
        assert t.digest == hashlib.sha256(f.read_bytes() + b"\0" + b"[]").hexdigest()
        assert t.page == f.read_text("utf-8")
        assert t.path == f"/widget-frame/{name}@{version}/{t.digest}"


def test_every_template_script_has_its_hash_and_the_page_is_lint_clean():
    banned = re.compile(
        r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|Function\(|setTimeout\(\s*['\"`]|srcdoc|<a[ >]"
        r"|location|\bopen\(|fetch\(|XMLHttpRequest|WebSocket|sendBeacon|importScripts|import\("
    )
    for t in widget_frame.TEMPLATES.values():
        scripts = re.findall(r"<script>(.*?)</script>", t.page, re.S)
        assert scripts and t.page.count("<script") == len(scripts)
        assert t.hashes == tuple("'sha256-" + b64(s) + "'" for s in (widget_frame.RUNTIME, *scripts))
        assert not banned.search(t.page), t.name
        assert "orch.data" in t.page


def test_the_catalog_lists_the_templates_with_their_digests(client, token, widgets_on, stub):
    res = client.get(f"/api/w/{stub.ws}/widgets/catalog", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True and body["enabled"] is True
    rows = {r["ref"]: r for r in body["templates"]}
    assert sorted(rows) == [f"{n}@1" for n in sorted(widget_frame.META)]
    for t in widget_frame.TEMPLATES.values():
        r = rows[t.ref]
        assert (r["name"], r["version"], r["sha256"], r["frame"]) == (t.name, t.version, t.digest, t.path)
        assert r["min_height"] == t.min_height and r["title"] and r["description"]
    widgets_on["on"] = False
    off = client.get(f"/api/w/{stub.ws}/widgets/catalog", headers={"Authorization": f"Bearer {token}"}).json()
    assert off["enabled"] is False and len(off["templates"]) == 6  # the gallery is public shipped text


def test_the_catalog_needs_a_session(client, stub):
    res = client.get(f"/api/w/{stub.ws}/widgets/catalog")
    assert res.status_code == 401


def test_an_exact_hit_answers_with_exactly_the_frame_headers(client, widgets_on):
    res = client.get(LINE.path, headers=IFRAME)
    assert res.status_code == 200
    assert res.content == LINE.document
    assert res.headers["content-type"] == "text/html; charset=utf-8"
    assert res.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "x-frame-options" not in res.headers
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["referrer-policy"] == "no-referrer"
    assert res.headers["cross-origin-resource-policy"] == "same-origin"
    csp = csp_of(res)
    assert csp == widget_frame.frame_csp(LINE.path, PORT)
    d = directives(csp)
    assert csp.startswith("sandbox allow-scripts; default-src 'none'; ")
    assert d["script-src"] == " ".join(LINE.hashes) and "unsafe-inline" not in d["script-src"]
    assert "unsafe-eval" not in csp
    assert d["style-src"] == "'unsafe-inline'" and d["img-src"] == "data:"
    for name in (
        "default-src connect-src frame-src child-src worker-src object-src manifest-src font-src base-uri form-action"
    ).split():
        assert d[name] == "'none'", name
    assert d["frame-ancestors"] == f"http://127.0.0.1:{PORT}" and csp.endswith(FRAME_ANCESTORS)
    # every inline script of the served document is one of the hashed ones
    inline = re.findall(r"<script>(.*?)</script>", res.text, re.S)
    assert inline[0] == widget_frame.RUNTIME or widget_frame.RUNTIME in inline
    assert all(f"'sha256-{b64(s)}'" in d["script-src"] for s in inline)  # the page's own sit, inert, in the <template>
    assert res.text.index("<template") < res.text.index("<script>" + widget_frame.RUNTIME)
    assert '<template id="orch-page">' in res.text and LINE.page in res.text


def test_a_browser_written_at_sign_is_the_one_accepted_escape(client, widgets_on):
    res = client.get(LINE.path.replace("@", "%40"), headers=IFRAME)
    assert res.status_code == 200 and res.content == LINE.document
    assert "sec-fetch-dest" in res.headers["vary"].lower()


def test_the_global_csp_is_replaced_not_added_to(client, widgets_on):
    res = client.get(LINE.path, headers=IFRAME)
    assert len(res.headers.get_list("content-security-policy")) == 1
    assert "frame-ancestors 'none'" not in str(res.headers)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda t: t.path + "?x=1", id="query"),
        pytest.param(lambda t: t.path + "/", id="trailing-slash"),
        pytest.param(lambda t: t.path[:-1] + ("0" if t.digest[-1] != "0" else "1"), id="wrong-digest"),
        pytest.param(lambda t: t.path.replace("line-chart@1", "line-chart@2"), id="unknown-version"),
        pytest.param(lambda t: t.path.replace("line-chart@1", "no-such@1"), id="unknown-name"),
        pytest.param(lambda t: t.path.replace("/widget-frame/", "/widget-frame//"), id="double-slash"),
        pytest.param(lambda t: t.path + "/x", id="extra-segment"),
        pytest.param(lambda t: t.path.replace("@1", "@01"), id="leading-zero-version"),
        pytest.param(lambda t: t.path.replace("@1/", "%401%2f"), id="encoded-separator"),
        pytest.param(lambda t: t.path + "%0a", id="trailing-newline"),
        pytest.param(lambda t: t.path.replace("line-chart", "line%2dchart"), id="encoded-name"),
        pytest.param(lambda t: "/widget-frame/line-chart@1", id="no-digest"),
        pytest.param(lambda t: "/widget-frame/", id="prefix-only"),
    ],
)
def test_anything_but_the_exact_pin_is_a_404_without_a_redirect(client, widgets_on, mutate):
    res = client.get(mutate(LINE), headers=IFRAME, follow_redirects=False)
    assert res.status_code == 404, mutate(LINE)
    assert "location" not in res.headers
    assert res.headers["cache-control"] == "no-store"
    assert "x-frame-options" not in res.headers
    assert "unsafe-inline'" not in directives(csp_of(res)).get("script-src", "")
    # a 404 (JSON, no script) carries no script source, or at most the pinned hashes when only the query was wrong
    assert all(w == "'none'" or w.startswith("'sha256-") for w in directives(csp_of(res))["script-src"].split())


def test_nothing_under_the_prefix_ever_redirects(client, widgets_on):
    paths = [
        "/widget-frame",
        "/widget-frame/",
        "/widget-frame/a",
        "/widget-frame/a/b/c/d",
        LINE.path + "/",
        LINE.path + "//",
        "/widget-frame/%2e%2e/",
    ]
    for path in paths:
        for headers in (IFRAME, {}, {"Sec-Fetch-Dest": "document"}):
            res = client.get(path, headers=headers, follow_redirects=False)
            assert res.status_code not in (301, 302, 303, 307, 308), (path, headers)
            assert "location" not in res.headers


@pytest.mark.parametrize("dest", ["document", "empty", "script", "frame", "embed", "object", "image", "style", ""])
def test_a_request_that_is_not_an_iframe_load_is_a_404(client, widgets_on, dest):
    res = client.get(LINE.path, headers={**IFRAME, "Sec-Fetch-Dest": dest})
    assert res.status_code == 404
    assert res.headers["cache-control"] == "no-store"
    assert LINE.page not in res.text


def test_top_level_navigation_and_missing_metadata_are_refused(client, widgets_on):
    nav = {"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none"}
    assert client.get(LINE.path, headers=nav).status_code == 404
    assert client.get(LINE.path).status_code == 404  # no Sec-Fetch-Dest at all: not provably an iframe, refused
    for site in ("cross-site", "same-site", "none"):
        assert client.get(LINE.path, headers={**IFRAME, "Sec-Fetch-Site": site}).status_code == 404
    assert (
        client.get(LINE.path, headers=[("Sec-Fetch-Dest", "iframe"), ("Sec-Fetch-Dest", "document")]).status_code == 404
    )


def test_with_widgets_off_the_frame_is_a_404(client, widgets_on):
    widgets_on["on"] = False
    res = client.get(LINE.path, headers=IFRAME)
    assert res.status_code == 404 and res.headers["cache-control"] == "no-store"
    assert LINE.page not in res.text
    widgets_on["on"] = True
    assert client.get(LINE.path, headers=IFRAME).status_code == 200


def test_a_wrong_host_is_refused_before_the_route(client, widgets_on):
    res = client.get(LINE.path, headers={**IFRAME, "Host": "localhost:" + str(PORT)})
    assert res.status_code == 421


def test_only_the_exact_hit_is_cacheable(client, widgets_on):
    for path in (LINE.path + "?a=b", LINE.path[:-1] + "0", "/widget-frame/x"):
        assert "immutable" not in client.get(path, headers=IFRAME).headers.get("cache-control", "")


def test_the_global_csp_is_unchanged_for_every_other_route(client, token, stub):
    for path in (
        "/",
        "/assets/main-hash.js",
        "/api/no-such-route",
        "/auth/probe?n=x",
        "/widget-frames",
        "/widget-frame",
    ):
        res = client.get(path)
        csp = csp_of(res)
        assert res.headers["x-frame-options"] == "DENY", path
        d = directives(csp)
        assert d["frame-ancestors"] == "'none'" and d["default-src"] == "'self'" and d["script-src"] == "'self'"
        assert "sandbox" not in d and "cross-origin-resource-policy" not in res.headers
    api = client.get(f"/api/w/{stub.ws}/widgets/catalog", headers={"Authorization": f"Bearer {token}"})
    assert api.headers["x-frame-options"] == "DENY" and directives(csp_of(api))["frame-ancestors"] == "'none'"


def test_the_parent_csp_narrows_frames_to_the_widget_path(client):
    res = client.get("/")
    d = directives(csp_of(res))
    assert d["frame-src"] == f"http://127.0.0.1:{PORT}/widget-frame/"
    assert "'self'" not in d["frame-src"] and "*" not in d["frame-src"]
    assert directives(csp_of(client.get("/", headers={"Sec-Fetch-Site": "none"})))["frame-src"] == d["frame-src"]
    assert ORIGIN == f"http://127.0.0.1:{PORT}"
