"""#1170: the terminal hand-off (`orch sign CODE`) keeps working while click-to-sign (One click) is on. `auth` names
the custody, not the path: a hand-off bound to the One-click key says `convenience` and passes the same checks as a
click. Design: ~/orch-dev/state/design-dashboard-first.md part 2."""

from __future__ import annotations

import io
import threading
from datetime import UTC

import pytest

from orch.cli.main import main
from orch.host import cli as host_cli
from orch.host import convenience
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION
from tests.host.human_helpers import err, ok
from tests.host.lifecycle_helpers import UID
from tests.host.test_hub_ops import dev_workspace, join_args  # noqa: F401  (autouse: the lab is a dev workspace)

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

GONE = "sign.click_only"
CONV = {"auth": "convenience"}


@pytest.fixture(autouse=True)
def _lab_clock(served, monkeypatch):
    monkeypatch.setattr(host_cli, "_clock", lambda: served.lab.ws.clock[0])
    monkeypatch.setattr(convenience, "_own_device", lambda state, wid: served.lab.ws.owner.device)  # B10 preflight
    from orch.custody import select

    # `host convenience on` moves a passphrase key first; these tests are about the lock and the hand-offs,
    # so the key is taken as already held
    monkeypatch.setattr(select, "status", lambda st, wid: {"custody": "convenience", "device": "d", "gen": 1})


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


def conv(human):
    """The signer a terminal hand-off binds with while One click is on: the owner's device, custody `convenience`."""
    return {**human.signer, **CONV}


def sign_conv(human, prepared):
    human.check(prepared, auth="convenience")
    human.backend.auth = "convenience"  # the One-click key: its signatures say so
    from orch.host import signer as host_signer

    return host_signer.sign(prepared, backend=human.backend, key_id="dk", workspace_id=human.served.wid)


def test_a_held_lock_is_lock_busy_after_a_short_wait_not_forever(served, monkeypatch):
    monkeypatch.setattr(convenience, "LOCK_WAIT", 0.2)
    with convenience.workspace_lock(served.lab.state, served.wid, UID):
        code, text = flag(served, True)
        assert code == 9 and "lock.busy" in text
        with pytest.raises(convenience.HostError) as exc:
            with convenience.workspace_lock(served.lab.state, served.wid, UID):
                pass
        assert exc.value.code == "lock.busy"
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


def test_human_get_and_pending_carry_submitted_at_null_until_submit(served, human):
    import re

    _, key = human.ticket()
    rid = ok(hand(human, key, "when"))["data"]["request"]["id"]
    assert ok(human.get(rid))["data"]["request"]["submitted_at"] is None
    row = ok(human.c.req("human.pending", {"request_id": rid}))["data"]["requests"][0]
    assert row["submitted_at"] is None
    signed = human.sign(ok(human.get(rid, signer=human.signer))["data"])
    served.lab.ws.clock[0] += 37  # time passes between prepare and submit
    ok(human.submit_res(rid, signed.events))
    got = ok(human.get(rid))["data"]["request"]["submitted_at"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", got)
    created = ok(human.c.req("human.pending", {"request_id": rid}))["data"]["requests"][0]["created_at"]
    from datetime import datetime

    def _t(s):
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()

    assert _t(got) - _t(created) == 37  # exactly the host clock at submit
    row = ok(human.c.req("human.pending", {"request_id": rid}))["data"]["requests"][0]
    assert row["submitted_at"] == got and row["created_at"] <= got
    served.lab.ws.clock[0] += 5
    ok(human.submit_res(rid, signed.events))  # an idempotent replay keeps the first time
    assert ok(human.get(rid))["data"]["request"]["submitted_at"] == got


def test_an_old_record_without_submitted_at_reads_as_null(served, human):
    import json

    from orch.host.requests import RequestStore

    _, key = human.ticket()
    rid = ok(hand(human, key, "old"))["data"]["request"]["id"]
    store = RequestStore(served.lab.state, served.wid, UID, clock=lambda: 0.0)
    path = next(p for p in store.dir.iterdir() if rid in p.name)
    assert "submitted_at" not in json.loads(path.read_text())
    assert ok(human.get(rid))["data"]["request"]["submitted_at"] is None


def test_a_submitted_record_without_submitted_at_reads_as_null(served, human):
    import json

    from orch.host.requests import RequestStore

    _, key = human.ticket()
    rid = ok(hand(human, key, "legacy"))["data"]["request"]["id"]
    signed = human.sign(ok(human.get(rid, signer=human.signer))["data"])
    ok(human.submit_res(rid, signed.events))
    store = RequestStore(served.lab.state, served.wid, UID, clock=lambda: 0.0)
    path = next(p for p in store.dir.iterdir() if rid in p.name)
    rec = json.loads(path.read_text())
    assert rec["state"] == "submitted" and rec["submitted_at"] is not None
    del rec["submitted_at"]
    path.write_text(json.dumps(rec))
    path.chmod(0o600)
    assert ok(human.get(rid))["data"]["request"]["submitted_at"] is None


def test_a_bad_request_record_does_not_stop_the_sweep(served, human):
    import json

    from orch.host.requests import RequestStore

    _, key = human.ticket()
    bad = ok(hand(human, key, "b"))["data"]["request"]["id"]
    good = ok(hand(human, key, "g"))["data"]["request"]["id"]
    store = RequestStore(served.lab.state, served.wid, UID, clock=lambda: 1e12)
    path = next(p for p in store.dir.iterdir() if bad in p.name)
    rec = json.loads(path.read_text())
    del rec["expires"]
    path.write_text(json.dumps(rec))
    path.chmod(0o600)
    store.sweep()  # no raise
    assert store.read(good)["state"] == "expired"
    assert "expires" not in store.read(bad)


# --- the #1170 behaviour -----------------------------------------------------------------------------------------


def logged_events(served, key):
    return [e for e in served.lab.ws.events(key) if e.get("type") == "log.added"]


def test_a_handoff_is_prepared_bound_with_convenience_signed_and_submitted_while_one_click_is_on(served, human):
    _, key = human.ticket()
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    rid = ok(hand(human, key, "via the terminal"))["data"]["request"]["id"]
    prepared = ok(human.get(rid, signer=conv(human)))["data"]
    signed = sign_conv(human, prepared)
    ok(human.submit_res(rid, signed.events))
    last = logged_events(served, key)[-1]
    assert last["text"] == "via the terminal" and last["auth"] == "convenience"


def test_switching_on_with_pending_handoffs_succeeds_and_they_still_land(served, human):
    _, key = human.ticket()
    rid = ok(hand(human, key, "pending"))["data"]["request"]["id"]
    assert flag(served, True)[0] == 0
    assert convenience.enabled(served.lab.state, served.wid, UID)
    prepared = ok(human.get(rid, signer=conv(human)))["data"]
    ok(human.submit_res(rid, sign_conv(human, prepared).events))
    assert logged_events(served, key)[-1]["text"] == "pending"


def test_the_catalog_has_no_sign_click_only():
    import pathlib

    from orch.host import errors
    from orch.ops.errors import ERRORS

    assert GONE not in errors.HOST_CODES and GONE not in ERRORS
    root = pathlib.Path(__file__).resolve().parents[2]
    for path in (root / "src").rglob("*.py"):
        assert GONE not in path.read_text(), path
    for path in (root / "tests").rglob("*.json"):  # the CLI goldens
        assert GONE not in path.read_text(), path


def test_a_hub_op_handoff_with_the_one_click_key_is_refused_at_binding_and_nothing_is_signed(served, human):
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    rid = ok(human.prepare_res("hub.join", join_args(), handoff=True))["data"]["request"]["id"]
    res = human.get(rid, signer=conv(human))
    assert err(res) == "hub.proof_invalid"
    assert human.backend.payloads == []
    # still unbound: it is refused at binding (no owner proof) every time, never bound for nothing
    assert err(human.get(rid, signer=conv(human))) == "hub.proof_invalid"
    rec = ok(human.c.req("human.pending", {"request_id": rid}))["data"]["requests"][0]
    assert rec["state"] == "prepared"
    # the owner's own passphrase key still binds it
    ok(human.get(rid, signer=human.signer))


def test_the_readers_condition_fails_a_handoff_binding_with_convenience(served, human):
    _, key = human.ticket()
    rid = ok(hand(human, key, "x"))["data"]["request"]["id"]
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    served.lab.add_member()  # a second member with no reader declaration
    assert err(human.get(rid, signer=conv(human))) == "sign.convenience_unavailable"
    assert human.backend.payloads == []


def test_a_handoff_cannot_submit_convenience_events_after_the_condition_stopped_holding(served, human):
    _, key = human.ticket()
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    rid = ok(hand(human, key, "late"))["data"]["request"]["id"]
    signed = sign_conv(human, ok(human.get(rid, signer=conv(human)))["data"])
    served.lab.add_member()
    res = human.submit_res(rid, signed.events)
    assert err(res) == "sign.convenience_unavailable"
    assert logged_events(served, key) == []


def test_with_click_left_out_of_sign_methods_nothing_lands_on_the_handoff_path_while_the_key_is_convenience(
    served, human
):
    _, key = human.ticket()
    rid = ok(hand(human, key, "x"))["data"]["request"]["id"]
    human.run("settings.set", {"set": {"sign_methods": ["terminal", "phone"]}})
    convenience.set_enabled(served.lab.state, served.wid, True, UID)  # the key is convenience, click is left out
    signed_before = len(human.backend.payloads)
    res = human.get(rid, signer=conv(human))  # refused at binding: click is left out and the key is convenience
    assert err(res) == "sign.convenience_unavailable"
    assert len(human.backend.payloads) == signed_before and logged_events(served, key) == []


def test_orch_sign_is_human_only_for_an_agent_grant(served, human):
    _, key = human.ticket()
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    res = human.prepare_res("log", {"ref": key, "text": "x"}, handoff=True, grant=served.grant, session=SESSION)
    assert err(res) == "human_only"


def test_a_handoff_bound_with_the_passphrase_cannot_be_rebound_with_the_one_click_key(served, human):
    """Opus finding 2: bound with the passphrase key, then the key moves to One click (custody_on): `orch sign`
    coming back with the convenience backend fails closed and nothing is signed."""
    _, key = human.ticket()
    rid = ok(hand(human, key, "bound first"))["data"]["request"]["id"]
    ok(human.get(rid, signer=human.signer))  # bound with the passphrase
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    before = len(human.backend.payloads)
    res = human.get(rid, signer=conv(human))
    assert err(res) == "human.request_state" and "another device" in res["error"]["message"]
    assert len(human.backend.payloads) == before and logged_events(served, key) == []
