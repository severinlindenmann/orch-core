"""D12b item 2: the hub's TLS key persists in the key store; a restart keeps the pin and the card_seq (design check G4)."""

from __future__ import annotations

import os
import stat

import pytest

from orch_hub.cli import main
from orch_hub.keys import HubKeys, KeysError
from orch_hub.keys.store import PLACEHOLDER_TLS, TLS_KEY_FILE
from orch_hub.link import LinkServer, Registry
from orch_hub.serve import FixedInvites, serve


@pytest.fixture
def state(tmp_path):
    s = tmp_path / "hub"
    HubKeys.init(s)
    return s


def _server(state, **kw):
    keys = HubKeys.open(state)
    reg = Registry(str(state / "registry.db"))
    srv = LinkServer.from_keys(keys, registry=reg, invites=FixedInvites([]), dev_mode=True, **kw).start()
    return keys, srv, reg


def _old_store(state):
    """A store from before D12b: no tls.key, the card pins the placeholder."""
    HubKeys.init(state, tls_spki_sha256=PLACEHOLDER_TLS)
    return state


def test_init_makes_a_0600_key_and_pins_it(state):
    p = state / TLS_KEY_FILE
    assert stat.S_IMODE(os.lstat(p).st_mode) == 0o600
    keys = HubKeys.open(state)
    pin = keys.card()["o"]["tls_spki_sha256"]
    assert pin != PLACEHOLDER_TLS
    assert keys.tls_identity().spki_sha256 == pin


def test_restart_keeps_pin_and_card_seq(state):
    k1, s1, r1 = _server(state)
    pin, seq = k1.card()["o"]["tls_spki_sha256"], k1.card_seq
    s1.close()
    r1.close()
    for _ in range(3):
        k2, s2, r2 = _server(state)
        assert k2.card()["o"]["tls_spki_sha256"] == pin
        assert k2.card_seq == seq
        s2.close()
        r2.close()


def test_old_store_is_refused_until_rotate_tls(tmp_path):
    state = _old_store(tmp_path / "old")
    assert HubKeys.open(state).tls_identity() is None
    keys = HubKeys.open(state)
    reg = Registry(str(state / "registry.db"))
    with pytest.raises(KeysError) as e:
        LinkServer.from_keys(keys, registry=reg, invites=FixedInvites([]), dev_mode=True)
    assert e.value.code == "tls_key_missing"
    reg.close()
    seq = keys.card_seq
    card = keys.rotate_tls()
    assert keys.card_seq == seq + 1
    assert card["o"]["tls_spki_sha256"] != PLACEHOLDER_TLS
    _, srv, r = _server(state)
    srv.close()
    r.close()


def test_ephemeral_tls_still_works_for_loopback_tests(tmp_path):
    state = _old_store(tmp_path / "old")
    keys = HubKeys.open(state)
    reg = Registry(str(state / "registry.db"))
    srv = LinkServer.from_keys(keys, registry=reg, invites=FixedInvites([]), dev_mode=True, ephemeral_tls=True).start()
    assert keys.card()["o"]["tls_spki_sha256"] != PLACEHOLDER_TLS  # the card was re-issued with the generated key
    srv.close()
    reg.close()


@pytest.mark.parametrize("pin", ["A" * 43, PLACEHOLDER_TLS])
def test_a_card_that_does_not_name_the_stored_key_is_refused(state, pin):
    keys = HubKeys.open(state)
    keys.reissue_card(tls_spki_sha256=pin)
    reg = Registry(str(state / "registry.db"))
    with pytest.raises(KeysError) as e:
        LinkServer.from_keys(keys, registry=reg, invites=FixedInvites([]), dev_mode=True)
    assert e.value.code == "tls_pin_mismatch"
    reg.close()


def test_rotate_tls_changes_the_pin_and_repairs_a_mismatch(state):
    keys = HubKeys.open(state)
    old = keys.card()["o"]["tls_spki_sha256"]
    keys.reissue_card(tls_spki_sha256="A" * 43)
    seq = keys.card_seq
    keys.rotate_tls()
    pin = keys.card()["o"]["tls_spki_sha256"]
    assert pin not in (old, "A" * 43) and keys.card_seq == seq + 1
    assert keys.tls_identity().spki_sha256 == pin
    _, srv, r = _server(state)
    srv.close()
    r.close()


def test_loose_tls_key_mode_is_refused(state):
    os.chmod(state / TLS_KEY_FILE, 0o644)
    with pytest.raises(KeysError):
        HubKeys.open(state).tls_identity()


def test_garbage_tls_key_is_refused_without_echo(state):
    (state / TLS_KEY_FILE).write_text("hunter2-not-a-key\n")
    os.chmod(state / TLS_KEY_FILE, 0o600)
    with pytest.raises(KeysError) as e:
        HubKeys.open(state).tls_identity()
    assert "hunter2" not in str(e.value)


@pytest.mark.parametrize("kw", [{"listen": "127.0.0.1:0"}])
def test_serve_refuses_a_pin_mismatch(state, kw):
    HubKeys.open(state).reissue_card(tls_spki_sha256="A" * 43)
    with pytest.raises(KeysError) as e:
        serve(state, kw["listen"], "http://127.0.0.1:9", [])
    assert e.value.code == "tls_pin_mismatch"


def test_cli_rotate_tls(state, capsys):
    before = HubKeys.open(state).card()["o"]["tls_spki_sha256"]
    assert main(["keys", "--state", str(state), "rotate-tls"]) == 0
    assert HubKeys.open(state).card()["o"]["tls_spki_sha256"] != before
    HubKeys.open(state).reissue_card(tls_spki_sha256="A" * 43)
    capsys.readouterr()
    assert main(["serve", "--state", str(state), "--listen", "127.0.0.1:0", "--publish-url", "http://127.0.0.1:9"]) == 1
    assert "tls_pin_mismatch" in capsys.readouterr().err
