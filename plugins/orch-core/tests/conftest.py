"""Suite-wide guards."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def real_keychains() -> str:
    """The account's real ``~/Library/Keychains``, from the password database (``$HOME`` may be redirected)."""
    import pwd

    return os.path.realpath(os.path.join(pwd.getpwuid(os.getuid()).pw_dir, "Library", "Keychains"))


def refuse_real_keychain(path) -> None:
    if path is None:
        raise AssertionError("a test tried to open the login keychain; pass a throwaway keychain path")
    target = os.path.realpath(os.fspath(path))
    real = real_keychains()
    if target == real or target.startswith(real + os.sep):
        raise AssertionError(f"a test tried to reach the real keychain {target}")


def child_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """The environment of a child process in a test: HOME is always a temporary directory, never the owner's."""
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), **extra}
    home = os.path.realpath(env["HOME"])
    assert home == os.path.realpath(tmp_path) or home.startswith(os.path.realpath(tmp_path) + os.sep), home
    assert not real_keychains().startswith(home + os.sep), "HOME must not be the owner's home"
    return env


@pytest.fixture(autouse=True, scope="session")
def _never_the_real_keychain():
    """No test may reach the owner's real keychains (H5c): the store, ``_open`` and every keychain test helper refuse
    None and any path under the account's real ``~/Library/Keychains``. Session scope and no ``monkeypatch``: the guard
    must not share or reorder any test's own patches."""
    try:
        from orch.custody import keychain
    except Exception:  # noqa: BLE001 - not importable here: nothing to guard
        yield
        return
    saved = {
        "init": keychain.KeychainStore.__init__,
        "_open": keychain._open,
        "create_throwaway_keychain": keychain.create_throwaway_keychain,
    }
    real_init, real_open, real_create = saved["init"], saved["_open"], saved["create_throwaway_keychain"]

    def init(self, keychain_path=None):
        refuse_real_keychain(keychain_path)
        real_init(self, keychain_path)

    def open_(path):
        refuse_real_keychain(path)
        return real_open(path)

    def create(path, password):
        refuse_real_keychain(path)
        return real_create(path, password)

    keychain.KeychainStore.__init__ = init
    keychain._open = open_  # unlock, lock and delete go through _open
    keychain.create_throwaway_keychain = create
    try:
        yield
    finally:
        keychain.KeychainStore.__init__ = real_init
        keychain._open = real_open
        keychain.create_throwaway_keychain = real_create
