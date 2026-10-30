"""Restoring the hub from a backup (M4 S6, protocol §17.5-§17.8, Opus conditions 13-17, 20, 23, 24).

One code path for the console route, ``orch-hub restore FILE`` and (later) the installer. The order, which is the contract:

1. Every check passes before ANYTHING is written (cond 17): :func:`contract.backup.open_backup` (signature, hub id, dev flag,
   key, MAC, seal, tar rules, hashes), the sequence floor, the live merge plan. Nothing in the live state dir changes.
2. The listed files (and only those) are written under ``<state>.new`` (dirs 0700, files 0600, regular files, no paths from
   outside the list). The staged copy is then made safe: people removed or disabled in the LIVE hub stay removed or disabled,
   every other non-admin is suspended (no UK certificate; an admin turns each back on in People), every channel is revoked and
   every session ended, the backup's op certificate is revoked, and the hub rotates to a new op certificate with
   ``op_seq``/``card_seq`` above everything ever issued (in place ``max(live, backup) + 1000``, a fresh server
   ``max(backup + 1000, clock)``; the clock is in unix MILLISECONDS here, so a floor from "now" is always above a live
   counter that earlier restores pushed up by thousands), and the snapshot versions are lifted the same way.
3. The restore is audited (cond 23), then the LIVE op certificate is revoked and the live ``revoked/`` merged in, then
   ``<state>`` is swapped for ``<state>.new`` (two renames; :func:`recover` repairs a crash between them). The live backup
   key file, audit chain, activity history, ``hub-settings.json``, TLS key and the kept backups are carried over, never taken
   from the backup (a backup key file would bring back an old, possibly leaked key).

A failure before the swap removes the staged copy and leaves the live state as it was (only the live op certificate may
already be revoked, which a rotation fixes: the hub is fenced, not damaged)."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..contract import backup as B
from ..contract.canon import CanonError, b64u, cj
from ..contract.hub import Refused
from ..keys import HubKeys, KeysError
from ..keys.fsutil import FILE_MODE, StateError, check_dir, fsync_dir, make_dir, read_file
from . import guard as G
from . import snapshot as SN
from .hubop import HubOpBusy, hub_op
from .words import WordsError, check_words

ADMIN_GROUP = "hub-admin"
#: never taken from a backup: the live file stays (cond: a restored backup-key file would bring back an old key)
KEEP_LIVE = frozenset({SN.KEY_FILE})
#: kept from the live hub when restoring in place: logs are appended to, never rolled back (cond 15); the addresses and the
#: TLS key belong to this server as it is now
LOGS = frozenset({"audit.db", "activity.db"})
#: stores that hold withdrawals and permissions: in place the LIVE store is the truth, a backup's copy would revive what an
#: admin took away since (guard.py)
LIVE_WHOLE = frozenset(
    {
        "registry.db",
        "connections.db",
        "shares.db",
        "spaces.db",
        "changes.db",
        "wk-seal.db",
        "workspace-view.db",
        "oidc.db",
    }
)
#: company login (HUB-PE-04): the links (oidc.db) are LIVE_WHOLE above, and its settings, client secret and cookie key stay
#: the live ones in place: an admin may have unlinked a person, turned it off or replaced the secret since the backup, and a
#: restore never revives that. On a fresh server all four come from the backup (the backup is sealed like the hub keys) and
#: company login is switched OFF until an admin tests it again (G.oidc_off).
COMPANY_LOGIN = frozenset({"oidc.json", "oidc.secret", "oidc.key"})
LIVE_IN_PLACE = LOGS | LIVE_WHOLE | COMPANY_LOGIN | {"hub-settings.json", "tls.key"}
#: carried over by name (not in any backup)
CARRY_FILES = ("setup-token",)
CARRY_DIRS = ("backups",)
BUNDLE_TABLES = ("snapshots", "org_skills_bundle", "org_mcp_bundle", "org_secrets_bundle")
NOTICE_FILE = "restore-notice.json"
CLI_LINE = "orch-hub restore FILE"


class RestoreError(Exception):
    """A refused or failed restore; ``code`` is stable and the text never carries a word, a key or file content."""

    def __init__(self, code: str, reason: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.reason = (
            reason  # the contract's reason of ``backup_invalid`` (for the admin's log and the tests), never an oracle
        )


def paths(state: Path) -> tuple[Path, Path]:
    """``(<state>.new, <state>.old)`` next to the state dir."""
    s = Path(state)
    return s.parent / f"{s.name}.new", s.parent / f"{s.name}.old"


def fingerprint(hub_id: str) -> str:
    """``hub_id`` as 8 groups of 4 characters, as the phone shows it."""
    return " ".join(hub_id[i : i + 4] for i in range(0, 32, 4))


def _rm(path: Path) -> None:
    """Remove exactly this path (a symlink is unlinked, never followed)."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def recover(state: Path) -> None:
    """Called before the hub opens its state. A crash between the two renames of an in-place swap left no ``<state>`` but the
    live one as ``<state>.old``: the swap is completed if ``<state>.new`` is whole (its notice file is written last), else
    undone. A leftover ``<state>.new`` next to a good ``<state>`` is an unfinished restore and is removed."""
    state = Path(state)
    new, old = paths(state)
    if not state.exists():
        if old.is_dir():
            if new.is_dir() and (new / NOTICE_FILE).exists():
                os.rename(new, state)
            else:
                os.rename(old, state)
                _rescue_backups(state, new)
                _rm(new)
    elif new.exists():
        _rescue_backups(state, new)
        _rm(new)


def _rescue_backups(state: Path, new: Path) -> None:
    """The kept backups move into the staged copy just before the swap: a crash after that must give them back."""
    src = new / "backups"
    if src.is_dir() and not src.is_symlink() and not (state / "backups").exists():
        os.rename(src, state / "backups")


def finish(state: Path) -> None:
    """After the first good start: the pre-restore state (``<state>.old``, with the old keys) is removed."""
    _, old = paths(Path(state))
    if old.exists() and Path(state).is_dir() and (Path(state) / "meta.json").exists():
        _rm(old)


# --- reading the live hub (read only) ----------------------------------------------------------------------------------


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=15)


def _live_rows(state: Path, db: str, sql: str, args: tuple = ()) -> list[tuple]:
    p = state / db
    if not p.is_file():
        return []
    c = _ro(p)
    try:
        return c.execute(sql, args).fetchall()
    except sqlite3.OperationalError:  # a table an older store never had
        return []
    finally:
        c.close()


def live_snapshot_seq(state: Path) -> int:
    """The highest version the live hub ever issued in any signed per-workspace object (snapshots and the org bundles)."""
    top = 0
    for (v,) in _live_rows(state, "access.db", "SELECT floor FROM version_floor"):
        top = max(top, int(v or 0))
    for t in BUNDLE_TABLES:
        for (v,) in _live_rows(state, "access.db", f"SELECT MAX(version) FROM {t}"):
            top = max(top, int(v or 0))
    return top


# --- writing the staged copy -------------------------------------------------------------------------------------------


def _put(root: Path, rel: str, data: bytes) -> None:
    """One regular file under ``root`` (0600), its directories 0700. ``rel`` was validated by the contract (valid_path)."""
    if not B.valid_path(rel):
        raise RestoreError("backup_invalid")
    cur = root
    for seg in rel.split("/")[:-1]:
        cur = cur / seg
        make_dir(cur)
    fd = os.open(root / rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(root / rel)
        raise


def _stage(new: Path, files: dict[str, bytes], state: Path, in_place: bool) -> None:
    """Write the backup's listed files (not the ones that stay live) and, in place, the live files that are never taken from
    a backup. The live logs are copied last, by :func:`_copy_logs`."""
    _rm(new)
    os.mkdir(new, 0o700)
    os.chmod(new, 0o700)
    check_dir(new)
    skip = set(KEEP_LIVE) | (LIVE_IN_PLACE if in_place else set())
    for rel in sorted(files):
        if rel not in skip:
            _put(new, rel, files[rel])
    if in_place:
        for rel in (KEEP_LIVE | LIVE_IN_PLACE) - LOGS:
            src = state / rel
            if src.is_file() and not src.is_symlink():
                _put(new, rel, SN.copy_db(src)[0] if rel.endswith(".db") else read_file(src))
    for sub in ("uk", "op", "revoked"):
        if not (new / sub).exists():
            make_dir(new / sub)


def _copy_logs(state: Path, new: Path) -> None:
    """The live audit chain and activity history, as they are NOW (after this restore's own lines), one consistent copy each:
    logs are appended to, never rolled back (cond 15)."""
    for rel in sorted(LOGS):
        src = state / rel
        if src.is_file() and not src.is_symlink():
            _put(new, rel, SN.copy_db(src)[0])


def _meta(new: Path, live: HubKeys | None) -> dict[str, Any]:
    try:
        m = json.loads(read_file(new / "meta.json"))
    except StateError, ValueError:
        raise RestoreError("backup_invalid") from None
    if live is not None:  # this server as it is now: its TLS pin and relay
        card = live.card()["o"]
        m["tls_spki_sha256"], m["relay_url"] = card["tls_spki_sha256"], card["relay_url"]
    return m


def _write_meta(new: Path, meta: dict[str, Any]) -> None:
    tmp = new / ".meta.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE)
    with os.fdopen(fd, "wb") as f:
        f.write(cj(meta) + b"\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, new / "meta.json")


# --- the people and channels of the staged copy -------------------------------------------------------------------------


def _merge_people(new: Path, live: Path | None, now: int) -> tuple[frozenset[str], set[str]]:
    """Make ``<new>/access.db`` safe (conds 15, 16): users removed in the live hub are removed, users disabled live stay
    disabled, an admin who is no longer an admin live is not one again, live channel revocations are added, every other
    person who is not an admin is suspended. Returns ``(admins, removed)``; refuses with ``no_admin`` when nobody could sign in."""
    removed: set[str] = set()
    db = sqlite3.connect(new / "access.db", isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        users = {u for (u,) in db.execute("SELECT user_id FROM users")}
        if live is not None:
            live_users = {u: d for u, d in _live_rows(live, "access.db", "SELECT user_id, disabled_ms FROM users")}
            live_admins = {
                u for (u,) in _live_rows(live, "access.db", "SELECT user_id FROM members WHERE grp=?", (ADMIN_GROUP,))
            }
            removed = users - set(live_users)
            for u in sorted(removed):
                db.execute("DELETE FROM users WHERE user_id=?", (u,))
            for u, d in live_users.items():
                if d is not None:
                    db.execute("UPDATE users SET disabled_ms=? WHERE user_id=? AND disabled_ms IS NULL", (d, u))
                if u in users and u not in live_admins:  # demoted since the backup: not an admin again
                    db.execute("DELETE FROM members WHERE grp=? AND user_id=?", (ADMIN_GROUP, u))
            for (c,) in _live_rows(live, "access.db", "SELECT channel_id FROM revoked_channels"):
                db.execute("INSERT OR IGNORE INTO revoked_channels VALUES (?)", (c,))
        admins = frozenset(
            u
            for (u,) in db.execute(
                "SELECT m.user_id FROM members m JOIN users x ON x.user_id=m.user_id "
                "WHERE m.grp=? AND x.disabled_ms IS NULL",
                (ADMIN_GROUP,),
            )
        )
        if not admins:
            raise RestoreError("no_admin")
        # everyone who is not an admin waits until an admin turns them on again (enabling also sets must_recover)
        db.execute(
            "UPDATE users SET disabled_ms=? WHERE disabled_ms IS NULL AND user_id NOT IN "
            "(SELECT user_id FROM members WHERE grp=?)",
            (now, ADMIN_GROUP),
        )
        db.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    return admins, removed


def _revoke_channels(new: Path, live: Path | None, removed: set[str], actor: str | None, now: int) -> int:
    """Every channel of the staged copy and of the live hub is revoked (also in ``revoked_channels`` of the access store,
    which is what hosts read), every session, pairing code and challenge is gone, the removed users' credentials too."""
    ids: set[str] = set()
    if live is not None:
        G.live_channels(new, live, now, actor)
    db = sqlite3.connect(new / "channels.db", isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        ids |= {c for (c,) in db.execute("SELECT channel_id FROM channels")}
        if live is not None:
            ids |= {c for (c,) in _live_rows(live, "channels.db", "SELECT channel_id FROM channels")}
        db.execute("UPDATE channels SET revoked_ms=?, revoked_by=? WHERE revoked_ms IS NULL", (now, actor or "restore"))
        for t in ("sessions", "pairing", "challenges", "pending_totp", "push_keys", "known_sources"):
            with contextlib.suppress(sqlite3.OperationalError):
                db.execute(f"DELETE FROM {t}")
        for u in sorted(removed):
            with contextlib.suppress(sqlite3.OperationalError):
                db.execute("DELETE FROM credentials WHERE user_id=?", (u,))
        db.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    acc = sqlite3.connect(new / "access.db", isolation_level=None)
    try:
        acc.execute("BEGIN IMMEDIATE")
        for c in sorted(ids):
            acc.execute("INSERT OR IGNORE INTO revoked_channels VALUES (?)", (c,))
        acc.execute("COMMIT")
    finally:
        acc.close()
    return len(ids)


def _lift_versions(new: Path, floor: int) -> None:
    """Every signed per-workspace object (snapshot, org bundles) is issued next with a version above ``floor``
    (``version_floor`` of the access store, read by ``AccessSnapshots``): rows that are new or rolled back are covered too."""
    db = sqlite3.connect(new / "access.db", isolation_level=None)
    try:
        db.execute(
            "CREATE TABLE IF NOT EXISTS version_floor (id INTEGER PRIMARY KEY CHECK (id = 1), floor INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT INTO version_floor VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET floor=MAX(floor, excluded.floor)",
            (floor,),
        )
    finally:
        db.close()


# --- the restore --------------------------------------------------------------------------------------------------------


def preview(
    data: bytes, *, dev_mode: bool, state: Path | None = None, now_ms: Callable[[], int] | None = None
) -> dict[str, Any]:
    """What needs no words: the checks of protocol §17.5 steps 1-6 and the facts to show before asking for them (a fresh
    server compares the fingerprint). ``state`` given and holding a hub = in place (a different hub is ``other_hub``)."""
    live = None
    if state is not None and (Path(state) / "meta.json").exists():
        k = HubKeys.open(Path(state), prune=False, **({} if now_ms is None else {"now_ms": now_ms}))
        live = {"hub_id": k.hub_id, "hk_root_pub": b64u(k.hk_root_pub)}
        dev_mode = k.dev
    _env, o, _sealed = B.read_header(data, dev_mode=dev_mode, live=live)
    return {
        "hub_id": o["hub_id"],
        "fingerprint": fingerprint(o["hub_id"]),
        "made_ms": o["made_ms"],
        "in_place": live is not None,
    }


def restore(
    state: Path,
    data: bytes,
    words: str,
    *,
    dev_mode: bool,
    keys: HubKeys | None = None,
    actor: str | None = None,
    audit: Any = None,
    activity: Any = None,
    now_ms: Callable[[], int] | None = None,
) -> dict[str, Any]:
    """Restore ``data`` (a backup file) into ``state``. In place when ``state`` holds a hub (``keys`` = the running hub's
    key store, else it is opened from disk); otherwise a fresh server (``dev_mode`` is the installed part's flag). Returns
    ``{"made_ms", "gap_ms", "op_seq", "card_seq", "snapshot_seq", "suspended", "channels_revoked", "in_place"}``. Raises
    :class:`RestoreError` (``code``: ``wrong_backup_key``, ``backup_invalid``, ``other_hub``, ``dev_mismatch``, ``dev_refused``,
    ``words``, ``restore_busy``, ``no_admin``, ``audit_failed``, ``restore_failed``). The words are used for key derivation only."""
    now = now_ms or (lambda: int(time.time() * 1000))
    state = Path(state)
    try:
        words = check_words(words)
    except WordsError:
        raise RestoreError("words") from None
    in_place = (state / "meta.json").exists()
    if not in_place:
        state.parent.mkdir(parents=True, exist_ok=True)
    try:
        with hub_op(state, "restore"):
            return _restore(state, data, words, in_place, dev_mode, keys, actor, audit, activity, now)
    except HubOpBusy:
        raise RestoreError("restore_busy") from None


def _restore(
    state: Path,
    data: bytes,
    words: str,
    in_place: bool,
    dev_mode: bool,
    keys: HubKeys | None,
    actor: str | None,
    audit: Any,
    activity: Any,
    now_ms: Callable[[], int],
) -> dict[str, Any]:
    recover(state)
    new, old = paths(state)
    live: HubKeys | None = None
    if in_place:
        live = keys if keys is not None else HubKeys.open(state, prune=False, now_ms=now_ms)
        dev_mode = live.dev
    # (1) every check, nothing written
    try:
        opened = B.open_backup(
            data,
            words=words,
            dev_mode=dev_mode,
            live={"hub_id": live.hub_id, "hk_root_pub": b64u(live.hk_root_pub)} if live is not None else None,
        )
        h = opened.header
        now = now_ms()
        floor = B.restore_floor(
            {"op_seq": live.op_seq, "card_seq": live.card_seq, "snapshot_seq": live_snapshot_seq(state)}
            if live is not None
            else None,
            {k: h[k] for k in B.SEQ_FIELDS},
            now,  # unix MILLISECONDS (see the module text): the contract only takes the max
        )
    except Refused as e:
        raise RestoreError(e.code, e.extra.get("reason")) from None
    swapped = False
    try:
        # (2) the staged copy
        _stage(new, opened.files, state, in_place)
        meta = _meta(new, live)
        _write_meta(new, meta)
        admins, removed = _merge_people(new, state if in_place else None, now)
        G.never_widen(new, state if in_place else None)  # one rule for every table: never widen, never revive
        G.credentials(new, state if in_place else None)
        G.oidc_off(new, in_place)
        recovery = G.recovery_links(new, admins, now_ms) if not in_place else {}
        names = _names(new)
        channels_revoked = _revoke_channels(new, state if in_place else None, removed, actor, now)
        _lift_versions(new, floor["snapshot_seq"])
        for u in removed:
            (new / "uk" / f"{u}.key").unlink(missing_ok=True)
        for sub in ("uk", "ch"):
            gen = new / "op" / str(meta["op_seq"]) / sub
            if not gen.exists():
                make_dir(gen)
        staged = HubKeys.open(new, prune=False, now_ms=now_ms)
        staged.revoke_op()  # the op certificate of the backup: revocations only add
        staged.rotate_for_restore(op_seq=floor["op_seq"], card_seq=floor["card_seq"], only_users=admins)
        for rel in CARRY_FILES:
            if in_place and (state / rel).is_file() and not (state / rel).is_symlink():
                _put(new, rel, read_file(state / rel))
        notice = {"v": 1, "made_ms": h["made_ms"], "restored_ms": now, "suspended": _count_suspended(new)}
        gap = max(0, now - h["made_ms"])
        # (3) the audit line and the history line first (they go into the live logs, which are then copied), then the live hub
        # is fenced, then the swap. The notice is the last file: it marks the staged copy as whole (see recover).
        own_audit = None
        try:
            if not in_place:  # a fresh server has no live chain: the staged one (from the backup) gets the line, signed by the new op key
                from ..audit import open_log

                own_audit = audit = open_log(new, staged, dev_mode=dev_mode, now_ms=now_ms)
            if audit is not None:
                try:
                    audit.hub_restore(
                        backup_made_ms=h["made_ms"], gap_ms=gap, op_seq=floor["op_seq"], actor_user_id=actor
                    )
                except Exception:  # noqa: BLE001  # nothing is restored without its audit line
                    raise RestoreError("audit_failed") from None
        finally:
            if own_audit is not None:
                own_audit.store.close()
        if activity is not None:
            with contextlib.suppress(Exception):  # a failed history line does not stop a restore
                activity.record("hub.backup.restored", actor_kind="person" if actor else "hub", actor_id=actor)
        if in_place:
            _copy_logs(state, new)
        if live is not None:
            live.revoke_op()
            _merge_revoked(state, new)
        _put(new, NOTICE_FILE, json.dumps(notice, sort_keys=True).encode() + b"\n")
        fsync_dir(new)
        if old.exists():
            _rm(old)
        if in_place:
            for sub in CARRY_DIRS:
                if (state / sub).is_dir() and not (state / sub).is_symlink():
                    os.rename(state / sub, new / sub)
            os.rename(state, old)
            try:
                os.rename(new, state)
            except OSError:
                os.rename(old, state)  # put the live state back (and its kept backups)
                if (new / "backups").is_dir():
                    os.rename(new / "backups", state / "backups")
                raise
        else:
            os.rename(new, state)
        swapped = True
        fsync_dir(state.parent)
    except RestoreError:
        if not swapped:
            _rm(new)
        raise
    except BaseException as e:
        if not swapped:
            _rm(new)
        if isinstance(
            e, (OSError, sqlite3.Error, KeysError, StateError, CanonError, ValueError, KeyError, RuntimeError)
        ):
            raise RestoreError("restore_failed") from None
        raise
    return {
        "made_ms": h["made_ms"],
        "gap_ms": gap,
        "in_place": in_place,
        "suspended": notice["suspended"],
        "channels_revoked": channels_revoked,
        "recovery": recovery,
        "recovery_names": {
            u: n for u, n in names.items() if u in recovery
        },  # fresh server only: {admin_id: one-time recovery token}, shown once by the caller
        **floor,
    }


def _names(new: Path) -> dict[str, str]:
    db = _ro(new / "access.db")
    try:
        return dict(db.execute("SELECT user_id, name FROM users"))
    finally:
        db.close()


def _count_suspended(new: Path) -> int:
    db = _ro(new / "access.db")
    try:
        return int(db.execute("SELECT COUNT(*) FROM users WHERE disabled_ms IS NOT NULL").fetchone()[0])
    finally:
        db.close()


def _merge_revoked(state: Path, new: Path) -> None:
    """The live ``revoked/`` (now including the live op certificate) is added to the staged one; a revocation is never dropped."""
    src, dst = state / "revoked", new / "revoked"
    if not src.is_dir() or src.is_symlink():
        return
    for p in sorted(src.iterdir()):
        if p.suffix == ".json" and p.is_file() and not p.is_symlink() and not (dst / p.name).exists():
            _put(new, f"revoked/{p.name}", read_file(p))


def notice(state: Path) -> dict[str, Any] | None:
    """The restore banner: ``{"made_ms", "restored_ms", "suspended"}`` while it has not been dismissed, else None."""
    try:
        d = json.loads(read_file(Path(state) / NOTICE_FILE))
    except StateError, ValueError:
        return None
    ok = type(d) is dict and set(d) == {"v", "made_ms", "restored_ms", "suspended"}
    return {k: d[k] for k in ("made_ms", "restored_ms", "suspended")} if ok else None


def dismiss_notice(state: Path) -> None:
    (Path(state) / NOTICE_FILE).unlink(missing_ok=True)
