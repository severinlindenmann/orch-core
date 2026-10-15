"""A base for values that carry key material (the recovery envelope, a sealed Drop version with its DEK).

Such a value never prints, formats, pickles or copies its secret fields: ``repr``/``str``/``format`` give a fixed
redacted marker (so ``json.dumps(x, default=str)`` and log formatting show only the marker), and pickling, copying and
``__getstate__`` raise. It is a plain slotted class, not a dataclass (``dataclasses.asdict`` would expose its fields)
and not a mapping. Attributes are read-only after ``__init__``. Same-process code can still read the attributes; this is
a guard against accidental serialization, not isolation.
"""

from __future__ import annotations

from typing import Any, NoReturn

__all__ = ["Secret"]


class Secret:
    __slots__ = ()

    def _refuse(self, *_: Any) -> NoReturn:
        raise TypeError(f"{type(self).__name__} holds key material and is never serialized or copied")

    def __repr__(self) -> str:
        return f"<{type(self).__name__} redacted>"

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, spec: str) -> str:
        return self.__repr__()

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    __reduce__ = _refuse
    __reduce_ex__ = _refuse
    __getstate__ = _refuse
    __setstate__ = _refuse
    __copy__ = _refuse
    __deepcopy__ = _refuse

    def __json__(self) -> str:
        return self.__repr__()

    __hash__ = None  # type: ignore[assignment]

    def _set(self, **fields: Any) -> None:
        for k, v in fields.items():
            object.__setattr__(self, k, v)
