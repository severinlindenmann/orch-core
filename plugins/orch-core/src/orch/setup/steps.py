"""The pieces of the setup run: the secrets of one session now, the create sequence in later commits."""

from __future__ import annotations

import secrets as _secrets
import threading
from typing import Any

from .protocol import SetupError

__all__ = ["CONFIRM_ATTEMPTS", "Secrets", "set_no_core_dumps"]

CONFIRM_ATTEMPTS = 5
CODE_WORDS = 24
CHECKS = 3


def set_no_core_dumps() -> None:
    """RLIMIT_CORE=0: a crash of the setup process leaves no core file holding the passphrase or the recovery code."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):  # pragma: no cover - not available here: nothing more can be done
        pass


def _wipe(buf: bytearray) -> None:
    for i in range(len(buf)):
        buf[i] = 0


class Secrets:
    """The device passphrase and the recovery code of ONE session, generated in this process, held in bytearrays and
    wiped when the session ends. ``reveal`` works once (a second call is ``secrets.shown``, 410); ``confirm`` checks
    three words in core; five wrong tries end the session and the next session draws new secrets.

    Python cannot promise that no copy of a ``str`` survives (the custody API takes ``str``); the copies made by
    :meth:`passphrase` and :meth:`code` are as short-lived as the caller makes them. No secret is ever in a log, an
    error or a state file."""

    def __init__(self, *, show_passphrase: bool) -> None:
        from orch.custody.strength import generate_passphrase
        from orch.identity import generate_recovery_code

        self._lock = threading.Lock()
        self.show_passphrase = show_passphrase
        self._pass = bytearray(generate_passphrase().encode("ascii"))
        self._code = bytearray(generate_recovery_code().encode("ascii"))
        self.positions = sorted(_secrets.SystemRandom().sample(range(1, CODE_WORDS + 1), CHECKS))
        self.shown = False
        self.confirmed = False
        self.attempts_left = CONFIRM_ATTEMPTS
        self.dead = False

    # -- the browser's two calls
    def reveal(self) -> dict[str, Any]:
        with self._lock:
            if self.dead:
                raise SetupError("secrets.gone", "these secrets are gone; start the setup again", 410)
            if self.shown:
                raise SetupError("secrets.shown", "the secrets were shown once and are not shown again", 410)
            self.shown = True
            words = bytes(self._code).decode("ascii").split()
            return {
                "passphrase": bytes(self._pass).decode("ascii") if self.show_passphrase else None,
                "recovery_words": words,
                "check_positions": list(self.positions),
            }

    def confirm(self, given: Any) -> dict[str, Any]:
        with self._lock:
            if self.dead:
                raise SetupError("secrets.gone", "these secrets are gone; start the setup again", 410)
            if not self.shown:
                raise SetupError("secrets.not_shown", "show the secrets first", 409)
            if self.confirmed:
                return {"ok": True}
            if not isinstance(given, dict) or set(given) != {str(p) for p in self.positions}:
                raise SetupError("invalid.input", "words are the three asked positions", field="words")
            words = bytes(self._code).decode("ascii").split()
            ok = True
            for p in self.positions:  # all three are compared, whatever the first said
                w = given[str(p)]
                ok &= isinstance(w, str) and w.strip().lower() == words[p - 1]
            del words
            if ok:
                self.confirmed = True
                return {"ok": True}
            self.attempts_left -= 1
            if self.attempts_left <= 0:
                self._wipe()
            return {"ok": False, "attempts_left": self.attempts_left}

    # -- the create step's
    def passphrase(self) -> str:
        with self._lock:
            if self.dead or not self.confirmed:
                raise SetupError("secrets.not_confirmed", "the secrets were not confirmed", 409)
            return bytes(self._pass).decode("ascii")

    def code(self) -> str:
        with self._lock:
            if self.dead or not self.confirmed:
                raise SetupError("secrets.not_confirmed", "the secrets were not confirmed", 409)
            return bytes(self._code).decode("ascii")

    def _wipe(self) -> None:
        _wipe(self._pass)
        _wipe(self._code)
        self.dead = True

    def wipe(self) -> None:
        with self._lock:
            self._wipe()

    @property
    def exhausted(self) -> bool:
        return self.dead and self.attempts_left <= 0

    def __repr__(self) -> str:  # never the secrets
        return "Secrets(<redacted>)"
