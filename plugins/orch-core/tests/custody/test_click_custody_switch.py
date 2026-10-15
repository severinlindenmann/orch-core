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
TERMINAL = "custody.terminal_needed"


class Mono:
    def __init__(self) -> None:
        self.t = 1_760_000_000.0  # the switch's rate limit runs on wall time (it outlives the process)

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
        wall=mono,
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
    refused(lambda: signer.handle("custody_off", {"workspace_id": WS, "passphrase": STRONG}), TERMINAL)
    text = repr(log.lines) + repr(row)
    for secret in ("wrong secret words", provider.passphrase, STRONG):
        assert secret not in text
    assert [what for what, _ in log.lines if what.startswith("custody")] == ["custody_on"]


def test_the_written_log_has_the_switch_and_no_passphrase(switch):
    signer, state, provider, _, _, log = switch
    on(signer, provider.passphrase)
    signer.handle("custody_off", {"workspace_id": WS})
    text = (log.root / "click.jsonl").read_text()
    assert '"what": "custody_on"' in text and '"what": "custody_off"' in text
    assert provider.passphrase not in text


# -- fix-forward of the Opus review of #539 (H1, H2, M1, M2, L1-L3) ------------------------------------------------


def off(signer, **extra):
    return signer.handle("custody_off", {"workspace_id": WS, **extra})


def legacy_file(state):
    return human.keys_dir(state, WS) / human.KEY_FILE


def test_h1_on_after_off_refuses_any_passphrase(switch):
    """Probe of H1: on -> off -> on with a wrong passphrase used to switch on with no proof at all."""
    signer, state, provider, *_ = switch
    on(signer, provider.passphrase)
    off(signer)
    err = refused(lambda: on(signer, "not the passphrase"), TERMINAL)
    assert "orch host convenience on" in err.message
    assert not flag(state)
    refused(lambda: on(signer, provider.passphrase), TERMINAL)  # nothing to prove the right one against either
    assert not flag(state) and select.status(state, WS)["custody"] == "convenience"


def test_h1_a_key_converted_elsewhere_with_the_flag_off_is_refused(switch):
    signer, state, provider, store, _, _ = switch
    select.convert_to_convenience(state, WS, store=store)  # the terminal path (its prompt is the test provider)
    assert not flag(state)
    refused(lambda: on(signer, "anything at all"), TERMINAL)
    assert not flag(state)


def test_h1_the_refusal_costs_no_try(switch):
    signer, state, provider, *_ = switch
    on(signer, provider.passphrase)
    off(signer)
    for _ in range(CUSTODY_TRIES + 1):
        refused(lambda: on(signer, "x"), TERMINAL)
    assert not (state / "person" / "click-custody.json").exists()


def test_h1_already_on_is_idempotent(switch):
    signer, state, provider, *_ = switch
    on(signer, provider.passphrase)
    assert on(signer, "any text: nothing changes")["enabled"] is True
    assert flag(state)


def test_h1_an_unfinished_change_is_refused(switch):
    signer, state, provider, store, _, _ = switch
    sel = select.convert_to_convenience(state, WS, store=store)
    select._write_selection(state, WS, {**sel, "transition": {"to": "passphrase", "step": "written"}})
    refused(lambda: on(signer, provider.passphrase), "custody.unavailable")
    assert not flag(state)


@pytest.mark.parametrize("flag_on", [True, False])
def test_h2_off_never_takes_a_new_passphrase(switch, flag_on):
    """Probe of H2: off(P) re-keyed the device key under a dashboard-chosen P, then on(P) "proved" it."""
    signer, state, provider, store, _, _ = switch
    device = on(signer, provider.passphrase)["device"]
    if not flag_on:
        off(signer)
    err = refused(lambda: off(signer, passphrase=STRONG), TERMINAL)
    assert "orch keys custody passphrase" in err.message and "--in-place" in err.message
    assert flag(state) is flag_on  # nothing changed: not even the flag (L2)
    assert select.status(state, WS)["custody"] == "convenience" and store.exists(WS, device)
    assert not legacy_file(state).exists()
    if not flag_on:  # and the dashboard cannot then "prove" a passphrase it chose
        refused(lambda: on(signer, STRONG), TERMINAL)
        assert not flag(state)


def test_h2_a_weak_or_odd_passphrase_on_off_is_refused_the_same_way(switch):
    signer, state, provider, *_ = switch
    on(signer, provider.passphrase)
    for pw in ("password1", "", 7):
        refused(lambda pw=pw: off(signer, passphrase=pw), TERMINAL)
    assert flag(state)


def test_m1_wrong_passphrases_survive_a_signer_restart(switch, tmp_path):
    signer, state, provider, _, clock, _ = switch
    for _ in range(CUSTODY_TRIES):
        refused(lambda: on(signer, "wrong"), "custody.wrong_passphrase")
    signer.close()
    again = ClickSigner(
        state,
        uid=os.getuid(),
        connect=lambda e: pytest.fail("no host"),
        workspaces=signer._workspaces,
        enabled=signer._enabled,
        mono=clock,
        wall=clock,
    )
    try:
        err = refused(lambda: on(again, provider.passphrase), "rate.limited")
        assert "terminal" in err.message and 0 < err.retry_after_ms <= CUSTODY_WINDOW * 1000
        path = state / "person" / "click-custody.json"
        assert path.stat().st_mode & 0o777 == 0o600
        assert provider.passphrase not in path.read_text() and "wrong" not in path.read_text()
        clock.t += CUSTODY_WINDOW + 1
        assert on(again, provider.passphrase)["enabled"] is True
    finally:
        again.close()


def test_m1_the_record_is_bounded(switch):
    import json

    signer, state, provider, _, clock, _ = switch
    for _ in range(3):
        for _ in range(CUSTODY_TRIES):
            refused(lambda: on(signer, "wrong"), "custody.wrong_passphrase")
        refused(lambda: on(signer, "wrong"), "rate.limited")
        clock.t += CUSTODY_WINDOW + 1
    doc = json.loads((state / "person" / "click-custody.json").read_text())
    assert len(doc["misses"]) <= CUSTODY_TRIES


def test_m1_an_unreadable_record_fails_closed_for_one_window(switch):
    signer, state, provider, _, clock, _ = switch
    refused(lambda: on(signer, "wrong"), "custody.wrong_passphrase")
    path = state / "person" / "click-custody.json"
    path.write_text("not json")
    refused(lambda: on(signer, provider.passphrase), "rate.limited")
    clock.t += CUSTODY_WINDOW + 1
    assert on(signer, provider.passphrase)["enabled"] is True


def test_m1_the_terminal_path_is_not_limited(switch):
    signer, state, provider, store, _, _ = switch
    for _ in range(CUSTODY_TRIES):
        refused(lambda: on(signer, "wrong"), "custody.wrong_passphrase")
    convenience.set_enabled(state, WS, True, os.getuid())  # orch host convenience on
    assert select.convert_to_convenience(state, WS, store=store)["custody"] == "convenience"


def test_m2_the_answering_backend_only_exports(tmp_path, switch):
    _, state, provider, *_ = switch
    backend = PassphraseBackend.answering(human.keys_dir(state, WS), provider.passphrase)
    for name in ("sign", "unlocked", "unseal", "primary_unlocked", "create", "create_primary", "_provider"):
        assert not hasattr(backend, name), name
    assert backend.public_key("dk") and callable(backend.export_for_custody_change) and callable(backend.delete)


def test_l1_a_leftover_passphrase_file_is_not_reported_as_moved_back(switch, monkeypatch):
    """L1: a stale key file of the same device (a best-effort delete that failed, a restored backup) kept its OLD
    passphrase while the move back reported success. The terminal path now refuses and says why."""
    signer, state, provider, store, _, _ = switch
    stale = legacy_file(state).read_bytes()
    device = on(signer, provider.passphrase)["device"]
    legacy_file(state).write_bytes(stale)
    os.chmod(legacy_file(state), 0o600)
    with pytest.raises(select.CustodyError) as info:
        select.downgrade_in_place(state, WS, store=store)
    assert "already" in str(info.value)
    assert select.status(state, WS)["custody"] == "convenience" and store.exists(WS, device)


def test_l3_the_slow_part_runs_outside_the_flag_lock(switch, monkeypatch):
    """L3: scrypt and the key store write must not hold the flag lock (hand-offs would get lock.busy)."""
    signer, state, provider, store, _, _ = switch
    monkeypatch.setattr(convenience, "LOCK_WAIT", 0.2)
    seen = []
    put = store.put

    def put_and_take_the_lock(*a, **k):
        with convenience.workspace_lock(state, WS, os.getuid()):  # lock.busy if the switch held it here
            seen.append("free")
        return put(*a, **k)

    monkeypatch.setattr(store, "put", put_and_take_the_lock)
    assert on(signer, provider.passphrase)["enabled"] is True and seen == ["free"]


def test_l3_a_handoff_prepared_during_the_slow_part_undoes_the_move(switch, monkeypatch):
    signer, state, provider, store, _, _ = switch
    before = select.status(state, WS)
    put = store.put

    def put_then_a_handoff_arrives(*a, **k):
        put(*a, **k)
        monkeypatch.setattr(
            convenience, "pending_handoffs", lambda *a, **k: [{"id": "hr_1", "op": "approve", "code": "x"}]
        )

    monkeypatch.setattr(store, "put", put_then_a_handoff_arrives)
    refused(lambda: on(signer, provider.passphrase), "sign.click_only")
    assert not flag(state)
    assert select.status(state, WS) == before and legacy_file(state).exists()
    assert not store.exists(WS, before["device"])


def test_l3_a_custody_change_during_the_slow_part_is_not_overwritten(switch, monkeypatch):
    signer, state, provider, store, _, _ = switch
    calls = []

    def someone_else_changes_custody(*a, **k):  # the second call is the re-check under the lock, after staging
        calls.append(1)
        if len(calls) == 2:
            sel = select.read_selection(state, WS)
            select._write_selection(state, WS, {**sel, "gen": sel.gen + 5})
        return []

    monkeypatch.setattr(convenience, "pending_handoffs", someone_else_changes_custody)
    refused(lambda: on(signer, provider.passphrase), "custody.unavailable")
    assert len(calls) == 2 and not flag(state)
    assert select.read_selection(state, WS)["gen"] == 6  # the other change is kept, not overwritten


def test_l2_a_busy_lock_undoes_the_staged_move(switch, monkeypatch):
    signer, state, provider, store, _, _ = switch
    monkeypatch.setattr(convenience, "LOCK_WAIT", 0.1)
    before = select.status(state, WS)
    with convenience.workspace_lock(state, WS, os.getuid()):
        err = refused(lambda: on(signer, provider.passphrase), "lock.busy")
    assert err.retryable and not flag(state)
    assert select.status(state, WS) == before and legacy_file(state).exists()
    assert not store.exists(WS, before["device"])
    assert on(signer, provider.passphrase)["enabled"] is True  # and a retry works


def test_l4_tier_b_is_refused_before_anything_moves(switch):
    """L4: in tier B the signer (the person UID) cannot write the host-UID flag, so the switch is not offered there."""
    signer, state, provider, store, _, _ = switch
    entry = {"workspace_id": WS, "prefix": "INT", "host_uid": os.getuid() + 1}
    signer._workspaces = lambda: [entry]
    before = select.status(state, WS)
    refused(lambda: on(signer, provider.passphrase), TERMINAL)
    refused(lambda: off(signer), TERMINAL)
    assert select.status(state, WS) == before and legacy_file(state).exists() and not store.exists(WS, before["device"])
