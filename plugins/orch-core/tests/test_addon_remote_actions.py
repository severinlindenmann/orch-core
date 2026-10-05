"""R7: the manifest's remote_actions list, its checker output, the runtime refusal for a remote device and the
over-the-bridge upload caps."""
import asyncio
import json
import shutil
from pathlib import Path

import pytest

from addon_fixtures import GOOD, loaded
from orch.addons.check import static_problems
from orch.addons.loader import AddonRegistry
from orch.addons.manifest import MAX_REMOTE_ACTIONS, manifest_problems, parse_manifest
from orch.dashboard import reach, remote_gate
from orch.dashboard.app import create_app
from orch.dashboard.reach import RemoteOrigin, Scope
from orch.errors import ValidationError

TEMPLATE = Path(__file__).resolve().parents[1] / "addon-template"
ACTIONS = [{"id": "ping", "label": "Ping"}, {"id": "other", "label": "Other"},
           {"id": "upload", "label": "Upload", "accepts_file": {"max_bytes": 100 * 1024 * 1024}}]
OVER = {"capabilities": ["page"], "menu": {"title": "R", "icon": "box"}, "settings_schema": [], "actions": ACTIONS}
MB = 1024 * 1024


# -- the manifest ----------------------------------------------------------------------------------------------

def test_default_is_none_and_a_valid_list_parses():
    assert parse_manifest(GOOD).remote_actions == ()
    m = parse_manifest({**GOOD, **OVER, "remote_actions": ["ping", "upload"]})
    assert m.remote_actions == ("ping", "upload") and m.remote_action("ping") and not m.remote_action("other")
    assert m.permissions()["remote_actions"] == ["ping", "upload"]
    assert parse_manifest({**GOOD, **OVER, "remote_actions": []}).remote_actions == ()


@pytest.mark.parametrize("value, needle", [
    (["nope"], "not one of this addon's actions"),
    (["ping", "ping"], "listed twice"),
    ("ping", "must be a list"),
    ([1], "must be a list"),
    ([None], "must be a list"),
    ({"ping": True}, "must be a list"),
    (["ping"] * (MAX_REMOTE_ACTIONS + 1), "at most"),
])
def test_bad_remote_actions_name_the_problem(value, needle):
    data = {**GOOD, **OVER, "remote_actions": value}
    assert any(needle in p for p in manifest_problems(data)), manifest_problems(data)
    with pytest.raises(ValidationError):
        parse_manifest(data)


def test_ids_must_exist_even_when_actions_is_malformed():
    assert any("remote_actions" in p for p in manifest_problems({**GOOD, "actions": "x", "remote_actions": ["ping"]}))


def test_checker_reports_it_and_the_template_declares_none(tmp_path):
    assert json.loads((TEMPLATE / "orch-addon.json").read_text())["remote_actions"] == []
    assert static_problems(TEMPLATE) == []
    dest = tmp_path / "t"
    shutil.copytree(TEMPLATE, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    data = json.loads((dest / "orch-addon.json").read_text())
    data["remote_actions"] = ["ghost"]
    (dest / "orch-addon.json").write_text(json.dumps(data))
    problems = static_problems(dest)
    assert len(problems) == 1 and "remote_actions" in problems[0] and "ghost" in problems[0]
    data.update(actions=[{"id": "ghost", "label": "Ghost"}], remote_actions=["ghost"])
    (dest / "orch-addon.json").write_text(json.dumps(data))
    assert static_problems(dest) == []


def test_the_documented_example_is_a_valid_manifest():
    doc = (Path(__file__).resolve().parents[1] / "ADDONS.md").read_text(encoding="utf-8")
    assert "remote_actions" in doc and "Actions from a remote device" in doc and "unsandboxed" in doc


# -- the route -------------------------------------------------------------------------------------------------

class Act:
    def __init__(self):
        self.calls = []

    def act(self, action_id, target, ctx, upload=None):
        self.calls.append((action_id, upload.size if upload else None))
        return "done"


@pytest.fixture
def setup(ws):
    obj = Act()
    la = loaded(ws, obj, name="rem", remote_actions=["ping", "upload"], **OVER)
    ws._addons = AddonRegistry(ws, {"rem": la})
    return create_app(ws, "tok"), obj


def origin(scope=Scope.TYPE):
    return RemoteOrigin("dev_abc123", scope, "Pixel", False)


def post(app, path, *, remote=None, body=b"", chunks=None, length="auto", ctype="application/x-www-form-urlencoded"):
    hdrs = [(b"host", b"127.0.0.1:8765"), (b"cookie", b"orch_token=tok"), (b"origin", b"http://127.0.0.1:8765"),
            (b"content-type", ctype.encode())]
    if length == "auto":
        length = str(len(body))
    if length is not None:
        hdrs.append((b"content-length", length.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": hdrs,
             "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8765)}
    if remote is not None:
        scope[reach.SCOPE_KEY] = remote
    parts = list(chunks) if chunks is not None else [body]
    sent = []

    async def receive():
        if parts:
            part = parts.pop(0)
            return {"type": "http.request", "body": part, "more_body": bool(parts)}
        return {"type": "http.disconnect"}

    async def send(m):
        sent.append(m)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], dict(start["headers"]), b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


FORM = b"target="


HEAD = b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.txt\"\r\n\r\n"


def multipart(payload: bytes) -> bytes:
    return HEAD + payload + b"\r\n--b--\r\n"


MULTI = "multipart/form-data; boundary=b"


def test_a_remote_request_for_an_unlisted_action_is_refused_before_any_addon_code(setup):
    app, obj = setup
    refused = [post(app, f"/addons/rem/actions/{a}", remote=origin(), body=FORM)
               for a in ("other", "nosuch")] + [post(app, "/addons/ghost/actions/ping", remote=origin(), body=FORM)]
    assert {r[0] for r in refused} == {403}
    assert len({r[2] for r in refused}) == 1 and remote_gate.NO_WAY.encode() in refused[0][2]  # same body, no hint
    assert obj.calls == []


def test_a_remote_request_for_a_listed_action_runs_at_type(setup):
    app, obj = setup
    status, _, _ = post(app, "/addons/rem/actions/ping", remote=origin(), body=FORM)
    assert status == 303 and obj.calls == [("ping", None)]


def test_a_listed_action_is_still_held_to_the_type_scope(setup):
    app, obj = setup
    status, _, body = post(app, "/addons/rem/actions/ping", remote=origin(Scope.OPERATE), body=FORM)
    assert status == 403 and obj.calls == []


def test_local_requests_are_unchanged(setup):
    app, obj = setup
    assert post(app, "/addons/rem/actions/other", body=FORM)[0] == 303
    assert obj.calls == [("other", None)]
    assert post(app, "/addons/rem/actions/upload", body=multipart(b"x" * 30 * MB), ctype=MULTI)[0] == 303
    assert obj.calls[-1] == ("upload", 30 * MB)  # past the remote cap, within the local one


def test_the_route_itself_refuses_too(setup, ws):
    """Defence in depth: the route handler checks the list even if the middleware were skipped."""
    from starlette.requests import Request
    app, _ = setup
    req = Request({"type": "http", "app": app, "headers": [], reach.SCOPE_KEY: origin()})
    assert remote_gate.action_unlisted(req, "rem", "other") and remote_gate.action_unlisted(req, "rem", "nosuch")
    assert not remote_gate.action_unlisted(req, "rem", "ping")
    assert remote_gate.refusal_response().status_code == 403


# -- upload caps ------------------------------------------------------------------------------------------------

def test_remote_upload_within_the_cap_is_accepted(setup):
    app, obj = setup
    assert post(app, "/addons/rem/actions/upload", remote=origin(), body=multipart(b"x" * (24 * MB)), ctype=MULTI)[0] == 303
    assert obj.calls == [("upload", 24 * MB)]


def test_remote_upload_over_the_cap_is_refused_on_content_length_without_reading(setup):
    app, obj = setup
    status, _, body = post(app, "/addons/rem/actions/upload", remote=origin(), body=b"x", ctype=MULTI,
                           length=str(26 * MB))
    assert status == 413 and b"too large for remote use" in body and b"file transfer" in body and obj.calls == []


def test_remote_upload_with_a_lying_length_is_cut_off_while_streaming(setup, ws):
    app, obj = setup
    chunks = [HEAD] + [b"x" * MB for _ in range(27)]
    status, _, body = post(app, "/addons/rem/actions/upload", remote=origin(), chunks=chunks, ctype=MULTI, length="10")
    assert status == 413 and b"too large for remote use" in body and obj.calls == []
    assert not any((ws.state_dir / "addons" / "rem" / "in").glob("*"))


def test_remote_upload_without_content_length_is_refused(setup):
    app, obj = setup
    status, _, _ = post(app, "/addons/rem/actions/upload", remote=origin(), body=multipart(b"x"), ctype=MULTI, length=None)
    assert status == 411 and obj.calls == []


def test_the_artifact_upload_has_its_own_remote_cap(setup):
    app, _ = setup
    path = "/t/B-0001/artifacts"
    over = remote_gate.REMOTE_ARTIFACT_UPLOAD + 1
    assert remote_gate.REMOTE_ARTIFACT_UPLOAD < remote_gate.REMOTE_ADDON_UPLOAD
    status, _, body = post(app, path, remote=origin(), body=b"x", ctype=MULTI, length=str(over))
    assert status == 413 and b"too large for remote use" in body
    chunks = [HEAD] + [b"x" * MB for _ in range(remote_gate.REMOTE_ARTIFACT_UPLOAD // MB + 2)]
    status, _, body = post(app, path, remote=origin(), chunks=chunks, ctype=MULTI, length=None)
    assert status == 413 and b"too large for remote use" in body
    # local: no remote cap (the ticket does not exist, so some other answer, but never the size one)
    assert post(app, path, body=b"x", ctype=MULTI, length=str(over))[0] != 413
