"""The tester's checklist of an end-to-end run: per-step status, notes and screenshots, kept on this computer.

Files live under ``<state>/dashboard/testrun/<run>/``: ``notes.json`` and ``images/``. Nothing here talks to a
workspace or the host, so the page works before ``orch setup``. Every route sits behind the normal dashboard session
(Security checks the bearer token, the Origin of writes and the JSON content type). Names that reach the file system
are matched against fixed patterns, never joined from free text.
"""

import base64
import binascii
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Path as PathParam
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .errors import fail

RUN = r"^run[0-9]{1,3}$"
HASH = r"^[0-9a-f]{16}$"
PREVIOUS_RUNS_MAX = 20
STEP = r"^[A-Z][0-9]{1,2}(?:\.[0-9]{1,2})?$"
IMAGE = r"^[A-Z][0-9]{1,2}(?:\.[0-9]{1,2})?-[0-9a-f]{12}\.(?:png|jpg|webp)$"
STATUSES = ("todo", "pass", "fail_blocking", "fail_non_blocking", "skipped")
NOTE_MAX = 20_000
IMAGE_MAX = 5 * 1024 * 1024
IMAGES_PER_STEP = 12
STEP_ROWS_MAX = 500
RUN_BYTES_MAX = 300 * 1024 * 1024
ZIP_BYTES_MAX = 200 * 1024 * 1024
#: base64 of the largest image plus the JSON around it; the Security layer bounds the request to this.
UPLOAD_LIMIT = (IMAGE_MAX * 4 // 3) + 4096
TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
MEDIA = {ext: media for media, ext in TYPES.items()}


def sniff(data: bytes) -> str | None:
    """The image type by its first bytes (the declared type alone proves nothing)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class StepBody(BaseModel):
    """The changed fields of one step. A field that is absent is left as it is on the server (merged per field)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["todo", "pass", "fail_blocking", "fail_non_blocking", "skipped"] | None = None
    note: str | None = Field(default=None, max_length=NOTE_MAX)
    #: fingerprint of the step text the person saw when marking it (so a later run can tell if the step changed)
    hash: str | None = Field(default=None, pattern=HASH)
    #: true = the person reopened a step carried over from an earlier run ("Test again")
    retest: bool | None = None


class ImageBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["image/png", "image/jpeg", "image/webp"]
    data: str = Field(min_length=4, max_length=UPLOAD_LIMIT)


def _plain_dir(path: Path) -> None:
    """The directory exists, is private and is not a symlink (checked with lstat, never followed)."""
    if path.is_symlink():
        raise fail("invalid.input", exit=5)
    path.mkdir(mode=0o700, exist_ok=True)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise fail("invalid.input", exit=5)


def _read(path: Path, limit: int) -> bytes:
    """Read one regular file through an fd opened without following a symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as file:
            return file.read(limit + 1)
    finally:
        os.close(fd)


class Store:
    def __init__(self, state: Path):
        self.root = state / "dashboard" / "testrun"
        self.lock = threading.Lock()

    def run_dir(self, run: str) -> Path:
        return self.root / run

    def ensure(self, run: str, images: bool = False) -> Path:
        # The state directory itself may be a symlink (a person's choice); only what we create below it must not be.
        self.root.parent.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        _plain_dir(self.root.parent)
        _plain_dir(self.root)
        directory = self.run_dir(run)
        _plain_dir(directory)
        if images:
            _plain_dir(directory / "images")
        return directory

    def load(self, run: str) -> dict:
        directory = self.run_dir(run)
        if any(p.is_symlink() for p in (self.root.parent, self.root, directory, directory / "images")):
            raise fail("invalid.input", exit=5)
        try:
            doc = json.loads(_read(directory / "notes.json", 4_000_000).decode("utf-8"))
        except FileNotFoundError:
            return {"version": 1, "steps": {}}
        except (OSError, ValueError):
            raise fail("internal", exit=1) from None  # unreadable notes are never overwritten with an empty file
        if not isinstance(doc, dict) or not isinstance(doc.get("steps"), dict):
            raise fail("internal", exit=1)
        return doc

    def save(self, run: str, doc: dict) -> None:
        directory = self.ensure(run)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".notes-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(doc, file, indent=2, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, directory / "notes.json")
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def step(self, doc: dict, step: str) -> dict:
        if step not in doc["steps"] and len(doc["steps"]) >= STEP_ROWS_MAX:
            raise fail("invalid.input", exit=5)
        row = doc["steps"].setdefault(step, {"status": "todo", "note": "", "images": []})
        row["updated"] = datetime.now(UTC).isoformat(timespec="seconds")
        return row

    def used(self, run: str, doc: dict) -> int:
        total = 0
        for row in doc["steps"].values():
            for name in row.get("images", []):
                try:
                    total += (self.run_dir(run) / "images" / name).lstat().st_size
                except OSError:
                    pass
        return total

    def previous(self, run: str) -> list[dict]:
        """Earlier runs' notes, read only (nothing is created or changed). Only directories named run<n> with a
        smaller n, never a symlink; unreadable ones are skipped. Pictures are counted, never served from here."""
        number = int(run[3:])
        if any(p.is_symlink() for p in (self.root.parent, self.root)):
            raise fail("invalid.input", exit=5)
        try:
            names = [n.name for n in self.root.iterdir()]
        except OSError:
            return []
        found = sorted((int(n[3:]), n) for n in names if re.fullmatch(RUN, n) and int(n[3:]) < number)
        out = []
        for _, name in found[-PREVIOUS_RUNS_MAX:]:
            try:
                doc = self.load(name)
            except Exception:  # noqa: BLE001 - an unreadable earlier run is only skipped
                continue
            steps = {}
            for sid, row in doc["steps"].items():
                if not isinstance(row, dict) or not re.fullmatch(STEP, str(sid)) or row.get("status") not in STATUSES:
                    continue
                note = row.get("note")
                images = row.get("images")
                digest = row.get("hash")
                steps[sid] = {
                    "status": row["status"],
                    "note": note if isinstance(note, str) else "",
                    "images": len(images) if isinstance(images, list) else 0,
                    "updated": row.get("updated") if isinstance(row.get("updated"), str) else None,
                    "passed_at": row.get("passed_at") if isinstance(row.get("passed_at"), str) else None,
                    "hash": digest if isinstance(digest, str) and re.fullmatch(HASH, digest) else None,
                }
            out.append({"run": name, "steps": steps})
        return out

    def view(self, run: str, doc: dict) -> dict:
        directory = self.run_dir(run)
        return {"path": str(directory), "images_path": str(directory / "images"), "notes": doc["steps"]}


def register(app, gateway):
    store = Store(gateway.state)
    run_type = Annotated[str, PathParam(pattern=RUN)]
    step_type = Annotated[str, PathParam(pattern=STEP)]
    image_type = Annotated[str, PathParam(pattern=IMAGE)]

    @app.get("/api/testrun/{run}")
    def read(run: run_type):
        with store.lock:
            return store.view(run, store.load(run))

    @app.get("/api/testrun/{run}/previous")
    def read_previous(run: run_type):
        with store.lock:
            return {"runs": store.previous(run)}

    @app.put("/api/testrun/{run}/steps/{step}")
    def put_step(run: run_type, step: step_type, body: StepBody):
        with store.lock:
            doc = store.load(run)
            before = dict(doc["steps"].get(step) or {})
            row = store.step(doc, step)
            if body.status is None and body.note is None and body.hash is None and body.retest is None:
                raise fail("invalid.input", exit=5)
            if body.status is not None:
                row["status"] = body.status
            if body.hash is not None:
                row["hash"] = body.hash
            if body.retest is not None:
                row["retest"] = body.retest
            if row["status"] == "pass" and not row.get("passed_at"):
                # The day it was passed: an older pass without a date keeps the time of its last save.
                kept = before.get("updated") if before.get("status") == "pass" else None
                row["passed_at"] = kept or row["updated"]
            elif row["status"] != "pass":
                row.pop("passed_at", None)
            if body.note is not None:
                row["note"] = body.note
            store.save(run, doc)
            return {"updated": row["updated"]}

    @app.post("/api/testrun/{run}/steps/{step}/images")
    def add_image(run: run_type, step: step_type, body: ImageBody):
        try:
            data = base64.b64decode(body.data, validate=True)
        except (binascii.Error, ValueError):
            raise fail("invalid.input", exit=5) from None
        if not data or len(data) > IMAGE_MAX or sniff(data) != body.type:
            raise fail("invalid.input", exit=5)
        with store.lock:
            doc = store.load(run)
            row = store.step(doc, step)
            if len(row["images"]) >= IMAGES_PER_STEP or store.used(run, doc) + len(data) > RUN_BYTES_MAX:
                raise fail("invalid.input", exit=5)
            name = f"{step}-{secrets.token_hex(6)}.{TYPES[body.type]}"
            images = store.ensure(run, images=True) / "images"
            fd = os.open(images / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "wb") as file:
                    file.write(data)
                row["images"].append(name)
                store.save(run, doc)
            except BaseException:
                (images / name).unlink(missing_ok=True)
                raise
            return {"name": name, "path": str(images / name)}

    def owned(run, name):
        """The image's file, only if the notes list it (a name alone grants nothing)."""
        doc = store.load(run)
        if not any(name in row.get("images", []) for row in doc["steps"].values()):
            raise fail("not_found", exit=1)
        return store.run_dir(run) / "images" / name

    @app.get("/api/testrun/{run}/images/{name}")
    def read_image(run: run_type, name: image_type):
        with store.lock:
            path = owned(run, name)
            try:
                data = _read(path, IMAGE_MAX)
            except OSError:
                raise fail("not_found", exit=1) from None
        return Response(data, media_type=MEDIA[name.rsplit(".", 1)[1]])

    def zip_of(run):
        """Every picture the notes list, as one stored zip in a temporary file (pictures are already compressed).
        Only names that match IMAGE are read, each through an fd opened without following a symlink; a name that
        is missing, unsafe or not a regular file is left out. Refused above ZIP_BYTES_MAX."""
        doc = store.load(run)
        names = sorted(
            {
                n
                for row in doc["steps"].values()
                if isinstance(row, dict)
                for n in row.get("images", [])
                if isinstance(n, str) and re.fullmatch(IMAGE, n)
            }
        )
        images = store.run_dir(run) / "images"
        out = tempfile.TemporaryFile()
        total = count = 0
        try:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
                for name in names:
                    try:
                        data = _read(images / name, IMAGE_MAX)
                    except OSError:
                        continue
                    if len(data) > IMAGE_MAX:
                        continue
                    total += len(data)
                    if total > ZIP_BYTES_MAX:
                        raise fail(
                            "zip.too_large",
                            "The screenshots are larger than 200 MB; copy them from the folder instead.",
                            exit=5,
                        )
                    archive.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), data)
                    count += 1
        except BaseException:
            out.close()
            raise
        if count == 0:
            out.close()
            raise fail("not_found", exit=1)
        out.seek(0)
        return out

    @app.get("/api/testrun/{run}/screenshots.zip")
    def read_zip(run: run_type):
        with store.lock:
            out = zip_of(run)
        stamp = datetime.now().strftime("%Y%m%d-%H%M")  # local time

        def chunks():
            try:
                while block := out.read(1 << 20):
                    yield block
            finally:
                out.close()

        size = os.fstat(out.fileno()).st_size
        headers = {
            "Content-Disposition": f'attachment; filename="orch-{run}-screenshots-{stamp}.zip"',
            "Content-Length": str(size),
            "Cache-Control": "no-store",
        }
        return StreamingResponse(chunks(), media_type="application/zip", headers=headers)

    @app.delete("/api/testrun/{run}/images/{name}")
    def delete_image(run: run_type, name: image_type):
        with store.lock:
            path = owned(run, name)
            doc = store.load(run)
            for row in doc["steps"].values():
                if name in row.get("images", []):
                    row["images"].remove(name)
            store.save(run, doc)
            path.unlink(missing_ok=True)
            return {"ok": True}
