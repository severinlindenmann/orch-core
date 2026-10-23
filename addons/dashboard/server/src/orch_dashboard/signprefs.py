"""How I approve (B6, owner decision F): the person's own default way to sign, per workspace, kept on this machine.

``sign_default = {method, fallback?}`` is a preference of this person on this device, not a workspace rule and not
signed: it only chooses which button is primary in a sign dialog. What may be used at all is the workspace's signed
``sign_methods`` (the host's ``workspace.settings``); a default that names a way the workspace does not allow is
refused here, and one that stopped being allowed later is not used (the dialog falls back to ``terminal``, which is
always allowed). With no stored default, the effective one is ``click`` while the workspace has one-click signing
switched on (else ``terminal``), so every dialog and the Settings form agree. Nothing here signs, reads a key or changes the workspace.
"""

import json
import os
import threading
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict, model_validator

from .errors import fail
from .gateway import Gateway, state_dir
from .reads import workspace

Method = Literal["terminal", "click", "phone"]
FILE = "sign_default.json"
_LOCK = threading.Lock()


class SignDefault(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Method
    fallback: Method | None = None

    @model_validator(mode="after")
    def distinct(self):
        if self.fallback == self.method:
            raise ValueError("the fallback differs from the default")
        return self


def _path(env=None) -> Path:
    return state_dir(env) / "dashboard" / FILE


def load(person: str, ws: str, env=None) -> dict | None:
    """The stored default of ``person`` in ``ws``, or ``None`` (unset, unreadable or malformed: never an error)."""
    try:
        doc = json.loads(_path(env).read_text(encoding="utf-8"))
        entry = doc["v"] == 1 and doc["people"][person][ws]
        return SignDefault.model_validate(entry).model_dump(exclude_none=True) if entry else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def store(person: str, ws: str, value: dict, env=None) -> None:
    from orch.host.custody import write_private

    path = _path(env)
    with _LOCK:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("v") != 1 or not isinstance(doc.get("people"), dict):
                doc = {"v": 1, "people": {}}
        except (OSError, ValueError, AttributeError):
            doc = {"v": 1, "people": {}}
        doc["people"].setdefault(person, {})[ws] = value
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        write_private(path, doc)
    os.chmod(path, 0o600)


def register(app: FastAPI, gateway: Gateway):
    def allowed(ws: str) -> list[str]:
        link = gateway.enabled(ws)
        if not link.supports("workspace.settings", "settings/1"):
            return ["terminal"]
        methods = link.call("workspace.settings", {}).get("sign_methods")
        if methods is None:  # never set: the model judges no way, every way is allowed
            return ["terminal", "click", "phone"]
        ok = isinstance(methods, list) and "terminal" in methods
        return [m for m in methods if m in ("terminal", "click", "phone")] if ok else ["terminal"]

    def answer(ws: str) -> dict:
        person = app.state.person_of(ws)
        methods = allowed(ws)
        mine = load(person, ws)
        usable = mine is not None and mine["method"] in methods
        # No usable stored choice: the effective default is one click when the workspace switched it on (the signer's
        # flag, which needs the risk acknowledgement) and allows it, else terminal. Only the default selection moves:
        # what may sign is still ``sign_methods`` + the signer's own checks, and a stored choice always wins.
        click = "click" in methods and gateway.one_path_is_click(ws)
        out = dict(mine) if usable else {"method": "click" if click else "terminal"}
        if "fallback" in out and out["fallback"] not in methods:
            out.pop("fallback")
        return {**out, "is_default": not usable, "allowed": methods}

    @app.get("/api/w/{ws}/me/sign_default")
    def get(ws: str = Depends(workspace)):
        return answer(ws)

    @app.put("/api/w/{ws}/me/sign_default")
    def put(body: SignDefault, ws: str = Depends(workspace)):
        methods = allowed(ws)
        if body.method not in methods or (body.fallback is not None and body.fallback not in methods):
            raise fail("invalid.input", "This workspace does not allow that way to approve.", exit=2)
        store(app.state.person_of(ws), ws, body.model_dump(exclude_none=True))
        return answer(ws)
