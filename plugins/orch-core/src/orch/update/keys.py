"""The embedded root public keys that anchor every signed release and add-on catalog.

``PRODUCTION_ROOTS`` is empty until task U3b embeds the real root set (its private half never leaves the owner).
The TEST root below is derived from a public seed (``tests/vectors/release/gen_release_vectors.py``): anyone can
sign with it. It is therefore never part of :func:`embedded_roots` unless a test enters
:func:`enable_test_root_for_tests`.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

__all__ = ["PRODUCTION_ROOTS", "TEST_ROOTS", "embedded_roots", "test_hook_active", "enable_test_root_for_tests"]

# key id -> raw 32-byte Ed25519 public key. Filled by U3b.
PRODUCTION_ROOTS: Mapping[str, bytes] = {}

# TEST ONLY (public seed, see the module docstring): never copy into PRODUCTION_ROOTS.
TEST_ROOTS: Mapping[str, bytes] = {
    "root-1": base64.b64decode("/f5l4aGiNAveBGbZW+bAnbg7JJ3KpBc2d2YfyvZy/2k="),
}

_test_hook_depth = 0


@contextmanager
def enable_test_root_for_tests() -> Iterator[None]:
    """Make :func:`embedded_roots` include the TEST root. For tests only; nothing in ``src`` may call this."""
    global _test_hook_depth
    clash = set(TEST_ROOTS) & set(PRODUCTION_ROOTS)
    if clash:  # the TEST root must never shadow (or be mistaken for) a production root
        raise RuntimeError(f"test root id collides with a production root: {sorted(clash)}")
    _test_hook_depth += 1
    try:
        yield
    finally:
        _test_hook_depth -= 1


def test_hook_active() -> bool:
    return _test_hook_depth > 0


def embedded_roots() -> Mapping[str, bytes]:
    """The trusted roots a production verifier uses."""
    if _test_hook_depth > 0:
        return {**PRODUCTION_ROOTS, **TEST_ROOTS}
    return dict(PRODUCTION_ROOTS)
