"""Suite-wide guards."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True, scope="session")
def _never_the_login_keychain():
    """No test may open the real login keychain (H5c): a store without an explicit throwaway path is refused. Session
    scope and no ``monkeypatch``: the guard must not share or reorder any test's own patches."""
    try:
        from orch.custody import keychain
    except Exception:  # noqa: BLE001 - not importable here: nothing to guard
        yield
        return
    real = keychain.KeychainStore.__init__

    def guarded(self, keychain_path=None):
        if keychain_path is None:
            raise AssertionError("a test tried to open the login keychain; pass a throwaway keychain path")
        real(self, keychain_path)

    keychain.KeychainStore.__init__ = guarded
    try:
        yield
    finally:
        keychain.KeychainStore.__init__ = real
