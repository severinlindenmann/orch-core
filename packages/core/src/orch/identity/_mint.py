"""The content-bound mint of the H12c checked result types (review round 2 of #495, Opus D1-1/D1-2).

A type such as ``VerifiedCard`` promises "a check ran on exactly this content". Its ``mint`` field is an HMAC, under a
key private to this process, over the class name and **every** other field. ``__post_init__`` recomputes it, so
``dataclasses.replace`` or a constructor call with any changed field is refused; consumers call :func:`check` again,
so an instance assembled around ``__init__`` (``object.__setattr__``) is refused where it is used. ``copy``,
``deepcopy`` and ``pickle`` within the process keep the mint valid; an instance from another process does not verify
(a checked result is not a wire format). Same-process code that reads this module's key can still forge one: that is
the documented limit of in-process type rules (defence in depth, as the import ban).

Fields must be ``str``, ``bytes``, ``int``, ``bool`` or ``None``; mutable objects are stored as their ``cj`` bytes.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import secrets
from typing import Any, TypeVar

from orch import canon

from .errors import Refused

__all__ = ["check", "make", "no_subclass"]

_KEY = secrets.token_bytes(32)
T = TypeVar("T")


def _encode(value: object) -> object:
    if type(value) is bytes:
        return {"b": value.hex()}
    if value is None or type(value) in (str, int, bool):
        return value
    raise Refused("malformed", "a minted field must be str, bytes, int, bool or None")


def _tag(cls: type, values: dict[str, Any]) -> bytes:
    body = canon.dumps({k: _encode(v) for k, v in values.items()})
    return hmac.new(_KEY, cls.__qualname__.encode("ascii") + b"\0" + body, hashlib.sha256).digest()


def _content(obj: object) -> dict[str, Any]:
    return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj) if f.name != "mint"}  # type: ignore[arg-type]


def make(cls: type[T], **values: Any) -> T:
    """An instance of ``cls`` minted over ``values`` (the checker's only way to build one)."""
    return cls(**values, mint=_tag(cls, values))


def check(obj: object, cls: type) -> None:
    """Refuse (``malformed``) unless ``obj`` is exactly a ``cls`` whose mint matches its content."""
    if type(obj) is not cls:
        raise Refused("malformed", f"not a {cls.__name__}")
    mint = getattr(obj, "mint", None)
    if type(mint) is not bytes or not hmac.compare_digest(mint, _tag(cls, _content(obj))):
        raise Refused("malformed", f"a {cls.__name__} whose content is not the checked one")


def no_subclass(cls: type, **_: Any) -> None:
    """``__init_subclass__`` of a minted type: a subclass could change what the type promises."""
    raise TypeError(f"{cls.__name__}: minted result types cannot be subclassed")
