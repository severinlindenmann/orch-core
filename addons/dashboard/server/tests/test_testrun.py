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
    assert out["path"] == str(gateway.state / "dashboard" / "testrun" / "run2")
    assert out["notes"]["B1.4"]["note"] == "the page was blank"
    assert out["notes"]["A1"]["status"] == "fail_blocking"
    file = gateway.state / "dashboard" / "testrun" / "run2" / "notes.json"
    assert json.loads(file.read_text())["steps"]["B1.4"]["status"] == "pass"
    assert oct(file.stat().st_mode & 0o777) == "0o600"
    assert oct(file.parent.stat().st_mode & 0o777) == "0o700"


def test_add_on_step_ids_are_accepted(client, h):
    assert put(client, h, step="H1.1").status_code == 200
    assert put(client, h, step="Z12").status_code == 200


@pytest.mark.parametrize("step", ["../x", "A", "1A", "AA1", "A1.2.3", "a1", "A1%2F..", "A1.", "%2e%2e"])
def test_bad_step_ids_are_refused(client, h, step):
    assert put(client, h, step=step).status_code in (400, 404, 405)


@pytest.mark.parametrize("run", ["..", "Run2", "a/b", "x" * 40, "-a", "run", "run1234", "run2x"])
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
        file = gateway.state / "dashboard" / "testrun" / "run2" / "images" / name
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
    stray = gateway.state / "dashboard" / "testrun" / "run2" / "images" / "C5-aaaaaaaaaaaa.png"
    stray.write_bytes(PNG)
    assert client.get("/api/testrun/run2/images/C5-aaaaaaaaaaaa.png", headers=h).status_code == 404


def test_a_symlinked_image_is_not_served(client, h, gateway, tmp_path):
    name = upload(client, h, PNG).json()["name"]
    file = gateway.state / "dashboard" / "testrun" / "run2" / "images" / name
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"secret")
    os.unlink(file)
    os.symlink(secret, file)
    assert client.get(f"/api/testrun/run2/images/{name}", headers=h).status_code == 404


def test_corrupt_notes_are_never_overwritten(client, h, gateway):
    put(client, h)
    file = gateway.state / "dashboard" / "testrun" / "run2" / "notes.json"
    file.write_text("{not json")
    assert client.get("/api/testrun/run2", headers=h).status_code == 500
    assert put(client, h).status_code == 500
    assert file.read_text() == "{not json"


def test_the_checklist_page_is_a_dashboard_address(client):
    assert client.get("/checklist").status_code == 200
    assert client.get("/w/DEMO/checklist").status_code == 404


def test_a_symlinked_run_or_images_dir_is_refused(client, h, gateway, tmp_path):
    assert put(client, h).status_code == 200
    run = gateway.state / "dashboard" / "testrun" / "run2"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (run / "images").symlink_to(elsewhere)
    assert upload(client, h, PNG).status_code == 400
    assert client.get("/api/testrun/run2", headers=h).status_code == 400
    assert list(elsewhere.iterdir()) == []


def test_step_rows_are_capped(client, h, monkeypatch):
    from orch_dashboard import testrun

    monkeypatch.setattr(testrun, "STEP_ROWS_MAX", 2)
    assert put(client, h, step="A1").status_code == 200
    assert put(client, h, step="A2").status_code == 200
    assert put(client, h, step="A3").status_code == 400
    assert put(client, h, step="A1", note="again").status_code == 200  # an existing row is still editable


def test_total_image_bytes_are_capped(client, h, monkeypatch):
    from orch_dashboard import testrun

    monkeypatch.setattr(testrun, "RUN_BYTES_MAX", len(PNG) * 2)
    assert upload(client, h, PNG).status_code == 200
    assert upload(client, h, PNG).status_code == 200
    assert upload(client, h, PNG).status_code == 400


def test_a_failed_save_leaves_no_image_behind(client, h, gateway, monkeypatch):
    from orch_dashboard import testrun

    def boom(self, run, doc):
        raise OSError("disk full")

    monkeypatch.setattr(testrun.Store, "save", boom)
    assert upload(client, h, PNG).status_code == 500
    images = gateway.state / "dashboard" / "testrun" / "run2" / "images"
    assert list(images.iterdir()) == []


def test_concurrent_first_saves_of_different_steps_all_persist(client, h):
    from concurrent.futures import ThreadPoolExecutor

    jobs = [("A1", {"status": "pass"}), ("B1.1", {"note": "n1"}), ("C2", {"status": "fail_blocking", "note": "n2"})]

    def go(job):
        step, body = job
        return client.put(f"/api/testrun/run2/steps/{step}", json=body, headers=h).status_code

    with ThreadPoolExecutor(6) as pool:
        codes = list(pool.map(go, jobs * 4 + [("D1", {"status": "skipped"})]))
        up = pool.submit(upload, client, h, PNG, "image/png", "E1")
        assert up.result().status_code == 200
    assert set(codes) == {200}
    notes = client.get("/api/testrun/run2", headers=h).json()["notes"]
    assert notes["A1"]["status"] == "pass"
    assert notes["B1.1"]["note"] == "n1"
    assert (notes["C2"]["status"], notes["C2"]["note"]) == ("fail_blocking", "n2")
    assert notes["D1"]["status"] == "skipped"
    assert len(notes["E1"]["images"]) == 1


def test_a_partial_save_keeps_the_other_field(client, h):
    put(client, h, step="C1", status="pass", note="kept")
    assert client.put("/api/testrun/run2/steps/C1", json={"status": "fail_blocking"}, headers=h).status_code == 200
    assert client.put("/api/testrun/run2/steps/C1", json={}, headers=h).status_code == 400
    row = client.get("/api/testrun/run2", headers=h).json()["notes"]["C1"]
    assert (row["status"], row["note"]) == ("fail_blocking", "kept")


def test_a_symlinked_state_dir_still_saves_but_a_symlinked_testrun_is_refused(tmp_path):
    from orch_dashboard.testrun import Store

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    store = Store(link)
    store.save("run2", {"version": 1, "steps": {"A1": {"status": "pass", "note": "", "images": []}}})
    assert store.load("run2")["steps"]["A1"]["status"] == "pass"
    (real / "dashboard" / "testrun").rename(real / "dashboard" / "moved")
    (real / "dashboard" / "testrun").symlink_to(real / "dashboard" / "moved")
    with pytest.raises(Exception):  # noqa: B017, PT011  (HostError invalid.input)
        store.load("run2")


H16 = "0123456789abcdef"


def test_previous_runs_are_read_only_and_lower_numbers_only(client, h, gateway):
    assert put(client, h, step="A1", status="pass", note="ok", run="run3").status_code == 200
    assert client.put("/api/testrun/run3/steps/A1", json={"hash": H16}, headers=h).status_code == 200
    put(client, h, step="A2", status="fail_blocking", note="broke", run="run3")
    put(client, h, step="A1", status="pass", note="newer", run="run5")
    before = sorted(str(p) for p in (gateway.state / "dashboard" / "testrun").rglob("*"))
    out = client.get("/api/testrun/run4/previous", headers=h).json()
    assert [r["run"] for r in out["runs"]] == ["run3"]
    a1 = out["runs"][0]["steps"]["A1"]
    assert a1["status"] == "pass" and a1["hash"] == H16 and a1["passed_at"] and a1["note"] == "ok"
    assert out["runs"][0]["steps"]["A2"]["status"] == "fail_blocking"
    assert before == sorted(str(p) for p in (gateway.state / "dashboard" / "testrun").rglob("*"))
    assert not (gateway.state / "dashboard" / "testrun" / "run4").exists()
    assert client.get("/api/testrun/run3/previous", headers=h).json() == {"runs": []}


def test_previous_needs_a_session_and_a_valid_run(client, h):
    assert client.get("/api/testrun/run4/previous", headers={"Origin": ORIGIN}).status_code == 401
    for run in ("run", "run1234", "../x", "RUN3", "run3x"):
        assert client.get(f"/api/testrun/{run}/previous", headers=h).status_code in (400, 404, 422)


def test_previous_ignores_odd_directories_symlinks_and_corrupt_runs(client, h, gateway, tmp_path):
    put(client, h, step="A1", status="pass", run="run1")
    root = gateway.state / "dashboard" / "testrun"
    (root / "run2").mkdir()
    (root / "run2" / "notes.json").write_text("{not json")
    (root / "notes-run").mkdir()
    (root / "run0x").mkdir()
    (root / "run0x" / "notes.json").write_text(json.dumps({"steps": {"A1": {"status": "pass"}}}))
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "notes.json").write_text(json.dumps({"steps": {"A1": {"status": "pass", "note": "secret"}}}))
    os.symlink(outside, root / "run3")
    out = client.get("/api/testrun/run9/previous", headers=h).json()
    assert [r["run"] for r in out["runs"]] == ["run1"]


def test_previous_drops_malformed_rows_and_bad_hashes(client, h, gateway):
    put(client, h, step="A1", status="pass", run="run1")
    path = gateway.state / "dashboard" / "testrun" / "run1" / "notes.json"
    doc = json.loads(path.read_text())
    doc["steps"]["A1"]["hash"] = "<script>"
    doc["steps"]["bad id"] = {"status": "pass"}
    doc["steps"]["A2"] = {"status": "weird"}
    doc["steps"]["A3"] = "text"
    path.write_text(json.dumps(doc))
    steps = client.get("/api/testrun/run2/previous", headers=h).json()["runs"][0]["steps"]
    assert list(steps) == ["A1"] and steps["A1"]["hash"] is None


def test_pass_date_is_kept_and_cleared_when_no_longer_pass(client, h):
    put(client, h, step="A1", status="pass", run="run3")
    first = client.get("/api/testrun/run4/previous", headers=h).json()["runs"][0]["steps"]["A1"]["passed_at"]
    put(client, h, step="A1", status="pass", note="more", run="run3")
    assert client.get("/api/testrun/run4/previous", headers=h).json()["runs"][0]["steps"]["A1"]["passed_at"] == first
    put(client, h, step="A1", status="fail_blocking", run="run3")
    assert client.get("/api/testrun/run4/previous", headers=h).json()["runs"][0]["steps"]["A1"]["passed_at"] is None


def test_hash_retest_only_bodies_and_bad_hash(client, h):
    assert client.put("/api/testrun/run4/steps/A1", json={"retest": True}, headers=h).status_code == 200
    assert client.get("/api/testrun/run4", headers=h).json()["notes"]["A1"]["retest"] is True
    for bad in ({"hash": "XYZ"}, {"hash": "0123456789ABCDEF"}, {"retest": "yes"}, {}):
        assert client.put("/api/testrun/run4/steps/A1", json=bad, headers=h).status_code in (400, 422)


def test_screenshots_zip_has_every_picture_by_stored_name(client, h):
    import io
    import re
    import zipfile

    a = upload(client, h, PNG, step="B1.2").json()["name"]
    b = upload(client, h, JPG, "image/jpeg", step="C5").json()["name"]
    res = client.get("/api/testrun/run2/screenshots.zip", headers=h)
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    assert re.fullmatch(
        r'attachment; filename="orch-run2-screenshots-\d{8}-\d{4}\.zip"', res.headers["content-disposition"]
    )
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        assert sorted(z.namelist()) == sorted([a, b])
        assert z.read(a) == PNG and z.read(b) == JPG


def test_screenshots_zip_needs_a_session_and_a_picture(client, h):
    assert client.get("/api/testrun/run2/screenshots.zip", headers={"Origin": ORIGIN}).status_code == 401
    assert client.get("/api/testrun/run2/screenshots.zip", headers=h).status_code == 404  # empty run
    put(client, h)
    assert client.get("/api/testrun/run2/screenshots.zip", headers=h).status_code == 404  # notes but no picture
    assert client.get("/api/testrun/run../screenshots.zip", headers=h).status_code in (400, 404, 422)


def test_screenshots_zip_skips_symlinks_and_unsafe_names(client, h, gateway, tmp_path):
    import io
    import zipfile

    good = upload(client, h, PNG).json()["name"]
    link = upload(client, h, PNG).json()["name"]
    run = gateway.state / "dashboard" / "testrun" / "run2"
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"secret")
    os.unlink(run / "images" / link)
    os.symlink(secret, run / "images" / link)
    # a hand-edited notes file naming a traversal path and a non-image must not be read either
    (tmp_path / "x.png").write_bytes(b"outside")
    notes = json.loads((run / "notes.json").read_text())
    notes["steps"]["C5"]["images"] += ["../../../../x.png", "notes.json", "C5-0123456789ab.png"]
    (run / "notes.json").write_text(json.dumps(notes))
    res = client.get("/api/testrun/run2/screenshots.zip", headers=h)
    assert res.status_code == 200
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        assert z.namelist() == [good]


def test_screenshots_zip_refuses_a_symlinked_images_dir(client, h, gateway, tmp_path):
    upload(client, h, PNG)
    run = gateway.state / "dashboard" / "testrun" / "run2"
    elsewhere = tmp_path / "elsewhere"
    run.joinpath("images").rename(elsewhere)
    (run / "images").symlink_to(elsewhere)
    assert client.get("/api/testrun/run2/screenshots.zip", headers=h).status_code == 400


def test_screenshots_zip_is_capped(client, h, monkeypatch):
    from orch_dashboard import testrun

    upload(client, h, PNG)
    monkeypatch.setattr(testrun, "ZIP_BYTES_MAX", 10)
    res = client.get("/api/testrun/run2/screenshots.zip", headers=h)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "zip.too_large"
