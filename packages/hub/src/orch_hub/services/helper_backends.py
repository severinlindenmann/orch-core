"""How the helper runs a part (HUB-SV-02). Linux first: ``systemd`` (a system unit through ``sudo -n``) and
``systemd-user`` (the helper's own user manager). launchd and a pidfile backend are later; a part that names one is
refused when the config is loaded, never half-run.

Only FIXED argv lists are built here, from a closed set of verbs and a unit name that matched ``UNIT_RE`` when the file was
read. There is no shell, no string the web side can influence reaches an argument, and the sudoers text this module prints
lists exactly the argv it runs (no wildcards)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..fsutil import PrivateFileError, _check_existing
from .helper_proto import HOST_RE, PART_RE, HelperError

CONFIG_FILE = "helper.json"
UNIT_RE = re.compile(r"orch-[a-z0-9][a-z0-9-]{0,40}(@[a-z0-9-]{1,40})?\.service")
LABEL_RE = re.compile(r"[A-Za-z][A-Za-z0-9 -]{0,23}")
PARTS = ("hub", "relay", "publish")  # the closed set the console knows
KINDS = ("systemd", "systemd-user")
VERBS = ("start", "stop", "restart")
SYSTEMCTL = "/usr/bin/systemctl"
JOURNALCTL = "/usr/bin/journalctl"
LOG_TAIL = ("--since", "-1h", "-n", "500", "-o", "short-iso", "--no-pager")
ACTION_TIMEOUT_S = 60
LOG_TIMEOUT_S = 5
LOG_MAX_BYTES = 256 * 1024

#: argv -> (return code, stdout, stderr); never raises on a failing command, raises OSError when it cannot start
Runner = Callable[[Sequence[str], int], tuple[int, str, str]]


@dataclass(frozen=True)
class Part:
    name: str
    label: str
    kind: str
    unit: str

    def action_argv(self, verb: str) -> list[str]:
        if verb not in VERBS:
            raise HelperError("bad_action")
        if self.kind == "systemd":
            return ["sudo", "-n", SYSTEMCTL, verb, self.unit]
        return ["systemctl", "--user", verb, self.unit]

    def logs_argv(self) -> list[str]:
        if self.kind == "systemd":
            return ["sudo", "-n", JOURNALCTL, "-u", self.unit, *LOG_TAIL]
        return ["journalctl", "--user", "-u", self.unit, *LOG_TAIL]


@dataclass(frozen=True)
class Config:
    host_id: str
    parts: dict[str, Part]


def parse_config(obj: object) -> Config:
    if type(obj) is not dict or set(obj) != {"v", "host_id", "parts"} or obj["v"] != 1:
        raise HelperError("bad_config", "shape")
    host = obj["host_id"]
    if type(host) is not str or not HOST_RE.fullmatch(host):
        raise HelperError("bad_config", "host_id")
    raw = obj["parts"]
    if type(raw) is not dict or not raw or len(raw) > 8:
        raise HelperError("bad_config", "parts")
    parts: dict[str, Part] = {}
    for name, p in raw.items():
        if type(name) is not str or name not in PARTS or not PART_RE.fullmatch(name):
            raise HelperError("bad_config", "part name")
        if type(p) is not dict or set(p) != {"label", "kind", "unit"}:
            raise HelperError("bad_config", f"part {name}")
        label, kind, unit = p["label"], p["kind"], p["unit"]
        if type(label) is not str or not LABEL_RE.fullmatch(label):
            raise HelperError("bad_config", f"{name} label")
        if kind not in KINDS:
            raise HelperError("bad_config", f"{name} kind (systemd or systemd-user)")
        if type(unit) is not str or not UNIT_RE.fullmatch(unit) or unit.startswith("orch-hub-helper"):
            raise HelperError("bad_config", f"{name} unit")
        parts[name] = Part(name, label, kind, unit)
    return Config(host, parts)


def load_config(state: Path) -> Config:
    path = state / CONFIG_FILE
    try:
        _check_existing(str(path), CONFIG_FILE)  # plain file, ours, 0600
        obj = json.loads(path.read_text())
    except PrivateFileError, OSError, ValueError:
        raise HelperError("not_set_up") from None
    return parse_config(obj)


def run_argv(argv: Sequence[str], timeout_s: int) -> tuple[int, str, str]:
    """The real runner: no shell, no stdin, a minimal environment, bounded time and output."""
    env = {"PATH": "/usr/bin:/bin", "LANG": "C"}
    for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):  # the user manager needs these (systemd-user)
        if k in os.environ:
            env[k] = os.environ[k]
    try:
        r = subprocess.run(
            list(argv), stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout_s, env=env, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    return (
        r.returncode,
        r.stdout[:LOG_MAX_BYTES].decode("utf-8", "replace"),
        r.stderr[:2048].decode("utf-8", "replace"),
    )


def sudoers_text(user: str, parts: Sequence[Part]) -> str:
    """The exact lines for a drop-in (``visudo -cf`` it first): no wildcards, ``--no-pager`` fixed, never status or edit."""
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise HelperError("bad_config", "user")
    cmds: list[str] = []
    for p in parts:
        if p.kind != "systemd":
            continue
        cmds += [f"{SYSTEMCTL} {v} {p.unit}" for v in VERBS]
        cmds.append(f"{JOURNALCTL} -u {p.unit} {' '.join(LOG_TAIL)}")
    if not cmds:
        return "# no system units configured: nothing to allow\n"
    body = ",\\\n    ".join(cmds)
    return (
        "# /etc/sudoers.d/orch-hub-helper (owner installs; check with: visudo -cf <file>)\n"
        f"Defaults:{user} env_reset\n"
        f"Cmnd_Alias ORCH_SVC = {body}\n"
        f"{user} ALL=(root) NOPASSWD: ORCH_SVC\n"
    )
