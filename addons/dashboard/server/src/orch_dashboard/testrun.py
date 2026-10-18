"""The tester's checklist of an end-to-end run: per-step status, notes and screenshots, kept on this computer.

Files live under ``<state>/testrun/<run>/``: ``notes.json`` and ``images/``. Nothing here talks to a workspace or the
host, so the page works before ``orch setup``. Every route sits behind the normal dashboard session (Security checks
the bearer token, the Origin of writes and the JSON content type). Names that reach the file system are matched
against fixed patterns, never joined from free text.
"""

import base64
import binascii
import json
import os
import secrets
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .errors import fail

RUN = r"^[a-z0-9][a-z0-9-]{0,31}$"
STEP = r"^[A-Z][0-9]{1,2}(?:\.[0-9]{1,2})?$"
IMAGE = r"^[A-Z][0-9]{1,2}(?:\.[0-9]{1,2})?-[0-9a-f]{12}\.(?:png|jpg|webp)$"
STATUSES = ("todo", "pass", "fail_blocking", "fail_non_blocking", "skipped")
NOTE_MAX = 20_000
IMAGE_MAX = 5 * 1024 * 1024
IMAGES_PER_STEP = 12
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
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["todo", "pass", "fail_blocking", "fail_non_blocking", "skipped"]
    note: str = Field(max_length=NOTE_MAX)


class ImageBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["image/png", "image/jpeg", "image/webp"]
    data: str = Field(min_length=4, max_length=UPLOAD_LIMIT)


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()

    def run_dir(self, run: str) -> Path:
        return self.root / "testrun" / run

    def _private(self, path: Path) -> None:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise fail("invalid.input", exit=5)

    def load(self, run: str) -> dict:
        path = self.run_dir(run) / "notes.json"
        try:
            doc = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            return {"version": 1, "steps": {}}
        except (OSError, ValueError):
            raise fail("internal", exit=1) from None  # unreadable notes are never overwritten with an empty file
        if not isinstance(doc, dict) or not isinstance(doc.get("steps"), dict):
            raise fail("internal", exit=1)
        return doc

    def save(self, run: str, doc: dict) -> None:
        directory = self.run_dir(run)
        self._private(directory.parent)
        self._private(directory)
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
        row = doc["steps"].setdefault(step, {"status": "todo", "note": "", "images": []})
        row["updated"] = datetime.now(UTC).isoformat(timespec="seconds")
        return row

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

    @app.put("/api/testrun/{run}/steps/{step}")
    def put_step(run: run_type, step: step_type, body: StepBody):
        with store.lock:
            doc = store.load(run)
            row = store.step(doc, step)
            row["status"], row["note"] = body.status, body.note
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
            if len(row["images"]) >= IMAGES_PER_STEP:
                raise fail("invalid.input", exit=5)
            name = f"{step}-{secrets.token_hex(6)}.{TYPES[body.type]}"
            images = store.run_dir(run) / "images"
            store._private(images.parent.parent)
            store._private(images.parent)
            store._private(images)
            fd = os.open(images / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as file:
                file.write(data)
            row["images"].append(name)
            store.save(run, doc)
            return {"name": name, "path": str(images / name)}

    def owned(run, name):
        """The image's file, only if the notes list it (a name alone grants nothing)."""
        doc = store.load(run)
        if not any(name in row.get("images", []) for row in doc["steps"].values()):
            raise fail("not_found", exit=1)
        path = store.run_dir(run) / "images" / name
        if path.is_symlink() or not path.is_file():
            raise fail("not_found", exit=1)
        return path

    @app.get("/api/testrun/{run}/images/{name}")
    def read_image(run: run_type, name: image_type):
        with store.lock:
            path = owned(run, name)
        return FileResponse(path, media_type=MEDIA[name.rsplit(".", 1)[1]])

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
