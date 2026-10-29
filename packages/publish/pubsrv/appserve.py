"""Serving apps under ``/a/<slug>/`` (M4 #1357 S3; contracts/hub/protocol.md 11.4, 11.7).

Part of the share server: it is mounted by ``serve.ShareServer`` for every path that starts with ``/a/``.

Rules (Opus conditions 4, 5, 6, 9, 12):
- EVERY response under /a/ (files, gate, 404, 410, errors) carries the sandbox CSP built by ``published.csp`` from the
  CONFIGURED share origin (never a Host header), the view path and the live version's approved reach; nosniff,
  ``Referrer-Policy: no-referrer``, ``Cache-Control: no-store``; never ``Set-Cookie`` and never an
  ``Access-Control-Allow-Origin``. ``_Resp`` is the only thing that builds a response, and ``_checked`` refuses a header
  that must not be there.
- A view session lives in the URL path (``/a/<slug>/v/<view id>/``, 30 minutes); no cookie is set or read. Nothing
  under a view base ever redirects.
- A file is served only from the live version of a running app, content type only from the extension allowlist
  (anything else is octet-stream and an attachment), the path is the raw one decoded once and checked by
  ``published.file_path_problem``, and nothing follows a symlink.
- Stop and remove delete the view sessions (the next request is 410 / 404).
- A password app: the password comes by POST to the gate page only (never a query, never logged). 5 tries a minute per
  app and the per-IP bucket come BEFORE the KDF; a KDF slot is taken without waiting; a wrong password is not a view.
- Visits are a daily count per app, counted when a view session is issued. No IP, user agent, referrer or identity.
- Company and group apps need the hub's pass (S5, not built): they answer a plain refusal.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import secrets
import stat
import threading
from collections import deque
from urllib.parse import parse_qs, quote, unquote

from . import db
from . import published as P
from . import serve as S

ZERO_VIEW = "0" * 32  # the gate's path segment: never a real view id
FALLBACK_SLUG = (
    "no-such-app"  # the CSP of a response for a path that names no valid slug is built on this
)
FALLBACK_ORIGIN = (
    "http://apps.invalid"  # used only when no share origin is configured: every /a/ answer is a 404
)
VIEW_MS = S.VIEW_MS
DAY_MS = 86_400_000
VISITS_KEEP_DAYS = 90
GATE_TRIES_PER_MIN = 5
KDF_SLOTS = 2
MAX_POST = 2048
_SLUG = re.compile(P.SLUG.pattern)
_ROUTE = re.compile(r"/a/([^/]*)(/.*)?", re.DOTALL)
_VIEW = re.compile(r"/v/([0-9a-f]{32})/(.*)", re.DOTALL)
_NEVER = ("set-cookie", "access-control-allow-origin", "service-worker-allowed")

NOT_FOUND = ("Not found", "Nothing is published at this address.")
STOPPED = ("Stopped", "This app was stopped by the person who published it.")
VIEW_ENDED = ("Ended", "This viewing session has ended. Open the address again.")
NO_SIGN_IN = ("Sign-in is not available right now", "Try again later.")
WRONG = "That password did not work."


class AppServer:
    def __init__(self, db_path, apps_dir, clock, share_origin, api_host, ip_ok):
        self.db_path, self.apps_dir, self.clock, self.ip_ok = db_path, apps_dir, clock, ip_ok
        self.enabled = share_origin is not None
        self.origin = (share_origin or FALLBACK_ORIGIN).rstrip("/").lower()
        self.forbidden = tuple(x for x in (self.origin, api_host) if x)
        self._tries = {}  # app id -> deque of attempt times (ms); in memory, per process
        self._lock = threading.Lock()
        self._kdf = threading.BoundedSemaphore(KDF_SLOTS)
        self.csp(
            FALLBACK_SLUG, ZERO_VIEW, []
        )  # a share origin that cannot make a base fails at start, not per request

    # -- responses -----------------------------------------------------------------------------------------

    def csp(self, slug, view, reach):
        return P.csp(f"{self.origin}/a/{slug}/v/{view}/", reach, self.forbidden)

    def _checked(self, resp):
        for k in resp.extra:
            if k.lower() in _NEVER:
                raise AssertionError("a /a/ response must not carry " + k)
        return resp

    def page(self, status, title_text, slug=None, view=ZERO_VIEW, reach=(), body=None, extra=None):
        slug = slug if slug is not None and _SLUG.fullmatch(slug) else FALLBACK_SLUG
        try:
            csp = self.csp(slug, view, list(reach))
        except (
            ValueError
        ):  # an approved reach that is no longer allowed (publish's own address): drop it
            csp = self.csp(slug, view, [])
        return self._checked(S.page(status, title_text, body=body, csp=csp, extra=extra))

    # -- routing -------------------------------------------------------------------------------------------

    async def route(self, scope, receive, method, path):
        m = _ROUTE.fullmatch(path)
        slug = m.group(1) if m else None
        rest = (m.group(2) or "") if m else ""
        if not self.enabled or m is None or not _SLUG.fullmatch(slug) or P.slug_problem(slug):
            return self.page(404, NOT_FOUND)
        if method not in ("GET", "HEAD", "POST"):
            return self.page(
                405,
                ("Not allowed", "This request is not allowed."),
                slug,
                extra={"allow": "GET, HEAD"},
            )
        if rest == "":  # /a/<slug> without the slash: not under a view base, so a redirect is fine
            return self._checked(
                S._Resp(
                    303, b"", csp=self.csp(slug, ZERO_VIEW, []), extra={"location": f"/a/{slug}/"}
                )
            )
        if rest == "/":
            if method != "GET":  # a HEAD or a POST must not open a view session
                return self.page(
                    405,
                    ("Not allowed", "This request is not allowed."),
                    slug,
                    extra={"allow": "GET"},
                )
            if not self.ip_ok(scope):
                return self.page(429, S.TOO_MANY, slug, extra={"retry-after": "60"})
            return await asyncio.to_thread(self._landing, slug)
        v = _VIEW.fullmatch(rest)
        if v is None:
            return self.page(404, NOT_FOUND, slug)
        view, tail = v.group(1), v.group(2)
        if view == ZERO_VIEW:  # the gate lives here, under a base that no real view has
            if tail != "gate":
                return self.page(404, NOT_FOUND, slug)
            if method != "POST":
                return self.page(
                    405,
                    ("Not allowed", "This request is not allowed."),
                    slug,
                    extra={"allow": "POST"},
                )
            if not self.ip_ok(scope):
                return self.page(429, S.TOO_MANY, slug, extra={"retry-after": "60"})
            raw = await self._body(receive)
            if raw is None:
                return self.page(413, ("Too large", "That request is too large."), slug)
            return await asyncio.to_thread(self._gate, slug, raw)
        if method == "POST":  # form rows are S7
            return self.page(
                405,
                ("Not allowed", "This request is not allowed."),
                slug,
                view,
                extra={"allow": "GET, HEAD"},
            )
        return await asyncio.to_thread(self._view, slug, view, tail)

    @staticmethod
    async def _body(receive):
        chunks, size = [], 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return None
            size += len(msg.get("body", b""))
            if size > MAX_POST:
                return None
            chunks.append(msg.get("body", b""))
            if not msg.get("more_body"):
                return b"".join(chunks)

    # -- state ---------------------------------------------------------------------------------------------

    def _app(self, conn, slug):
        return conn.execute("SELECT * FROM apps WHERE slug=?", (slug,)).fetchone()

    def _unavailable(self, app, slug):
        """None when the app serves, else the response: unknown, never run and removed are 404; stopped is 410."""
        if app is None or app["state"] in ("staged", "removed"):
            return self.page(404, NOT_FOUND, slug)
        if app["state"] == "stopped":
            return self.page(410, STOPPED, slug)
        return None

    def _reach(self, conn, app):
        row = conn.execute(
            "SELECT meta_json FROM app_versions WHERE app_id=? AND version=?",
            (app["id"], app["live_version"]),
        ).fetchone()
        return json.loads(row["meta_json"])["reach"] if row is not None else []

    def _landing(self, slug):
        conn = db.connect(self.db_path)
        try:
            app = self._app(conn, slug)
            bad = self._unavailable(app, slug)
            if bad is not None:
                return bad
            if app["access"] == "public":
                view = self._issue(conn, app["id"])
                if view is None:
                    return self.page(410, STOPPED, slug)
                return self._checked(
                    S._Resp(
                        303,
                        b"",
                        csp=self.csp(slug, ZERO_VIEW, []),
                        extra={"location": f"/a/{slug}/v/{view}/"},
                    )
                )
            if app["access"] == "password":
                return self._gate_page(slug)
            return self.page(403, NO_SIGN_IN, slug)  # company and group: the hub's pass is S5
        finally:
            conn.close()

    def _gate_page(self, slug, message=None, status=200):
        note = f'<p role="alert">{html.escape(message)}</p>' if message else ""
        body = (
            "<h1>This page needs a password</h1>"
            f'{note}<form method="post" action="/a/{slug}/v/{ZERO_VIEW}/gate">'
            '<p><input type="password" name="p" autocomplete="off" autofocus required maxlength="256"'
            ' aria-label="Password"></p><button type="submit">Open</button></form>'
        )
        return self.page(status, ("Password", ""), slug, body=body)

    def _issue(self, conn, app_id):
        """A view session, or None when the app is not running. Counts one visit (a daily total, nothing else)."""
        now = self.clock()
        view = secrets.token_hex(16)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT state FROM apps WHERE id=?", (app_id,)).fetchone()
            if row is None or row["state"] != "running":
                conn.execute("COMMIT")
                return None
            conn.execute("DELETE FROM app_views WHERE until_ms<?", (now,))
            conn.execute(
                "INSERT INTO app_views(view_id, app_id, until_ms) VALUES (?,?,?)",
                (view, app_id, now + VIEW_MS),
            )
            day = now // DAY_MS
            conn.execute(
                "INSERT INTO app_visits(app_id, day, n) VALUES (?,?,1) ON CONFLICT(app_id, day) DO UPDATE SET n=n+1",
                (app_id, day),
            )
            conn.execute(
                "DELETE FROM app_visits WHERE app_id=? AND day<?", (app_id, day - VISITS_KEEP_DAYS)
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return view

    # -- the password gate ---------------------------------------------------------------------------------

    def _try_ok(self, app_id, now):
        with self._lock:
            q = self._tries.setdefault(app_id, deque())
            while q and q[0] <= now - 60_000:
                q.popleft()
            if len(q) >= GATE_TRIES_PER_MIN:
                return False
            q.append(now)
            if len(self._tries) > 10_000:
                self._tries = {k: v for k, v in self._tries.items() if v and v[-1] > now - 60_000}
            return True

    def _gate(self, slug, raw):
        conn = db.connect(self.db_path)
        try:
            app = self._app(conn, slug)
            bad = self._unavailable(app, slug)
            if bad is not None:
                return bad
            if app["access"] != "password" or not app["pw"]:
                return self.page(404, NOT_FOUND, slug)
            if not self._try_ok(app["id"], self.clock()):  # BEFORE any KDF work
                return self.page(429, S.TOO_MANY, slug, extra={"retry-after": "60"})
            try:
                form = parse_qs(raw.decode("ascii"), strict_parsing=False, max_num_fields=4)
            except (UnicodeError, ValueError):
                return self._gate_page(slug, WRONG, 401)
            given = (form.get("p") or [""])[0]
            if not given or len(given.encode("utf-8", "replace")) > P.PW_INPUT_MAX:
                return self._gate_page(slug, WRONG, 401)
            if not self._kdf.acquire(blocking=False):  # a slot without waiting: the KDF is bounded
                return self.page(
                    503, ("Busy", "Try again in a moment."), slug, extra={"retry-after": "5"}
                )
            try:
                ok = P.password_ok(json.loads(app["pw"]), given)
            finally:
                self._kdf.release()
            if not ok:
                return self._gate_page(slug, WRONG, 401)  # no view, no visit
            view = self._issue(conn, app["id"])
            if view is None:
                return self.page(410, STOPPED, slug)
            url = f"/a/{slug}/v/{view}/"
            body = (
                f'<meta http-equiv="refresh" content="0;url={url}"><h1>Opening</h1>'
                f'<p><a href="{url}">Continue</a></p>'
            )
            return self.page(200, ("Opening", ""), slug, view, self._reach(conn, app), body=body)
        finally:
            conn.close()

    # -- files ---------------------------------------------------------------------------------------------

    def _view(self, slug, view, rest):
        now = self.clock()
        conn = db.connect(self.db_path)
        try:
            app = self._app(conn, slug)
            bad = self._unavailable(app, slug)
            if bad is not None:
                return bad
            v = conn.execute(
                "SELECT * FROM app_views WHERE view_id=? AND app_id=?", (view, app["id"])
            ).fetchone()
            if v is None:
                return self.page(404, NOT_FOUND, slug)
            reach = self._reach(conn, app)
            if v["until_ms"] <= now:
                return self.page(410, VIEW_ENDED, slug, view, reach)
            live = app["live_version"]
        finally:
            conn.close()
        return self._file(slug, view, reach, app["id"], live, rest)

    @staticmethod
    def _clean(rest):
        """The relative path inside files/ or None: the raw path decoded ONCE, then checked like an upload path."""
        rest = rest.split("?", 1)[0]
        try:
            rel = unquote(rest, errors="strict")
        except UnicodeError:
            return None
        if rel == "":
            return "index.html"
        return None if P.file_path_problem(rel) else rel

    def _file(self, slug, view, reach, app_id, live, rest):
        rel = self._clean(rest)
        if rel is None:
            return self.page(404, NOT_FOUND, slug, view, reach)
        opened = open_file(os.path.join(self.apps_dir, app_id, str(live), "files"), rel)
        if opened is None:
            return self.page(404, NOT_FOUND, slug, view, reach)
        fd, size = opened
        if size > S.MAX_FILE:
            os.close(fd)
            return self.page(404, NOT_FOUND, slug, view, reach)
        ext = os.path.splitext(rel)[1].lower()
        extra = {}
        if ext in S.INLINE_TYPES:
            ctype = S.INLINE_TYPES[ext]
        else:
            ctype = S.ATTACHMENT_TYPES.get(ext, "application/octet-stream")
            name = os.path.basename(rel)
            extra["content-disposition"] = (
                f"attachment; filename=\"{S.safe_name(name)}\"; filename*=UTF-8''{quote(name)}"
            )
        return self._checked(
            S._Resp(200, b"", ctype, csp=self.csp(slug, view, reach), extra=extra, fd=fd, size=size)
        )


def open_file(root, rel):
    """(fd, size) of the regular file ``rel`` under ``root``, opened through directory fds with O_NOFOLLOW (a symlink
    anywhere on the way is a miss), or None."""
    fds = []
    try:
        cur = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(cur)
        parts = rel.split("/")
        for part in parts[:-1]:
            cur = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cur)
            fds.append(cur)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=cur)
    except OSError:
        return None
    finally:
        for f in fds:
            os.close(f)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        return None
    return fd, st.st_size
