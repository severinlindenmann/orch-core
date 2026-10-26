"""Import a POSIX-only stdlib module (``pwd``, ``grp``, ``termios``, ``resource``, ``fcntl``, ``tty``) so that the
importing module still loads on Windows: there the name is a stub whose attributes (usable as default
arguments) raise ``NotImplementedError`` when called."""

from __future__ import annotations

import importlib
from typing import Any


class _Missing:
    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("__"):
            raise AttributeError(attr)
        name = self._name

        def unsupported(*_a: Any, **_k: Any) -> Any:
            raise NotImplementedError(f"{name} is not supported on Windows yet")

        return unsupported

    def __bool__(self) -> bool:
        return False


def posixmod(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError:
        return _Missing(name)
