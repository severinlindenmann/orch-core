"""The dashboard's click-to-sign switch (orch-v2-host §0.8.6 item 3a): the click signer's ``custody_on`` and
``custody_off``. The device key moves into a FakeStore, never the login keychain."""

from __future__ import annotations

import os

import pytest

from orch.click import sealed
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


@pytest.fixture(autouse=True)
def _no_exposure_mark(monkeypatch):
    """#1097 E3 needs a registered workspace and a running host (tests: test_key_exposure_custody.py)."""
    from orch.custody import exposure

    monkeypatch.setattr(exposure, "mark_before_move", lambda *a, **k: None)


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
    monkeypatch.setattr(convenience, "refuse_turn_on", lambda *a, **k: None)  # no workspace log here (B10 tests below)
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


def test_the_signer_advertises_device_rotate():
    assert "rotate" in OPS and "device_rotate" in FEATURES


def test_status_lists_the_feature(switch):
    signer, *_ = switch
    out = signer.status({})
    assert out["features"] == [
        *[
            "custody",
            "sequence",
            "images",
            "reveal",
            "backup_key",
            "sealed_passphrase",
            "device_rotate",
            "runner_switch",
        ]
    ]
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
    refused(lambda: off(signer, passphrase="x"), "custody.unavailable")
    text = repr(log.lines) + repr(row)
    for secret in ("wrong secret words", provider.passphrase):
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


def offer(signer):
    return signer.handle("custody_off", {"workspace_id": WS, "begin": True})


def sealed_for(got, secret, *, purpose="custody.passphrase", workspace=WS):
    ctx = sealed.Context(purpose, workspace, got["request_id"], got["view_digest"])
    return {
        "request_id": got["request_id"],
        "view_digest": got["view_digest"],
        "sealed": {"custody.passphrase": sealed.seal(got["seal"]["pk"], secret, ctx)},
    }


def off(signer, **extra):
    """Off as the dashboard does it: a passphrase goes through begin, then sealed to the offered key (#1198 C12)."""
    if "passphrase" in extra:
        extra = {**sealed_for(offer(signer), extra.pop("passphrase")), **extra}
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


def test_h2_off_with_a_passphrase_while_the_flag_is_already_off_is_terminal_only(switch):
    """H2 narrowed (V3-D31): a dashboard-chosen passphrase is accepted only in the locked step that ends a one-click
    authority the session held. Flag already off: refused, nothing changes."""
    signer, state, provider, store, _, _ = switch
    device = on(signer, provider.passphrase)["device"]
    off(signer)
    err = refused(lambda: off(signer, passphrase=STRONG), TERMINAL)
    assert "orch keys custody passphrase" in err.message and "--in-place" in err.message
    assert not flag(state) and select.status(state, WS)["custody"] == "convenience" and store.exists(WS, device)
    assert not legacy_file(state).exists()
    refused(lambda: on(signer, STRONG), TERMINAL)  # and the dashboard cannot then "prove" a passphrase it chose
    assert not flag(state)


def test_off_with_a_new_passphrase_moves_the_key_back_under_it(switch):
    signer, state, provider, store, _, log = switch
    device = on(signer, provider.passphrase)["device"]
    row = off(signer, passphrase=STRONG)
    assert row["enabled"] is False and row["custody"] == "passphrase" and row["device"] == device
    assert not flag(state) and not store.exists(WS, device) and legacy_file(state).exists()
    backend = PassphraseBackend(human.keys_dir(state, WS), _passphrase_provider=lambda r: STRONG, _kdf=KDF)
    from orch import crypto

    assert backend.public_key("dk") == crypto.unb64u(select.read_selection(state, WS)["pub"], crypto.PUB_LEN)
    assert select.status(state, WS)["custody"] == "passphrase" and "problem" not in select.status(state, WS)
    text = repr(log.lines) + repr(row)
    assert STRONG not in text and provider.passphrase not in text  # G: never logged or echoed
    refused(lambda: on(signer, STRONG), TERMINAL)  # but the dashboard's own choice is no proof for the dashboard


def test_off_with_a_weak_passphrase_changes_nothing_and_the_flag_stays_on(switch):
    signer, state, provider, store, _, _ = switch
    device = on(signer, provider.passphrase)["device"]
    err = refused(lambda: off(signer, passphrase="password1"), "custody.unavailable")
    assert "password1" not in err.message
    assert flag(state) and store.exists(WS, device) and not legacy_file(state).exists()
    assert off(signer, passphrase=STRONG)["custody"] == "passphrase"  # the retry works


@pytest.mark.parametrize("pw", ["", 7, None, STRONG])
def test_off_with_a_clear_passphrase_is_invalid_input(switch, pw):
    """#1198 C12: the passphrase is never accepted in the clear, whatever it is."""
    signer, state, provider, *_ = switch
    on(signer, provider.passphrase)
    refused(lambda: signer.handle("custody_off", {"workspace_id": WS, "passphrase": pw}), "invalid.input")
    assert flag(state)


def test_off_with_a_passphrase_while_a_change_is_pending_is_terminal_only(switch):
    signer, state, provider, store, _, _ = switch
    on(signer, provider.passphrase)
    sel = select.read_selection(state, WS)
    select._write_selection(state, WS, {**sel, "transition": {"to": "passphrase", "step": "written"}})
    refused(lambda: off(signer, passphrase=STRONG), TERMINAL)
    assert not flag(state)  # authority is taken away first, whatever else is refused


def test_a_corrupt_selection_still_leaves_the_flag_off(switch, monkeypatch):
    """Review of #1062: the flag is cleared before any custody work; no error path may leave it on."""
    signer, state, provider, *_ = switch
    on(signer, provider.passphrase)

    def broken(*a, **k):
        raise select.CustodyError("the custody selection is corrupt")

    monkeypatch.setattr(select, "read_selection", broken)
    refused(lambda: off(signer, passphrase=STRONG), "custody.unavailable")
    assert not flag(state)


def test_a_dashboard_chosen_passphrase_cannot_turn_signing_back_on(switch):
    """Review of #1062: the dashboard picks P in off(P); on(P) must not then "prove" it. Only the terminal converts
    again (it bumps the generation, which ends the refusal)."""
    signer, state, provider, store, _, _ = switch
    on(signer, provider.passphrase)
    off(signer, passphrase=STRONG)
    err = refused(lambda: on(signer, STRONG), TERMINAL)
    assert "orch keys custody convenience" in err.message
    assert not flag(state) and select.status(state, WS)["custody"] == "passphrase" and legacy_file(state).exists()
    # the person converts in the terminal (their own prompt), after which the dashboard works as before
    backend = lambda d: PassphraseBackend(d, _passphrase_provider=lambda r: STRONG, _kdf=KDF)  # noqa: E731
    import orch.ops.human as h

    h.open_backend = backend  # the fixture's monkeypatch restores it
    select.convert_to_convenience(state, WS, store=store)
    assert select.status(state, WS)["custody"] == "convenience"


def test_a_terminal_move_back_does_not_block_the_dashboard(switch):
    signer, state, provider, store, _, _ = switch
    on(signer, provider.passphrase)
    off(signer)
    select.downgrade_in_place(state, WS, store=store)  # the terminal prompt (the fixture's provider)
    assert on(signer, provider.passphrase)["enabled"] is True


def test_a_crash_after_the_selection_write_leaves_a_scalar_that_off_sweeps(switch, monkeypatch):
    """Opus A: custody says passphrase but the scalar is still in the key store; the next off removes it."""
    signer, state, provider, store, _, _ = switch
    device = on(signer, provider.passphrase)["device"]

    class Crash(Exception):
        pass

    real = FakeStore.delete

    def boom(self, *a, **k):
        raise Crash

    monkeypatch.setattr(FakeStore, "delete", boom)
    with pytest.raises(Crash):
        select.turn_off_convenience(state, WS, os.getuid(), passphrase=STRONG, store=store)
    monkeypatch.setattr(FakeStore, "delete", real)
    st = select.status(state, WS)
    assert st["custody"] == "passphrase" and store.exists(WS, device)  # the leftover
    assert select.turn_off_convenience(state, WS, os.getuid(), move_key=True, store=store) is None
    assert not store.exists(WS, device)


def test_off_works_in_full_hub_mode_and_on_a_dev_host(switch, monkeypatch):
    """Opus B: the way back is never refused by the entry refusals."""
    signer, state, provider, *_ = switch
    on(signer, provider.passphrase)

    def refuse(*a, **k):
        raise AssertionError("turning convenience off must not call an entry refusal")

    monkeypatch.setattr(select, "refuse_full_mode", refuse)
    monkeypatch.setattr(select, "refuse_dev_host", refuse)
    assert off(signer, passphrase=STRONG)["custody"] == "passphrase"


def test_off_takes_the_flag_check_and_the_move_under_one_lock(switch, monkeypatch):
    """Opus C: while another process holds the workspace lock, off is lock.busy and nothing changed."""
    from orch.host.errors import HostError

    signer, state, provider, store, _, _ = switch
    device = on(signer, provider.passphrase)["device"]
    with convenience.workspace_lock(state, WS, os.getuid()):
        try:
            off(signer, passphrase=STRONG)
        except ClickError as exc:
            assert exc.code == "lock.busy"
        else:
            pytest.fail("off ran while the lock was held")
    assert flag(state) and store.exists(WS, device)
    assert HostError  # imported for the code above


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


def test_l3_a_custody_change_during_the_slow_part_is_not_overwritten(switch, monkeypatch):
    signer, state, provider, store, _, _ = switch
    calls = []
    real = select.finish_convenience

    def someone_else_changes_custody(*a, **k):  # under the lock, after staging
        calls.append(1)
        sel = select.read_selection(state, WS)
        select._write_selection(state, WS, {**sel, "gen": sel.gen + 5})
        return real(*a, **k)

    monkeypatch.setattr(select, "finish_convenience", someone_else_changes_custody)
    refused(lambda: on(signer, provider.passphrase), "custody.unavailable")
    assert len(calls) == 1 and not flag(state)
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


# -- the Opus review of #578 (F1-F4) ------------------------------------------------------------------------------


def test_f1_a_conversion_staged_meanwhile_is_not_resumed_with_a_dashboard_passphrase(switch, monkeypatch):
    """F1: the terminal stages a conversion between custody_on's status check and its stage step. Resuming it needs no
    passphrase, so any string used to finish it and turn the flag on. Only the terminal resumes."""
    signer, state, provider, store, _, _ = switch
    staged = []
    real = select.stage_convenience

    def the_terminal_stages_meanwhile(*a, **k):
        if not staged:
            staged.append(real(state, WS, store=store))  # orch keys custody convenience, cut short
        return real(*a, **k)

    monkeypatch.setattr(select, "stage_convenience", the_terminal_stages_meanwhile)
    refused(lambda: on(signer, "not the passphrase"), "custody.unavailable")
    assert not flag(state)
    assert select.read_selection(state, WS) == staged[0]  # left for the terminal to finish
    assert store.exists(WS, staged[0]["device"]) and legacy_file(state).exists()  # nothing deleted
    assert not (state / "person" / "click-custody.json").exists()  # not a wrong passphrase: no try counted


def test_f1_stage_refuses_to_resume_with_a_passphrase(switch):
    _, state, provider, store, _, _ = switch
    sel = select.stage_convenience(state, WS, store=store)
    with pytest.raises(select.CustodyError):
        select.stage_convenience(state, WS, store=store, passphrase=provider.passphrase)
    assert select.read_selection(state, WS) == sel
    assert select.stage_convenience(state, WS, store=store) == sel  # the terminal still resumes


def test_f2_misses_from_a_clock_set_back_do_not_extend_the_lockout(switch):
    """F2: misses recorded ahead of the clock (it was set back) count as now ONCE: they are written back clamped, so
    the lockout ends one window later instead of whenever the clock catches up."""
    import json

    signer, state, provider, _, clock, _ = switch
    refused(lambda: on(signer, "wrong"), "custody.wrong_passphrase")  # creates the record
    path = state / "person" / "click-custody.json"
    path.write_text(json.dumps({"v": 1, "misses": [clock.t + 100_000.0] * CUSTODY_TRIES}))
    err = refused(lambda: on(signer, provider.passphrase), "rate.limited")
    assert err.retry_after_ms <= CUSTODY_WINDOW * 1000
    assert all(t <= clock.t for t in json.loads(path.read_text())["misses"])
    clock.t += CUSTODY_WINDOW + 1
    assert on(signer, provider.passphrase)["enabled"] is True


def test_f3_a_flag_write_failure_after_the_conversion_reports_the_host_error(switch, monkeypatch):
    """F3: once finish_convenience succeeded there is nothing to undo (convenience custody, flag off is safe); the
    real host error is reported, not a failed undo's custody.unavailable."""
    from orch.host.errors import HostError

    signer, state, provider, store, _, _ = switch

    def unsafe(*a, **k):
        raise HostError("custody.unsafe_mode", "the flag directory is not private")

    monkeypatch.setattr(convenience, "set_enabled", unsafe)
    refused(lambda: on(signer, provider.passphrase), "custody.unsafe_mode")
    st = select.status(state, WS)
    assert st["custody"] == "convenience" and st.get("problem") is None and store.exists(WS, st["device"])
    assert not flag(state)


def test_f4_only_turn_off_passes_the_downgrade_a_passphrase():
    """V3-D31 (was: no passphrase): the pin that only select.turn_off_convenience passes one lives in
    tests/custody/test_seams.py."""
    import inspect

    assert inspect.signature(select.downgrade_in_place).parameters["passphrase"].default is None


def test_on_is_refused_up_front_while_a_reader_is_below_level_1_and_nothing_moves(switch, monkeypatch):
    """B10 (the lockout fix): the host-side readers check runs before any passphrase work or key move."""
    from orch.host.errors import HostError

    signer, state, provider, store, _, _ = switch
    err = convenience.unavailable("phone_device", ["d_" + "ab" * 16])

    def boom(*a, **k):
        raise HostError(err.code, err.message, hint="h", detail=err.detail)

    monkeypatch.setattr(convenience, "refuse_turn_on", boom)
    info = refused(lambda: on(signer, provider.passphrase), "sign.convenience_unavailable")
    assert info.reason == "phone_device" and info.detail == {"reason": "phone_device", "devices": ["d_" + "ab" * 16]}
    assert not flag(state) and signer.status({})["workspaces"][0]["custody"] == "passphrase"


def _hub_only(*a, **k):
    from orch.host.errors import HostError

    raise HostError("sign.hub_only", "this workspace is in full hub mode")


def test_a_refused_finish_in_full_mode_puts_the_terminal_conversion_back(switch, monkeypatch):
    """L2-C11f-2: finish refused with sign.hub_only after stage: no stored copy, no stuck transition."""
    from orch.host.errors import HostError

    _, state, _, store, _, _ = switch
    before = select.read_selection(state, WS)
    monkeypatch.setattr(select, "finish_convenience", _hub_only)
    with pytest.raises(HostError):
        select.convert_to_convenience(state, WS, store=store)
    assert select.read_selection(state, WS) == before
    st = select.status(state, WS)
    assert st["custody"] == "passphrase" and "problem" not in st
    assert not store.exists(WS, st["device"]) and legacy_file(state).exists()


def test_turn_off_clears_a_stuck_convenience_transition_in_full_mode(switch, monkeypatch):
    """L2-C11f-2: a transition left at step written (older build, or a crash) is abandoned by the way back, which never
    calls the full-mode refusal."""
    signer, state, provider, store, _, _ = switch
    monkeypatch.setattr(select, "finish_convenience", _hub_only)
    staged = select.stage_convenience(state, WS, store=store)  # what the stuck state looks like
    assert select.status(state, WS)["problem"] == "unfinished change to convenience"
    monkeypatch.setattr(select, "refuse_full_mode", _hub_only)
    assert select.turn_off_convenience(state, WS, os.getuid(), store=store) is not None
    st = select.status(state, WS)
    assert st["custody"] == "passphrase" and "problem" not in st and not store.exists(WS, staged["device"])
    assert legacy_file(state).exists()


def test_the_rekey_mark_is_written_before_the_move_so_a_failing_delete_cannot_skip_it(switch, monkeypatch):
    """L2-1075-1: the final store.delete fails (a Keychain error); the chosen factor is still no proof."""
    signer, state, provider, store, _, _ = switch
    on(signer, provider.passphrase)

    def boom(self, *a, **k):
        raise select.BackendUnavailable("the key store refused")

    monkeypatch.setattr(FakeStore, "delete", boom)
    with pytest.raises(ClickError):
        off(signer, passphrase=STRONG)
    assert not flag(state)
    assert select.status(state, WS)["custody"] == "passphrase"
    assert select._dashboard_rekeyed(state, WS, select.read_selection(state, WS))


def test_a_failed_move_leaves_no_stale_mark(switch):
    signer, state, provider, store, _, _ = switch
    on(signer, provider.passphrase)
    legacy_file(state).write_bytes(b"{}")  # a stray file: the L1 refusal, nothing moves
    refused(lambda: off(signer, passphrase=STRONG), "custody.unavailable")
    assert not select._dashboard_rekeyed(state, WS, select.read_selection(state, WS))


def test_the_abandon_refuses_without_the_passphrase_key_and_keeps_the_stored_copy(switch):
    """L2-1075-2: never delete the last copy of the device key."""
    signer, state, provider, store, _, _ = switch
    staged = select.stage_convenience(state, WS, store=store)
    legacy_file(state).unlink()  # the passphrase file is gone (a racing finish removed it)
    with pytest.raises(select.CustodyError):
        select.downgrade_in_place(state, WS, store=store)
    assert store.exists(WS, staged["device"])


def test_the_abandon_takes_the_workspace_lock(switch):
    from orch.host.errors import HostError

    signer, state, provider, store, _, _ = switch
    select.stage_convenience(state, WS, store=store)
    with convenience.workspace_lock(state, WS, os.getuid()):
        with pytest.raises(HostError) as ei:
            select.downgrade_in_place(state, WS, store=store)
    assert ei.value.code == "lock.busy"


def test_the_pending_refusal_says_signing_is_off_and_the_log_records_it(switch):
    signer, state, provider, store, _, log = switch
    on(signer, provider.passphrase)
    sel = select.read_selection(state, WS)
    select._write_selection(state, WS, {**sel, "transition": {"to": "passphrase", "step": "written"}})
    err = refused(lambda: off(signer, passphrase=STRONG), TERMINAL)
    assert "click-to-sign is now off" in err.message and not flag(state)
    assert ("custody_off", {"workspace": WS}) in log.lines


def test_the_leftover_sweep_runs_for_a_dashboard_off_without_a_passphrase(switch):
    signer, state, provider, store, _, _ = switch
    device = on(signer, provider.passphrase)["device"]
    select.downgrade_in_place(state, WS, store=store)
    store.put(WS, device, b"\x01" * 32)  # what a crashed move left behind
    off(signer)
    assert not store.exists(WS, device)


# --- #1198 C17: turning One click back on with a plain click, key already in convenience custody ---------------------


def resume(signer, **extra):
    return signer.handle("custody_on", {"workspace_id": WS, "resume": True, **extra})


def test_c17_a_plain_click_turns_it_on_again_after_idle_and_restarts_the_clock(switch, monkeypatch):
    signer, state, provider, *_ = switch
    clock = Mono()
    monkeypatch.setattr(convenience, "_now", clock)
    on(signer, provider.passphrase)
    clock.t += 13 * 3600
    assert not flag(state) and convenience.idle_off(state, WS, os.getuid()) == 12
    row = resume(signer)
    assert row["enabled"] is True and row["custody"] == "convenience" and row["risk_ack"]
    assert flag(state) and convenience.idle_off(state, WS, os.getuid()) is None
    clock.t += 11 * 3600
    assert flag(state)  # the clock restarted at the click


def test_c17_a_plain_click_after_turn_off_without_passphrase(switch):
    signer, state, provider, *_ = switch
    on(signer, provider.passphrase)
    off(signer)
    assert resume(signer)["enabled"] is True and flag(state)


def test_c17_resume_goes_through_the_readers_rule(switch, monkeypatch):
    from orch.host.errors import HostError

    signer, state, provider, *_ = switch
    on(signer, provider.passphrase)
    off(signer)

    def refuse(*a, **k):
        raise HostError("sign.convenience_unavailable", "other_member: a reader is too old")

    monkeypatch.setattr(convenience, "refuse_turn_on", refuse)
    err = refused(lambda: resume(signer), "sign.convenience_unavailable")
    assert err.reason == "other_member" and not flag(state)


def test_c17_resume_refuses_on_a_dev_host(switch, monkeypatch):
    signer, state, provider, *_ = switch
    on(signer, provider.passphrase)
    off(signer)
    monkeypatch.setattr(
        convenience,
        "refuse_dev_host",
        lambda s, env=None: (_ for _ in ()).throw(convenience.unavailable("dev_host", [])),
    )
    refused(lambda: resume(signer), "sign.convenience_unavailable")
    assert not flag(state)


def test_c17_resume_needs_the_key_in_convenience_custody_and_no_passphrase(switch):
    signer, state, provider, *_ = switch
    refused(lambda: resume(signer), "invalid.input")  # the key is still under the passphrase
    on(signer, provider.passphrase)
    refused(lambda: resume(signer, passphrase="x"), "invalid.input")
    refused(lambda: signer.handle("custody_on", {"workspace_id": WS, "resume": "yes"}), "invalid.input")
    assert resume(signer)["enabled"] is True  # already on: nothing changes


# -- #1198 C12: the new passphrase of "One click off" arrives sealed -------------------------------------------


def test_c12_the_offer_carries_a_key_a_digest_and_nothing_secret(switch):
    signer, *_ = switch
    got = offer(signer)
    assert set(got) == {"request_id", "view_digest", "seal", "expires_in"}
    assert got["seal"]["purposes"] == ["custody.passphrase"] and got["seal"]["alg"] == sealed.ALG
    assert "sealed_passphrase" in FEATURES


def test_c12_a_sealed_passphrase_moves_the_key_and_leaves_no_trace(switch):
    signer, state, provider, store, _, log = switch
    on(signer, provider.passphrase)
    got = offer(signer)
    args = sealed_for(got, STRONG)
    row = signer.handle("custody_off", {"workspace_id": WS, **args})
    assert row["custody"] == "passphrase" and row["enabled"] is False
    spilled = repr(log.lines) + repr(row) + repr(signer.__dict__.get("problems"))
    assert STRONG not in spilled
    for f in (state / "click").rglob("*") if (state / "click").exists() else []:
        if f.is_file():
            assert STRONG.encode() not in f.read_bytes()
    assert not signer._offers  # the key was dropped with its use


def test_c12_an_offer_is_used_once_a_replay_is_refused(switch):
    signer, state, provider, *_ = switch
    on(signer, provider.passphrase)
    args = sealed_for(offer(signer), STRONG)
    signer.handle("custody_off", {"workspace_id": WS, **args})
    refused(lambda: signer.handle("custody_off", {"workspace_id": WS, **args}), "invalid.input")


def test_c12_a_weak_passphrase_uses_up_the_offer_and_a_new_one_works(switch):
    signer, state, provider, *_ = switch
    on(signer, provider.passphrase)
    args = sealed_for(offer(signer), "password1")
    refused(lambda: signer.handle("custody_off", {"workspace_id": WS, **args}), "custody.unavailable")
    assert flag(state)
    refused(lambda: signer.handle("custody_off", {"workspace_id": WS, **args}), "invalid.input")
    assert off(signer, passphrase=STRONG)["custody"] == "passphrase"


def test_c12_wrong_binding_is_refused_and_does_not_burn_the_offer(switch):
    signer, state, provider, *_ = switch
    on(signer, provider.passphrase)
    got = offer(signer)
    # another request's context (the digest of a different offer), another purpose, a flipped bit: all refused
    wrong_purpose = sealed_for(got, STRONG)
    ctx = sealed.Context("hub.backup_key", WS, got["request_id"], got["view_digest"])
    wrong_purpose["sealed"]["custody.passphrase"] = sealed.seal(got["seal"]["pk"], STRONG, ctx)
    refused(lambda: signer.handle("custody_off", {"workspace_id": WS, **wrong_purpose}), "invalid.input")
    good = sealed_for(got, STRONG)
    flipped = {
        **good,
        "sealed": {
            "custody.passphrase": {
                **good["sealed"]["custody.passphrase"],
                "ct": "A" + good["sealed"]["custody.passphrase"]["ct"][1:],
            }
        },
    }
    refused(lambda: signer.handle("custody_off", {"workspace_id": WS, **flipped}), "invalid.input")
    assert flag(state)
    assert signer.handle("custody_off", {"workspace_id": WS, **good})["custody"] == "passphrase"  # still opens


def test_c12_another_digest_or_request_or_workspace_is_refused(switch):
    signer, state, provider, *_ = switch
    on(signer, provider.passphrase)
    got = offer(signer)
    args = sealed_for(got, STRONG)
    refused(
        lambda: signer.handle("custody_off", {"workspace_id": WS, **args, "view_digest": "sha256:" + "0" * 64}),
        "invalid.input",
    )
    refused(
        lambda: signer.handle("custody_off", {"workspace_id": WS, **args, "request_id": "hr_" + "0" * 26}),
        "invalid.input",
    )
    refused(lambda: signer.handle("custody_off", {"workspace_id": "f" * 32, **args}), "not_found")
    assert flag(state)


def test_c12_a_newer_offer_drops_the_older_and_an_expired_one_is_refused(switch):
    signer, state, provider, _, mono, _ = switch
    on(signer, provider.passphrase)
    first = sealed_for(offer(signer), STRONG)
    second = offer(signer)
    refused(lambda: signer.handle("custody_off", {"workspace_id": WS, **first}), "invalid.input")
    late = sealed_for(second, STRONG)
    mono.t += 400  # mono is the wall seam of this fixture; the offer's clock is the signer's mono
    signer.mono = lambda: mono.t + 10_000
    refused(lambda: signer.handle("custody_off", {"workspace_id": WS, **late}), "invalid.input")
    assert flag(state)


def test_c12_odd_sealed_shapes_and_begin_extras_are_invalid(switch):
    signer, *_ = switch
    got = offer(signer)
    good = sealed_for(got, STRONG)
    for bad in (
        {**good, "sealed": {}},
        {**good, "sealed": {"hub.backup_key": good["sealed"]["custody.passphrase"]}},
        {**good, "extra": 1},
        {"request_id": got["request_id"], "sealed": good["sealed"]},
    ):
        refused(lambda bad=bad: signer.handle("custody_off", {"workspace_id": WS, **bad}), "invalid.input")
    refused(lambda: signer.handle("custody_off", {"workspace_id": WS, "begin": True, "x": 1}), "invalid.input")
    refused(lambda: signer.handle("custody_off", {"workspace_id": WS, "begin": False}), "invalid.input")


def test_c12_nothing_secret_in_the_written_log(switch):
    signer, state, provider, _, _, log = switch
    on(signer, provider.passphrase)
    off(signer, passphrase=STRONG)
    assert STRONG not in (log.root / "click.jsonl").read_text()


def test_c12_the_log_says_the_key_moved_only_when_it_did(switch):
    signer, state, provider, _, _, log = switch
    on(signer, provider.passphrase)
    off(signer, passphrase=STRONG)
    assert ("custody_off", {"workspace": WS, "moved": "true"}) in log.lines
    text = (log.root / "click.jsonl").read_text()
    assert '"moved": "true"' in text and STRONG not in text
    on2 = signer.handle("custody_off", {"workspace_id": WS})  # flag only, key already under a passphrase
    assert on2 and log.lines[-1] == ("custody_off", {"workspace": WS})
