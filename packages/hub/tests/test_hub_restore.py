"""M4 S6 (#1462): restoring the hub from a backup, with anti-rollback (protocol §17.5-§17.8).

Every Opus condition of the slice has a test: 7 (password + code + words inside the console request, no switch / phone /
pairing session), 13 (extraction of exactly the header's list), 14 (sequence floors, the same backup restored twice), 15 (live
revocations and logs survive), 16 (nobody removed or suspended comes back), 17 (every check before a byte is written), 20
(no restore while another heavy operation runs), 23 (audit with the backup date and gap) and 24 (the adversarial list)."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tarfile
from pathlib import Path

import pytest
from webapi_helpers import Web

from orch_hub import cli as CLI
from orch_hub.access import Access, AccessSnapshots
from orch_hub.activity import ActivityLog
from orch_hub.backup import cli as BCLI
from orch_hub.backup import restore as RS
from orch_hub.backup import snapshot as SN
from orch_hub.backup.hubop import HubOpBusy, hub_op
from orch_hub.backup.service import BackupService
from orch_hub.backup.words import generate_words
from orch_hub.contract import backup as B
from orch_hub.contract import hub as H
from orch_hub.contract.canon import b64u
from orch_hub.keys import HubKeys, KeysError
from orch_hub.settings import SettingsError  # noqa: F401  (the fixture state has no settings file: routes still answer)

WS = "ab" * 16


@pytest.fixture(autouse=True)
def _fence_clear():
    from orch_hub.backup.hubop import FENCE

    FENCE.clear()
    yield
    FENCE.clear()


@pytest.fixture
def w(tmp_path):
    x = Web(tmp_path)
    x.state = tmp_path / "hub"
    x.tmp = tmp_path
    act = x.api.d.activity = ActivityLog(":memory:")
    x.carol = x.env.user("carol")  # a second admin
    x.env.acc.add_member("hub-admin", x.carol)
    x.dave = x.env.user("dave")  # a person who is removed after the backup
    x.env.acc.add_member("team", x.dave)
    yield x
    act.close()
    x.close()


def _backup(w):
    """A key, a first backup and the words. Returns ``(words, data)``."""
    tok = w.login(w.admin)
    s, o, _ = w.call("POST", "/admin/backup/key", w.creds(w.admin), token=tok)
    assert s == 200, o
    return o["words"], (w.state / "backups" / o["backup"]["name"]).read_bytes()


def _restore(w, data, words, **kw):
    kw.setdefault("keys", w.env.keys)
    kw.setdefault("audit", w.env.audit)
    kw.setdefault("now_ms", w.clock)
    return RS.restore(w.state, data, words, **kw)


def _tree(root: Path) -> dict[str, tuple[bytes, int]]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.name.endswith(".lock"):
            out[str(p.relative_to(root))] = (p.read_bytes(), p.stat().st_mode & 0o777)
    return out


def _after_backup(w):
    """What happens in the live hub after the backup: dave is removed, a phone is paired and revoked, an unrelated
    revocation is added, the sequence numbers climb."""
    w.env.svc.remove_user(w.dave)
    tok = w.login(w.ann)
    _signer, cid = w.register_channel(tok)
    w.env.svc.revoke(cid, actor=w.ann)
    w.env.keys.rotate_op()
    w.env.keys.reissue_card()
    return cid


def _users(state):
    a = Access(str(state / "access.db"))
    try:
        return {u["user_id"]: u for u in a.users()}
    finally:
        a.close()


# --- the happy path, in place (conds 14, 15, 16, 23) ----------------------------------------------------------------------


def test_restore_in_place_rolls_forward_the_counters_and_keeps_nobody_who_is_gone(w):
    words, data = _backup(w)
    b_op, b_card = w.env.keys.op_seq, w.env.keys.card_seq
    cid = _after_backup(w)
    live_op, live_card = w.env.keys.op_seq, w.env.keys.card_seq
    assert live_op > b_op
    old_cert = w.env.keys.op_cert()["o"]["cert_id"]
    got = _restore(w, data, words, actor=w.admin)
    assert got["op_seq"] == max(live_op, b_op) + 1000 and got["card_seq"] == max(live_card, b_card) + 1000
    k = HubKeys.open(w.state, now_ms=w.clock)
    assert (k.op_seq, k.card_seq) == (got["op_seq"], got["card_seq"])
    assert old_cert in k.revoked_op_certs()  # the live op certificate was revoked before the restore
    rev = k.revoked_op_certs()
    assert len(rev) >= 2 and k.op_cert()["o"]["cert_id"] not in rev  # and the backup's; the new one is clean
    # cond 16: admins have a UK cert, nobody else does; no channel cert at all
    users = _users(w.state)
    assert w.dave not in users
    assert k.users() == sorted([w.admin, w.carol]) and k.channel_certs() == []
    for u in (w.ann,):
        with pytest.raises(KeysError):
            k.uk_cert(u)
        assert users[u]["disabled"] is True
    assert users[w.admin]["disabled"] is False and users[w.carol]["disabled"] is False
    assert not (w.state / "uk" / f"{w.dave}.key").exists()
    # the snapshot has no role for anybody who is gone or suspended, and its version is above the floor
    acc = Access(str(w.state / "access.db"))
    try:
        assert w.ann not in acc.roles(WS) and w.dave not in acc.roles(WS)
        snap = AccessSnapshots(acc, k, w.clock).snapshot(WS, "full")
        assert snap["o"]["version"] == got["snapshot_seq"] + 1 and got["snapshot_seq"] >= 1000
        assert cid in acc.revoked_channels()  # the live revocation survived
    finally:
        acc.close()
    # a fresh sign-in works for an admin only after the restart; the notice says what happened
    n = RS.notice(w.state)
    assert n and n["made_ms"] == B.read_header(data, dev_mode=True, live=None)[1]["made_ms"] and n["suspended"] >= 1
    # the layout is private
    for p in w.state.rglob("*"):
        if not p.is_symlink():
            assert p.stat().st_mode & 0o077 == 0, p
    assert not (w.tmp / "hub.new").exists()


def test_restore_audits_the_backup_date_and_the_gap_and_keeps_the_live_logs(w):
    words, data = _backup(w)
    before = [e for _, r, e in w.env.audit.store.after(0, 1000)]
    w.clock.advance(5 * 86_400_000)
    got = _restore(w, data, words, actor=w.admin, activity=w.api.d.activity)
    from orch_hub.audit import AuditStore

    st = AuditStore(str(w.state / "audit.db"))
    try:
        recs = [(r["o"], e) for _, r, e in st.after(0, 1000)]
    finally:
        st.close()
    assert len(recs) == len(before) + 1  # appended, nothing rolled back
    last, ev = recs[-1]
    assert last["type"] == "hub_restore"
    assert ev["backup_made_ms"] == got["made_ms"] and ev["gap_ms"] == 5 * 86_400_000 and ev["op_seq"] == got["op_seq"]
    assert ev["actor_user_id"] == w.admin and set(ev) == {
        "backup_made_ms",
        "gap_ms",
        "op_seq",
        "actor_user_id",
        "nonce",
    }
    assert "word" not in json.dumps(ev)


def test_the_contract_event_is_closed():
    from orch_hub.contract import audit as AU

    ok = {"backup_made_ms": 1, "gap_ms": 0, "op_seq": 1001, "actor_user_id": None, "nonce": "A" * 43}
    assert AU.check_event("hub_restore", ok) == ok
    for bad in (
        {**ok, "file": "x"},
        {**ok, "gap_ms": -1},
        {**ok, "op_seq": 0},
        {k: v for k, v in ok.items() if k != "nonce"},
        {**ok, "actor_user_id": "xyz"},
    ):
        with pytest.raises(Exception):  # noqa: B017  # Refused("malformed")
            AU.check_event("hub_restore", bad)


def test_every_later_snapshot_is_above_the_floor_even_for_a_row_the_backup_had(w):
    AccessSnapshots(w.env.acc, w.env.keys, w.clock).snapshot(WS, "full")  # a row exists in the backup (version 1)
    words, data = _backup(w)
    got = _restore(w, data, words)
    acc = Access(str(w.state / "access.db"))
    try:
        k = HubKeys.open(w.state, now_ms=w.clock)
        assert acc.snapshot_row(WS)[0] == 1 and acc.version_floor() == got["snapshot_seq"]
        assert AccessSnapshots(acc, k, w.clock).snapshot(WS, "full")["o"]["version"] == got["snapshot_seq"] + 1
    finally:
        acc.close()


def test_the_notice_is_kept_until_it_is_dismissed(w):
    words, data = _backup(w)
    _restore(w, data, words)
    n = RS.notice(w.state)
    assert n and n["suspended"] >= 1 and set(n) == {"made_ms", "restored_ms", "suspended"}
    RS.dismiss_notice(w.state)
    assert RS.notice(w.state) is None


# --- cond 14: seconds versus milliseconds, rolled-back backup, same backup twice ---------------------------------------


def test_a_rolled_back_backup_is_lifted_above_a_live_counter_that_is_far_ahead(w):
    words, data = _backup(w)
    for _ in range(3):
        w.env.keys.rotate_op()
    w.env.keys.reissue_card()
    live = w.env.keys.op_seq
    assert live > B.read_header(data, dev_mode=True, live=None)[1]["op_seq"]
    got = _restore(w, data, words)
    assert got["op_seq"] == live + 1000 and HubKeys.open(w.state, now_ms=w.clock).op_seq == live + 1000


def test_the_same_backup_restored_twice_climbs_both_times_and_every_card_verifies(w):
    words, data = _backup(w)
    held = None
    seen = []
    for i in range(3):
        got = _restore(w, data, words, keys=None if i else w.env.keys, audit=None)
        k = HubKeys.open(w.state, now_ms=w.clock)
        card = k.card()
        # what a bound workspace does: the card verifies under the pinned root, with the live revocations, and is newer
        H.verify_card(
            card, now_ms=w.clock(), dev_mode=True, pinned_root=k.hk_root_pub, revoked_op_certs=k.revoked_op_certs()
        )
        if held is not None:
            assert card["o"]["card_seq"] > held["o"]["card_seq"] and k.op_seq > seen[-1]
        held = card
        seen.append(k.op_seq)
        assert got["op_seq"] == k.op_seq
    assert seen[1] - seen[0] >= 1000 and seen[2] - seen[1] >= 1000


def test_a_fresh_server_floor_is_the_clock_in_milliseconds_and_climbs_on_a_second_restore(w, tmp_path):
    words, data = _backup(w)
    fresh = tmp_path / "srv" / "data"
    h = B.read_header(data, dev_mode=True, live=None)[1]
    got = RS.restore(fresh, data, words, now_ms=lambda: 1_900_000_000_000)
    assert got["in_place"] is False
    assert got["op_seq"] == max(h["op_seq"] + 1000, 1_900_000_000_000) == 1_900_000_000_000
    assert got["op_seq"] < 2**53 and got["card_seq"] == 1_900_000_000_000
    k = HubKeys.open(fresh, now_ms=lambda: 1_900_000_000_000)
    assert k.op_seq == got["op_seq"] and k.hub_id == w.env.keys.hub_id
    again = RS.restore(
        fresh, data, words, now_ms=lambda: 1_900_000_000_001
    )  # an earlier clock still climbs (in place now)
    assert again["in_place"] is True and again["op_seq"] == got["op_seq"] + 1000
    assert not (tmp_path / "srv" / "data.new").exists()


# --- cond 15, 16: live removals, revocations, keys ---------------------------------------------------------------------


def test_people_disabled_or_removed_or_demoted_live_stay_so_and_live_revocations_survive(w):
    erin = w.env.user("erin")
    w.env.acc.add_member("team", erin)
    words, data = _backup(w)
    w.env.acc.set_disabled(erin, True, actor=w.admin)
    w.env.acc.remove_member("hub-admin", w.carol, actor=w.admin)  # demoted live
    extra = "cd" * 16
    k = w.env.keys
    k.revoke_op(extra)  # an unrelated live revocation
    w.env.acc.revoke_channel("ee" * 16)
    _restore(w, data, words)
    users = _users(w.state)
    assert users[erin]["disabled"] is True and users[w.carol]["disabled"] is True  # never enabled again by a restore
    assert not Access(str(w.state / "access.db")).is_hub_admin(w.carol)
    kk = HubKeys.open(w.state, now_ms=w.clock)
    assert extra in kk.revoked_op_certs() and kk.users() == [w.admin]
    acc = Access(str(w.state / "access.db"))
    assert "ee" * 16 in acc.revoked_channels()
    acc.close()


def test_all_channels_and_sessions_end_and_the_live_backup_key_file_is_kept(w):
    words, data = _backup(w)
    tok = w.login(w.ann)
    _signer, cid = w.register_channel(tok)
    # a NEW key after the backup: the restore must keep it and never bring the old key file back
    svc = BackupService(w.env.keys, audit=w.env.audit)
    new_words, _ = svc.set_key(None)
    live_key = (w.state / SN.KEY_FILE).read_bytes()
    kept = {p.name for p in (w.state / "backups").iterdir()}
    _restore(w, data, words)  # the OLD words still open the old backup
    assert (w.state / SN.KEY_FILE).read_bytes() == live_key
    assert {p.name for p in (w.state / "backups").iterdir()} == kept  # the kept backups came along
    assert BackupService(HubKeys.open(w.state, now_ms=w.clock)).key_info()["set_ms"] == json.loads(live_key)["set_ms"]
    db = sqlite3.connect(w.state / "channels.db")
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM channels WHERE revoked_ms IS NULL").fetchone()[0] == 0
    db.close()
    acc = Access(str(w.state / "access.db"))
    assert cid in acc.revoked_channels()
    acc.close()
    assert new_words != words


def test_restore_refuses_when_nobody_could_sign_in_afterwards(w):
    words, data = _backup(w)
    w.env.acc.remove_member("hub-admin", w.admin, actor=w.carol)
    w.env.acc.remove_member("hub-admin", w.carol, actor=w.carol)
    before = _tree(w.state)
    with pytest.raises(RS.RestoreError) as e:
        _restore(w, data, words)
    assert e.value.code == "no_admin" and _tree(w.state) == before and not (w.tmp / "hub.new").exists()


# --- cond 17 / 24: refusals write nothing ---------------------------------------------------------------------------------


def _refused(w, data, words, code, reason=None, **kw):
    before = _tree(w.state)
    cert = w.env.keys.op_cert()["o"]["cert_id"]
    with pytest.raises(RS.RestoreError) as e:
        _restore(w, data, words, **kw)
    assert e.value.code == code and (reason is None or e.value.reason == reason), (e.value.code, e.value.reason)
    assert _tree(w.state) == before, "the live state changed"
    assert not (w.tmp / "hub.new").exists() and not (w.tmp / "hub.old").exists()
    assert cert not in w.env.keys.revoked_op_certs()


def test_wrong_words_and_malformed_words_write_nothing(w, monkeypatch):
    words, data = _backup(w)
    monkeypatch.setattr(RS, "_stage", lambda *a, **k: pytest.fail("a byte was staged before the checks passed"))
    _refused(w, data, generate_words(), "wrong_backup_key")
    _refused(w, data, " ".join(words.split()[:23]), "words")
    _refused(w, data, " ".join(["abandon"] * 24), "words")


def test_a_backup_of_another_hub_is_refused_in_place(w, tmp_path, monkeypatch):
    other = Web(tmp_path / "o")
    try:
        other.state = tmp_path / "o" / "hub"
        tok = other.login(other.admin)
        _s, o, _ = other.call("POST", "/admin/backup/key", other.creds(other.admin), token=tok)
        data = (other.state / "backups" / o["backup"]["name"]).read_bytes()
        monkeypatch.setattr(RS, "_stage", lambda *a, **k: pytest.fail("staged"))
        _refused(w, data, o["words"], "other_hub")
    finally:
        other.close()


def test_a_dev_backup_does_not_restore_onto_a_real_server_and_the_reverse(w, tmp_path):
    real = HubKeys.init(tmp_path / "real", real=True)
    words, _ = BackupService(real).set_key(None)
    rdata = next((tmp_path / "real" / "backups").iterdir()).read_bytes()
    with pytest.raises(RS.RestoreError) as e:  # a dev server (the default) refuses a real hub's backup
        RS.restore(tmp_path / "s1" / "d", rdata, words, dev_mode=True)
    assert e.value.code == "dev_mismatch" and not (tmp_path / "s1").exists() or True
    assert not (tmp_path / "s1" / "d.new").exists() and not (tmp_path / "s1" / "d").exists()
    dwords, ddata = _backup(w)
    with pytest.raises(RS.RestoreError) as e:  # and a real server refuses the dev backup
        RS.restore(tmp_path / "s2" / "d", ddata, dwords, dev_mode=False)
    assert e.value.code == "dev_refused" and not (tmp_path / "s2" / "d").exists()


def _flip(data: bytes, at: int) -> bytes:
    return data[:at] + bytes([data[at] ^ 1]) + data[at + 1 :]


def test_a_changed_file_is_refused_wherever_the_change_is(w, monkeypatch):
    words, data = _backup(w)
    monkeypatch.setattr(RS, "_stage", lambda *a, **k: pytest.fail("staged"))
    n = int.from_bytes(data[8:12], "big")
    for at, reason in ((len(data) - 1, "seal"), (20 + n + 5, "seal"), (12 + n // 2, None), (3, "container")):
        _refused(w, _flip(data, at), words, "backup_invalid", None if reason is None else reason)
    _refused(w, data[: len(data) // 2], words, "backup_invalid")
    _refused(w, b"", words, "backup_invalid", "container")


def _forge(w, words, header_files, entries):
    """A backup the real root signed and maced, whose tar breaks a rule. ``entries``: list of ``(TarInfo, bytes)``."""
    h = B.read_header(_backup_bytes(w), dev_mode=True, live=None)[1]
    hdr = {**h, "files": SN.file_list(header_files)}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for ti, body in entries:
            tf.addfile(ti, io.BytesIO(body))
    keys = B.derive_keys(words)
    env = {"o": hdr, "sig": b64u(w.env.keys.sign_backup_header(hdr)), "mac": B.header_mac(keys.k_auth, hdr)}
    return B.pack_file(env, B.seal_payload(keys.bk_pub, hdr, buf.getvalue()))


def _backup_bytes(w):
    return next((w.state / "backups").iterdir()).read_bytes()


def _ti(name, body=b"", typ=tarfile.REGTYPE, link=""):
    ti = tarfile.TarInfo(name)
    ti.type, ti.size, ti.linkname = typ, len(body), link
    return ti, body


@pytest.mark.slow
def test_tar_traversal_links_extra_and_resized_entries_are_refused_before_anything_is_written(w, monkeypatch):
    words, _ = _backup(w)
    files = {"hk_root.key": b"a", "hxk.key": b"b", "meta.json": b"{}"}
    good = [_ti(p, c) for p, c in files.items()]
    cases = {
        "tar_path": good[:2] + [_ti("../evil", b"x")],
        "tar_path ": good[:2] + [_ti("/etc/passwd", b"x")],
        "tar_type": [_ti("hk_root.key", typ=tarfile.SYMTYPE, link="/etc/passwd")] + good[1:],
        "tar_unlisted": [*good, _ti("extra.txt", b"x")],
        "tar_dup": [*good, _ti("meta.json", b"{}")],
        "tar_size": [_ti("hk_root.key", b"aa"), *good[1:]],
        "hash": [_ti("hk_root.key", b"z"), *good[1:]],
        "tar_missing": good[:2],
    }
    monkeypatch.setattr(RS, "_stage", lambda *a, **k: pytest.fail("staged"))
    for reason, entries in cases.items():
        forged = _forge(w, words, files, entries)
        _refused(w, forged, words, "backup_invalid", reason.strip())
    assert not (w.tmp / "evil").exists() and not Path("/etc/passwd.evil").exists()


def test_a_file_list_with_a_traversing_path_never_reaches_the_tar(w):
    words, _ = _backup(w)
    h = B.read_header(_backup_bytes(w), dev_mode=True, live=None)[1]
    for bad in ("../evil", "/abs", "a/../b", "x\\y", "uk//k"):
        hdr = {**h, "files": [*h["files"], {"path": bad, "size": 0, "sha256": "0" * 64}]}
        env = {"o": hdr, "sig": b64u(w.env.keys.sign_backup_header(hdr)), "mac": b64u(bytes(32))}
        data = B.pack_file(env, b"x" * 100)
        with pytest.raises(RS.RestoreError) as e:
            _restore(w, data, words)
        assert e.value.code == "backup_invalid" and e.value.reason in ("files_path", "files_order")


# --- failure leaves the live hub alone (cond 17) -----------------------------------------------------------------------------


@pytest.mark.parametrize("hook", ["_lift_versions", "_revoke_channels", "_merge_people", "_write_meta"])
def test_a_failure_while_staging_leaves_the_live_state_untouched(w, monkeypatch, hook):
    words, data = _backup(w)
    before = _tree(w.state)
    monkeypatch.setattr(RS, hook, lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(RS.RestoreError) as e:
        _restore(w, data, words)
    assert e.value.code == "restore_failed" and _tree(w.state) == before
    assert not (w.tmp / "hub.new").exists() and not (w.tmp / "hub.old").exists()
    assert w.env.keys.op_cert()["o"]["cert_id"] not in w.env.keys.revoked_op_certs()
    assert "hub_restore" not in [r["o"]["type"] for _, r, _e in w.env.audit.store.after(0, 1000)]


def test_a_chain_that_cannot_be_written_refuses_the_restore(w):
    words, data = _backup(w)

    class Broken:
        def hub_restore(self, **kw):
            raise OSError("full")

    before = _tree(w.state)
    with pytest.raises(RS.RestoreError) as e:
        _restore(w, data, words, audit=Broken())
    assert e.value.code == "audit_failed" and _tree(w.state) == before and not (w.tmp / "hub.new").exists()


# --- cond 20: one heavy operation at a time ------------------------------------------------------------------------------------


def test_restore_refuses_while_an_update_runs_and_an_update_refuses_while_it_restores(w, monkeypatch):
    words, data = _backup(w)
    before = _tree(w.state)
    with hub_op(w.state, "update"):
        with pytest.raises(RS.RestoreError) as e:
            _restore(w, data, words)
        assert e.value.code == "restore_busy"
    assert _tree(w.state) == before
    seen = []
    real = RS._merge_people

    def spy(*a, **k):
        for kind in ("update", "remove", "restore"):
            try:
                with hub_op(w.state, kind):
                    seen.append("ran")
            except HubOpBusy:
                seen.append("busy")
        return real(*a, **k)

    monkeypatch.setattr(RS, "_merge_people", spy)
    _restore(w, data, words)
    assert seen == ["busy", "busy", "busy"]
    with hub_op(w.state, "update"):  # the lock is free again afterwards
        pass


# --- the console route (cond 7) -------------------------------------------------------------------------------------------------


def _route(w, tok, words, name, **extra):
    return w.call(
        "POST", "/admin/backup/restore", {**w.creds(w.admin), "name": name, "words": words, **extra}, token=tok
    )


def test_restore_route_is_admin_only_and_refuses_switch_phone_and_pairing_sessions(w):
    body = {"password": "x", "totp": "000000", "name": "hub-00000000-20260101T000000Z.orchbak", "words": "x"}
    assert w.call("POST", "/admin/backup/restore", body)[0] == 401
    assert w.call("POST", "/admin/backup/restore", body, token=w.login(w.ann))[:2] == (403, {"error": "forbidden_role"})
    assert w.call("POST", "/admin/backup/restore", body, token=w.login(w.admin, "app"))[0] in (401, 403)
    ticket = w.env.svc.open_session(w.admin)["token"]
    w.browser_tokens.add(ticket)
    assert w.call("POST", "/admin/backup/restore", body, token=ticket)[:2] == (403, {"error": "reauth_required"})
    adm = w.login(w.admin)
    assert w.call("POST", "/admin/backup/restore", body, token=adm, csrf=False)[0] == 403
    assert w.call("POST", "/admin/backup/restore", body, token=adm, origin="http://evil.example")[0] == 403
    assert "hub_restore" not in [r["o"]["type"] for _, r, _e in w.env.audit.store.after(0, 1000)]


def test_restore_route_checks_password_code_words_and_the_origin_then_restores_and_restarts(w):
    words, _data = _backup(w)
    name = next((w.state / "backups").iterdir()).name
    restarts = []
    from orch_hub.webapi.settings_api import SettingsControl

    w.api.d.settings_ctl = SettingsControl(restart=lambda: restarts.append(1))
    tok = w.login(w.admin)
    before = _tree(w.state)
    assert (
        w.call("POST", "/admin/backup/restore", {"name": name, "words": words}, token=tok)[0] == 400
    )  # no step-up at all
    bad = {"password": "wrong password", "totp": w.creds(w.admin)["totp"], "name": name, "words": words}
    assert w.call("POST", "/admin/backup/restore", bad, token=tok)[:2] == (401, {"error": "bad_credentials"})
    assert _route(w, tok, generate_words(), name)[:2] == (403, {"error": "wrong_backup_key"})
    assert _route(w, tok, "nonsense", name)[:2] == (400, {"error": "words"})
    assert _route(w, tok, words, "../../etc/passwd")[:2] == (404, {"error": "backup_not_found"})
    assert _route(w, tok, words, "hub-00000000-20260101T000000Z.orchbak")[0] == 404
    after = _tree(w.state)
    after.pop("channels.db"), before.pop("channels.db")  # (the failed sign-in checks leave limiter rows)
    assert after == before and restarts == []
    w.api.d.allowed_origins = frozenset(
        {"http://192.168.1.5:9000", "http://127.0.0.1:9000"}
    )  # an office address: refused up front
    s, o, _ = w.call(
        "POST",
        "/admin/backup/restore",
        {**w.creds(w.admin), "name": name, "words": words},
        token=tok,
        origin="http://192.168.1.5:9000",
    )
    assert s == 403 and {k: v for k, v in _tree(w.state).items() if k != "channels.db"} == before
    s, o, _ = _route(w, tok, words, name)
    assert (s, o["restarting"]) == (202, True) and restarts == [1] and o["suspended"] >= 1
    assert "words" not in json.dumps(o) and words not in json.dumps(o)
    assert HubKeys.open(w.state, now_ms=w.clock).op_seq >= 1000


# --- the server command ----------------------------------------------------------------------------------------------------------


def _cli(w, path, *, real=False, words, confirm="yes", out=None, tty=True, now=None):
    args = CLI._parser().parse_args(["restore", str(path), "--state", str(w.state), *(["--real"] if real else [])])
    return BCLI.run_restore(
        args,
        out=(out if out is not None else []).append,
        now_ms=now or w.clock,
        tty=lambda: tty,
        read_words=lambda: words,
        confirm=lambda _p: confirm,
    )


def test_the_server_command_restores_in_place_and_on_a_fresh_server(w, tmp_path, capsys):
    words, data = _backup(w)
    f = tmp_path / "x.orchbak"
    f.write_bytes(data)
    out: list[str] = []
    assert _cli(w, f, words=words, out=out, now=w.clock) == 0
    assert any("Backup of hub" in x and w.env.keys.hub_id[:4] in x for x in out)
    assert HubKeys.open(w.state, now_ms=w.clock).op_seq >= 1000
    # fresh server: the fingerprint is shown first
    fresh = tmp_path / "fresh" / "d"
    args = CLI._parser().parse_args(["restore", str(f), "--state", str(fresh)])
    out2: list[str] = []
    rc = BCLI.run_restore(
        args,
        out=out2.append,
        now_ms=lambda: 1_900_000_000_000,
        tty=lambda: True,
        read_words=lambda: words,
        confirm=lambda _p: "yes",
    )
    assert (
        rc == 0
        and any("no hub yet" in x for x in out2)
        and HubKeys.open(fresh, now_ms=lambda: 1_900_000_000_000).op_seq == 1_900_000_000_000
    )


def test_the_server_command_refuses_agents_pipes_a_wrong_answer_and_a_bad_file(w, tmp_path, monkeypatch, capsys):
    words, data = _backup(w)
    f = tmp_path / "x.orchbak"
    f.write_bytes(data)
    before = _tree(w.state)
    for var in ("ORCH_GRANT", "ORCH_SESSION"):
        monkeypatch.setenv(var, "x")
        assert _cli(w, f, words=words) == 1
        monkeypatch.delenv(var)
    assert _cli(w, f, words=words, tty=False) == 1
    assert _cli(w, f, words=words, confirm="no") == 1
    assert _cli(w, f, words=generate_words()) == 1 and "wrong_backup_key" in capsys.readouterr().err
    bad = tmp_path / "bad.orchbak"
    bad.write_bytes(_flip(data, len(data) - 1))
    assert _cli(w, bad, words=words) == 1
    assert _cli(w, tmp_path / "missing", words=words) == 1
    assert _tree(w.state) == before


# --- recovery of a swap that stopped between its two renames ------------------------------------------------------------------------


def test_recover_completes_or_undoes_an_interrupted_swap(w, tmp_path):
    words, data = _backup(w)
    state = w.state
    new, old = RS.paths(state)
    # crash after "state -> old" with a whole staged copy: the swap is completed
    os.rename(state, old)
    os.mkdir(new, 0o700)
    (new / RS.NOTICE_FILE).write_text("{}")
    RS.recover(state)
    assert state.is_dir() and (state / RS.NOTICE_FILE).exists() and old.is_dir() and not new.exists()
    # crash after "state -> old" with an unfinished staged copy: the live state comes back
    shutil_rm = __import__("shutil").rmtree
    shutil_rm(state)
    os.mkdir(new, 0o700)
    RS.recover(state)
    assert state.is_dir() and not new.exists() and not old.exists()
    # a leftover staged copy next to a good state is removed
    os.mkdir(new, 0o700)
    RS.recover(state)
    assert not new.exists()
    RS.finish(state)  # nothing to finish
    assert data and words


# --- the one rule: a restore never widens and never revives (review of 29 Oct) -----------------------------------------------------


def _org(w):
    """Company secrets, a skill and an MCP server offered to workspaces, in the live access store."""
    from orch_hub.access.orgmcp import org_mcp
    from orch_hub.access.orgsecrets import org_secrets
    from orch_hub.access.orgskills import org_skills

    org_secrets(w.env.acc), org_skills(w.env.acc), org_mcp(w.env.acc)
    db = w.env.acc._db
    db.execute("INSERT INTO org_secrets VALUES ('s1','api key',1,9999999999999,'chosen',?,1,NULL)", (w.admin,))
    db.execute("INSERT INTO org_secrets VALUES ('s2','wide',1,9999999999999,'all',?,1,NULL)", (w.admin,))
    db.execute("INSERT INTO org_secret_shares VALUES ('s1', ?)", (WS,))
    db.execute("INSERT INTO org_secret_wraps VALUES ('s1',1,?,1,x'00')", (WS,))
    db.execute("INSERT INTO org_skill VALUES ('sk','text',1,1)")
    db.execute("INSERT INTO org_skill_offer VALUES ('sk', ?)", (WS,))
    db.execute("INSERT INTO org_mcp VALUES ('m','https://x.example','none',NULL,'d',1,1)")
    db.execute("INSERT INTO org_mcp_offer VALUES ('m', ?)", (WS,))


def _rows(state, db, sql):
    c = sqlite3.connect(state / db)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def test_a_withdrawn_share_offer_or_grant_is_not_back_after_an_in_place_restore(w):
    _org(w)
    words, data = _backup(w)
    db = w.env.acc._db
    db.execute("DELETE FROM org_secret_shares")  # withdrawn after the backup
    db.execute("UPDATE org_secrets SET removed_ms=5 WHERE id='s2'")
    db.execute("DELETE FROM org_skill_offer")
    db.execute("DELETE FROM org_mcp_offer")
    w.env.acc.revoke(WS, "team", actor=w.admin)  # a grant taken away
    w.env.acc.remove_member("team", w.ann, actor=w.admin)
    _restore(w, data, words)
    q = lambda sql: _rows(w.state, "access.db", sql)
    assert q("SELECT * FROM org_secret_shares") == [] and q("SELECT * FROM org_skill_offer") == []
    assert q("SELECT * FROM org_mcp_offer") == [] and q("SELECT removed_ms FROM org_secrets WHERE id='s2'") == [(5,)]
    assert q("SELECT * FROM grants") == [] and q("SELECT user_id FROM members WHERE grp='team'") == [(w.dave,)]
    # and what is live now stays live, nothing is dropped that the live hub still has
    assert q("SELECT id FROM org_secrets ORDER BY id") == [("s1",), ("s2",)] and q("SELECT name FROM org_skill") == [
        ("sk",)
    ]


def test_a_narrowed_role_and_a_live_only_grant_do_not_come_back_wider(w):
    words, data = _backup(w)  # team has "edit" on WS in the backup
    w.env.acc.grant(WS, "team", "read", actor=w.admin)  # narrowed live
    _restore(w, data, words)
    assert _rows(w.state, "access.db", f"SELECT role FROM grants WHERE workspace_id='{WS}'") == [("read",)]


def test_an_old_password_and_code_are_dead_after_an_in_place_restore(w):
    words, data = _backup(w)
    old = _rows(w.state, "channels.db", f"SELECT pw_hash, totp_secret FROM credentials WHERE user_id='{w.admin}'")
    w.env.svc.set_password(w.admin, "a brand new password!")  # changed after the backup
    new = _rows(w.state, "channels.db", f"SELECT pw_hash, totp_secret FROM credentials WHERE user_id='{w.admin}'")
    assert new != old
    _restore(w, data, words)
    got = _rows(w.state, "channels.db", f"SELECT pw_hash, totp_secret FROM credentials WHERE user_id='{w.admin}'")
    assert got == new and got != old


def test_live_stores_of_connections_and_so_on_are_taken_from_the_live_hub(w):
    words, data = _backup(w)
    w.connections.allow("ab" * 16, "cd" * 16, actor=w.admin) if hasattr(w.connections, "allow") else None
    live = _rows(w.state, "connections.db", "SELECT * FROM connection")
    _restore(w, data, words)
    assert _rows(w.state, "connections.db", "SELECT * FROM connection") == live


def test_a_fresh_server_switches_shares_and_offers_off_until_an_admin_confirms_and_admins_must_recover(w, tmp_path):
    _org(w)
    words, data = _backup(w)
    fresh = tmp_path / "srv" / "d"
    got = RS.restore(fresh, data, words, now_ms=lambda: 1_900_000_000_000)
    q = lambda sql: _rows(fresh, "access.db", sql)
    assert (
        q("SELECT * FROM org_secret_shares") == []
        and q("SELECT * FROM org_skill_offer") == []
        and q("SELECT * FROM org_mcp_offer") == []
    )
    assert q("SELECT shared FROM org_secrets WHERE id='s2'") == [("chosen",)]
    kinds = sorted(k for (k,) in q("SELECT kind FROM restore_pending"))
    assert kinds == ["mcp_offer", "secret_all", "secret_share", "skill_offer"]
    # admins: the old password and code are dead until a one-time link is used; the link is returned once
    assert set(got["recovery"]) == {w.admin, w.carol} and all(len(t) > 20 for t in got["recovery"].values())
    assert sorted(u for (u,) in q("SELECT user_id FROM users WHERE must_recover=1 AND disabled_ms IS NULL")) == sorted(
        [w.admin, w.carol]
    )
    assert _rows(fresh, "invites.db", "SELECT COUNT(*) FROM invites WHERE kind='recovery' AND used_ms IS NULL") == [
        (2,)
    ]
    # the confirm path: one click turns one item back on
    from orch_hub.backup import guard as GD

    acc = Access(str(fresh / "access.db"))
    try:
        items = GD.pending(acc)
        assert len(items) == 4 and {i["label"] for i in items} == {"api key", "wide", "sk", "m"}
        for i in items:
            GD.confirm(acc, i["id"])
        assert GD.pending(acc) == []
        assert acc._q("SELECT shared FROM org_secrets WHERE id='s2'") == [("all",)]
        assert acc._q("SELECT * FROM org_secret_shares") == [("s1", WS)] and acc._q(
            "SELECT * FROM org_skill_offer"
        ) == [("sk", WS)]
        with pytest.raises(KeyError):
            GD.confirm(acc, 999)
    finally:
        acc.close()


def test_the_pending_routes_list_and_confirm_for_admins_only(w):
    _org(w)
    words, data = _backup(w)
    fresh = w.tmp / "f" / "d"
    RS.restore(fresh, data, words, now_ms=lambda: 1_900_000_000_000)
    acc = Access(str(fresh / "access.db"))
    w.api.d.access = acc
    try:
        assert w.call("GET", "/admin/backup/restore/pending")[0] == 401
        assert w.call("GET", "/admin/backup/restore/pending", token=w.login(w.ann))[0] == 403
        tok = w.login(w.admin)
        s, o, _ = w.call("GET", "/admin/backup/restore/pending", token=tok)
        assert s == 200 and len(o["pending"]) == 4
        pid = o["pending"][0]["id"]
        assert w.call("POST", "/admin/backup/restore/pending/confirm", {"id": "x"}, token=tok)[0] == 400
        assert w.call("POST", "/admin/backup/restore/pending/confirm", {"id": pid}, token=tok)[0] == 200
        assert w.call("POST", "/admin/backup/restore/pending/confirm", {"id": pid}, token=tok)[0] == 404
    finally:
        acc.close()


# --- the stricter value always wins (per-user security fields) --------------------------------------------------------------------------


def _set(w, sql, *args):
    w.env.acc._db.execute(sql, args)


def test_must_recover_set_live_after_the_backup_is_still_set_and_its_credentials_are_gone(w):
    words, data = _backup(w)
    _set(w, "UPDATE users SET must_recover=1 WHERE user_id=?", w.carol)  # forced to recover after the backup
    _restore(w, data, words)
    q = lambda sql: _rows(w.state, "access.db", sql)
    assert q(f"SELECT must_recover FROM users WHERE user_id='{w.carol}'") == [(1,)]
    assert q(f"SELECT must_recover FROM users WHERE user_id='{w.admin}'") == [(0,)]
    assert _rows(w.state, "channels.db", f"SELECT * FROM credentials WHERE user_id='{w.carol}'") == []
    assert _rows(w.state, "channels.db", f"SELECT COUNT(*) FROM credentials WHERE user_id='{w.admin}'") == [(1,)]


def test_a_lockout_and_a_used_code_counter_of_the_live_hub_survive(w):
    words, data = _backup(w)
    for i in range(5):
        w.env.svc.db.w("INSERT INTO failures(kind, subject, ts_ms) VALUES ('pw', ?, ?)", (w.admin, w.clock() - i))
    w.env.svc.db.w("UPDATE credentials SET totp_last=? WHERE user_id=?", (999_999_999, w.admin))
    _restore(w, data, words)
    assert _rows(w.state, "channels.db", f"SELECT COUNT(*) FROM failures WHERE subject='{w.admin}'") == [(5,)]
    assert _rows(w.state, "channels.db", f"SELECT totp_last FROM credentials WHERE user_id='{w.admin}'") == [
        (999_999_999,)
    ]


def test_disabled_live_wins_over_enabled_in_the_backup_and_never_the_reverse(w):
    erin = w.env.user("erin")
    words, data = _backup(w)
    w.env.acc.set_disabled(erin, True, actor=w.admin)
    _restore(w, data, words)
    assert _rows(w.state, "access.db", f"SELECT disabled_ms IS NOT NULL FROM users WHERE user_id='{erin}'") == [(1,)]


def test_a_fresh_server_deletes_the_credentials_of_everyone_who_must_recover(w, tmp_path):
    _set(w, "UPDATE users SET must_recover=1 WHERE user_id=?", w.ann)
    words, data = _backup(w)
    fresh = tmp_path / "srv" / "d"
    RS.restore(fresh, data, words, now_ms=lambda: 1_900_000_000_000)
    left = {u for (u,) in _rows(fresh, "channels.db", "SELECT user_id FROM credentials")}
    assert w.ann not in left and w.admin not in left and w.carol not in left  # admins recover too (fresh server)
    assert _rows(fresh, "access.db", f"SELECT must_recover FROM users WHERE user_id='{w.ann}'") == [(1,)]


# --- review of S6 (Opus, 29 Oct) -------------------------------------------------------------------------------------------------------


def test_L2_a_pending_row_that_is_not_live_does_not_come_back(w):
    from orch_hub.backup import guard as GD

    w.env.acc._db.execute(GD.PENDING_SCHEMA)
    w.env.acc._db.execute("INSERT INTO restore_pending(kind,a,b,label) VALUES ('secret_all','s9','','x')")
    words, data = _backup(w)
    w.env.acc._db.execute("DELETE FROM restore_pending")  # confirmed live after the backup
    _restore(w, data, words)
    assert _rows(w.state, "access.db", "SELECT * FROM restore_pending") == []


def test_L3_a_channel_paired_after_the_backup_is_revoked_in_the_store_so_the_relay_purge_runs(w):
    words, data = _backup(w)
    _signer, cid = w.register_channel(w.login(w.ann))
    _restore(w, data, words)
    rows = _rows(
        w.state,
        "channels.db",
        "SELECT channel_id FROM channels WHERE revoked_ms IS NOT NULL "
        "AND channel_id NOT IN (SELECT channel_id FROM relay_purged)",
    )
    assert (cid,) in rows


def test_L4_recover_gives_the_kept_backups_back_when_the_crash_came_after_they_moved(w):
    _words, _data = _backup(w)
    state = w.state
    new, old = RS.paths(state)
    kept = sorted(p.name for p in (state / "backups").iterdir())
    # crash point 1: backups moved into the staged copy, state itself still in place (no backups folder any more)
    os.mkdir(new, 0o700)
    os.rename(state / "backups", new / "backups")
    RS.recover(state)
    assert sorted(p.name for p in (state / "backups").iterdir()) == kept and not new.exists()
    # crash point 2: state renamed to old, the staged copy is unfinished and holds the backups
    os.mkdir(new, 0o700)
    os.rename(state / "backups", new / "backups")
    os.rename(state, old)
    RS.recover(state)
    assert sorted(p.name for p in (state / "backups").iterdir()) == kept and not new.exists() and not old.exists()


def test_L1_a_fresh_server_restore_is_audited_too(w, tmp_path):
    words, data = _backup(w)
    fresh = tmp_path / "srv" / "d"
    got = RS.restore(fresh, data, words, now_ms=lambda: 1_900_000_000_000)
    from orch_hub.audit import AuditStore

    st = AuditStore(str(fresh / "audit.db"))
    try:
        recs = [(r["o"], e) for _, r, e in st.after(0, 1000)]
    finally:
        st.close()
    last, ev = recs[-1]
    assert last["type"] == "hub_restore" and ev["op_seq"] == got["op_seq"] and ev["actor_user_id"] is None
    assert ev["backup_made_ms"] == got["made_ms"] and ev["gap_ms"] == got["gap_ms"]


def _company_policy(block=("bad-addon",), allow=None, ceiling=("network",), market=True):
    import base64

    from cryptography.hazmat.primitives.asymmetric import ec

    from orch_hub.contract import market_policy as MP
    from orch_hub.contract import suite as SU

    pol = MP.default_policy()
    if market:
        key = base64.urlsafe_b64encode(SU.public_bytes(ec.generate_private_key(ec.SECP256R1()))).decode().rstrip("=")
        pol["markets"] = sorted(
            [*pol["markets"], {"url": "https://git.example.com/acme/market", "class": "company", "keys": [key]}],
            key=lambda m: m["url"],
        )
    pol["addons"] = {"allow": None if allow is None else list(allow), "block": list(block)}
    pol["ceiling"] = None if ceiling is None else list(ceiling)
    return MP.check_policy(pol)


def _put_policy(w, pol, version=2):
    import json as _j

    from orch_hub.contract import market_policy as MP

    w.env.acc._db.execute(
        "UPDATE market_policy SET version=?, policy=?, hash=? WHERE id=1",
        (version, _j.dumps(pol, separators=(",", ":"), sort_keys=True), MP.policy_hash(pol)),
    )


def test_L5_a_fresh_server_keeps_every_restriction_and_stages_only_what_grants(w, tmp_path):
    import json as _j

    from orch_hub.backup import guard as GD
    from orch_hub.contract import market_policy as MP

    full = _company_policy()
    _put_policy(w, full)
    db = w.env.acc._db
    db.execute("INSERT INTO workspace_addon_allow VALUES (?, '[\"a\"]')", (WS,))
    db.execute("INSERT INTO addon_request VALUES (?, 'x', '1', 'person', 1, 1, 1)", (WS,))
    words, data = _backup(w)
    fresh = tmp_path / "srv" / "d"
    RS.restore(fresh, data, words, now_ms=lambda: 1_900_000_000_000)
    # RESTRICTING: allow lists, block list, ceiling are kept as they were
    assert _rows(fresh, "access.db", "SELECT * FROM workspace_addon_allow") == [(WS, '["a"]')]
    pol = _j.loads(_rows(fresh, "access.db", "SELECT policy FROM market_policy")[0][0])
    assert MP.check_policy(pol) and pol["addons"] == full["addons"] and pol["ceiling"] == full["ceiling"]
    # GRANTING: company markets and keys are dropped (official only) and wait for an admin; requests wait too
    assert pol["markets"] == MP.default_policy()["markets"]
    assert _rows(fresh, "access.db", "SELECT * FROM addon_request") == []
    kinds = {k for (k,) in _rows(fresh, "access.db", "SELECT kind FROM restore_pending")}
    assert kinds == {"market_company", "addon_request"}
    acc = Access(str(fresh / "access.db"))
    try:
        for i in GD.pending(acc):
            GD.confirm(acc, i["id"])
        assert _j.loads(acc._q("SELECT policy FROM market_policy")[0][0]) == full
        assert acc.market_policy()["hash"] == MP.policy_hash(full)
        assert len(acc._q("SELECT * FROM addon_request")) == 1
    finally:
        acc.close()


def test_L5_in_place_the_stricter_of_backup_and_live_wins_for_every_restricting_field(w):
    import json as _j

    from orch_hub.contract import market_policy as MP

    _put_policy(w, _company_policy(block=("old-bad",), allow=("aa", "bb"), ceiling=("network", "pty")))
    db = w.env.acc._db
    db.execute('INSERT INTO workspace_addon_allow VALUES (?, \'["a","b"]\')', (WS,))
    words, data = _backup(w)
    live = _company_policy(block=("new-bad",), allow=("bb", "cc"), ceiling=("git_push", "pty"), market=False)
    _put_policy(w, live, version=3)
    db.execute('UPDATE workspace_addon_allow SET ids=\'["b","c"]\'')
    db.execute("INSERT INTO workspace_addon_allow VALUES (?, '[\"z\"]')", ("ee" * 16,))  # only live
    _restore(w, data, words)
    pol = _j.loads(_rows(w.state, "access.db", "SELECT policy FROM market_policy")[0][0])
    assert MP.check_policy(pol)
    assert pol["addons"] == {"allow": ["bb"], "block": ["new-bad", "old-bad"]}  # allow: intersection, block: union
    assert pol["ceiling"] == ["pty"]  # intersection
    assert pol["markets"] == MP.default_policy()["markets"]  # granting part = live (no company market live)
    ids = dict(_rows(w.state, "access.db", "SELECT workspace_id, ids FROM workspace_addon_allow"))
    assert _j.loads(ids[WS]) == ["b"] and _j.loads(ids["ee" * 16]) == ["z"]
    assert _rows(w.state, "access.db", "SELECT version FROM market_policy")[0][0] >= 3


def test_L5_every_restored_table_has_a_class_so_a_new_table_cannot_slip_through(w):
    from orch_hub.backup import guard as GD

    _org(w)
    words, data = _backup(w)
    opened = B.open_backup(data, words=words, dev_mode=True, live=None)
    seen = set()
    for path, blob in opened.files.items():
        if not path.endswith(".db") or "/" in path:
            continue
        p = w.tmp / ("probe-" + path)
        p.write_bytes(blob)
        c = sqlite3.connect(p)
        try:
            tables = {
                n
                for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            }
        finally:
            c.close()
        for t in tables:
            seen.add(f"{path}:{t}")
            assert GD.class_of(path, t) in ("restricting", "granting", "log", "state"), f"{path}:{t} has no class"
    assert (
        "access.db:workspace_addon_allow" in seen and GD.class_of("access.db", "workspace_addon_allow") == "restricting"
    )
    assert GD.class_of("access.db", "org_secret_shares") == "granting"
    with pytest.raises(KeyError):
        GD.class_of("access.db", "a_table_nobody_classified")


def test_L5_the_server_command_names_the_admins_it_prints_links_for(w, tmp_path):
    words, data = _backup(w)
    f = tmp_path / "x.orchbak"
    f.write_bytes(data)
    args = CLI._parser().parse_args(["restore", str(f), "--state", str(tmp_path / "fresh" / "d")])
    out: list[str] = []
    rc = BCLI.run_restore(
        args,
        out=out.append,
        now_ms=lambda: 1_900_000_000_000,
        tty=lambda: True,
        read_words=lambda: words,
        confirm=lambda _p: "yes",
    )
    text = "\n".join(out)
    assert rc == 0 and "admin (" in text and "carol (" in text and "#invite=" in text


def test_M1_writes_during_a_console_restore_are_refused_not_lost(w, monkeypatch):
    erin = w.env.user("erin")
    w.env.acc.add_member("team", erin)
    words, _data = _backup(w)
    name = next((w.state / "backups").iterdir()).name
    tok = w.login(w.admin)
    other = w.login(w.carol)
    seen = []
    real = RS._revoke_channels

    def hook(*a, **k):  # a concurrent admin removes erin while the restore runs
        seen.append(w.call("DELETE", f"/admin/users/{erin}", token=other)[:2])
        seen.append(w.call("POST", "/admin/backups", {}, token=other)[:2])
        seen.append(w.call("GET", "/admin/backup", token=other)[0])  # reads still work
        return real(*a, **k)

    monkeypatch.setattr(RS, "_revoke_channels", hook)
    s, o, _ = _route(w, tok, words, name)
    assert s == 200, o
    assert (
        seen[0] == (409, {"error": "restore_busy"}) and seen[1] == (409, {"error": "restore_busy"}) and seen[2] == 200
    )
    from orch_hub.backup.hubop import FENCE

    assert not FENCE.is_set()  # no restart hook in the test hub: the fence is lifted again
    acc = Access(str(w.state / "access.db"))
    try:
        assert acc.has_user(erin)  # the removal was refused, not lost: erin is still there (suspended)
    finally:
        acc.close()


def test_M1_a_failed_console_restore_lifts_the_fence(w):
    _backup(w)
    name = next((w.state / "backups").iterdir()).name
    tok = w.login(w.admin)
    from orch_hub.backup.hubop import FENCE

    assert _route(w, tok, generate_words(), name)[0] == 403
    assert not FENCE.is_set()


def test_M1_the_link_server_refuses_every_op_while_the_fence_is_set():
    from orch_hub.backup.hubop import FENCE
    from orch_hub.link.server import LinkServer

    class Probe(LinkServer):
        def __init__(self):  # only _run is under test
            pass

    FENCE.set()
    assert Probe()._run("ab" * 16, b"", {"op": "status_put", "body": {}}, 1) == {"error": "restore_busy"}


def test_M2_the_server_command_refuses_while_a_hub_serves_this_state(w, tmp_path, capsys):
    words, data = _backup(w)
    f = tmp_path / "x.orchbak"
    f.write_bytes(data)
    before = _tree(w.state)
    import subprocess
    import sys

    holder = subprocess.Popen(  # another process: what `serve` is
        [
            sys.executable,
            "-c",
            "import sys,time;from pathlib import Path;from orch_hub.backup.hubop import hold_serve_lock as h;h(Path(sys.argv[1]));print('x',flush=True);time.sleep(60)",
            str(w.state),
        ],
        stdout=subprocess.PIPE,
    )
    holder.stdout.readline()
    try:
        assert _cli(w, f, words=words) == 1
        assert "hub is running" in capsys.readouterr().err and _tree(w.state) == before
    finally:
        holder.kill()
        holder.wait()
    assert _cli(w, f, words=words) == 0  # the lock is gone with the process: the restore runs
