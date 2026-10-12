"""KeychainStore against a throwaway keychain in tmp_path. Never touches the login keychain."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

if sys.platform != "darwin":
    pytest.skip("macOS keychain only", allow_module_level=True)

from orch.custody import keychain as kc
from orch.custody.base import BackendUnavailable, CustodyError, KeyExists, KeyNotFound

PW = "throwaway-test-pw"
WS = "a" * 32
ACC = f"{WS}/d_{'b' * 32}"
ACC2 = f"{WS}/d_{'c' * 32}"
OTHER = f"{'d' * 32}/d_{'e' * 32}"


@pytest.fixture
def kpath(tmp_path: Path):
    path = tmp_path / "throwaway.keychain-db"
    kc.create_throwaway_keychain(path, PW)
    try:
        yield path
    finally:
        try:
            kc.delete_keychain(path)
        except CustodyError:
            pass


@pytest.fixture
def store(kpath: Path):
    s = kc.KeychainStore(kpath)
    try:
        yield s
    finally:
        s.close()


def test_opens_the_tmp_keychain_not_login(store: kc.KeychainStore, kpath: Path) -> None:
    assert os.path.realpath(store.path) == os.path.realpath(kpath)
    assert "login.keychain" not in store.path


def test_round_trip_and_exceptions_hide_secret(store: kc.KeychainStore) -> None:
    secret = os.urandom(32)
    store.put(ACC, secret, label="orch device key", description="orch test")
    assert store.get(ACC) == secret
    assert store.exists(ACC)
    with pytest.raises(KeyExists) as ei:
        store.put(ACC, secret, label="orch device key", description="orch test")
    assert secret.hex() not in str(ei.value)


def test_missing(store: kc.KeychainStore) -> None:
    assert not store.exists(ACC)
    with pytest.raises(KeyNotFound):
        store.get(ACC)
    with pytest.raises(KeyNotFound):
        store.delete(ACC)


def test_delete(store: kc.KeychainStore) -> None:
    store.put(ACC, b"\x01" * 32, label="l", description="d")
    store.delete(ACC)
    assert not store.exists(ACC)


def test_accounts_prefix(store: kc.KeychainStore) -> None:
    for a in (ACC, ACC2, OTHER):
        store.put(a, b"\x02" * 32, label="l", description="d")
    assert store.accounts(WS + "/") == sorted([ACC, ACC2])
    assert store.accounts("") == sorted([ACC, ACC2, OTHER])
    assert store.accounts("f") == []


@pytest.mark.parametrize("bad", ["", "x", ACC.upper(), ACC + "0", f"{WS}/{'b' * 32}"])
def test_invalid_account_refused(store: kc.KeychainStore, bad: str) -> None:
    with pytest.raises(CustodyError):
        store.put(bad, b"\x03" * 32, label="l", description="d")
    with pytest.raises(CustodyError):
        store.get(bad)


def test_invalid_label_refused(store: kc.KeychainStore) -> None:
    with pytest.raises(CustodyError):
        store.put(ACC, b"\x03" * 32, label="x" * 201, description="d")
    with pytest.raises(CustodyError):
        store.put(ACC, b"\x03" * 32, label="l", description="bad\nline")


def test_locked_keychain_is_unavailable_without_prompt(store: kc.KeychainStore, kpath: Path) -> None:
    secret = os.urandom(32)
    store.put(ACC, secret, label="l", description="d")
    kc.lock_keychain(kpath)
    with pytest.raises(BackendUnavailable) as ei:
        store.get(ACC)
    assert secret.hex() not in str(ei.value)
    assert "unlock" in str(ei.value)
    kc.unlock_keychain(kpath, PW)
    assert store.get(ACC) == secret


def test_ambiguity_fails_closed() -> None:
    kc._check_match_count(1)
    with pytest.raises(KeyNotFound):
        kc._check_match_count(0)
    with pytest.raises(CustodyError, match="ambiguous"):
        kc._check_match_count(2)


def test_closed_store_refuses(kpath: Path) -> None:
    s = kc.KeychainStore(kpath)
    s.close()
    with pytest.raises(CustodyError):
        s.exists(ACC)
