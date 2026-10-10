"""Usage problems found by the parser."""

from __future__ import annotations

from orch.ops.errors import OrchError

__all__ = ["UsageError"]


class UsageError(OrchError):
    """The command line does not parse. ``cmd`` (a registry name) points the hint at the right ``describe``."""

    def __init__(self, message: str, cmd: str | None = None) -> None:
        if cmd:
            super().__init__("usage", message, hint=f"orch describe {cmd}", fix=["orch", "describe", cmd])
        else:
            super().__init__("usage", message, hint="orch help", fix=["orch", "help"])
        self.cmd = cmd
