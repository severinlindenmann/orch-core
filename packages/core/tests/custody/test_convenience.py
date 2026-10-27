"""H5c: the convenience custody backend, its stores and the custody transitions (orch-v2-host §0.8.6 items 2, 3).
No test touches the login keychain: a FakeStore, a FileStore under tmp_path, or a throwaway keychain file."""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

from orch import canon, crypto
from orch.custody import PassphraseBackend, select
from orch.custody.base import BackendUnavailable, CustodyError, KeyNotFound
from orch.custody.convenience import ConvenienceBackend, FakeStore, FileStore
from orch.identity import device_ref
from orch.ops import human
from tests.ops.humans import KDF, Provider

WS = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _no_exposure_mark(monkeypatch):
    """#1097 E3 needs a registered workspace and a running host (tests: test_key_exposure_custody.py)."""
    from orch.custody import exposure

    monkeypatch.setattr(exposure, "mark_before_move", lambda *a, **k: None)


def _payload(label: str = "sig_ticket_event") -> bytes:
    return canon.LABELS[label].encode("ascii") + b'{"x":1}'


def _key() -> tuple[bytes, bytes, str]:
    k = crypto.generate_private_key()
    pub = crypto.public_bytes(k)
    return crypto.private_scalar(k), pub, device_ref(pub)


# --- FileStore --------------------------------------------------------------------------------------------------------


def test_the_file_store_keeps_a_0600_key_in_0700_directories(tmp_path):
    scalar, _, dev = _key()
    fs = FileStore(tmp_path / "person")
    fs.put(WS, dev, scalar)
    path = tmp_path / "person" / WS / "devices" / f"{dev}.key"
    assert path.stat().st_mode & 0o777 == 0o600
    for d in (tmp_path / "person", tmp_path / "person" / WS, path.parent):
        assert d.stat().st_mode & 0o777 == 0o700
    assert fs.get(WS, dev) == scalar and fs.exists(WS, dev)
    fs.delete(WS, dev)
    assert not fs.exists(WS, dev)
    with pytest.raises(KeyNotFound):
        fs.get(WS, dev)


def test_the_file_store_refuses_loose_modes_links_and_symlinks(tmp_path):
    scalar, _, dev = _key()
    fs = FileStore(tmp_path / "person")
    fs.put(WS, dev, scalar)
    path = tmp_path / "person" / WS / "devices" / f"{dev}.key"
    path.chmod(0o640)
    with pytest.raises(CustodyError, match="accessible by others"):
        fs.get(WS, dev)
    path.chmod(0o600)
    os.link(path, tmp_path / "copy")
    with pytest.raises(CustodyError, match="hard link"):
        fs.get(WS, dev)
    os.unlink(tmp_path / "copy")
    (tmp_path / "person" / WS).chmod(0o755)
    fs.get(WS, dev)  # B2: a loose directory of ours is tightened, not refused
    assert (tmp_path / "person" / WS).stat().st_mode & 0o077 == 0
    # a symlinked devices directory is never followed
    real = tmp_path / "person" / WS / "devices"
    os.rename(real, tmp_path / "elsewhere")
    os.symlink(tmp_path / "elsewhere", real)
    with pytest.raises(CustodyError):
        fs.get(WS, dev)


def test_the_file_store_refuses_a_group_writable_ancestor(tmp_path):
    scalar, _, dev = _key()
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o777)
    try:
        with pytest.raises(CustodyError, match="writable by others"):
            FileStore(loose / "state" / "person").put(WS, dev, scalar)
    finally:
        loose.chmod(0o700)


def test_errors_never_carry_key_bytes(tmp_path):
    scalar, _, dev = _key()
    fs = FileStore(tmp_path / "person")
    fs.put(WS, dev, scalar)
    path = tmp_path / "person" / WS / "devices" / f"{dev}.key"
    doc = json.loads(path.read_text())
    doc["device"] = "d_" + "0" * 32
    path.write_text(json.dumps(doc))
    with pytest.raises(CustodyError) as info:
        fs.get(WS, dev)
    assert scalar.hex() not in str(info.value) and crypto.b64u(scalar) not in str(info.value)


# --- the backend ------------------------------------------------------------------------------------------------------


def test_the_backend_signs_device_labels_only_and_checks_its_device():
    scalar, pub, dev = _key()
    store = FakeStore()
    store.put(WS, dev, scalar)
    b = ConvenienceBackend(store, WS, dev, pub)
    assert b.auth == "convenience" and b.person_capable
    payload = _payload()
    assert crypto.verify(pub, b.sign("dk", payload, action="x"), payload)
    with pytest.raises(CustodyError, match="does not sign"):
        b.sign("dk", b"orch/v2/sig/device-cert|{}", action="x")
    with pytest.raises(CustodyError, match="does not sign"):
        b.sign("dk", canon.LABELS["sig_host_event"].encode() + b"{}", action="x")
    assert b.public_key("dk") == pub
    before = store.gets
    b.public_key("dk")
    assert store.gets == before  # the public key comes from the selection
    other, _, _ = _key()
    store.items[(WS, dev)] = other
    with pytest.raises(CustodyError, match="does not match"):
        b.sign("dk", payload, action="x")
    with pytest.raises(CustodyError):
        ConvenienceBackend(store, WS, dev, crypto.public_bytes(crypto.generate_private_key()))
    with pytest.raises(CustodyError):
        b.create("dk")


# --- transitions ----------------------------------------------------------------------------------------------


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """A passphrase device key where H5b's ``orch sign`` looks, with the test passphrase provider."""
    provider = Provider()
    state = tmp_path / "state"
    directory = human.keys_dir(state, WS)
    monkeypatch.setattr(human, "open_backend", lambda d: PassphraseBackend(d, _passphrase_provider=provider, _kdf=KDF))
    backend = human.open_backend(directory)
    pub = backend.create("dk", role="device", passphrase=provider.passphrase)
    return state, provider, pub


def test_convert_keeps_the_device_and_removes_the_passphrase_copy(legacy):
    state, provider, pub = legacy
    store = FakeStore()
    asked = len(provider.requests)
    sel = select.convert_to_convenience(state, WS, store=store)
    assert len(provider.requests) == asked + 1 and provider.requests[-1].kind == "convert"
    assert sel["custody"] == "convenience" and sel["device"] == device_ref(pub) and sel.gen == 1
    assert not (human.keys_dir(state, WS) / human.KEY_FILE).exists()
    assert store.exists(WS, sel["device"])
    st = select.status(state, WS)
    gets = store.gets
    assert st == {"custody": "convenience", "device": sel["device"], "gen": 1}
    assert store.gets == gets
    import orch.custody.convenience as conv

    conv_open = conv.open_store
    try:
        conv.open_store = lambda s: store
        backend, got = select.device_backend(state, WS)
    finally:
        conv.open_store = conv_open
    assert backend.auth == "convenience" and got.gen == 1
    assert crypto.verify(pub, backend.sign("dk", _payload(), action="x"), _payload())
    sel_path = state / "person" / WS / "custody.json"
    assert sel_path.stat().st_mode & 0o777 == 0o600


def test_an_interrupted_conversion_is_reported_and_resumed_without_a_second_prompt(legacy, monkeypatch):
    state, provider, pub = legacy
    store = FakeStore()

    def boom(self):
        raise CustodyError("power cut")

    with monkeypatch.context() as m:
        m.setattr(ConvenienceBackend, "verify_capability", boom)
        with pytest.raises(CustodyError, match="power cut"):
            select.convert_to_convenience(state, WS, store=store)
    st = select.status(state, WS)
    assert st["problem"] == "unfinished change to convenience"
    with pytest.raises(BackendUnavailable):
        select.device_backend(state, WS)
    assert (human.keys_dir(state, WS) / human.KEY_FILE).exists()  # the source stays until the destination is proven
    asked = len(provider.requests)
    sel = select.convert_to_convenience(state, WS, store=store)
    assert len(provider.requests) == asked and sel["custody"] == "convenience"


def test_downgrade_in_place_reencrypts_the_same_key_and_bumps_the_generation(legacy):
    state, provider, pub = legacy
    store = FakeStore()
    sel = select.convert_to_convenience(state, WS, store=store)
    back = select.downgrade_in_place(state, WS, store=store)  # the backend asks (the test provider)
    assert back["custody"] == "passphrase" and back.gen == sel.gen + 1 and back["device"] == sel["device"]
    assert not store.exists(WS, sel["device"])
    b = human.open_backend(human.keys_dir(state, WS))
    assert b.public_key("dk") == pub
    backend, _ = select.device_backend(state, WS)
    assert backend.auth == "passphrase"


def test_forget_removes_local_custody_only(legacy):
    state, _, _ = legacy
    store = FakeStore()
    select.convert_to_convenience(state, WS, store=store)
    assert select.forget(state, WS, store=store) == ["convenience key", "selection"]
    assert select.status(state, WS)["custody"] == "none"


def test_only_the_custody_change_exports_a_device_key():
    src = Path(select.__file__).parent.parent
    users = [
        p.relative_to(src).as_posix()
        for p in src.rglob("*.py")
        if "export_for_custody_change(" in p.read_text(encoding="utf-8")
    ]
    assert sorted(users) == ["custody/passphrase.py", "custody/select.py"]


def test_a_corrupt_selection_is_refused(legacy):
    state, _, _ = legacy
    select.convert_to_convenience(state, WS, store=FakeStore())
    path = state / "person" / WS / "custody.json"
    doc = json.loads(path.read_text())
    doc["device"] = "d_" + "1" * 32
    path.write_text(json.dumps(doc))
    with pytest.raises(CustodyError, match="corrupt"):
        select.read_selection(state, WS)


# --- F1: the value and old readers ------------------------------------------------------------------------------------


def test_an_old_reader_refuses_a_convenience_event_and_the_new_one_accepts_it():
    from jsonschema import Draft202012Validator

    from orch import schema

    common = schema.load("common")
    assert "convenience" in common["$defs"]["auth"]["enum"]
    old = copy.deepcopy(common["$defs"]["auth"])
    old["enum"].remove("convenience")
    assert not Draft202012Validator(old).is_valid("convenience")
    assert Draft202012Validator(common["$defs"]["auth"]).is_valid("convenience")


# --- the key file is the store on every OS --------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_open_store_is_the_owner_only_key_file_on_every_os(tmp_path):
    from orch.custody.convenience import FileStore, open_store

    store = open_store(tmp_path)
    assert isinstance(store, FileStore) and store.kind == "file"
    scalar, pub, dev = _key()
    store.put(WS, dev, scalar, prefix="DEMO")
    path = tmp_path / "person" / WS / "devices" / f"{dev}.key"
    assert path.stat().st_mode & 0o777 == 0o600
    b = ConvenienceBackend(store, WS, dev, pub)
    assert crypto.verify(pub, b.sign("dk", _payload(), action="x"), _payload())


def test_open_store_never_picks_the_keychain(tmp_path, monkeypatch):
    from orch.custody import convenience

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(convenience, "KeychainAdapter", lambda *a, **k: pytest.fail("keychain on the core path"))
    assert convenience.open_store(tmp_path).kind == "file"


# --- the macOS keychain store (optional extra), on a throwaway keychain ---------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS keychain")
def test_the_keychain_adapter_round_trips_through_the_backend(tmp_path):
    from orch.custody import keychain
    from orch.custody.convenience import KeychainAdapter

    path = tmp_path / "throwaway.keychain-db"
    keychain.create_throwaway_keychain(path, "throwaway-password")
    try:
        store = KeychainAdapter(path)
        scalar, pub, dev = _key()
        store.put(WS, dev, scalar, prefix="DEMO")
        b = ConvenienceBackend(store, WS, dev, pub)
        assert crypto.verify(pub, b.sign("dk", _payload(), action="x"), _payload())
        keychain.lock_keychain(path)
        with pytest.raises(BackendUnavailable):
            b.sign("dk", _payload(), action="x")
        keychain.unlock_keychain(path, "throwaway-password")
        store.delete(WS, dev)
        assert not store.exists(WS, dev)
    finally:
        keychain.delete_keychain(path)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS keychain")
def test_the_suite_guard_refuses_the_login_keychain():
    from orch.custody.convenience import KeychainAdapter

    with pytest.raises(AssertionError, match="login keychain"):
        KeychainAdapter()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS keychain")
def test_the_real_keychains_are_unreachable_by_explicit_path_too():
    from orch.custody import keychain
    from orch.custody.convenience import KeychainAdapter
    from tests.conftest import real_keychains

    login = os.path.join(real_keychains(), "login.keychain-db")
    for call in (
        lambda: KeychainAdapter(login),
        lambda: keychain.KeychainStore(login),
        lambda: keychain.unlock_keychain(login, "x"),
        lambda: keychain.lock_keychain(login),
        lambda: keychain.delete_keychain(login),
        lambda: keychain.create_throwaway_keychain(os.path.join(real_keychains(), "new.keychain-db"), "x"),
    ):
        with pytest.raises((AssertionError, CustodyError)):
            call()
    with pytest.raises(CustodyError):  # the module itself refuses too, guard or not
        keychain._refuse_real_keychains(login)


def test_the_backend_never_signs_a_reader_declaration():
    """#463 review L1, decided in H12d-2: sig_reader_decl is a device-key label, but the convenience backend signs
    without any review, so it refuses the label outright; the passphrase prompt shows every field instead."""
    scalar, pub, dev = _key()
    store = FakeStore()
    store.put(WS, dev, scalar)
    b = ConvenienceBackend(store, WS, dev, pub)
    before = store.gets
    with pytest.raises(CustodyError, match="does not sign reader declarations"):
        b.sign("dk", canon.LABELS["sig_reader_decl"].encode() + b"{}", action="x")
    assert store.gets == before and b.signatures == 0  # refused before the key is read
