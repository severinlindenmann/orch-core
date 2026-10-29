"""App storage and release (M4 #1357 S3; contracts/hub/protocol.md 11.4): versions, commit, rollback, start, stop,
remove. An app is static files; a version is a meta plus its bytes, and it serves nothing until a commit, a rollback
or a start carries the owner's approval (verified in the request middleware by `published.check_app_approval`, on the
hub path and on the direct v2 12.1 path alike).

Rules this module keeps:
- Routes are classified ONLY through `published.app_route` over the raw request target (`route_parts`); the file path
  of a file PUT is the raw one, checked by `published.file_path_problem`.
- A committed version is frozen: any later meta or file PUT for it is refused (409 committed), so the bytes the
  owner approved are the bytes that serve.
- A commit, rollback or start checks the body with `published.check_release` against the STORED meta of
  (route app id, body version), and against the sha256 of every stored file recomputed from disk.
- The workspace is the signed one (`orch_ws`); another workspace's app looks like a missing one.
- A slug is claimed by the first meta PUT and is never freed, a removed app keeps its row.
"""

from __future__ import annotations

import hashlib
import json
import re

from . import db
from . import hub_path as H
from . import published as P
from .auth import Refusal
from .shares import MAX_WORKSPACE, _no_dup_pairs, _txn

MAX_APPS = 50  # per workspace (not removed)
MAX_TOMBSTONES = (
    500  # removed apps keep their slug for ever; a workspace cannot squat more than this many
)
MAX_VERSION_BYTES = 50 * 1024 * 1024
MAX_STAGED = 3  # open (uncommitted) versions per app
KEEP_OLD = 4  # committed versions kept next to the live one (rollback)
MAX_META_BODY = 512 * 1024
MAX_APP_FILE_BODY = (
    P.FILE_SIZE_MAX + 1024 * 1024
)  # a file PUT's body limit (the meta pins the exact size)


def route_parts(method, target):
    """``(route, args)`` for an app route, classified over the raw target by ``published.app_route``, else None.
    ``args`` are the groups after workspace and id: ``(version,)`` for a meta PUT, ``(version, path)`` for a file."""
    route = P.app_route(method, target)
    if route is None or not route["op"].startswith("app."):
        return None  # share.extend / share.password are the share routes (S2), never handled or admitted here
    for m, rx, op, _ in P.APP_ROUTES:
        if m == method and op == route["op"]:
            hit = re.fullmatch(rx, target)
            if hit is not None:
                return route, hit.groups()[2:]
    return None


def refusal(code, status=None, **extra):
    return Refusal(status or H.STATUS.get(code, 400), code, **extra)


def owned_app(conn, ws, app_id):
    row = conn.execute("SELECT * FROM apps WHERE id=? AND owner_ws=?", (app_id, ws)).fetchone()
    if row is None:
        raise Refusal(404, "not_found")
    return row


def _gone(row):
    raise Refusal(410, "gone", state=row["state"])


def parse_meta(raw, forbidden):
    """Strict JSON (no duplicate keys), then ``published.meta_problem``; the size sum is bounded."""
    try:
        meta = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dup_pairs)
    except (UnicodeError, ValueError, RecursionError):
        raise Refusal(400, "malformed") from None
    bad = P.meta_problem(meta, forbidden)
    if bad is not None:
        raise refusal(bad)
    total = sum(f["size"] for f in meta["files"])
    if total > MAX_VERSION_BYTES:
        raise Refusal(413, "share_too_large")
    return meta, total


def _version(conn, app_id, version):
    row = conn.execute(
        "SELECT * FROM app_versions WHERE app_id=? AND version=?", (app_id, version)
    ).fetchone()
    if row is None:
        raise Refusal(404, "not_found")
    return row


def _sid(app_id, version):
    return f"{app_id}/{version}"


class AppOps:
    """The write side. ``forbidden`` are the origins/hosts of orch-publish itself (a reach entry may not name them)."""

    def __init__(self, settings, store):
        self.s, self.store = settings, store

    @property
    def forbidden(self):
        return tuple(x for x in (self.s.api_host, self.s.share_origin) if x)

    def handle(self, op, ws, wsk, app_id, args, body, info):
        """One verified request. ``body`` is the raw body, ``info`` the approval result for the three release ops."""
        if op == "app.meta":
            return self.put_meta(ws, wsk, app_id, int(args[0]), body)
        if op == "app.file":
            return self.put_file(ws, wsk, app_id, int(args[0]), args[1], body)
        if op in ("app.commit", "app.rollback", "app.start"):
            return self.release(ws, wsk, app_id, info)
        if op == "app.stop":
            return self.stop(ws, wsk, app_id)
        if op == "app.remove":
            return self.remove(ws, wsk, app_id)
        raise Refusal(404, "not_found")

    # -- meta ----------------------------------------------------------------------------------------------

    def put_meta(self, ws, wsk, app_id, version, raw):
        if len(raw) > MAX_META_BODY:
            raise Refusal(413, "too_large")
        meta, total = parse_meta(raw, self.forbidden)
        mh = P.meta_hash(meta)
        now = self.s.clock()
        conn = db.connect(self.s.db_path)
        try:
            _txn(conn, ws, wsk)
            try:
                app = conn.execute("SELECT * FROM apps WHERE id=?", (app_id,)).fetchone()
                if app is None:
                    if self.s.share_origin is None:
                        # without the configured share origin the reach rule cannot exclude it (and nothing serves)
                        raise Refusal(503, "apps_not_configured")
                    if conn.execute("SELECT 1 FROM apps WHERE slug=?", (meta["slug"],)).fetchone():
                        raise Refusal(
                            409, "slug_taken"
                        )  # also for a removed app: a slug is never freed
                    n = conn.execute(
                        "SELECT COUNT(*) FROM apps WHERE owner_ws=? AND state!='removed'", (ws,)
                    ).fetchone()[0]
                    if n >= MAX_APPS:
                        raise Refusal(429, "too_many_apps")
                    gone_n = conn.execute(
                        "SELECT COUNT(*) FROM apps WHERE owner_ws=? AND state='removed'", (ws,)
                    ).fetchone()[0]
                    if gone_n >= MAX_TOMBSTONES:
                        raise Refusal(429, "too_many_removed")
                    conn.execute(
                        "INSERT INTO apps(id, owner_ws, slug, name, kind, access, audience, pw, state, live_version,"
                        " created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,NULL,'staged',NULL,?,?)",
                        (app_id, ws, meta["slug"], meta["name"], meta["kind"], meta["access"],
                         meta["audience"], now, now),
                    )  # fmt: skip
                    created = True
                else:
                    if app["owner_ws"] != ws:
                        raise Refusal(
                            409, "conflict"
                        )  # same answer for live or tombstone, nothing about the owner
                    if app["state"] == "removed":
                        _gone(app)
                    if app["slug"] != meta["slug"]:
                        raise Refusal(409, "slug_changed")
                    created = False
                v = conn.execute(
                    "SELECT * FROM app_versions WHERE app_id=? AND version=?", (app_id, version)
                ).fetchone()
                if v is not None:
                    if v["meta_hash"] != mh:
                        raise Refusal(
                            409, "conflict"
                        )  # a stored meta is never replaced: use a new version number
                    conn.execute("COMMIT")
                    return {
                        "id": app_id,
                        "version": version,
                        "state": v["state"],
                        "result": "unchanged",
                    }
                if (
                    conn.execute(
                        "SELECT COUNT(*) FROM app_versions WHERE app_id=? AND state='staged'",
                        (app_id,),
                    ).fetchone()[0]
                    >= MAX_STAGED
                ):
                    raise Refusal(429, "too_many_versions")
                used = conn.execute(
                    "SELECT COALESCE(SUM(v.bytes),0) FROM app_versions v JOIN apps a ON a.id=v.app_id"
                    " WHERE a.owner_ws=?",
                    (ws,),
                ).fetchone()[0]
                if used + total > MAX_WORKSPACE:
                    raise Refusal(413, "quota_exceeded")
                conn.execute(
                    "INSERT INTO app_versions(app_id, version, meta_json, meta_hash, bytes, state, created_ms)"
                    " VALUES (?,?,?,?,?,'staged',?)",
                    (app_id, version, json.dumps(meta, sort_keys=True, separators=(",", ":")), mh, total, now),
                )  # fmt: skip
                conn.executemany(
                    "INSERT INTO app_files(app_id, version, path, size, sha256) VALUES (?,?,?,?,?)",
                    [(app_id, version, f["path"], f["size"], f["sha256"]) for f in meta["files"]],
                )
                self.store.make_incoming(_sid(app_id, version), "apps")
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            return {
                "id": app_id,
                "version": version,
                "state": "staged",
                "result": "created" if created else "version_added",
            }
        finally:
            conn.close()

    # -- files ---------------------------------------------------------------------------------------------

    def put_file(self, ws, wsk, app_id, version, path, body):
        if P.file_path_problem(path):
            raise Refusal(400, "bad_path")
        conn = db.connect(self.s.db_path)
        try:
            app = owned_app(conn, ws, app_id)
            if app["state"] == "removed":
                _gone(app)
            v = _version(conn, app_id, version)
            if v["state"] != "staged":
                raise Refusal(
                    409, "committed"
                )  # frozen: the approved bytes are the bytes that serve
            f = conn.execute(
                "SELECT * FROM app_files WHERE app_id=? AND version=? AND path=?",
                (app_id, version, path),
            ).fetchone()
            if f is None:
                raise Refusal(404, "not_in_manifest")
            if len(body) != f["size"] or hashlib.sha256(body).hexdigest() != f["sha256"]:
                raise Refusal(422, "content_mismatch")
            _txn(conn, ws, wsk)
            try:
                app = owned_app(conn, ws, app_id)
                if app["state"] == "removed":
                    _gone(app)
                if _version(conn, app_id, version)["state"] != "staged":
                    raise Refusal(409, "committed")
                self.store.write_file(_sid(app_id, version), path, body, "apps")
                conn.execute(
                    "UPDATE app_files SET present=1 WHERE app_id=? AND version=? AND path=?",
                    (app_id, version, path),
                )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            return {"path": path, "result": "stored"}
        finally:
            conn.close()

    # -- commit, rollback, start ---------------------------------------------------------------------------

    def release(self, ws, wsk, app_id, info):
        """``info`` is the approval result of the middleware; ``info["body"]`` is the one parsed body, and
        ``check_release`` below runs on exactly it (never a second parse)."""
        body = info["body"]
        op, version = body["op"], body["version"]
        if body["app_id"] != app_id or info["app_id"] != app_id:
            raise Refusal(400, "malformed")
        conn = db.connect(self.s.db_path)
        try:
            app = owned_app(conn, ws, app_id)
            if app["state"] == "removed":
                _gone(app)
            v = _version(conn, app_id, version)  # looked up by (route app id, body version)
            meta = json.loads(v["meta_json"])
            self._check_state(conn, op, app, v)
            stored = self.store.stat_files("apps", _sid(app_id, version))
            bad = P.check_release(body, meta, stored, self.forbidden)
            if bad is not None:
                raise refusal(bad)
            if meta["slug"] != app["slug"]:
                raise Refusal(409, "slug_changed")
            now = self.s.clock()
            _txn(conn, ws, wsk)
            try:
                app = owned_app(conn, ws, app_id)
                if app["state"] == "removed":
                    _gone(app)
                v = _version(conn, app_id, version)
                if self._check_state(conn, op, app, v) == "unchanged":
                    conn.execute("COMMIT")
                    return {
                        "id": app_id,
                        "version": version,
                        "state": "running",
                        "result": "unchanged",
                    }
                widened_session = (app["access"], app["audience"], app["pw"]) != (
                    meta["access"],
                    meta["audience"],
                    None if meta["pw"] is None else json.dumps(meta["pw"], sort_keys=True),
                )
                conn.execute(
                    "UPDATE app_versions SET state='committed', approved_by_person=?, approved_ms=?,"
                    " committed_ms=COALESCE(committed_ms, ?) WHERE app_id=? AND version=?",
                    (info["approved_by_person"], now, now, app_id, version),
                )
                conn.execute(
                    "UPDATE apps SET name=?, kind=?, access=?, audience=?, pw=?, state='running', live_version=?,"
                    " updated_ms=? WHERE id=?",
                    (meta["name"], meta["kind"], meta["access"], meta["audience"],
                     None if meta["pw"] is None else json.dumps(meta["pw"], sort_keys=True),
                     version, now, app_id),
                )  # fmt: skip
                if widened_session:
                    # sessions granted under the old access do not carry over to a different gate
                    conn.execute("DELETE FROM app_views WHERE app_id=?", (app_id,))
                dropped = self._prune(conn, app_id, version)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            for n in dropped:
                self.store.remove_tree("apps", _sid(app_id, n))
            return {"id": app_id, "version": version, "state": "running", "result": op}
        finally:
            conn.close()

    @staticmethod
    def _check_state(conn, op, app, v):
        """The state rules of the three ops. Returns "unchanged" for a repeat of a commit that is live."""
        live = app["live_version"]
        if op == "commit":
            if v["state"] != "staged":
                if app["state"] == "running" and live == v["version"]:
                    return "unchanged"
                raise Refusal(409, "committed")
            newest = conn.execute(
                "SELECT MAX(version) FROM app_versions WHERE app_id=? AND state='committed'",
                (v["app_id"],),
            ).fetchone()[0]
            if newest is not None and v["version"] < newest:
                raise Refusal(409, "version_old")
        elif op == "rollback":
            if app["state"] != "running":
                raise Refusal(409, "not_running")
            if v["state"] != "committed" or live is None or v["version"] >= live:
                raise Refusal(409, "not_older")
        elif op == "start":
            if app["state"] != "stopped":
                raise Refusal(409, "not_stopped")
            if v["state"] != "committed":
                raise Refusal(409, "not_committed")
        return None

    @staticmethod
    def _prune(conn, app_id, live):
        """Keep the live version and KEEP_OLD older committed ones; returns the version numbers whose rows went."""
        old = [
            r["version"]
            for r in conn.execute(
                "SELECT version FROM app_versions WHERE app_id=? AND state='committed' AND version!=?"
                " ORDER BY version DESC",
                (app_id, live),
            )
        ][KEEP_OLD:]
        for n in old:
            conn.execute("DELETE FROM app_files WHERE app_id=? AND version=?", (app_id, n))
            conn.execute("DELETE FROM app_versions WHERE app_id=? AND version=?", (app_id, n))
        return old

    # -- stop, remove (narrowing: WSK only, no approval) ---------------------------------------------------

    def stop(self, ws, wsk, app_id):
        conn = db.connect(self.s.db_path)
        try:
            _txn(conn, ws, wsk)
            try:
                app = owned_app(conn, ws, app_id)
                if app["state"] == "removed":
                    _gone(app)
                result = stop_row(conn, app_id, self.s.clock())
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            return {
                "id": app_id,
                "state": "stopped" if result != "staged" else "staged",
                "result": result,
            }
        finally:
            conn.close()

    def remove(self, ws, wsk, app_id):
        conn = db.connect(self.s.db_path)
        try:
            _txn(conn, ws, wsk)
            try:
                app = owned_app(conn, ws, app_id)
                again = app["state"] == "removed"
                if not again:
                    # the row stays as a tombstone (its slug is never freed); files, versions and view sessions go.
                    # Form rows (S7) are deleted here too.
                    conn.execute("DELETE FROM app_views WHERE app_id=?", (app_id,))
                    conn.execute("DELETE FROM app_visits WHERE app_id=?", (app_id,))
                    conn.execute("DELETE FROM app_files WHERE app_id=?", (app_id,))
                    conn.execute("DELETE FROM app_versions WHERE app_id=?", (app_id,))
                    conn.execute(
                        "UPDATE apps SET state='removed', live_version=NULL, pw=NULL, updated_ms=? WHERE id=?",
                        (self.s.clock(), app_id),
                    )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            self.store.remove_tree("apps", app_id)
            return {"id": app_id, "state": "removed", "result": "unchanged" if again else "removed"}
        finally:
            conn.close()


def stop_row(conn, app_id, now_ms):
    """Inside a write transaction: a running app becomes stopped and every view session is deleted (the next request
    is 410). Also what the hub's narrowing (S2) calls. Returns "stopped", "unchanged" or "staged" (never run)."""
    app = conn.execute("SELECT state FROM apps WHERE id=?", (app_id,)).fetchone()
    if app is None or app["state"] == "staged":
        return "staged"
    if app["state"] == "stopped":
        return "unchanged"
    conn.execute("UPDATE apps SET state='stopped', updated_ms=? WHERE id=?", (now_ms, app_id))
    conn.execute("DELETE FROM app_views WHERE app_id=?", (app_id,))
    return "stopped"


__all__ = ["AppOps", "route_parts", "stop_row"]
