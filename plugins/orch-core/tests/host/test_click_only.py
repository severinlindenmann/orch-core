"""One signing path per workspace (orch-v2-host §0.8.6 item 8): no terminal hand-off while click-to-sign is on."""

from __future__ import annotations

import io
import threading

import pytest

from orch.cli.main import main
from orch.host import convenience
from tests.host import human_helpers
from tests.host.human_helpers import err, ok
from tests.host.lifecycle_helpers import UID

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

CODE = "sign.click_only"


@pytest.fixture(autouse=True)
def _lab_clock(served, monkeypatch):
    monkeypatch.setattr(convenience, "_now", lambda: served.lab.ws.clock[0])


def run(*argv):
    out, errs = io.StringIO(), io.StringIO()
    code = main(list(argv), env={"HOME": "/nonexistent"}, stdout=out, stderr=errs)
    return code, out.getvalue()


def flag(served, on: bool) -> tuple[int, str]:
    return run(
        "host", "convenience", "on" if on else "off", "--workspace", served.wid, "--state", str(served.lab.state)
    )


def hand(human, key, text="x", **kw):
    return human.prepare_res("log", {"ref": key, "text": text}, handoff=True, **kw)


def test_handoff_prepare_is_refused_while_convenience_is_on_and_works_after_off(served, human):
    _, key = human.ticket()
    ok(hand(human, key))  # off: hand-off works
    assert flag(served, True)[0] == 3  # the pending hand-off blocks it (item 2); cancel it first
    ok(human.c.req("human.cancel", {"request_id": ok(human.c.req("human.pending", {}))["data"]["requests"][0]["id"]}))
    assert flag(served, True)[0] == 0
    res = hand(human, key, "y")
    assert err(res) == CODE
    msg = res["error"]["message"]
    assert "Sign in the dashboard" in msg and "orch host convenience off --workspace" in msg
    # a prepare with a signer (not a hand-off) is not affected
    ok(human.prepare_res("log", {"ref": key, "text": "s"}))
    code, _ = flag(served, False)  # off re-enables hand-off
    assert code == 0
    ok(hand(human, key, "after"))


def test_the_code_is_in_the_catalog_with_exit_3_not_retryable():
    from orch.host.errors import disposition

    assert disposition(CODE) == (3, False)


def test_switching_on_is_refused_while_handoff_requests_are_pending(served, human):
    _, key = human.ticket()
    h = ok(hand(human, key))["data"]["request"]
    code, out = flag(served, True)
    assert code == 3 and CODE in out and h["id"] in out and h["code"] in out
    assert not convenience.enabled(served.lab.state, served.wid, UID)
    # a signer-bound (non-hand-off) request does not block it
    ok(human.c.req("human.cancel", {"request_id": h["id"]}))
    ok(human.prepare_res("log", {"ref": key, "text": "bound"}))
    assert flag(served, True)[0] == 0
    assert convenience.enabled(served.lab.state, served.wid, UID)


def test_an_expired_handoff_does_not_block_switching_on(served, human, monkeypatch):
    _, key = human.ticket()
    ok(hand(human, key))
    served.lab.ws.clock[0] += 3600
    assert flag(served, True)[0] == 0


def test_a_pending_handoff_cannot_bind_or_submit_after_the_flag_flipped_by_another_path(served, human):
    _, key = human.ticket()
    unbound = ok(hand(human, key, "a"))["data"]["request"]["id"]
    prepared = ok(hand(human, key, "b"))["data"]
    bound = prepared["request"]["id"]
    ok(human.get(bound, signer=human.signer))  # bound while off
    signed = human.sign(ok(human.get(bound, signer=human.signer))["data"])
    convenience.set_enabled(served.lab.state, served.wid, True, UID)  # not through `orch host convenience on`
    assert err(human.get(unbound, signer=human.signer)) == CODE
    assert err(human.submit_res(bound, signed.events)) == CODE
    convenience.set_enabled(served.lab.state, served.wid, False, UID)
    ok(human.submit_res(bound, signed.events))  # off again: the CLI fallback works


def test_a_prepare_and_switching_on_at_the_same_time_have_exactly_one_winner(served, human):
    _, key = human.ticket()
    # deterministic: the lock is held, the prepare waits, the flag is set under the lock, the prepare is refused
    box: dict = {}
    with convenience.workspace_lock(served.lab.state, served.wid, UID):
        t = threading.Thread(target=lambda: box.update(res=hand(human, key)))
        t.start()
        t.join(0.5)
        assert t.is_alive()  # serialized on the workspace
        convenience.set_enabled(served.lab.state, served.wid, True, UID)
    t.join(10)
    assert err(box["res"]) == CODE
    flag(served, False)
    # and the real race: whichever order, exactly one wins
    flag(served, False)
    for i in range(5):
        res: dict = {}
        a = threading.Thread(target=lambda i=i, res=res: res.update(p=hand(human, key, f"r{i}")))
        b = threading.Thread(target=lambda res=res: res.update(f=flag(served, True)))
        a.start()
        b.start()
        a.join(10)
        b.join(10)
        prepared_ok = res["p"]["ok"] is True
        flag_ok = res["f"][0] == 0
        assert prepared_ok != flag_ok, (res, i)
        if flag_ok:
            assert err(res["p"]) == CODE
        else:
            assert res["f"][0] == 3 and CODE in res["f"][1]
            ok(human.c.req("human.cancel", {"request_id": ok(res["p"])["data"]["request"]["id"]}))
        flag(served, False)


def test_a_submit_at_the_expiry_boundary_cannot_land_after_switching_on(served, human, monkeypatch):
    from orch.host.human import HumanOps

    _, key = human.ticket()
    rid = ok(hand(human, key, "edge"))["data"]["request"]["id"]
    signed = human.sign(ok(human.get(rid, signer=human.signer))["data"])
    clock = served.lab.ws.clock
    t0 = clock[0]
    clock[0] = t0 + 599.999
    orig, box = HumanOps._judge, {}

    def judge(store, record, events, git):
        # the host already passed its expiry and flag checks; the clock crosses `expires`, the operator switches on
        clock[0] = t0 + 600.5
        box["flag"] = flag(served, True)
        return orig(store, record, events, git)

    monkeypatch.setattr(HumanOps, "_judge", staticmethod(judge))
    ok(human.submit_res(rid, signed.events))
    assert box["flag"][0] == 3 and CODE in box["flag"][1]  # a bound request still counts a minute past its expiry
    assert not convenience.enabled(served.lab.state, served.wid, UID)
    clock[0] = t0 + 600 + 61
    monkeypatch.setattr(HumanOps, "_judge", staticmethod(orig))
    assert flag(served, True)[0] == 0  # signed and submitted: nothing pending


def test_a_held_lock_is_host_busy_after_a_short_wait_not_forever(served, monkeypatch):
    monkeypatch.setattr(convenience, "LOCK_WAIT", 0.2)
    with convenience.workspace_lock(served.lab.state, served.wid, UID):
        code, text = flag(served, True)
        assert code == 4 and "host.busy" in text
        with pytest.raises(convenience.HostError) as exc:
            with convenience.workspace_lock(served.lab.state, served.wid, UID):
                pass
        assert exc.value.code == "host.busy"
    assert flag(served, True)[0] == 0


@pytest.mark.parametrize("kind", ["fifo", "dir", "loose", "link"])
def test_an_unsafe_lock_file_is_refused_as_custody_unsafe_mode(served, kind):
    import os

    path = convenience.host_dir(served.lab.state) / f"convenience-{served.wid}.lock"
    path.unlink(missing_ok=True)
    if kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "dir":
        path.mkdir(mode=0o700)
    elif kind == "loose":
        path.touch(mode=0o644)
        path.chmod(0o644)
    else:
        path.symlink_to(path.parent / "elsewhere")
    with pytest.raises(convenience.HostError) as exc:
        with convenience.workspace_lock(served.lab.state, served.wid, UID):
            pass
    assert exc.value.code == "custody.unsafe_mode"
    code, text = flag(served, True)
    assert code != 0 and "custody.unsafe_mode" in text


def test_concurrent_flag_updates_for_different_workspaces_lose_nothing(served, monkeypatch):
    # force the read-modify-write windows to overlap: every read waits for the other writer, unless the lock serializes
    state, barrier = served.lab.state, threading.Barrier(2, timeout=0.3)
    real = convenience._doc

    def slow(*a):
        doc = real(*a)
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return doc

    monkeypatch.setattr(convenience, "_doc", slow)
    ts = [threading.Thread(target=convenience.set_enabled, args=(state, f"ws_{n}", True, UID)) for n in "ab"]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    monkeypatch.setattr(convenience, "_doc", real)
    assert set(convenience.read(state, UID)["workspaces"]) == {"ws_a", "ws_b"}
