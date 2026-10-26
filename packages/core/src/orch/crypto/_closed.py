"""Fail-closed public boundaries for the H13-0 builders and openers (``push``, ``drop``, ``recovery``).

The invariant: a public function of those modules raises **only** its module's closed error type (or returns a complete
result). Hostile input never surfaces as a raw ``KeyError``, ``TypeError``, ``ValueError``, ``IndexError``,
``OverflowError``, ``JcsError``, ``EncodingError``, ``CryptoError`` or ``InvalidTag`` that a caller might catch
broadly and read as "absent".

**Bugs and resource problems propagate unchanged** (review F5): ``NameError``, ``AttributeError``, ``AssertionError``,
``MemoryError`` and ``RecursionError`` are never input errors, so they are never turned into the module's error. The
original exception stays chained as ``__cause__`` for debug logs; the message names only its class, never its text.

**Callers must never read ``DropError``, ``PushError`` or ``RecoveryError`` as "absent" or "not found".** They mean
"refused": malformed or hostile input, a failed check, or (the tag subclasses) failed authentication. A caller that
maps one to an absent object, a default value or a skipped check is a bug.

:func:`close_module` wraps every function named in a module's ``__all__`` once, at import, so a new public function is
covered without anyone remembering to (a test checks every name).

The error types derive from ``Exception``, not ``ValueError``, so an ``except ValueError`` elsewhere never swallows
one. An authentication failure (``InvalidTag``) becomes the module's tag subclass, so callers may still tell "this is
not for you / was tampered with" from "malformed" without catching a library type.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from cryptography.exceptions import InvalidTag

__all__ = ["close_module", "closed"]

_PROPAGATE = (NameError, AttributeError, AssertionError, MemoryError, RecursionError)


def closed(
    err: type[Exception], make_err: Callable[[str], Exception], make_tag: Callable[[str], Exception]
) -> Callable[[Any], Any]:
    """Wrap ``fn`` so that ``err`` passes through, ``InvalidTag`` becomes ``make_tag(...)`` and any other exception
    becomes ``make_err(...)``; both factories build an ``err``."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        if getattr(fn, "__orch_closed__", False):
            return fn

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except err:
                raise
            except _PROPAGATE:
                raise
            except InvalidTag as e:
                raise make_tag("authentication failed") from e
            except Exception as e:  # noqa: BLE001 - the closed boundary: every other failure is the module's error
                raise make_err(f"malformed input ({type(e).__name__})") from e

        wrapper.__orch_closed__ = True  # type: ignore[attr-defined]
        return wrapper

    return deco


def close_module(
    namespace: dict[str, Any],
    err: type[Exception],
    make_err: Callable[[str], Exception],
    make_tag: Callable[[str], Exception],
    overrides: dict[str, tuple[Callable[[str], Exception], Callable[[str], Exception]]] | None = None,
) -> None:
    """Wrap every plain function named in ``namespace["__all__"]`` (classes and constants are left alone);
    ``overrides`` gives a function its own factories."""
    import types

    for name in namespace["__all__"]:
        obj = namespace[name]
        if isinstance(obj, types.FunctionType):
            me, mt = (overrides or {}).get(name, (make_err, make_tag))
            namespace[name] = closed(err, me, mt)(obj)
