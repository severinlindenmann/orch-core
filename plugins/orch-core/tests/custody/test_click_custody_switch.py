"""The dashboard's click-to-sign switch (orch-v2-host §0.8.6 item 3a): the click signer's ``custody_on`` and
``custody_off``. The device key moves into a FakeStore, never the login keychain."""

from __future__ import annotations

import os

import pytest

from orch.click.log import ClickLog
from orch.click.protocol import FEATURES, OPS, ClickError
from orch.click.signer import CUSTODY_TRIES, CUSTODY_WINDOW, ClickSigner
from orch.custody import PassphraseBackend, select
from orch.custody.convenience import FakeStore
from orch.host import convenience
from orch.ops import human
from tests.ops.humans import KDF, Provider

WS = "0123456789abcdef0123456789abcdef"
STRONG = "violet harbour 7 kettles juggle quietly!"


class Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Log(ClickLog):
    """The real signer log (its closed set of entries and field rules), also kept in memory for the assertions."""

    def __init__(self, state) -> None:
        super().__init__(state)
        self.lines: list[tuple[str, dict]] = []

    def write(self, what: str, **kw) -> None:
        super().write(what, **kw)  # raises for an entry the log does not know
        self.lines.append((what, kw))


@pytest.fixture
def switch(tmp_path, monkeypatch):
    """A passphrase device key for WS, a signer over it, and a FakeStore in place of the key store."""
    provider = Provider()
    state = tmp_path / "state"
    monkeypatch.setattr(human, "open_backend", lambda d: PassphraseBackend(d, _passphrase_provider=provider, _kdf=KDF))
    human.open_backend(human.keys_dir(state, WS)).create("dk", role="device", passphrase=provider.passphrase)
    store = FakeStore()
    monkeypatch.setattr(select, "_store", lambda _state: store)
    uid = os.getuid()
    entry = {"workspace_id": WS, "prefix": "INT", "host_uid": uid}
    mono, log = Mono(), Log(state)
    signer = ClickSigner(
        state,
        uid=uid,
        connect=lambda e: pytest.fail("the switch never connects to the host"),
        workspaces=lambda: [entry],
        enabled=lambda e: convenience.enabled(state, WS, uid),
        mono=mono,
        log=log,
    )
    yield signer, state, provider, store, mono, log
    signer.close()


def refused(fn, code: str) -> ClickError:
    with pytest.raises(ClickError) as info:
        fn()
    assert info.value.code == code, info.value
    return info.value


def on(signer, passphrase):
    return signer.handle("custody_on", {"workspace_id": WS, "passphrase": passphrase})


def flag(state) -> bool:
    return convenience.enabled(state, WS, os.getuid())


def test_the_signer_advertises_the_switch():
    assert {"custody_on", "custody_off"} <= set(OPS) and "custody" in FEATURES


def test_status_lists_the_feature(switch):
    signer, *_ = switch
    out = signer.status({})
    assert out["features"] == ["custody"]
    (row,) = out["workspaces"]
    assert row["custody"] == "passphrase" and row["enabled"] is False and row["problem"] == "not_convenience"


def test_on_moves_the_key_and_sets_the_flag(switch):
    signer, state, provider, store, _, _ = switch
    row = on(signer, provider.passphrase)
    assert row["custody"] == "convenience" and row["enabled"] is True and row["available"] is True
    assert flag(state) and store.exists(WS, row["device"])
    assert not (human.keys_dir(state, WS) / human.KEY_FILE).exists()
    assert on(signer, provider.passphrase)["available"] is True  # again: already on, nothing to move


def test_a_wrong_passphrase_changes_nothing(switch):
    signer, state, *_ = switch
    refused(lambda: on(signer, "not the passphrase"), "custody.wrong_passphrase")
    assert not flag(state) and select.status(state, WS)["custody"] == "passphrase"
    assert (human.keys_dir(state, WS) / human.KEY_FILE).exists()


def test_wrong_passphrases_are_limited_then_the_window_passes(switch):
    signer, state, provider, _, mono, _ = switch
    for _ in range(CUSTODY_TRIES):
        refused(lambda: on(signer, "wrong"), "custody.wrong_passphrase")
    err = refused(lambda: on(signer, provider.passphrase), "rate.limited")  # even the right one waits
    assert err.retryable and 0 < err.retry_after_ms <= CUSTODY_WINDOW * 1000
    assert not flag(state)
    mono.t += CUSTODY_WINDOW + 1
    assert on(signer, provider.passphrase)["enabled"] is True


def test_on_waits_for_pending_terminal_handoffs(switch, monkeypatch):
    signer, state, provider, *_ = switch
    monkeypatch.setattr(convenience, "pending_handoffs", lambda *a, **k: [{"id": "hr_1", "op": "approve", "code": "x"}])
    refused(lambda: on(signer, provider.passphrase), "sign.click_only")
    assert not flag(state) and select.status(state, WS)["custody"] == "passphrase"


def test_off_without_a_passphrase_keeps_the_key_in_the_store(switch):
    signer, state, provider, store, _, _ = switch
    device = on(signer, provider.passphrase)["device"]
    row = signer.handle("custody_off", {"workspace_id": WS})
    assert row["enabled"] is False and row["custody"] == "convenience" and row["problem"] == "disabled"
    assert not flag(state) and store.exists(WS, device)


def test_off_with_a_new_passphrase_moves_the_key_back(switch):
    signer, state, provider, store, _, _ = switch
    device = on(signer, provider.passphrase)["device"]
    row = signer.handle("custody_off", {"workspace_id": WS, "passphrase": STRONG})
    assert row["enabled"] is False and row["custody"] == "passphrase"
    assert not store.exists(WS, device) and (human.keys_dir(state, WS) / human.KEY_FILE).exists()


def test_off_refuses_a_weak_new_passphrase_before_changing_anything(switch):
    signer, state, provider, store, _, _ = switch
    device = on(signer, provider.passphrase)["device"]
    refused(
        lambda: signer.handle("custody_off", {"workspace_id": WS, "passphrase": "password1"}), "custody.weak_passphrase"
    )
    assert flag(state) and store.exists(WS, device)


@pytest.mark.parametrize(
    "args",
    [
        {"workspace_id": WS},
        {"workspace_id": WS, "passphrase": ""},
        {"workspace_id": WS, "passphrase": 7},
        {"workspace_id": WS, "passphrase": "x" * 1025},
    ],
)
def test_on_needs_a_passphrase(switch, args):
    signer, state, *_ = switch
    refused(lambda: signer.handle("custody_on", args), "invalid.input")
    assert not flag(state)


def test_an_unknown_workspace_is_not_found(switch):
    signer, *_ = switch
    refused(lambda: signer.handle("custody_on", {"workspace_id": "f" * 32, "passphrase": "x"}), "not_found")
    refused(lambda: signer.handle("custody_off", {"workspace_id": "f" * 32}), "not_found")


def test_the_passphrase_never_reaches_the_log_or_an_answer(switch):
    signer, _, provider, _, _, log = switch
    refused(lambda: on(signer, "wrong secret words"), "custody.wrong_passphrase")
    row = on(signer, provider.passphrase)
    signer.handle("custody_off", {"workspace_id": WS, "passphrase": STRONG})
    text = repr(log.lines) + repr(row)
    for secret in ("wrong secret words", provider.passphrase, STRONG):
        assert secret not in text
    assert [what for what, _ in log.lines if what.startswith("custody")] == ["custody_on", "custody_off"]


def test_the_written_log_has_the_switch_and_no_passphrase(switch):
    signer, state, provider, _, _, log = switch
    on(signer, provider.passphrase)
    signer.handle("custody_off", {"workspace_id": WS})
    text = (log.root / "click.jsonl").read_text()
    assert '"what": "custody_on"' in text and '"what": "custody_off"' in text
    assert provider.passphrase not in text
