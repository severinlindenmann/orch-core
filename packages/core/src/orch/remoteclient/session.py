"""An open workspace session on the person's computer (R5, #1593; design sections 1, 3.2, 3.3, 5).

:func:`open_session` runs the ``open`` message (the presence key signs the open bytes), checks ``opened`` and its API
version, and returns a :class:`Session`. The session then carries browser requests to the workspace as app frames:

* :meth:`Session.request` / :meth:`Session.websocket` (the loopback server's whole interface to a stream), thread safe;
  header allow-list, ids that only rise, at most 8 HTTP and 2 WebSocket exchanges at once (the rest wait here instead of
  drawing a ``remote.busy``), bodies up to 1 MiB;
* **presence for actions** (D88): a ``428 remote.presence_required`` is held back from the page. The client recomputes
  what it is asked to confirm -- the route digest from what it sent, or the review digest of a click-to-sign view it
  saw -- and prompts only when its own value equals the workspace's. Up to 16 actions within one second share one
  prompt.
  Then in-stream nonce -> sign -> presence, and the held request is replayed byte-identical. The page only sees the
  final answer; a cancelled or unverifiable one is a ``403`` "Not confirmed".

Threads: one reader thread per session decodes frames into per-exchange queues; callers block on their own queue.
Nothing here logs a frame, a key or a signature."""

from __future__ import annotations

import queue
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from orch.canon.jcs import JcsError, dumps, loads_strict
from orch.crypto.encoding import EncodingError, b64u, unb64u

from . import frames as F
from . import presence as P
from .channel import Channel
from .codes import ClientError

__all__ = ["REMOTE_API", "Response", "Session", "WebSocket", "WsRefused", "open_session"]

REMOTE_API = (1, 1)
BODY_MAX = 1 << 20
MAX_HTTP, MAX_WS = 8, 2
QUEUED_MAX = 8 << 20  # bytes a page may leave unread on one exchange before the exchange is cancelled
BATCH_WINDOW_S = 1.0
VIEWS_MAX, VIEW_TTL_S = 64, 300
_SHA = re.compile(r"sha256:[0-9a-f]{64}")
_HEX32 = re.compile(r"[0-9a-f]{32}")


class WsRefused(Exception):
    """The workspace answered a WebSocket open with an HTTP response instead of ``101``."""

    def __init__(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        super().__init__(status)
        self.status, self.headers, self.body = status, headers, body


# --- exchanges ---


class _Ex:
    def __init__(self, ident: int, ws: bool) -> None:
        self.id, self.ws = ident, ws
        self.q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.mode = "wait"  # wait (no head yet) -> body | live | done
        self.queued = 0
        self.shut = {"d2h": False, "h2d": False}
        self.lock = threading.Lock()


@dataclass
class Response:
    """An HTTP answer. ``status`` and ``headers`` are known; :meth:`chunks` / :meth:`read` give the body (blocking)."""

    status: int
    headers: list[tuple[str, str]]
    _next: Callable[[float], bytes | None] = field(repr=False)
    _cancel: Callable[[], None] = field(repr=False)

    def chunks(self, timeout: float = 120.0) -> Iterator[bytes]:
        while True:
            c = self._next(timeout)
            if c is None:
                return
            yield c

    def read(self, limit: int = BODY_MAX, timeout: float = 120.0) -> bytes:
        out = bytearray()
        for c in self.chunks(timeout):
            out += c
            if len(out) > limit:
                self.cancel()
                raise ClientError("too_large")
        return bytes(out)

    def cancel(self) -> None:
        self._cancel()


class WebSocket:
    def __init__(self, session: Session, ex: _Ex) -> None:
        self._s, self._ex = session, ex

    def send_text(self, text: str) -> None:
        raw = text.encode("utf-8")
        if len(raw) > F.BODY_CHUNK:
            raise ClientError("too_large")  # the loopback server closes the browser's socket 1009
        self._s._ws_send(self._ex, "WS_TEXT", raw)  # noqa: SLF001

    def send_binary(self, data: bytes) -> None:
        if len(data) > F.BODY_CHUNK:
            raise ClientError("too_large")
        self._s._ws_send(self._ex, "WS_BINARY", data)  # noqa: SLF001

    def close(self, code: int = 1000) -> None:
        self._s._ws_close(self._ex, code)  # noqa: SLF001

    def recv(self, timeout: float = 60.0) -> tuple[str, Any] | None:
        """``("text", str)``, ``("binary", bytes)``, ``("close", code)`` (the workspace closed, or the session ended
        with
        1011) or ``None`` after ``timeout``."""
        try:
            kind, val = self._ex.q.get(timeout=timeout)
        except queue.Empty:
            return None
        if kind in ("text", "binary"):
            self._s._consumed(self._ex, len(val if kind == "binary" else val.encode()))  # noqa: SLF001
            return kind, val
        if kind == "close":
            return "close", val
        return "close", 1011


# --- the session ----


def _short(ws: str) -> str:
    return ws[-8:]


class Session:
    def __init__(
        self,
        ch: Channel,
        *,
        workspace_id: str,
        device_id: str,
        api: int,
        assert_: Callable[[bytes, P.Prompt], dict[str, Any]],
        name: str | None = None,
        notify: Callable[[P.Prompt | None], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        batch_window: float = BATCH_WINDOW_S,
    ) -> None:
        self.ch, self.workspace_id, self.device_id, self.api = ch, workspace_id, device_id, api
        self.name = name  # the workspace's OWN name (read end to end), never the hub's
        self._assert, self._notify, self._clock = assert_, notify or (lambda p: None), clock
        self._window = batch_window
        self.closed: str | None = None
        self._send_mu = threading.Lock()
        self._mu = threading.Lock()
        self._open_mu = threading.Lock()
        self._next_id = 0
        self._ex: dict[int, _Ex] = {}
        self._http_slots = threading.Semaphore(MAX_HTTP)
        self._ws_slots = threading.Semaphore(MAX_WS)
        self._views: dict[str, tuple[float, str]] = {}
        self._wait: list[tuple[bytes, str, threading.Event, list[str]]] = []  # digest, line, done, result
        self._wait_cv = threading.Condition()
        self._stop = False
        self._reader = threading.Thread(target=self._read_loop, name="remote-client-reader", daemon=True)
        self._presenter = threading.Thread(target=self._presence_loop, name="remote-client-presence", daemon=True)

    def start(self) -> Session:
        self._reader.start()
        self._presenter.start()
        return self

    # -- labels (never from the hub) --

    def label(self) -> str:
        return self.name or f"workspace {_short(self.workspace_id)}"

    # -- receiving --

    def _read_loop(self) -> None:
        reason = "closed"
        try:
            while not self._stop:
                pt = self.ch.recv(0.25)
                if pt is None:
                    continue
                self._on_frame(pt)
        except ClientError as exc:
            reason = exc.code
        finally:
            self._finish(reason)

    def _finish(self, reason: str) -> None:
        with self._mu:
            if self.closed is not None:
                return
            self.closed = reason
            exs, self._ex = list(self._ex.values()), {}
        self._stop = True
        for ex in exs:
            ex.q.put(("closed", reason))
        with self._wait_cv:
            self._wait_cv.notify_all()
        self.ch.close("closed" if reason in ("closed", "idle", "expired", "revoked") else "malformed")

    def _fatal(self, reason: str = "malformed") -> None:
        self.ch.hub.send_close(reason)
        raise ClientError(reason)

    def _on_frame(self, pt: bytes) -> None:
        try:
            kind, ident, p = F.split_kind(pt)
        except ClientError:
            self._fatal()
        if kind == "WS_OPEN":
            self._fatal()  # only the device opens a WebSocket
        if (ident == 0) != (kind in ("PING", "PONG")):
            self._fatal()
        if kind == "PING":
            if p:
                self._fatal()
            self._send_raw(F.frame("PONG", 0))
            return
        if kind == "PONG":
            return
        with self._mu:
            ex = self._ex.get(ident)
            high = self._next_id
        if ex is None:
            if ident > high:
                self._fatal()  # an exchange this device never opened
            return  # it ended; a late frame is dropped
        if kind == "RESET":
            if p.decode("latin-1") not in F.RESETS:
                self._fatal()
            self._drop(ex)
            ex.q.put(("reset", p.decode()))
            return
        if kind == "HEAD":
            if ex.mode != "wait":
                self._fatal()
            try:
                status, headers = F.parse_response_head(p)
            except ClientError:
                self._fatal()
            if status == 101:
                if not ex.ws:
                    self._fatal()
                ex.mode = "live"
            else:
                ex.mode = "body"
            ex.q.put(("head", (status, headers)))
            return
        if kind == "BODY":
            if ex.mode != "body" or not p:
                self._fatal()
            self._enqueue(ex, ("body", p), len(p))
            return
        if kind == "END":
            if ex.mode != "body" or p:
                self._fatal()
            self._drop(ex)
            ex.q.put(("end", None))
            return
        # WS_TEXT, WS_BINARY, WS_CLOSE
        if not ex.ws or ex.mode != "live" or ex.shut["h2d"]:
            self._fatal()
        if kind == "WS_CLOSE":
            if len(p) != 2:
                self._fatal()
            code = int.from_bytes(p, "big")
            if not (1000 <= code <= 1003 or 1007 <= code <= 1011 or 3000 <= code <= 4999):
                self._fatal()
            ex.shut["h2d"] = True
            if ex.shut["d2h"]:
                self._drop(ex)
            ex.q.put(("close", code))
        elif kind == "WS_TEXT":
            try:
                text = p.decode("utf-8")
            except UnicodeDecodeError:
                self._fatal()
            self._enqueue(ex, ("text", text), len(p))
        elif kind == "WS_BINARY":
            self._enqueue(ex, ("binary", p), len(p))
        else:
            self._fatal()

    def _enqueue(self, ex: _Ex, item: tuple[str, Any], n: int) -> None:
        with ex.lock:
            ex.queued += n
            over = ex.queued > QUEUED_MAX
        if over:  # a page that does not read: cancel that one exchange instead of stalling the others
            self._drop(ex)
            self._send_raw(F.frame("RESET", ex.id, b"cancelled"))
            ex.q.put(("reset", "cancelled"))
            return
        ex.q.put(item)

    def _consumed(self, ex: _Ex, n: int) -> None:
        with ex.lock:
            ex.queued = max(0, ex.queued - n)

    def _drop(self, ex: _Ex) -> None:
        with self._mu:
            if self._ex.pop(ex.id, None) is not None:
                (self._ws_slots if ex.ws else self._http_slots).release()

    # -- sending --

    def _send_raw(self, pt: bytes) -> None:
        with self._send_mu:
            self.ch.send(pt)

    def _alive(self) -> None:
        if self.closed is not None:
            raise ClientError(self.closed if self.closed != "closed" else "remote_client.closed")

    def _open_ex(self, ws: bool, kind: str, head: bytes) -> _Ex:
        """A slot (queued here, not a ``remote.busy``), the next id and the opening frame -- id and frame under one
        lock, because the host requires ids that only rise **in the order they arrive**."""
        slots = self._ws_slots if ws else self._http_slots
        while not slots.acquire(timeout=0.25):
            self._alive()
        registered = False
        try:
            with self._open_mu:
                with self._mu:
                    self._alive()
                    self._next_id += 1
                    ex = _Ex(self._next_id, ws)
                    self._ex[ex.id] = ex
                    registered = True
                try:
                    self._send_raw(F.frame(kind, ex.id, head))
                except BaseException:
                    self._drop(ex)  # releases the slot
                    raise
                return ex
        except BaseException:
            if not registered:
                slots.release()
            raise

    def _chunks(self, body: bytes) -> Iterator[bytes]:
        for i in range(0, len(body), F.BODY_CHUNK):
            yield body[i : i + F.BODY_CHUNK]

    def _await_head(self, ex: _Ex, timeout: float) -> tuple[int, list[tuple[str, str]]]:
        while True:
            try:
                kind, val = ex.q.get(timeout=timeout)
            except queue.Empty:
                self._cancel(ex)
                raise ClientError("remote_client.timeout") from None
            if kind == "head":
                return val
            if kind == "reset":
                raise ClientError("remote.busy" if val == "remote.busy" else "remote_client.closed")
            if kind == "closed":
                raise ClientError(val if val != "closed" else "remote_client.closed")
            # a body / end before a head cannot happen (the reader checked the order)

    def _cancel(self, ex: _Ex) -> None:
        with self._mu:
            live = ex.id in self._ex
        if live:
            self._drop(ex)
            try:
                self._send_raw(F.frame("RESET", ex.id, b"cancelled"))
            except ClientError:
                pass

    def _response(self, ex: _Ex, status: int, headers: list[tuple[str, str]]) -> Response:
        done = [False]

        def nxt(timeout: float) -> bytes | None:
            if done[0]:
                return None
            try:
                kind, val = ex.q.get(timeout=timeout)
            except queue.Empty:
                self._cancel(ex)
                done[0] = True
                raise ClientError("remote_client.timeout") from None
            if kind == "body":
                self._consumed(ex, len(val))
                return val  # type: ignore[no-any-return]
            done[0] = True
            if kind == "end":
                return None
            if kind == "reset":
                raise ClientError("remote.busy" if val == "remote.busy" else "remote_client.closed")
            raise ClientError(val if val != "closed" else "remote_client.closed")

        return Response(status, F.clean_response_headers(headers), nxt, lambda: self._cancel(ex))

    @staticmethod
    def _body(body: bytes | Iterable[bytes]) -> bytes:
        if isinstance(body, (bytes, bytearray)):
            b = bytes(body)
        else:
            b = b"".join(body)
        if len(b) > BODY_MAX:
            raise ClientError("too_large")
        return b

    def _send_http(
        self, method: str, path: str, query: str, headers: Iterable[tuple[str, str]], body: bytes
    ) -> tuple[_Ex, int, list[tuple[str, str]]]:
        head = F.build_request_head(method, path, query, headers)
        ex = self._open_ex(False, "HEAD", head)
        try:
            for c in self._chunks(body):
                self._send_raw(F.frame("BODY", ex.id, c))
            self._send_raw(F.frame("END", ex.id))
            status, hs = self._await_head(ex, 60.0)
        except BaseException:
            self._drop(ex)
            raise
        return ex, status, hs

    # -- the interface of the loopback server --

    def request(
        self,
        method: str,
        path: str,
        query: str = "",
        headers: Iterable[tuple[str, str]] = (),
        body: bytes | Iterable[bytes] = b"",
    ) -> Response:
        """One HTTP exchange; blocks until the response head. A presence request (428) is handled here: the page gets
        the final answer of the replay, or a 403 "Not confirmed"."""
        data = self._body(body)
        hs = list(headers)
        for attempt in range(3):
            ex, status, rh = self._send_http(method, path, query, hs, data)
            resp = self._response(ex, status, rh)
            self._maybe_record_view(method, path, resp)
            if status != 428 or attempt == 2:
                return resp
            try:
                raw = resp.read(64 * 1024)
                ok = self._confirm_request(method, path, query, data, raw)
            except ClientError:
                ok = False
            if not ok:
                return self._not_confirmed()
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _not_confirmed() -> Response:
        body = dumps(
            {
                "ok": False,
                "error": {"code": "remote_client.not_confirmed", "message": "Not confirmed.", "retryable": False},
            }
        )
        chunks = [body]

        def nxt(timeout: float) -> bytes | None:
            return chunks.pop(0) if chunks else None

        return Response(403, [("content-type", "application/json")], nxt, lambda: None)

    def websocket(self, path: str, query: str = "", headers: Iterable[tuple[str, str]] = ()) -> WebSocket:
        """Open a WebSocket in the stream. Held until the workspace answers ``101``; a presence request is satisfied and
        the open repeated. Anything else is :class:`WsRefused`."""
        hs = list(headers)
        for attempt in range(3):
            head = F.build_request_head(None, path, query, hs)
            ex = self._open_ex(True, "WS_OPEN", head)
            try:
                status, rh = self._await_head(ex, 30.0)
            except BaseException:
                self._drop(ex)
                raise
            if status == 101:
                return WebSocket(self, ex)
            resp = self._response(ex, status, rh)
            try:
                raw = resp.read(64 * 1024)
            except ClientError:
                raw = b""
            if status == 428 and attempt < 2:
                try:
                    if self._confirm_request("WEBSOCKET", path, query, b"", raw):
                        continue
                except ClientError:
                    pass
                raise WsRefused(403, [], self._not_confirmed().read())
            raise WsRefused(status, resp.headers, raw)
        raise AssertionError("unreachable")  # pragma: no cover

    def _ws_send(self, ex: _Ex, kind: str, payload: bytes) -> None:
        self._alive()
        if ex.shut["d2h"]:
            raise ClientError("remote_client.closed")
        self._send_raw(F.frame(kind, ex.id, payload))

    def _ws_close(self, ex: _Ex, code: int) -> None:
        if ex.shut["d2h"]:
            return
        ex.shut["d2h"] = True
        try:
            self._send_raw(F.frame("WS_CLOSE", ex.id, code.to_bytes(2, "big")))
        except ClientError:
            pass
        if ex.shut["h2d"]:
            self._drop(ex)

    def close(self) -> None:
        """End the session (the person's "End")."""
        self._stop = True
        self._finish("closed")

    # -- presence for actions --

    def _maybe_record_view(self, method: str, path: str, resp: Response) -> None:
        """Remember the review views that pass through (``click/prepare``, ``click/view``): the digest of a
        click-to-sign
        view is recomputed HERE, from the view, never taken from the workspace's word."""
        if method != "POST" or not path.endswith(("/click/prepare", "/click/view")) or resp.status != 200:
            return
        try:
            raw = resp.read(BODY_MAX)
        except ClientError:
            raw = b""
        buf = [raw]
        resp._next = lambda timeout: buf.pop(0) if buf else None  # noqa: SLF001 - the body is now local
        self._record_view_bytes(raw)

    def _record_view_bytes(self, raw: bytes) -> str | None:
        from orch.click import view as click_view

        try:
            doc = loads_strict(raw)
        except (JcsError, UnicodeDecodeError, ValueError):
            return None
        view = doc.get("view") if isinstance(doc, dict) else None
        if view is None and isinstance(doc, dict) and isinstance(doc.get("data"), dict):
            view = doc["data"].get("view")
        if not isinstance(view, dict):
            return None
        try:
            d = click_view.digest(view)
        except Exception:  # noqa: BLE001 - anything that is not a view
            return None
        now = self._clock()
        with self._mu:
            self._views = {k: v for k, v in self._views.items() if now - v[0] < VIEW_TTL_S}
            if len(self._views) >= VIEWS_MAX:
                self._views.pop(min(self._views, key=lambda k: self._views[k][0]))
            h = view.get("headline")
            self._views[d] = (now, _plain_line(h) if isinstance(h, str) and h else "Approve a signed change")
        return d

    def _known_view(self, digest: str) -> bool:
        now = self._clock()
        with self._mu:
            v = self._views.get(digest)
        return v is not None and now - v[0] < VIEW_TTL_S

    def _confirm_request(self, method: str, path: str, query: str, body: bytes, answer: bytes) -> bool:
        """The 428 answered for ``(method, path, query, body)``: derive the digest ourselves, compare, prompt,
        replay-ready.
        True when a presence covering exactly this request is now in place."""
        try:
            doc = loads_strict(answer)
            err = doc["error"]
        except (JcsError, UnicodeDecodeError, ValueError, KeyError, TypeError):
            return False
        if type(err) is not dict or err.get("code") != "remote.presence_required":
            return False
        if "view_digest" in err or "template" in err:
            theirs, template = err.get("view_digest"), err.get("template")
            if type(theirs) is not str or type(template) is not str or not _SHA.fullmatch(theirs):
                return False
            if not P.template_matches(template, path, self.workspace_id):
                return False
            try:
                mine = P.route_digest(self.workspace_id, method, template, path, query, body)
            except ClientError:
                return False
            if "sha256:" + mine.hex() != theirs:
                return False  # not what we sent: no prompt
            return self._ask(mine, self._route_line(method, template))
        if method == "POST" and path.endswith("/click/sign"):
            return self._confirm_signed(path, body)
        return False

    def _confirm_signed(self, path: str, body: bytes) -> bool:
        try:
            req = loads_strict(body)
            vd = req["view_digest"]
        except (JcsError, UnicodeDecodeError, ValueError, KeyError, TypeError):
            return False
        if type(vd) is not str or not _SHA.fullmatch(vd):
            return False
        if not self._known_view(vd):
            self._fetch_view(path, req)
            if not self._known_view(vd):
                return False
        with self._mu:
            line = self._views.get(vd, (0.0, "Approve a signed change"))[1]
        return self._ask(bytes.fromhex(vd[7:]), line)

    def _fetch_view(self, sign_path: str, req: dict[str, Any]) -> None:
        """The review was not seen on this stream: fetch it once, in the stream, and recompute its digest."""
        rid = req.get("request_id")
        if type(rid) is not str:
            return
        vpath = sign_path[: -len("/sign")] + "/view"
        try:
            resp = self.request("POST", vpath, "", [("content-type", "application/json")], dumps({"request_id": rid}))
            if resp.status == 200:
                self._record_view_bytes(resp.read(BODY_MAX))
        except ClientError:
            return

    def _route_line(self, method: str, template: str) -> str:
        seg = [s for s in template.split("/") if s and not s.startswith("{")]
        what = " ".join(seg[2:] if seg[:2] == ["api", "w"] else seg) or "an action"
        return _plain_line(f"{'Open' if method == 'WEBSOCKET' else 'Change'}: {what}")

    def _ask(self, digest: bytes, line: str) -> bool:
        """Queue one action for the next prompt and wait for the answer."""
        done, result = threading.Event(), []
        with self._wait_cv:
            self._wait.append((digest, line, done, result))
            self._wait_cv.notify_all()
        while not done.wait(0.25):
            if self.closed is not None:
                return False
        return result == ["ok"]

    def presence(self, digests: list[bytes], lines: list[str]) -> None:
        """Confirm the given actions (<= 16, sorted unique) in one prompt: nonce, sign, present. For the UI's own use
        and the batcher; raises :class:`ClientError` when it is not confirmed."""
        digests = sorted(set(digests))
        if not 1 <= len(digests) <= P.DIGESTS_MAX:
            raise ClientError("malformed")
        nonce_resp = self.request(
            "POST",
            f"/api/w/{self.workspace_id}/remote/presence/nonce",
            "",
            [("content-type", "application/json")],
            b"{}",
        )
        raw = nonce_resp.read(64 * 1024)
        if nonce_resp.status != 200:
            raise ClientError("remote_client.not_confirmed")
        try:
            nonce = unb64u(loads_strict(raw)["nonce"], 32)
        except (EncodingError, JcsError, KeyError, TypeError, ValueError):
            raise ClientError("remote_client.not_confirmed") from None
        msg = P.presence_bytes(self.workspace_id, self.device_id, self.ch.hh, nonce, digests)
        n = len(digests)
        prompt = P.Prompt(
            _plain_line(f"Confirm in {self.label()}"),
            tuple(lines),
            _reason(f"Confirm {n} action{'s' if n != 1 else ''} in {self.label()}"),
        )
        self._notify(prompt)
        try:
            assertion = self._assert(msg, prompt)
        finally:
            self._notify(None)
        body = dumps({"nonce": b64u(nonce), "digests": ["sha256:" + d.hex() for d in digests], "assertion": assertion})
        got = self.request(
            "POST", f"/api/w/{self.workspace_id}/remote/presence", "", [("content-type", "application/json")], body
        )
        got.read(64 * 1024)
        if got.status != 200:
            raise ClientError("presence")

    def _presence_loop(self) -> None:
        while True:
            with self._wait_cv:
                while not self._wait and self.closed is None:
                    self._wait_cv.wait(0.5)
                if self.closed is not None:
                    batch, self._wait = self._wait, []
                else:
                    batch = []
            if self.closed is not None:
                for _d, _l, done, _r in batch:
                    done.set()
                return
            time.sleep(self._window)  # the batch window: everything that needs a presence within it shares a prompt
            with self._wait_cv:
                take: dict[bytes, tuple[bytes, str, threading.Event, list[str]]] = {}
                rest = []
                for item in self._wait:
                    if item[0] in take or len(take) >= P.DIGESTS_MAX:
                        rest.append(item)  # a digest authorizes ONE action: a twin waits for the next round
                    else:
                        take[item[0]] = item
                self._wait = rest
            ordered = sorted(take)
            try:
                self.presence(ordered, [take[d][1] for d in ordered])
                outcome = "ok"
            except (ClientError, ValueError):
                outcome = "no"
            for d in ordered:
                take[d][3].append(outcome)
                take[d][2].set()


def _reason(text: str) -> str:
    """The one line a Touch ID sheet shows: printable, at most 200 bytes, cut on a character boundary."""
    t = "".join(c for c in text if c.isprintable()).strip() or "Confirm on this computer"
    return t.encode("utf-8")[:200].decode("utf-8", "ignore").strip()


def _plain_line(text: str) -> str:
    """One printable line for a prompt, from text the workspace's fixed fields gave (control characters dropped)."""
    t = "".join(c for c in text if c.isprintable())[:120].strip()
    return t or "An action"


def open_session(
    ch: Channel,
    *,
    workspace_id: str,
    device_id: str,
    assert_: Callable[[bytes, P.Prompt], dict[str, Any]],
    name: str | None = None,
    api: tuple[int, int] = REMOTE_API,
    notify: Callable[[P.Prompt | None], None] | None = None,
    batch_window: float = BATCH_WINDOW_S,
    wait_s: float = 30.0,
) -> Session:
    """The first transport message: ``open`` (the API range of the installed UI build, the presence over the open
    bytes);
    the host answers ``opened{api}``. A close with ``remote.version`` (no overlap) is the plain "update orch" error."""
    label = name or f"workspace {_short(workspace_id)}"
    try:
        msg = P.open_bytes(workspace_id, device_id, ch.hh, ch.challenge)
        presence = assert_(
            msg,
            P.Prompt(
                _plain_line(f"Open {label}"),
                (_plain_line(f"Open {label} on this computer"),),
                _reason(f"Open {label} on this computer"),
            ),
        )
        ch.send(dumps({"t": "open", "api": [api[0], api[1]], "presence": presence}))
        end = time.monotonic() + wait_s
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise ClientError("remote_client.timeout")
            pt = ch.recv(min(left, 1.0))
            if pt is not None:
                break
        try:
            o = loads_strict(pt)
        except (JcsError, UnicodeDecodeError, ValueError):
            raise ClientError("remote_client.protocol") from None
        if (
            type(o) is not dict
            or set(o) != {"t", "api"}
            or o["t"] != "opened"
            or type(o["api"]) is not int
            or not api[0] <= o["api"] <= api[1]
        ):
            raise ClientError("remote_client.protocol")  # opened.api outside our range: closed `malformed`
    except ClientError as exc:
        ch.close(
            "presence"
            if exc.code == "remote_client.cancelled"
            else "malformed"
            if exc.code == "remote_client.protocol"
            else "closed"
        )
        raise
    return Session(
        ch,
        workspace_id=workspace_id,
        device_id=device_id,
        api=o["api"],
        assert_=assert_,
        name=name,
        notify=notify,
        batch_window=batch_window,
    ).start()
