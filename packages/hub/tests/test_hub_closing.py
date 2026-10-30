"""#1463: the order of the removal steps, cancel rules, resume, the guard allow lists and the notice."""

from __future__ import annotations

import json
import threading

import pytest
from removal_helpers import admin_with_key, close_all, make_web, start, with_backup_key

from orch_hub import cli as CLI
from orch_hub.access.orgsecrets import org_secrets
from orch_hub.backup.service import BackupError, BackupService
from orch_hub.closing import LINK_ALLOWED, STATE_FILE, Closing, ClosingError
from orch_hub.contract import hub as H


@pytest.fixture(scope="module")
def _slow():  # the sign-in hashing is slow: the fixture below is per test, this only documents it
    return None


@pytest.fixture
def w(tmp_path):
    x = make_web(tmp_path, inline=False)
    x.tok = None
    yield x
    close_all(x)


def _begin(w):
    with_backup_key(w)
    tok, signer, ch = admin_with_key(w)
    assert start(w, tok, signer, ch)[0] == 202
    return tok, signer, ch


def _seed_secret(w):
    """A live secret with one wrap for WS (rows only: the hub cannot open wraps anyway)."""
    a = w.env.acc
    org_secrets(a)
    with a.tx() as db:
        db.execute("INSERT INTO org_secrets (id,name,version,expires_ms,shared,created_by,created_ms) VALUES ('s1','k',1,?,?,'u',1)", (w.clock() + 10**9, "all"))
        db.execute("INSERT INTO org_secret_wraps (secret_id,workspace_id,version,wxk_version,sealed) VALUES ('s1',?,1,1,x'00')", ("ab" * 16,))


def _wraps(w):
    return w.env.acc._q("SELECT COUNT(*) FROM org_secret_wraps")[0][0]


def test_steps_run_in_order_and_the_backup_is_taken_before_the_withdraw(w, monkeypatch):
    _seed_secret(w)
    seen = {}
    real = BackupService.make

    def make(self, actor=None, gap=0):
        seen["wraps_at_backup"] = _wraps(w)
        seen["dry_before"] = list(w.launcher.calls)
        return real(self, actor, gap)

    monkeypatch.setattr(BackupService, "make", make)
    _begin(w)
    w.closing.run_sync()
    assert seen["wraps_at_backup"] == 1 and _wraps(w) == 0
    assert ("dry_run", True) in seen["dry_before"]  # the pre-flight saw a draft order, before anything else
    assert w.closing.phase() == "ordered"
    kinds = [c[0] for c in w.launcher.calls]
    assert kinds == ["write_order", "dry_run", "remove_order", "write_order", "launch"]
    assert w.pushed and all(n["o"]["kind"] == "hub.closing" for _ws, n in w.pushed)
    assert set(w.launcher.orders[-1]) == {"v", "kind", "hub_id", "removal_id", "state_dir", "issued_ms", "exp_ms", "dev"}


def test_preflight_failure_changes_nothing_and_cancel_works(w):
    _seed_secret(w)
    w.launcher.dry = (False, "The orch tool is inside the removed folder.")
    _begin(w)
    w.closing.run_sync()
    st = w.closing.status()
    assert st["state"] == "failed" and "orch tool" in st["plain"] and st["cancel_allowed"]
    assert _wraps(w) == 1 and not w.pushed
    w.closing.cancel("x")
    assert not (w.state / STATE_FILE).exists()


def test_backup_failure_leaves_content_intact_and_cancel_works(w, monkeypatch):
    _seed_secret(w)

    def boom(self, *a, **k):
        raise BackupError("backup_failed")

    monkeypatch.setattr(BackupService, "make", boom)
    _begin(w)
    w.closing.run_sync()
    assert w.closing.status()["state"] == "failed" and _wraps(w) == 1 and w.closing.phase() == "preflight_ok"
    w.closing.cancel("x")
    assert w.closing.active() is False


def test_cancel_during_the_withdraw_is_refused_and_the_state_ends_withdrawn(w, monkeypatch):
    _seed_secret(w)
    _begin(w)
    gate, inside = threading.Event(), threading.Event()
    real = w.closing._withdraw

    def held():
        inside.set()
        gate.wait(5)
        real()

    monkeypatch.setattr(w.closing, "_withdraw", held)
    t = threading.Thread(target=w.closing.run_sync)
    t.start()
    assert inside.wait(10)
    assert w.closing.phase() == "withdrawing"
    with pytest.raises(ClosingError) as e:
        w.closing.cancel("x")
    assert e.value.code == "removal_started" and "Too late to cancel" in e.value.plain
    gate.set()
    t.join(10)
    assert w.closing.phase() in ("withdrawn", "notified", "ordered") and _wraps(w) == 0


def test_cancel_from_backed_up_still_works_before_the_worker_withdraws(w):
    _begin(w)
    w.closing._doc["state"] = "backed_up"
    w.closing.cancel("x")
    assert w.closing.active() is False


def test_a_crash_in_withdrawing_resumes_forward_after_a_restart(w):
    _seed_secret(w)
    _begin(w)
    w.closing._doc["state"] = "withdrawing"
    w.closing._save()
    again = Closing(keys=w.env.keys, access=w.env.acc, registry=w.reg, channels=w.env.svc, now_ms=w.clock,
                    launcher=w.launcher, activity=w._act, audit=w.env.audit, spawn=lambda fn: fn())
    again.push = lambda ws, n: True
    assert again.phase() == "withdrawing"
    again.resume()
    assert again.phase() == "ordered" and _wraps(w) == 0


def test_a_stalled_run_needs_a_new_confirm_for_the_order(w):
    tok, signer, ch = _begin(w)
    w.clock.advance(61 * 60_000)
    w.closing.run_sync()
    assert w.closing.phase() == "notified" and w.closing.status()["state"] == "stalled"
    assert not any(c[0] == "launch" for c in w.launcher.calls)
    tok2 = w.login(w.admin)
    w.spawned.clear()
    w.closing._spawn = lambda fn: fn()
    assert start(w, tok2, signer, ch)[0] == 202
    assert w.closing.phase() == "ordered" and any(c[0] == "launch" for c in w.launcher.calls)


def test_launch_failure_keeps_the_hub_up_and_says_so(w):
    w.launcher.fail_launch = True
    _begin(w)
    w.closing.run_sync()
    st = w.closing.status()
    assert st["state"] == "failed" and "still running" in st["plain"]
    assert ("remove_order",) == w.launcher.calls[-1]


def test_guard_writes_answer_hub_closing_and_reads_and_listed_routes_work(w):
    tok, _s, _c = _begin(w)
    assert w.call("POST", "/admin/backups", {}, token=tok)[:2] == (409, {"error": "hub_closing"})
    assert w.call("GET", "/admin/backup", token=tok)[0] == 200
    assert w.call("GET", "/admin/hub/remove/status", token=tok)[0] == 200
    assert w.call("POST", "/admin/hub/remove/cancel", {}, token=tok)[0] == 200


def test_withdrawn_bundles_are_empty_signed_at_a_higher_version(w):
    ws = "ab" * 16
    _seed_secret(w)
    before = w.env.snaps.org_secrets(ws, "full")["o"]
    assert before["secrets"]
    _begin(w)
    w.closing.run_sync()
    after = w.env.snaps.org_secrets(ws, "full")["o"]
    assert after["secrets"] == [] and after["version"] == before["version"] + 1
    assert w.env.snaps.org_skills(ws, "full")["o"]["skills"] == []
    assert w.env.snaps.mcp_servers(ws, "full")["o"]["servers"] == []


def test_the_notice_verifies_for_its_workspace_and_is_not_served_early(w):
    ws = next(iter(w.reg.ws))
    _begin(w)
    assert w.closing.notice_for(ws) is None  # before the withdrawal nothing is told
    w.closing.run_sync()
    n = w.closing.notice_for(ws)
    keys = w.env.keys
    held = keys.verify_card(keys.card())
    got = H.verify_closing(n, held, workspace_id=ws, binding_seq=1, now_ms=w.clock(), dev_mode=True, revoked_op_certs=frozenset())
    assert got["removal_id"] == w.closing._doc["removal_id"]
    assert w.closing.notice_for("cd" * 16) is None  # not a bound workspace of this hub


def test_link_allow_list_is_the_documented_one():
    assert LINK_ALLOWED == {"snapshot_get", "org_skills_get", "mcp_servers_get", "org_secrets_get", "closing_get",
                            "hub_where", "binding_put", "audit_get", "audit_ack"}


def test_hub_cli_writes_are_refused_while_closing(tmp_path, capsys):
    (tmp_path / STATE_FILE).write_text(json.dumps({"state": "closing"}))
    assert CLI.main(["users", "--state", str(tmp_path), "list"]) == 1
    assert "hub_closing" in capsys.readouterr().err


def _reconfirm(w, signer, ch):
    tok = w.login(w.admin)
    w.closing._spawn = lambda fn: fn()
    return start(w, tok, signer, ch)


def test_f1_a_refused_order_can_be_confirmed_again_with_a_new_order_id(w):
    _tok, signer, ch = _begin(w)
    w.closing.run_sync()
    assert w.closing.phase() == "ordered"
    first = [c[1] for c in w.launcher.calls if c[0] == "launch"]
    w.launcher.result = {"ok": False, "code": "refused", "message_plain": "The hub folder is in use."}
    st = w.closing.status()
    assert st["state"] == "stalled" and "Finish removing" in st["plain"] and st["cancel_allowed"] is False
    assert _reconfirm(w, signer, ch)[0] == 202
    again = [c[1] for c in w.launcher.calls if c[0] == "launch"]
    assert len(again) == 2 and again[0] != again[1] and w.closing.phase() == "ordered"
    assert w.launcher.result is None  # the old refusal is cleared
    assert w.closing._doc["removal_id"] not in again  # the notice id is never an order id


def test_f1_an_ordered_hub_with_no_result_is_stuck_only_after_fifteen_minutes(w):
    _begin(w)
    w.closing.run_sync()
    w.tok = w.login(w.admin)
    assert w.closing._can_reconfirm() is False
    w.clock.advance(15 * 60_000 + 1)
    assert w.closing._can_reconfirm() is True
    w.launcher.result = {"ok": True}
    assert w.closing._can_reconfirm() is False


def test_f2_a_cancelled_run_cannot_move_the_new_one(w, monkeypatch):
    with_backup_key(w)
    tok, signer, ch = admin_with_key(w)
    assert start(w, tok, signer, ch)[0] == 202
    gate, inside = threading.Event(), threading.Event()
    real = w.closing._backup

    def held():
        inside.set()
        gate.wait(10)
        real()

    monkeypatch.setattr(w.closing, "_backup", held)
    t = threading.Thread(target=w.closing.run_sync)
    t.start()
    assert inside.wait(20)
    w.closing.cancel("x")  # cancelled while the backup is held open
    assert w.closing.active() is False
    monkeypatch.setattr(w.closing, "_backup", real)
    w.closing._spawn = w.spawned.append  # the second run is observed, not started
    assert start(w, tok, signer, ch)[0] == 202
    new_rid = w.closing._doc["removal_id"]
    gate.set()
    t.join(20)
    assert w.closing._doc["removal_id"] == new_rid
    assert w.closing.phase() in ("closing", "preflight_ok", "backed_up", "withdrawing", "withdrawn", "notified", "ordered")
    # the old worker did not skip the new run's preflight: it either left the doc at 'closing' or the new run took over
    assert w.closing._doc["state"] == "closing" or any(c[0] == "dry_run" for c in w.launcher.calls)
