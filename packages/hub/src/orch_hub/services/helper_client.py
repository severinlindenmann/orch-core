"""The hub web's side of the helper socket (HUB-SV-02): one JSON line out, one back, over the unix socket in the hub state
dir. There is no other transport. A missing socket is ``not_set_up``, a refused or silent one is ``helper_unavailable``."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from .helper_backends import HELPER_UNIT_RE, UNIT_RE, load_config
from .helper_proto import HelperError

PREPARE_TIMEOUT_S = 10
RUN_TIMEOUT_S = 70  # the helper waits up to 60 s for the command
MAX_REPLY = 1024 * 1024 + 16


class HelperClient:
    def __init__(self, sock_path: Path) -> None:
        self._path = sock_path

    def call(self, op: str, *, timeout_s: float = PREPARE_TIMEOUT_S, **fields: Any) -> dict[str, Any]:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout_s)
        try:
            try:
                s.connect(str(self._path))
            except FileNotFoundError:
                raise HelperError("not_set_up") from None
            except OSError:
                raise HelperError("helper_unavailable") from None
            try:
                s.sendall(json.dumps({"op": op, **fields}, separators=(",", ":")).encode() + b"\n")
                buf = b""
                while len(buf) < MAX_REPLY:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                obj = json.loads(buf)
            except OSError, ValueError:
                raise HelperError("helper_unavailable") from None
        finally:
            s.close()
        if type(obj) is not dict:
            raise HelperError("helper_unavailable")
        if obj.get("ok") is not True:
            code = obj.get("code")
            raise HelperError(code if type(code) is str else "internal", str(obj.get("detail", ""))[:200])
        return {k: v for k, v in obj.items() if k != "ok"}

    def helper_unit(self) -> str | None:
        """The user unit ``orch-<tag>-hub-helper.service`` the installer made for this hub (named after the hub's own
        unit in helper.json), so the page can say the real command; ``None`` when it cannot be told. Display only."""
        try:
            hub = load_config(self._path.parent).parts.get("hub")
        except HelperError:
            return None
        if hub is None or hub.kind != "systemd-user" or not hub.unit.endswith("-hub.service"):
            return None
        unit = hub.unit[: -len("-hub.service")] + "-hub-helper.service"
        return unit if UNIT_RE.fullmatch(unit) and HELPER_UNIT_RE.fullmatch(unit) else None

    def info(self) -> dict[str, Any]:
        """What the Services page needs: ``{state: up|down|not_set_up, host_id, parts: [{name, label}], hub_busy}``;
        when the helper does not answer, also ``helper_unit`` (the user unit to start) if it can be told."""
        try:
            return {"state": "up", **self.call("parts", timeout_s=2)}
        except HelperError as e:
            out: dict[str, Any] = {"state": "not_set_up" if e.code == "not_set_up" else "down", "parts": []}
            unit = self.helper_unit()
            if unit is not None:
                out["helper_unit"] = unit
            return out
