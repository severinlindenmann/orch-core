"""Which risk notices this person has already read (U6): kept on this machine, per person and workspace, not signed.

The Start agent dialog shows its full "runs as you" notice until the person acknowledges it once; afterwards it is a
small line with a Details link. This only chooses how much of the notice is open at first: the notice text itself is
never removed and stays one click away. Nothing here signs, reads a key or changes the workspace.
"""

import json
import os
import threading
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict

from .gateway import Gateway, state_dir
from .reads import workspace

FILE = "risk_notices.json"
_LOCK = threading.Lock()
# Bump a notice's version when its meaning changes: an older acknowledgement then no longer counts.
VERSIONS = {"start_agent": 1, "login_shell": 1, "grant": 1}


class Ack(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notice: Literal["start_agent", "login_shell", "grant"]


def _path(env=None) -> Path:
    return state_dir(env) / "dashboard" / FILE


def load(person: str, ws: str, env=None) -> dict:
    """The notices of ``person`` in ``ws`` acknowledged at their current version (unreadable or malformed: none)."""
    try:
        doc = json.loads(_path(env).read_text(encoding="utf-8"))
        mine = doc["people"][person][ws] if doc["v"] == 1 else {}
        return {k: True for k, v in VERSIONS.items() if mine.get(k) == v}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def store(person: str, ws: str, notice: str, env=None) -> None:
    from orch.host.custody import write_private

    path = _path(env)
    with _LOCK:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("v") != 1 or not isinstance(doc.get("people"), dict):
                doc = {"v": 1, "people": {}}
        except (OSError, ValueError, AttributeError):
            doc = {"v": 1, "people": {}}
        mine = doc["people"].setdefault(person, {}).setdefault(ws, {})
        if not isinstance(mine, dict):
            mine = doc["people"][person][ws] = {}
        mine[notice] = VERSIONS[notice]
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        write_private(path, doc)
    os.chmod(path, 0o600)


def register(app: FastAPI, gateway: Gateway):
    @app.get("/api/w/{ws}/me/risk_notices")
    def get(ws: str = Depends(workspace)):
        gateway.enabled(ws)
        return {"acknowledged": load(app.state.person_of(ws), ws)}

    @app.post("/api/w/{ws}/me/risk_notices")
    def post(body: Ack, ws: str = Depends(workspace)):
        gateway.enabled(ws)
        person = app.state.person_of(ws)
        store(person, ws, body.notice)
        return {"acknowledged": load(person, ws)}
