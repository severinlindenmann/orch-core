"""The remote client's brain: the home screens' API, the presence prompt, and one listener per open session (R5 design
sections 1, 3 and 6). The loopback app (:mod:`.origin`) only moves bytes; every decision is here.

Names: a workspace's name in a prompt is the one the workspace itself told us through the end-to-end session (stored by
the client core); until then it reads "workspace <last 8 of the id>". The hub's name for a workspace is shown in
the list
only, as plain text, labelled as the hub's, and is NEVER put in a prompt."""

from __future__ import annotations

import contextlib
import platform
import re
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orch.remoteclient import Client, ClientError, PresenceBackend, Prompt, Session
from orch.remoteclient import enroll as _enroll
from orch.remoteclient.presence import check_signable

from .launcher import Launcher
from .listener import Listener, bind_loopback
from .origin import Origin, error_doc
from .static import StaticSite

__all__ = ["Controller", "GatedPresence", "PinAsker", "workspace_label"]

PROMPT_WAIT_S = 120.0
HEX32 = re.compile(r"[0-9a-f]{32}")
_ctx = threading.local()


@contextlib.contextmanager
def _for_workspace(ws: str | None) -> Iterator[None]:
    before = getattr(_ctx, "ws", None)
    _ctx.ws = ws
    try:
        yield
    finally:
        _ctx.ws = before


@dataclass
class Pending:
    id: str
    workspace_id: str | None
    prompt: Prompt
    event: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


class GatedPresence:
    """A presence backend whose signature is first asked for on the home page: the person reads what the workspace asks
    ("<workspace> asks you to confirm", the list), then clicks once; only then does the key (Touch ID / the security
    key) get to sign. Cancelling, or nobody answering in two minutes, is "Not confirmed" and nothing is signed. The
    inner backend still re-checks the bytes and shows its own sheet; this gate adds no authority."""

    def __init__(self, inner: PresenceBackend, wait_s: float = PROMPT_WAIT_S) -> None:
        self.inner, self.wait_s = inner, wait_s
        self.kind = inner.kind
        self._mu = threading.Lock()
        self._pending: list[Pending] = []
        self.closed = False

    def create(self, name: str) -> dict[str, Any]:
        return self.inner.create(name)

    def assert_(self, ref: str, msg: bytes, prompt: Prompt) -> dict[str, Any]:
        check_signable(msg)  # the person is never asked to confirm bytes the key would refuse to sign
        p = Pending(secrets.token_urlsafe(9), getattr(_ctx, "ws", None), prompt)
        with self._mu:
            self._pending.append(p)
        try:
            end = time.monotonic() + self.wait_s
            while not p.event.wait(0.1):
                if self.closed or time.monotonic() > end:
                    break
            if not p.approved:
                raise ClientError("remote_client.cancelled")
        finally:
            with self._mu:
                if p in self._pending:
                    self._pending.remove(p)
        return self.inner.assert_(ref, msg, prompt)

    def current(self) -> Pending | None:
        with self._mu:
            return self._pending[0] if self._pending else None

    def answer(self, ident: str, approve: bool) -> bool:
        with self._mu:
            p = next((x for x in self._pending if x.id == ident), None)
        if p is None:
            return False
        p.approved = approve
        p.event.set()
        return True

    def cancel_workspace(self, ws: str) -> None:
        """Stop waiting for the person on every prompt that belongs to this workspace (nothing is signed)."""
        with self._mu:
            mine = [p for p in self._pending if p.workspace_id == ws]
        for p in mine:
            p.approved = False
            p.event.set()

    def shut(self) -> None:
        self.closed = True


class PinAsker:
    """The security key's PIN, asked of the person on the home page only when the key itself asks for one. The PIN is
    held in memory from the moment it is typed until the key has used it; it is never written, logged or reused."""

    def __init__(self, wait_s: float = PROMPT_WAIT_S) -> None:
        self.wait_s = wait_s
        self._mu = threading.Lock()
        self._req: tuple[str, threading.Event] | None = None
        self._pin: str | None = None
        self.closed = False

    def ask(self) -> str | None:
        """Called by the key backend (its own thread): block until the person typed the PIN or cancelled."""
        ev = threading.Event()
        ident = secrets.token_urlsafe(9)
        with self._mu:
            self._req, self._pin = (ident, ev), None
        end = time.monotonic() + self.wait_s
        try:
            while not ev.wait(0.1):
                if self.closed or time.monotonic() > end:
                    return None
            with self._mu:
                pin, self._pin = self._pin, None
            return pin
        finally:
            with self._mu:
                if self._req and self._req[0] == ident:
                    self._req, self._pin = None, None

    def current(self) -> str | None:
        with self._mu:
            return self._req[0] if self._req else None

    def answer(self, ident: str, pin: str | None) -> bool:
        with self._mu:
            if not self._req or self._req[0] != ident:
                return False
            self._pin = pin if isinstance(pin, str) and 0 < len(pin) <= 63 else None
            ev = self._req[1]
        ev.set()
        return True

    def shut(self) -> None:
        self.closed = True


def workspace_label(client: Client, ws: str | None) -> str:
    """What a prompt calls a workspace: the name it gave through the end-to-end session, else its short id."""
    if ws is None:
        return "this workspace"
    try:
        own = client.store.workspaces().get(ws, {}).get("name")
    except ClientError:
        own = None
    return own if isinstance(own, str) and own else f"workspace {ws[-8:]}"


@dataclass
class Open:
    workspace_id: str
    session: Session
    listener: Listener
    origin: Origin
    started_ms: int


@dataclass
class EnrollJob:
    id: str
    workspace_id: str
    status: str = "starting"  # starting | words | added | error | stopped
    words: list[str] = field(default_factory=list)
    message: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)


class Controller:
    def __init__(
        self,
        client: Client,
        static: StaticSite,
        *,
        gate: GatedPresence | None = None,
        opener: Callable[[str], object] = webbrowser.open,
        computer_name: str | None = None,
        clock: Callable[[], float] = time.time,
        launch_dir: Path | None = None,
        pin: PinAsker | None = None,
    ) -> None:
        self.pin = pin
        self.client, self.static, self.gate = client, static, gate
        self._clock = clock
        # the browser is opened with the path of a private redirect file, never an address with a code in it (argv)
        self.launcher = Launcher(opener, launch_dir)
        self.computer_name = computer_name or _computer_name()
        self._mu = threading.Lock()
        self._open: dict[str, Open] = {}
        self._ended: dict[str, str] = {}
        self._jobs: dict[str, EnrollJob] = {}
        self.home_listener: Listener | None = None
        self.home: Origin | None = None

    # -- the home origin --

    def start_home(self) -> str:
        """Bind the home page's own port and return its address with a single-use launch code."""
        sock = bind_loopback()
        lst = Listener(sock)
        self.home = Origin(port=lst.port, kind="home", static=self.static, home_api=self.route)
        self.home.on_redeem = lambda: self.launcher.redeemed("home")
        self.home_listener = lst.serve(self.home)
        return self.home_url()

    def open_home(self) -> bool:
        """Open the browser at the home page (through the redirect file)."""
        return self.launcher.open("home", self.home_url().replace("/#launch=", "/go#launch="))

    def home_url(self) -> str:
        assert self.home is not None
        return f"{self.home.origin}/#launch={self.home.launch_code()}"

    def stop(self) -> None:
        if self.gate:
            self.gate.shut()
        if self.pin:
            self.pin.shut()
        for ws in list(self._open):
            self.end(ws)
        if self.home_listener:
            self.home_listener.stop()
        self.launcher.close()

    # -- routes of the home page --

    def route(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            return self._route(method, path, body)
        except ClientError as exc:
            msg = (
                "Check your details and try again."
                if exc.code == "no_session" and path.endswith("/connect")
                else exc.message
            )
            return (409 if exc.code.startswith("remote_client.") else 400), error_doc(exc.code, msg)

    def _route(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        p = path.removeprefix("/api/remote-client")
        if method == "GET" and p == "/state":
            return 200, self.state()
        if method == "POST" and p == "/connect":
            return 200, self.connect(body)
        if method == "GET" and p == "/workspaces":
            return 200, {"workspaces": self.workspaces()}
        if method == "POST" and p == "/enroll":
            return 200, self.enroll_start(body)
        if method == "GET" and p.startswith("/enroll/"):
            return 200, self.enroll_status(p.rsplit("/", 1)[1])
        if method == "POST" and p.startswith("/enroll/") and p.endswith("/stop"):
            return 200, self.enroll_stop(p.split("/")[2])
        if method == "POST" and p == "/open":
            return 200, self.open(body)
        if method == "POST" and p == "/end":
            self.end(_ws(body))
            return 200, {"ok": True}
        if method == "POST" and p == "/pin":
            ident, pin = body.get("id"), body.get("pin")
            if type(ident) is not str or (pin is not None and type(pin) is not str) or self.pin is None:
                return 400, error_doc("malformed", "That did not look right.")
            return 200, {"ok": self.pin.answer(ident, pin)}
        if method == "POST" and p == "/answer":
            ident, ok = body.get("id"), body.get("approve")
            if type(ident) is not str or type(ok) is not bool or self.gate is None:
                return 400, error_doc("malformed", "That did not look right.")
            return 200, {"ok": self.gate.answer(ident, ok)}
        return 404, error_doc("not_found", "Not found.")

    def state(self) -> dict[str, Any]:
        self._reap()
        pend = self.gate.current() if self.gate else None
        return {
            "signed_in": self.client.signed_in(),
            "hub": _hub_view(self.client),
            "presence": {
                "available": self.gate is not None,
                "method": "key" if self.gate and self.gate.kind == "fido2" else "touch_id",
                "tied": getattr(self.gate.inner, "bound", None) if self.gate else None,
            },
            "prompt": None if pend is None else self._prompt_view(pend),
            "pin": self.pin.current() if self.pin else None,
            "open": sorted(self._open),
            "ended": dict(self._ended),
            "computer": self.computer_name,
        }

    def _prompt_view(self, p: Pending) -> dict[str, Any]:
        return {
            "id": p.id,
            "workspace": workspace_label(self.client, p.workspace_id),
            "lines": list(p.prompt.lines) or [p.prompt.title],
        }

    # -- the hub --

    def connect(self, body: dict[str, Any]) -> dict[str, Any]:
        link, user, password, totp = (body.get(k) for k in ("link", "user", "password", "code"))
        if not all(type(x) is str and x for x in (link, user, password, totp)):
            raise ClientError("remote_client.no_hub")
        # the pin comes from the hub's signed card the link names (the core client does the hub part)
        self.client.connect_with_link(link, user, password, totp, now_ms=int(self._clock() * 1000))
        return {"ok": True}

    def workspaces(self) -> list[dict[str, Any]]:
        rows = self.client.workspaces()
        self._reap()
        return [
            {**r, "open": r["workspace_id"] in self._open, "ended": self._ended.get(r["workspace_id"])} for r in rows
        ]

    # -- adding this computer --

    def enroll_start(self, body: dict[str, Any]) -> dict[str, Any]:
        text = body.get("code")
        if type(text) is not str:
            raise ClientError("remote_client.bad_code")
        text = text.strip()
        code = _enroll.parse_code(text)  # a bad or browser code is refused here, before anything starts
        job = EnrollJob(secrets.token_urlsafe(9), code.workspace_id)  # the code itself is never kept
        with self._mu:
            self._jobs[job.id] = job

        def words(w: str) -> None:
            job.words = w.split()
            if job.status == "starting":
                job.status = "words"

        def run() -> None:
            try:
                with _for_workspace(code.workspace_id):
                    self.client.enroll(text, self.computer_name, words, cancel=job.cancel)
                job.status = "added"  # it was added: a late Stop cannot undo what the workspace already did
            except ClientError as exc:
                if job.status != "stopped":
                    job.status, job.message = "error", exc.message
            except Exception:  # noqa: BLE001 - never a trace on the page
                if job.status != "stopped":
                    job.status, job.message = "error", ClientError("x").message

        threading.Thread(target=run, name="remote-client-enroll", daemon=True).start()
        return {"id": job.id, "workspace_id": code.workspace_id}

    def enroll_status(self, ident: str) -> dict[str, Any]:
        job = self._jobs.get(ident)
        if job is None:
            raise ClientError("remote_client.not_added")
        return {"status": job.status, "words": job.words, "message": job.message, "workspace_id": job.workspace_id}

    def enroll_stop(self, ident: str) -> dict[str, Any]:
        job = self._jobs.get(ident)
        if job is not None and job.status in ("starting", "words"):
            job.status = "stopped"
            job.cancel.set()  # the enrollment stream is closed by the client core and nothing is stored
            if self.gate is not None:
                self.gate.cancel_workspace(job.workspace_id)
        return {"ok": True}

    # -- sessions --

    def open(self, body: dict[str, Any]) -> dict[str, Any]:
        ws = _ws(body)
        with self._mu:
            have = self._open.get(ws)
        if have is not None and have.session.closed is None:
            self._launch(have)
            return {"ok": True}
        self._reap()
        self._ended.pop(ws, None)

        def notify(prompt: Prompt | None) -> None:  # runs in the session's presence thread, just before it asks
            _ctx.ws = ws if prompt is not None else None

        with _for_workspace(ws):
            session = self.client.open(ws, notify=notify)
        sock = bind_loopback()
        lst = Listener(sock)
        origin = Origin(port=lst.port, kind="session", static=self.static, session=session)
        lst.serve(origin)
        origin.on_redeem = lambda ws=ws: self.launcher.redeemed(ws)
        rec = Open(ws, session, lst, origin, int(self._clock() * 1000))
        with self._mu:
            self._open[ws] = rec
        self._launch(rec)
        return {"ok": True}

    def _launch(self, rec: Open) -> None:
        self.launcher.open(rec.workspace_id, f"{rec.origin.origin}/go#launch={rec.origin.launch_code()}")

    def end(self, ws: str) -> None:
        with self._mu:
            rec = self._open.pop(ws, None)
        if rec is None:
            return
        with contextlib.suppress(Exception):
            rec.session.close()
        rec.listener.stop()

    def _reap(self) -> None:
        """A session the workspace or the hub ended: stop its origin and remember the plain reason."""
        for ws, rec in list(self._open.items()):
            if rec.session.closed is not None:
                reason = rec.session.closed
                self.end(ws)
                self._ended[ws] = ClientError(reason).message if reason != "closed" else ""


def _ws(body: dict[str, Any]) -> str:
    ws = body.get("workspace_id")
    if type(ws) is not str or not HEX32.fullmatch(ws):
        raise ClientError("remote_client.not_added")
    return ws


def _hub_view(client: Client) -> dict[str, Any] | None:
    hub = client.store.hub()
    if hub is None:
        return None
    return {"id": hub["hub_id"], "address": hub["origin"].split("://", 1)[-1]}


def _computer_name() -> str:
    n = "".join(c for c in platform.node() if c.isprintable()).strip()[:64]
    return n or "This computer"
