"""``<state>/runner.json``: the person's terminal-only launch settings (D-P2-28, §12.4.1, §12.4.3; H11c).

``{"v":1, "login_shell":bool, "programs":{"claude-code":"/abs/path", "codex":"/abs/path"}, "pass_env":["NAME", …]}``,
0600, owned by the person (tier A: the host UID). Every key is optional:

* ``login_shell`` (default ``true``): agents and person-run harnesses start through the person's login shell in exec
  form ``[$SHELL, "-l", "-c", 'exec "$0" "$@"', program, …]``. ``false`` is the opt-out: the program is then the
  absolute path in ``programs`` (no entry: ``harness.not_found``).
* ``programs``: absolute paths per harness id of the closed registry (:data:`orch.host.launch.HARNESSES`).
* ``pass_env``: extra variable names a run gets from the runner's own environment. Names matching the deny-list
  (:data:`PASS_ENV_DENY`) make the whole file invalid: nothing passes then (fail closed).

Nothing in the dashboard or on the socket writes this file; the host reads it to compute a launch spec, the runner reads
it again to check every ``spawn`` (defence in depth). A file that cannot be read or does not validate refuses every
launch that depends on it (``launch.invalid``), it never falls back to defaults.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["ENV_NAME", "PASS_ENV_DENY", "ConfigError", "RunnerConfig", "config_path", "parse", "read_config"]

ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
#: §12.4.3: names ``pass_env`` may never name (loader, interpreter, shell start-up hooks, orch's own, and the
#: variables the runner sets itself); used with ``fullmatch``
PASS_ENV_DENY = re.compile(
    r"(?:LD_|DYLD_|PYTHON|GIT_|ORCH_).*|BASH_ENV|ENV|ZDOTDIR|PATH|HOME|SHELL|IFS|PS4|NODE_OPTIONS|PERL5OPT|RUBYOPT"
    r"|BASH_FUNC_.*|TERM|COLORTERM|CLAUDE_CODE_SUBAGENT_MODEL"
)
HARNESS_IDS = ("claude-code", "codex")
MAX_PASS = 32


class ConfigError(Exception):
    """``runner.json`` exists but is not valid: every launch that reads it is refused."""


@dataclass(frozen=True)
class RunnerConfig:
    login_shell: bool = True
    programs: dict[str, str] = field(default_factory=dict)
    pass_env: tuple[str, ...] = ()

    def digest_view(self) -> dict[str, Any]:
        return {"login_shell": self.login_shell, "programs": dict(sorted(self.programs.items())),
                "pass_env": list(self.pass_env)}  # fmt: skip


def config_path(state_dir: Path) -> Path:
    return Path(state_dir) / "runner.json"


def parse(doc: Any) -> RunnerConfig:
    if not isinstance(doc, dict) or doc.get("v", 1) != 1:
        raise ConfigError("runner.json is not a version 1 object")
    unknown = set(doc) - {"v", "login_shell", "programs", "pass_env"}
    if unknown:
        raise ConfigError(f"runner.json: unknown key {sorted(unknown)[0][:40]}")
    login = doc.get("login_shell", True)
    if type(login) is not bool:
        raise ConfigError("runner.json: login_shell is true or false")
    programs = doc.get("programs", {})
    if not isinstance(programs, dict) or any(k not in HARNESS_IDS for k in programs):
        raise ConfigError("runner.json: programs maps claude-code and codex to absolute paths")
    for path in programs.values():
        if not isinstance(path, str) or not path.startswith("/") or "\0" in path or len(path) > 1024:
            raise ConfigError("runner.json: a program is an absolute path")
    names = doc.get("pass_env", [])
    if not isinstance(names, list) or len(names) > MAX_PASS:
        raise ConfigError(f"runner.json: pass_env is a list of at most {MAX_PASS} names")
    for name in names:
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
            raise ConfigError("runner.json: pass_env holds variable names")
        if PASS_ENV_DENY.fullmatch(name):
            raise ConfigError(f"runner.json: pass_env may not name {name}")
    return RunnerConfig(login, dict(programs), tuple(sorted(set(names))))


def read_config(state_dir: Path, uid: int) -> RunnerConfig:
    """The config (defaults when there is no file). Raises :class:`ConfigError` for a file that is not a private file
    of ``uid`` or does not validate."""
    from orch.host.custody import read_json
    from orch.host.errors import HostError

    path = config_path(state_dir)
    if not os.path.lexists(path):  # only a missing file means defaults; JSON null is an invalid file (Codex P2 #4)
        return RunnerConfig()
    try:
        doc = read_json(path, uid)
    except HostError as exc:
        raise ConfigError(f"runner.json cannot be read: {exc.message}") from None
    if doc is None:  # it vanished between the two looks, or it says null: refused either way
        raise ConfigError("runner.json is not an object")
    return parse(doc)
