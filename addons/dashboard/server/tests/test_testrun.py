"""The tester's checklist store: notes and screenshots under <state>/testrun, behind the dashboard session."""

import base64
import json
import os

import pytest
from conftest import ORIGIN

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64
JPG = b"\xff\xd8\xff\xe0" + b"x" * 64
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"x" * 64
B64 = lambda raw: base64.b64encode(raw).decode()  # noqa: E731


@pytest.fixture
def h(token):
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN}


def put(client, h, step="B1.4", status="pass", note="fine", run="run2"):
    return client.put(f"/api/testrun/{run}/steps/{step}", json={"status": status, "note": note}, headers=h)


def upload(client, h, raw, type="image/png", step="C5"):
    return client.post(f"/api/testrun/run2/steps/{step}/images", json={"type": type, "data": B64(raw)}, headers=h)


def test_needs_a_session_for_every_route(client):
    for method, path in [
        ("get", "/api/testrun/run2"),
        ("put", "/api/testrun/run2/steps/A1"),
        ("post", "/api/testrun/run2/steps/A1/images"),
        ("get", "/api/testrun/run2/images/A1-0123456789ab.png"),
        ("delete", "/api/testrun/run2/images/A1-0123456789ab.png"),
    ]:
        assert getattr(client, method)(path, headers={"Origin": ORIGIN}).status_code == 401


def test_writes_need_the_origin_and_json(client, h):
    body = {"status": "pass", "note": ""}
    assert (
        client.put("/api/testrun/run2/steps/A1", json=body, headers={"Authorization": h["Authorization"]}).status_code
        == 403
    )
    res = client.put("/api/testrun/run2/steps/A1", content="x", headers={**h, "content-type": "text/plain"})
    assert res.status_code == 400
    assert (
        client.delete(
            "/api/testrun/run2/images/A1-0123456789ab.png", headers={"Authorization": h["Authorization"]}
        ).status_code
        == 403
    )


def test_notes_roundtrip_and_path_is_reported(client, h, gateway):
    assert client.get("/api/testrun/run2", headers=h).json()["notes"] == {}
    assert put(client, h, note="the page was blank").status_code == 200
    assert put(client, h, step="A1", status="fail_blocking", note="").status_code == 200
    out = client.get("/api/testrun/run2", headers=h).json()
    assert out["path"] == str(gateway.state / "testrun" / "run2")
    assert out["notes"]["B1.4"]["note"] == "the page was blank"
    assert out["notes"]["A1"]["status"] == "fail_blocking"
    file = gateway.state / "testrun" / "run2" / "notes.json"
    assert json.loads(file.read_text())["steps"]["B1.4"]["status"] == "pass"
    assert oct(file.stat().st_mode & 0o777) == "0o600"
    assert oct(file.parent.stat().st_mode & 0o777) == "0o700"


def test_add_on_step_ids_are_accepted(client, h):
    assert put(client, h, step="H1.1").status_code == 200
    assert put(client, h, step="Z12").status_code == 200


@pytest.mark.parametrize("step", ["../x", "A", "1A", "AA1", "A1.2.3", "a1", "A1%2F..", "A1.", "%2e%2e"])
def test_bad_step_ids_are_refused(client, h, step):
    assert put(client, h, step=step).status_code in (400, 404, 405)


@pytest.mark.parametrize("run", ["..", "Run2", "a/b", "x" * 40, "-a"])
def test_bad_run_ids_are_refused(client, h, run):
    assert client.get(f"/api/testrun/{run}", headers=h).status_code in (400, 404, 405)


def test_status_and_note_limits(client, h):
    assert put(client, h, status="done").status_code == 400
    assert put(client, h, note="x" * 20_001).status_code == 400
    assert put(client, h, note="x" * 20_000).status_code == 200
    res = client.put("/api/testrun/run2/steps/A1", json={"status": "pass", "note": "", "extra": 1}, headers=h)
    assert res.status_code == 400


def test_image_roundtrip_per_type_and_delete(client, h, gateway):
    for raw, type, ext in [(PNG, "image/png", "png"), (JPG, "image/jpeg", "jpg"), (WEBP, "image/webp", "webp")]:
        res = upload(client, h, raw, type)
        assert res.status_code == 200, res.text
        name = res.json()["name"]
        assert name.startswith("C5-") and name.endswith("." + ext)
        file = gateway.state / "testrun" / "run2" / "images" / name
        assert res.json()["path"] == str(file) and file.read_bytes() == raw
        assert oct(file.stat().st_mode & 0o777) == "0o600"
        got = client.get(f"/api/testrun/run2/images/{name}", headers=h)
        assert got.content == raw and got.headers["content-type"] == type
    notes = client.get("/api/testrun/run2", headers=h).json()["notes"]
    assert len(notes["C5"]["images"]) == 3
    name = notes["C5"]["images"][0]
    assert client.delete(f"/api/testrun/run2/images/{name}", headers=h).status_code == 200
    assert client.get(f"/api/testrun/run2/images/{name}", headers=h).status_code == 404
    assert len(client.get("/api/testrun/run2", headers=h).json()["notes"]["C5"]["images"]) == 2
    put(client, h, step="C5", note="n")  # saving text does not touch the images
    assert len(client.get("/api/testrun/run2", headers=h).json()["notes"]["C5"]["images"]) == 2


def test_image_type_and_size_limits(client, h):
    assert upload(client, h, b"GIF89a" + b"x" * 20, "image/png").status_code == 400  # not what it says
    assert upload(client, h, PNG, "image/jpeg").status_code == 400  # declared type must match the bytes
    assert upload(client, h, PNG, "image/gif").status_code == 400
    assert upload(client, h, b"<svg onload=1>", "image/png").status_code == 400
    assert upload(client, h, b"").status_code == 400
    ok = PNG + b"y" * (5 * 1024 * 1024 - len(PNG))
    assert upload(client, h, ok).status_code == 200
    assert upload(client, h, ok + b"z").status_code == 400
    res = client.post(
        "/api/testrun/run2/steps/C5/images", json={"type": "image/png", "data": "!!!notbase64"}, headers=h
    )
    assert res.status_code == 400
    huge = client.post(
        "/api/testrun/run2/steps/C5/images", json={"type": "image/png", "data": "A" * 8_000_000}, headers=h
    )
    assert huge.status_code in (400, 413)


def test_image_count_per_step_is_bounded(client, h):
    for _ in range(12):
        assert upload(client, h, PNG).status_code == 200
    assert upload(client, h, PNG).status_code == 400


@pytest.mark.parametrize(
    "name", ["../notes.json", "..%2Fnotes.json", "C5-0123456789ab.svg", "C5-0123456789ab.png.txt", "notes.json"]
)
def test_image_names_cannot_escape(client, h, name):
    assert client.get(f"/api/testrun/run2/images/{name}", headers=h).status_code in (400, 404, 405)
    assert client.delete(f"/api/testrun/run2/images/{name}", headers=h).status_code in (400, 404, 405)


def test_a_file_the_notes_do_not_list_is_not_served(client, h, gateway):
    assert upload(client, h, PNG).status_code == 200
    stray = gateway.state / "testrun" / "run2" / "images" / "C5-aaaaaaaaaaaa.png"
    stray.write_bytes(PNG)
    assert client.get("/api/testrun/run2/images/C5-aaaaaaaaaaaa.png", headers=h).status_code == 404


def test_a_symlinked_image_is_not_served(client, h, gateway, tmp_path):
    name = upload(client, h, PNG).json()["name"]
    file = gateway.state / "testrun" / "run2" / "images" / name
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"secret")
    os.unlink(file)
    os.symlink(secret, file)
    assert client.get(f"/api/testrun/run2/images/{name}", headers=h).status_code == 404


def test_corrupt_notes_are_never_overwritten(client, h, gateway):
    put(client, h)
    file = gateway.state / "testrun" / "run2" / "notes.json"
    file.write_text("{not json")
    assert client.get("/api/testrun/run2", headers=h).status_code == 500
    assert put(client, h).status_code == 500
    assert file.read_text() == "{not json"


def test_the_checklist_page_is_a_dashboard_address(client):
    assert client.get("/checklist").status_code == 200
    assert client.get("/w/DEMO/checklist").status_code == 404
