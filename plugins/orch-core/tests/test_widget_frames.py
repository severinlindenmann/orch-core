"""The agent-HTML layer (docs/widgets.md, "The frame", "Templates", "One-off pages"): frame documents at /w/, the
preview route, digests, images as data: URIs, widgets.html off, `orch widget promote` and `list`, the library page."""
import base64
import hashlib
import json
import re

import pytest

from orch import actor
from orch.cli import run

pytestmark = pytest.mark.usefixtures("html_on")  # agent HTML on, signed (#9)

pytest.importorskip("fastapi")

F = "```"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
MERMAID = {"source": "flowchart LR\n  a --> b", "alt": "a then b"}


def fence(obj) -> str:
    return f"{F}orch\n{json.dumps(obj)}\n{F}"

def pin_t(ws, obj: dict) -> dict:
    """`obj` with the template pin `orch widget add` would write (registry.template_digest of the version now)."""
    from orch.widgets import registry
    _, current = registry.template_state(ws.home, obj["widget"], None)
    return {**obj, "sha256": current or "0" * 64}  # no such template: a well-formed pin, so "no template" shows



def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def art(ws, tid, name, content: bytes):
    folder = ws.artifacts_dir / tid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(content)
    return f"artifacts/{tid}/{name}"


def findings(ws, tid, text):
    """Set a ticket's Findings straight on disk (blocks naming its artifacts need the id first)."""
    from orch.core import store
    t = store.load(ws, tid)[1]
    t.set_section("Findings", text)
    store.save(ws, t)


# -- /w/<ID>/<key> -----------------------------------------------------------------------------------------------

def test_frame_route_headers_nonce_and_auth(dash, put, ws, wurl):
    tid = put("in-progress", sections={"Findings": fence(pin_t(ws, {"widget": "mermaid@1", "data": MERMAID, "id": "flow"}))})
    r = dash.get(wurl(tid, "flow") + "?n=abcdefgh12")
    assert r.status_code == 200
    assert r.headers["content-security-policy"].startswith("sandbox allow-scripts;")
    assert "frame-ancestors 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    doc = r.text
    assert '<meta name="orch-frame" content="abcdefgh12">' in doc and "script-src 'unsafe-inline'" in doc
    assert "window.orch" in doc and "a then b" in doc and "mermaid" in doc
    assert doc.index("window.orch") < doc.index('id="orch-data"')
    for bad in ("short", "has space1", "x" * 65, "<script>1234"):
        assert dash.get(wurl(tid, "flow"), params={"n": bad}).status_code == 400, bad
    fresh = re.search(r'name="orch-frame" content="([\w-]+)"', dash.get(wurl(tid, "flow")).text)
    assert fresh and len(fresh.group(1)) >= 8  # no ?n=: a nonce of its own (a saved document)
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    stranger = TestClient(create_app(ws, "tok"))
    locked = stranger.get(wurl(tid, "flow") + "?n=abcdefgh12")
    assert locked.status_code == 401 and "window.orch" not in locked.text
    assert locked.headers["content-security-policy"].startswith("sandbox")


def test_ticket_page_loads_the_host_script_and_frames_carry_the_nonce(dash, put, ws):
    tid = put("in-progress", sections={"Findings": fence(pin_t(ws, {"widget": "mermaid@1", "data": MERMAID, "title": "Flow"}))})
    body = dash.get(f"/t/{tid}").text
    assert re.search(r'<script src="/static/widgets/orch-frames\.js[^"]*" defer></script>', body)
    assert re.search(r'href="/static/widgets/orch-frames\.css', body)
    m = re.search(rf'data-doc-url="/w/{tid}/Findings/[0-9a-f]{{64}}\?n=([\w-]+)" data-nonce="([\w-]+)"', body)
    assert m and m.group(1) == m.group(2) and 'data-title="Flow"' in body
    assert "<script>" not in body  # script-src 'self': nothing inline


def test_template_data_is_checked_against_its_version(dash, put, ws, wurl):
    tid = put("in-progress", sections={"Findings": fence(pin_t(ws, {"widget": "mermaid@1", "data": {"src": "x"}}))})
    assert "Widget not shown" in dash.get(f"/t/{tid}").text
    doc = dash.get(wurl(tid, "0")).text
    assert "window.orch" not in doc and "Widget not shown" in doc


def test_one_off_digest_states(dash, put, ws, wurl):
    tid = put("in-progress")
    ref = art(ws, tid, "replay.html", b"<p id=r>replay</p>")
    good, stale = sha(b"<p id=r>replay</p>"), "0" * 64
    findings(ws, tid, "\n\n".join([
        fence({"html": ref, "sha256": good, "id": "ok", "data": {"n": 1}}),
        fence({"html": ref, "sha256": stale, "id": "changed"}),
        fence({"html": f"artifacts/{tid}/gone.html", "sha256": good, "id": "gone"}),
        fence({"html": ref, "sha256": good, "id": "badlib", "libs": ["nope"]})]))
    page = dash.get(f"/t/{tid}").text
    assert "changed since this widget was written" in page and "is missing" in page
    ok = dash.get(wurl(tid, "ok") + "?n=abcdefgh12").text
    assert "<p id=r>replay</p>" in ok and '{"n":1}' in ok
    changed = dash.get(wurl(tid, "changed") + "?n=abcdefgh12").text
    assert "<p id=r>replay</p>" not in changed and "window.orch" not in changed  # only the pinned page ever runs
    assert "it does not run" in changed
    gone = dash.get(wurl(tid, "gone") + "?n=abcdefgh12").text
    assert "window.orch" not in gone and "is missing" in gone
    badlib = dash.get(wurl(tid, "badlib") + "?n=abcdefgh12").text
    assert "window.orch" not in badlib and "widget not drawn" in badlib and "nope" in badlib


def test_images_named_by_digest_reach_the_frame_as_data_uris(dash, put, ws, wurl):
    folder = ws.home / "widgets" / "shot"
    folder.mkdir(parents=True)
    (folder / "widget.json").write_text(json.dumps({"name": "shot", "title": "Shot", "moment": "review", "versions": {
        "1": {"schema": {"type": "object", "additionalProperties": False, "required": ["img"],
                         "properties": {"img": {"type": "string", "pattern": "^data:image/"}}}}}}))
    (folder / "v1.html").write_text("<img id=i><script>orch.text('a shot');orch.ready()</script>")
    tid = put("in-progress")
    ref = art(ws, tid, "a.png", PNG)
    findings(ws, tid, fence(pin_t(ws, {"widget": "shot@1", "data": {"img": {"path": ref, "sha256": sha(PNG)}}})))
    assert "Widget not shown" not in dash.get(f"/t/{tid}").text
    doc = dash.get(wurl(tid, "0") + "?n=abcdefgh12").text
    assert '"img":"data:image/png;base64,' in doc and ref not in doc


def test_html_off_draws_no_frame_and_serves_no_script(put, configure, ws, wurl):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    tid = put("in-progress", sections={"Findings": fence(pin_t(ws, {"widget": "mermaid@1", "data": MERMAID,
                                                          "caption": "a then b"}))})
    client = TestClient(create_app(configure(widgets={"html": False}), "tok"))
    assert client.get("/?token=tok").status_code == 200
    page = client.get(f"/t/{tid}").text
    assert "Agent HTML is off" in page and "w-frame" not in page
    doc = client.get(wurl(tid, "0") + "?n=abcdefgh12").text
    assert "<script" not in doc and "a then b" in doc
    assert "<script" not in client.get("/w/preview/mermaid@1?n=abcdefgh12").text
    assert "w-frame" not in client.get("/widgets").text


# -- /w/preview and the library ---------------------------------------------------------------------------------

def test_preview_route(dash):
    r = dash.get("/w/preview/mermaid@1?n=abcdefgh12")
    assert r.status_code == 200 and r.headers["content-security-policy"].startswith("sandbox allow-scripts;")
    assert r.headers["cache-control"] == "no-store" and '<meta name="orch-frame" content="abcdefgh12">' in r.text
    assert "Illustrative example" in r.text and "window.orch" in r.text
    assert dash.get("/w/preview/mermaid@9").status_code == 404
    assert dash.get("/w/preview/nope@1").status_code == 404
    assert dash.get("/w/preview/mermaid@1?n=bad").status_code == 400


def test_library_page(dash, put, ws):
    tid = put("in-progress", sections={"Findings": fence(pin_t(ws, {"widget": "mermaid@1", "data": MERMAID})) + "\n\n"
                                       + fence({"type": "callout", "role": "ok", "text": "fine"})})
    body = dash.get("/widgets").text  # redirected to the tab
    assert 'data-tab="widgets"' in body and 'aria-current="true"' in body
    assert re.search(r'<script src="/static/widgets/orch-frames\.js', body)
    for name in ("mermaid", "before-after", "callout", "checks"):
        assert f'id="w-{name}"' in body, name
    mermaid = dash.get("/workspace?tab=widgets&w=mermaid").text
    assert re.search(r'data-doc-url="/w/preview/mermaid@1\?n=[\w-]+"', mermaid)
    side = mermaid[mermaid.index('aria-label="Selected widget"'):]
    assert "1 use" in side and f'href="/t/{tid}"' in side and "<code>source</code>" in side
    treemap = dash.get("/workspace?tab=widgets&w=bundle-treemap").text
    assert "Not used on any ticket yet." in treemap[treemap.index('aria-label="Selected widget"'):]
    callout = dash.get("/workspace?tab=widgets&w=callout").text
    side = callout[callout.index('aria-label="Selected widget"'):]
    assert 'class="w w-t-callout"' in side and "1 use" in side  # drawn inline from EXAMPLE


# -- CLI ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch, ws_root, capsys):
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    monkeypatch.setattr(actor, "is_interactive", lambda: False)

    def call(*args):
        capsys.readouterr()
        code = run(list(args))
        out = capsys.readouterr()
        return code, out.out, out.err
    return call


def test_promote_list_and_use(cli, aops, ws, tmp_path):
    t = aops.new("Replay")
    page = tmp_path / "replay.html"
    page.write_text("<div id=r></div><script>orch.text('r');orch.ready()</script>")
    aops.artifact_add(t.id, page)
    data = {"runs": [{"label": "a", "ms": 12.5, "ok": True}], "unit": "ms"}
    assert cli("widget", "add", t.id, "--section", "Findings", "--html", "replay.html", "--data", json.dumps(data))[0] == 0
    code, out, err = cli("widget", "promote", t.id, "replay.html", "--name", "replay", "--json")
    assert code == 0, err
    assert json.loads(out)["widget"] == "replay@1"
    folder = ws.home / "widgets" / "replay"
    spec = json.loads((folder / "widget.json").read_text())
    schema = spec["versions"]["1"]["schema"]
    assert schema["additionalProperties"] is False and schema["required"] == ["runs", "unit"]
    assert schema["properties"]["runs"]["items"]["properties"]["ms"] == {"type": "number"}
    import jsonschema
    jsonschema.Draft202012Validator(schema).validate(data)
    assert json.loads((folder / "example.json").read_text()) == {"1": data}
    assert (folder / "v1.html").read_text() == page.read_text()
    code, _, err = cli("widget", "promote", t.id, "replay.html", "--name", "replay")
    assert code != 0 and "already exists" in err
    assert cli("widget", "promote", t.id, "replay.html", "--name", "mermaid", "--version-bump")[0] != 0  # built-in
    assert cli("widget", "promote", t.id, "nope.html", "--name", "other")[0] != 0
    code, out, err = cli("widget", "promote", t.id, "replay.html", "--name", "replay", "--version-bump", "--json")
    assert code == 0, err
    assert json.loads(out)["version"] == "2" and (folder / "v2.html").is_file()
    assert set(json.loads((folder / "widget.json").read_text())["versions"]) == {"1", "2"}
    # use it twice, list counts it
    assert cli("widget", "add", t.id, "--section", "Findings", "--widget", "replay@1", "--data", json.dumps(data))[0] == 0
    assert cli("widget", "add", t.id, "--section", "Context", "--widget", "replay@2", "--data", json.dumps(data))[0] == 0
    bad = {"runs": [], "unit": 3}
    assert cli("widget", "add", t.id, "--section", "Findings", "--widget", "replay@1", "--data", json.dumps(bad))[0] != 0
    rows = {r["name"]: r for r in json.loads(cli("widget", "list", "--json")[1])}
    assert rows["replay"]["uses"] == 2 and rows["replay"]["uses_by_version"] == {"1": 1, "2": 1}
    assert rows["replay"]["tickets"] == [t.id] and rows["replay"]["unused_30_days"] is False
    assert rows["replay"]["versions"] == ["1", "2"] and rows["mermaid"]["unused_30_days"] is True
    _, text, _ = cli("widget", "list")
    assert re.search(r"replay\s+v1,2\s+2 uses", text) and re.search(r"mermaid\s+v1\s+0 uses\s+unused 30 days", text)


def test_promote_without_a_block_gets_an_open_schema(cli, aops, ws, tmp_path):
    t = aops.new("Plain")
    page = tmp_path / "p.html"
    page.write_text("<p>p</p>")
    aops.artifact_add(t.id, page)
    assert cli("widget", "promote", t.id, f"artifacts/{t.id}/p.html", "--name", "plain")[0] == 0
    spec = json.loads((ws.home / "widgets" / "plain" / "widget.json").read_text())
    assert spec["versions"]["1"]["schema"] == {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}
    assert cli("widget", "promote", t.id, "p.html", "--name", "Bad Name")[0] != 0
