import os
import shutil

import pytest

from addon_fixtures import loaded
from orch.addons.api import FileResult, Reveal
from orch.addons.loader import AddonRegistry
from orch.addons.widgets import Action, Card
from orch.core.events import read_events

ORIGIN = {"origin": "http://testserver"}
SECRET = "https://example.invalid/p/tok#key-shown-once"
OVER = {"capabilities": ["page"], "menu": {"title": "Files", "icon": "box"}, "settings_schema": [], "actions": [
    {"id": "upload", "label": "Upload", "accepts_file": {"max_bytes": 1024, "types": [".txt", "text/plain"]}},
    {"id": "download", "label": "Download"}, {"id": "link", "label": "Create link"}]}


class Files:
    def __init__(self):
        self.ctx, self.seen = None, []          # ctx: the addon's own AddonContext, set by the fixture

    def widgets(self, slot, view):
        return [Card("Files", (Action("upload", "Upload"), Action("download", "Download", "FILE7")))]

    def act(self, action_id, target, ctx, upload=None):
        if action_id == "upload":
            self.seen.append((upload.name, upload.size, upload.path.read_bytes(), oct(upload.path.stat().st_mode & 0o777)))
            self.upload_path = upload.path
            return "Uploaded"
        if action_id == "download":
            out = self.ctx.state_dir / "dl.txt"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("hello", encoding="utf-8")
            return FileResult(out, "report.txt", "text/plain")
        return Reveal("Public link", SECRET)


@pytest.fixture
def files(ws):
    obj = Files()
    la = loaded(ws, obj, name="files", **OVER)
    obj.ctx = la.ctx
    ws._addons = AddonRegistry(ws, {"files": la})
    return obj


@pytest.fixture
def client(ws, files, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c


def _up(client, name="notes.txt", body=b"abc", **kw):
    return client.post("/addons/files/actions/upload", headers=ORIGIN, follow_redirects=False,
                       files={"file": (name, body, "text/plain")}, data={"target": ""}, **kw)


def test_page_offers_a_file_input_for_upload_actions(client):
    html = client.get("/addons/files/").text
    form = html[html.index('action="/addons/files/actions/upload"') - 200:]
    assert 'enctype="multipart/form-data"' in form and 'type="file"' in form and 'accept=".txt,text/plain"' in form


def test_upload_reaches_act_as_a_private_temp_file_and_is_deleted(client, files, ws):
    r = _up(client)
    assert r.status_code == 303 and "Uploaded" in r.headers["location"]
    name, size, data, mode = files.seen[0]
    assert (name, size, data, mode) == ("notes.txt", 3, b"abc", "0o600")
    assert files.upload_path.parent == ws.state_dir / "addons" / "files" / "in"
    assert not files.upload_path.exists()


def test_upload_is_deleted_when_act_fails(client, files, ws):
    seen = []

    def boom(a, t, c, upload=None):
        seen.append(upload.path)
        raise RuntimeError("addon bug")
    files.act = boom
    r = _up(client)
    assert "err=" in r.headers["location"] and seen and not seen[0].exists()


def test_upload_name_is_sanitised(client, files):
    _up(client, name="../../orchestrator/tickets/x‮.txt")
    assert files.seen[0][0] == "x.txt"


@pytest.mark.parametrize("raw, want", [("a/b\\c.txt", "c.txt"), ("..", "upload"), ("", "upload"),
                                       ("re port\x00.txt", "re port.txt"), ("x<y>.txt", "x_y_.txt"),
                                       ("CON.txt", "_CON.txt"), ("con", "_con"), ("Nul.log", "_Nul.log"),
                                       ("COM1.txt", "_COM1.txt"), ("lpt9", "_lpt9"), ("console.txt", "console.txt")])
def test_safe_upload_name(raw, want):
    from orch.dashboard.addon_files import safe_upload_name
    assert safe_upload_name(raw) == want


def test_oversized_upload_leaves_nothing(client, files, ws):
    r = _up(client, body=b"x" * 2048)
    assert "too+large" in r.headers["location"] or "too%20large" in r.headers["location"]
    assert files.seen == [] and not any((ws.state_dir / "addons" / "files" / "in").glob("*"))


def test_wrong_type_is_refused(client, files):
    r = _up(client, name="x.exe")
    assert "err=" in r.headers["location"] and files.seen == []


def test_upload_without_a_file_is_refused(client, files):
    r = client.post("/addons/files/actions/upload", headers=ORIGIN, data={"target": ""}, follow_redirects=False)
    assert "err=" in r.headers["location"] and files.seen == []


def test_upload_without_length_is_refused(client):
    def body():
        yield b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.txt\"\r\n\r\nabc\r\n--b--\r\n"
    r = client.post("/addons/files/actions/upload", headers={**ORIGIN, "content-type": "multipart/form-data; boundary=b"},
                    content=body(), follow_redirects=False)
    assert r.status_code == 411


def test_upload_over_the_hard_cap_is_refused_before_parsing(client, files):
    from orch.dashboard.addon_files import MAX_UPLOAD
    r = client.post("/addons/files/actions/upload", follow_redirects=False, content=b"x",
                    headers={**ORIGIN, "content-type": "multipart/form-data; boundary=b",
                             "content-length": str(MAX_UPLOAD + 10 * 1024 * 1024)})
    assert r.status_code == 413 and files.seen == []


def test_upload_needs_same_origin(client, files):
    r = client.post("/addons/files/actions/upload", files={"file": ("a.txt", b"a", "text/plain")}, follow_redirects=False)
    assert r.status_code == 403 and files.seen == []


def _asgi_request(path, headers, body_chunks):
    """A raw ASGI call, bypassing httpx/TestClient (which would recompute Content-Length itself and refuse to
    send a lying one): the caller controls exactly what scope headers and receive() messages the app sees, the
    way a desynced proxy or a hostile client could."""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "", "scheme": "http",
        "server": ("testserver", 80), "client": ("127.0.0.1", 1234),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    chunks = list(body_chunks)

    async def receive():
        if chunks:
            chunk = chunks.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}
        return {"type": "http.disconnect"}

    messages = []

    async def send(message):
        messages.append(message)

    return scope, receive, send, messages


def _run_asgi(app, scope, receive, send):
    import asyncio
    asyncio.run(app(scope, receive, send))


def _status(messages):
    return next(m["status"] for m in messages if m["type"] == "http.response.start")


def test_transfer_encoding_on_a_multipart_action_is_refused(ws, files):
    from orch.dashboard.app import create_app
    app = create_app(ws, "tok")
    headers = {"host": "testserver", "cookie": "orch_token=tok", "origin": "http://testserver",
               "content-type": "multipart/form-data; boundary=b", "content-length": "10",
               "transfer-encoding": "chunked"}
    scope, receive, send, messages = _asgi_request("/addons/files/actions/upload", headers, [b"short"])
    _run_asgi(app, scope, receive, send)
    assert _status(messages) == 411
    assert files.seen == []


def test_body_bytes_past_the_actions_cap_are_refused_whatever_content_length_claims(ws, files):
    """`files`'s upload action caps at 1024 bytes (OVER). A request that declares a small Content-Length but
    actually streams far more over several ASGI messages must be aborted once the real total crosses the cap —
    the declared header is not the thing being trusted."""
    from orch.dashboard.app import create_app
    app = create_app(ws, "tok")
    boundary = b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.txt\"\r\n\r\n"
    chunks = [boundary] + [b"x" * 4096 for _ in range(40)] + [b"\r\n--b--\r\n"]  # ~164 KiB, past cap + slack
    headers = {"host": "testserver", "cookie": "orch_token=tok", "origin": "http://testserver",
               "content-type": "multipart/form-data; boundary=b", "content-length": "10"}
    scope, receive, send, messages = _asgi_request("/addons/files/actions/upload", headers, chunks)
    _run_asgi(app, scope, receive, send)
    assert _status(messages) == 413
    assert files.seen == []
    assert not any((ws.state_dir / "addons" / "files" / "in").glob("*"))


def test_accepts_file_manifest_validation():
    from addon_fixtures import GOOD
    from orch.addons.manifest import manifest_problems, parse_manifest
    good = {**GOOD, "actions": [{"id": "up", "label": "Up", "accepts_file": {"max_bytes": 209_715_200}}]}
    m = parse_manifest(good)
    assert m.action("up").accepts_file == (209_715_200, ())
    assert m.permissions()["uploads"] == ["up"]
    for bad in ({"max_bytes": 209_715_201}, {"max_bytes": 0}, {"max_bytes": "1"}, {"max_bytes": 1, "types": ["exe"]},
                {"max_bytes": 1, "other": 1}, {"types": [".pdf"]}, "yes"):
        data = {**GOOD, "actions": [{"id": "up", "label": "Up", "accepts_file": bad}]}
        assert any("accepts_file" in p for p in manifest_problems(data)), bad


def test_download_is_single_use(client, ws):
    r = client.post("/addons/files/actions/download", data={"target": "FILE7"}, headers=ORIGIN, follow_redirects=False)
    url = r.headers["location"]
    assert url.startswith("/addons/files/files/")
    got = client.get(url)
    assert got.status_code == 200 and got.content == b"hello"
    assert got.headers["content-disposition"].startswith("attachment;")
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-security-policy"] == "sandbox"
    assert got.headers["cache-control"] == "no-store"
    assert client.get(url).status_code == 404
    assert not any((ws.state_dir / "addons" / "files" / "out").glob("*"))


def test_head_does_not_consume_the_download_token(client, ws):
    """Whatever a HEAD request to the download route answers (FastAPI's default is 405, since the route is
    GET-only), it must never use up the token: a GET straight after must still see the file."""
    r = client.post("/addons/files/actions/download", data={"target": "FILE7"}, headers=ORIGIN, follow_redirects=False)
    url = r.headers["location"]
    client.head(url)
    got = client.get(url)
    assert got.status_code == 200 and got.content == b"hello"  # the HEAD above must not have used the token up
    assert client.get(url).status_code == 404


def test_download_token_is_bound_to_its_addon(client, ws):
    url = client.post("/addons/files/actions/download", data={"target": "FILE7"}, headers=ORIGIN,
                      follow_redirects=False).headers["location"]
    assert client.get(url.replace("/addons/files/", "/addons/other/")).status_code == 404
    assert client.get(url).status_code == 404          # the wrong-addon try used the token up
    assert not any((ws.state_dir / "addons" / "files" / "out").glob("*"))


def test_download_token_expires(client, ws):
    from orch.dashboard.addon_files import OneTimeStore
    t = [0.0]
    client.app.state.downloads = OneTimeStore(300, clock=lambda: t[0])
    url = client.post("/addons/files/actions/download", data={"target": "FILE7"}, headers=ORIGIN,
                      follow_redirects=False).headers["location"]
    t[0] = 301.0
    assert client.get(url).status_code == 404
    assert not any((ws.state_dir / "addons" / "files" / "out").glob("*"))


def test_expired_download_is_swept_without_a_further_request(client, ws):
    """A staged download nobody ever asks for again must not keep its file past the TTL: sweep() (run
    periodically from the dashboard's lifespan, not only lazily from the next put/pop) clears it."""
    from orch.dashboard.addon_files import OneTimeStore
    t = [0.0]
    store = OneTimeStore(300, clock=lambda: t[0])
    client.app.state.downloads = store
    client.post("/addons/files/actions/download", data={"target": "FILE7"}, headers=ORIGIN, follow_redirects=False)
    assert any((ws.state_dir / "addons" / "files" / "out").glob("*"))
    t[0] = 301.0
    store.sweep()
    assert not any((ws.state_dir / "addons" / "files" / "out").glob("*"))


def test_lifespan_sweeps_stores_periodically(ws, monkeypatch):
    import asyncio
    from orch.dashboard import app as app_module

    monkeypatch.setattr(app_module, "STORE_SWEEP_SECONDS", 0.01)
    app = app_module.create_app(ws, "tok")
    swept = []
    app.state.downloads.sweep = lambda: swept.append("downloads")
    app.state.reveals.sweep = lambda: swept.append("reveals")

    async def run():
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)

    asyncio.run(run())
    assert "downloads" in swept and "reveals" in swept


def test_file_result_outside_the_addon_state_dir_is_refused(client, files, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("no", encoding="utf-8")
    files.act = lambda a, t, c, upload=None: FileResult(outside, "s.txt")
    r = client.post("/addons/files/actions/download", data={"target": "x"}, headers=ORIGIN, follow_redirects=False)
    assert "err=" in r.headers["location"] and outside.exists()


def test_file_result_that_is_a_symlink_is_refused(client, files, ws, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("no", encoding="utf-8")
    link = files.ctx.state_dir / "link.txt"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, link)
    files.act = lambda a, t, c, upload=None: FileResult(link, "s.txt")
    r = client.post("/addons/files/actions/download", data={"target": "x"}, headers=ORIGIN, follow_redirects=False)
    assert "err=" in r.headers["location"] and outside.exists()


def test_upload_refused_when_the_in_folder_is_a_symlink(client, files, ws, tmp_path):
    base = ws.state_dir / "addons" / "files"
    base.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, base / "in")
    r = _up(client)
    assert "err=" in r.headers["location"] and files.seen == [] and not list(elsewhere.glob("*"))


def test_upload_refused_when_the_addons_root_is_a_symlink(client, files, ws, tmp_path):
    """`_io_dir` checks `base` (state_dir/addons/<name>) and its `in`/`out` subfolder for a symlink, but a
    swapped state_dir/addons itself would make every addon's path silently resolve somewhere else."""
    addons_root = ws.state_dir / "addons"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.rmtree(addons_root, ignore_errors=True)
    os.symlink(elsewhere, addons_root)
    r = _up(client)
    assert "err=" in r.headers["location"] and files.seen == [] and not list(elsewhere.glob("**/*"))


def test_io_dir_tightens_an_already_existing_folder(client, files, ws):
    """A pre-existing in/ folder (from before this permission was tightened, or just a loose umask) must end
    up at 0700 too: mkdir's mode= only applies when it creates the folder, not when exist_ok finds one."""
    d = ws.state_dir / "addons" / "files" / "in"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o755)
    _up(client)
    assert oct(d.stat().st_mode & 0o777) == "0o700"


def test_reveal_is_shown_once_and_never_logged(client, ws):
    r = client.post("/addons/files/actions/link", data={"target": "FILE7"}, headers=ORIGIN, follow_redirects=False)
    loc = r.headers["location"]
    assert SECRET not in loc
    first = client.get(loc)
    assert SECRET in first.text and first.headers["cache-control"] == "no-store"
    assert SECRET not in client.get(loc).text
    assert all(SECRET not in str(e.data) for e in read_events(ws))
    assert SECRET not in ws.addons.errors()


def test_startup_sweep_clears_leftovers(ws):
    from orch.dashboard.addon_files import sweep_addon_io
    for which in ("in", "out"):
        d = ws.state_dir / "addons" / "files" / which
        d.mkdir(parents=True)
        (d / "leftover").write_bytes(b"x")
    keep = ws.state_dir / "addons" / "files" / "cache.default.json"
    keep.write_text("{}", encoding="utf-8")
    assert sweep_addon_io(ws) == 2 and keep.exists()


def test_download_mime_is_sanitised(client, files):
    def act(a, t, c, upload=None):
        out = files.ctx.state_dir / "dl.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("x", encoding="utf-8")
        return FileResult(out, "a.txt", "text/html\r\nSet-Cookie: x=1")
    files.act = act
    url = client.post("/addons/files/actions/download", data={"target": "x"}, headers=ORIGIN,
                      follow_redirects=False).headers["location"]
    got = client.get(url)
    assert got.headers["content-type"] == "application/octet-stream" and "set-cookie" not in got.headers


# -- Final review: actions without accepts_file get a small body cap -------------------------------------------

def test_an_action_without_accepts_file_refuses_a_large_form_body(client, files):
    from orch.dashboard.app import SMALL_ACTION_BODY
    r = client.post("/addons/files/actions/link", headers=ORIGIN, follow_redirects=False,
                    data={"target": "", "pad": "x" * (SMALL_ACTION_BODY + 1)})
    assert r.status_code == 413
    assert client.post("/addons/files/actions/link", headers=ORIGIN, follow_redirects=False,
                       data={"target": ""}).status_code == 303


def test_an_action_without_accepts_file_refuses_a_multipart_body_past_the_small_cap(client, files):
    from orch.dashboard.app import SMALL_ACTION_BODY
    r = client.post("/addons/files/actions/link", headers=ORIGIN, follow_redirects=False,
                    files={"file": ("a.txt", b"x" * (SMALL_ACTION_BODY + 1), "text/plain")}, data={"target": ""})
    assert r.status_code == 413


def test_small_action_cap_counts_bytes_whatever_content_length_claims(ws, files):
    from orch.dashboard.app import SMALL_ACTION_BODY, create_app
    app = create_app(ws, "tok")
    chunks = [b"target=&pad="] + [b"x" * 4096 for _ in range(SMALL_ACTION_BODY // 4096 + 2)]
    headers = {"host": "testserver", "cookie": "orch_token=tok", "origin": "http://testserver",
               "content-type": "application/x-www-form-urlencoded", "content-length": "10"}
    scope, receive, send, messages = _asgi_request("/addons/files/actions/link", headers, chunks)
    _run_asgi(app, scope, receive, send)
    assert _status(messages) == 413
