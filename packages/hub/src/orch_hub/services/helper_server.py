"""``orch-hub helper``: the small process that starts, stops and restarts the hub's parts on THIS machine (HUB-SV-02).

Never reachable from the network: it listens on one unix socket in the hub state dir (dir 0700, socket 0600) and refuses
a peer whose uid is not its own (SO_PEERCRED / LOCAL_PEERCRED). It runs as the same user as the hub and has no more
rights than that user plus the sudoers lines the owner installed (see :func:`helper_backends.sudoers_text`).

Ops (one JSON line in, one out): ``parts``, ``prepare`` (admin; the HELPER makes the nonce), ``run`` (the signed one) and
``logs`` (admin, read only; ``helper_logs``). The web layer found the person's session; the helper still checks, from the
stores, that they are a hub admin NOW and that their channel signed exactly the context the helper made."""

from __future__ import annotations

import json
import os
import secrets
import socket
import stat
import struct
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import helper_proto as P
from .helper_backends import ACTION_TIMEOUT_S, Config, Part, Runner, run_argv
from .helper_proto import HEX32, HelperError
from .helper_verify import ReadOnlyAuth
from .redact import redact

SOCK_FILE = "helper.sock"
AUDIT_FILE = "helper-audit.log"
MAX_REQUEST = 16 * 1024
MAX_REPLY = 1024 * 1024
HUB_DELAY_S = 1.0


def peer_uid(sock: socket.socket) -> int:
    if sys.platform == "linux":
        return struct.unpack("iII", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    if sys.platform == "darwin":
        raw = sock.getsockopt(0, 1, 76)  # SOL_LOCAL, LOCAL_PEERCRED (struct xucred)
        version, uid, groups = struct.unpack_from("@IIh", raw)
        if version != 0 or not 1 <= groups <= 16:
            raise OSError("bad LOCAL_PEERCRED")
        return uid
    raise OSError("unix peer credentials are unsupported here")


def _defer(delay_s: float, fn: Callable[[], None]) -> None:
    t = threading.Timer(delay_s, fn)
    t.daemon = True
    t.start()


class HelperService:
    """The rules, without a socket (the tests drive ``handle`` directly)."""

    def __init__(
        self,
        *,
        state: Path,
        config: Config,
        auth: Any,
        nonces: P.NonceStore,
        runner: Runner = run_argv,
        now_ms: Callable[[], int] | None = None,
        defer: Callable[[float, Callable[[], None]], None] = _defer,
        logs: Callable[[Part], str] | None = None,
    ) -> None:
        self._state, self._cfg, self._auth, self._nonces = state, config, auth, nonces
        self._run, self._defer = runner, defer
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._logs = logs
        self._locks = {n: threading.Lock() for n in config.parts}
        self._hub_busy = threading.Event()  # set while the hub itself is being stopped or restarted
        self._audit_mu = threading.Lock()

    # --- plumbing ----------------------------------------------------------------------------------------------------

    def _audit(self, **kv: Any) -> None:
        """One JSON line per read or action, BEFORE it happens; a line that cannot be written refuses the call."""
        line = json.dumps({"ts_ms": self._now(), **kv}, separators=(",", ":")) + "\n"
        try:
            with self._audit_mu:
                fd = os.open(self._state / AUDIT_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    os.write(fd, line.encode())
                finally:
                    os.close(fd)
        except OSError:
            raise HelperError("unavailable") from None

    def _part(self, name: object) -> Part:
        p = self._cfg.parts.get(name) if type(name) is str else None
        if p is None:
            raise HelperError("unknown_part")
        return p

    def _ids(self, req: dict) -> tuple[str, str]:
        u, c = req.get("user_id"), req.get("channel_id")
        for x in (u, c):
            if type(x) is not str or not HEX32.fullmatch(x):
                raise HelperError("malformed")
        return u, c  # type: ignore[return-value]

    def handle(self, req: Any) -> dict[str, Any]:
        try:
            if type(req) is not dict or type(req.get("op")) is not str:
                raise HelperError("malformed")
            op = req["op"]
            if op == "parts":
                return {"ok": True, **self.parts()}
            if op == "prepare":
                return {"ok": True, **self.prepare(req)}
            if op == "run":
                return {"ok": True, **self.run(req)}
            if op == "logs":
                return {"ok": True, **self.logs(req)}
            raise HelperError("unknown_op")
        except HelperError as e:
            return {"ok": False, "code": e.code, "detail": e.detail[:200]}
        except Exception:  # noqa: BLE001 - unexpected: no detail leaves the process
            return {"ok": False, "code": "internal"}

    # --- ops ---------------------------------------------------------------------------------------------------------

    def parts(self) -> dict[str, Any]:
        return {
            "host_id": self._cfg.host_id,
            "parts": [{"name": p.name, "label": p.label} for p in self._cfg.parts.values()],
            "hub_busy": self._hub_busy.is_set(),
        }

    def _admin(self, user_id: str, channel_id: str) -> None:
        if not self._auth.is_admin(user_id):
            raise HelperError("forbidden_role")
        if self._auth.is_channel_revoked(channel_id):
            raise HelperError("channel_revoked")

    def prepare(self, req: dict) -> dict[str, Any]:
        user, chan = self._ids(req)
        part, action, ver = self._part(req.get("part")), req.get("action"), req.get("target_version")
        if action not in P.ACTIONS:
            raise HelperError("bad_action")
        if action != "update" and ver is not None:
            raise HelperError("malformed")
        self._admin(user, chan)
        if action == "update":
            # signed releases do not exist yet (docs/design/install-update.md): refuse rather than run anything unverified
            raise HelperError("update_unavailable")
        text = P.text_for(part.name, part.label, action, self._cfg.host_id, None)
        nonce, iat = secrets.token_hex(16), self._now()
        ctx = P.context(
            hub_id=self._auth.hub_id(),
            host_id=self._cfg.host_id,
            part=part.name,
            action=action,
            target_version=None,
            nonce=nonce,
            iat=iat,
            channel_id=chan,
            user_id=user,
            text=text,
        )
        exp = self._nonces.put(nonce, user, ctx, text)
        return {"ctx": ctx, "digest": P.digest(ctx).hex(), "text": text, "expires_ms": exp}

    def run(self, req: dict) -> dict[str, Any]:
        user, chan = self._ids(req)
        taken = self._nonces.take(req.get("nonce"))  # consumed here, before anything else can fail
        if taken is None:
            raise HelperError("unknown_prepared")
        ctx, _text, exp = taken
        if exp <= self._now():
            raise HelperError("prepared_expired")
        if ctx["user_id"] != user or ctx["channel_id"] != chan:
            raise HelperError("other_user")
        self._auth.check_admin_proof(user, chan, P.digest(ctx), req.get("proof"))
        part = self._part(ctx["part"])
        action = ctx["action"]
        if action not in P.VERBS_RUN:
            raise HelperError("bad_action")
        if self._hub_busy.is_set():
            raise HelperError("busy")
        lock = self._locks[part.name]
        if not lock.acquire(blocking=False):
            raise HelperError("busy")
        try:
            self._audit(op="run", user=user, channel=chan, part=part.name, action=action, nonce=ctx["nonce"])
            argv = part.action_argv(action)
            if part.name == "hub" and action in ("stop", "restart"):
                self._hub_busy.set()
                held = lock
                self._defer(HUB_DELAY_S, lambda: self._run_hub(argv, held))
                lock = None  # released by the deferred run
                return {"accepted": True, "will_restart": True}
            rc, _out, err = self._run(argv, ACTION_TIMEOUT_S)
            if rc != 0:
                raise HelperError("action_failed", _short(redact(err, max_lines=2)))
            return {"accepted": True, "will_restart": False}
        finally:
            if lock is not None:
                lock.release()

    def _run_hub(self, argv: list[str], lock: threading.Lock) -> None:
        try:
            self._run(argv, ACTION_TIMEOUT_S)
        finally:
            self._hub_busy.clear()
            lock.release()

    def logs(self, req: dict) -> dict[str, Any]:
        user, chan = self._ids(req)
        part = self._part(req.get("part"))
        self._admin(user, chan)
        if self._logs is None:
            raise HelperError("unavailable")
        self._audit(op="logs", user=user, channel=chan, part=part.name)
        return {"part": part.name, "text": self._logs(part)}


def _short(err: str) -> str:
    return " ".join(err.split())[:200]


# --- the socket -----------------------------------------------------------------------------------------------------


def _read_line(conn: socket.socket) -> bytes | None:
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > MAX_REQUEST:
            return None
    return buf.split(b"\n", 1)[0] if buf else None


class HelperServer:
    def __init__(self, service: HelperService, sock_path: Path, *, uid: int | None = None) -> None:
        self._svc, self._path = service, sock_path
        self._uid = os.geteuid() if uid is None else uid
        self._stop = threading.Event()
        self._sem = threading.BoundedSemaphore(8)
        self._sock: socket.socket | None = None

    def bind(self) -> None:
        if len(os.fsencode(self._path)) > 100:  # sun_path is 104 (macOS) or 108 (Linux) bytes
            raise HelperError("bad_socket_path", "path too long")
        try:
            st = os.lstat(self._path)
        except FileNotFoundError:
            st = None
        if st is not None:
            if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.geteuid():
                raise HelperError("bad_socket_path")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(1)
            try:
                probe.connect(str(self._path))
            except OSError:
                pass  # nothing answers: a stale socket of ours
            else:
                raise HelperError("already_running")
            finally:
                probe.close()
            os.unlink(self._path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            s.bind(str(self._path))
        finally:
            os.umask(old)
        os.chmod(self._path, 0o600)
        s.listen(8)
        s.settimeout(0.5)
        self._sock = s

    def serve_forever(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            if not self._sem.acquire(blocking=False):
                conn.close()
                continue
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.close_listener()
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def close_listener(self) -> None:
        """Stop listening NOW. A plain close() while another thread is blocked in accept() leaves the socket listening on
        Linux until that accept returns, so a new helper would still find "a helper answers": shut it down first."""
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()

    def _one(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5)
            try:
                if peer_uid(conn) != self._uid:  # another user, or no credentials at all: nothing is read
                    return
            except OSError:
                return
            line = _read_line(conn)
            try:
                req = json.loads(line) if line else None
            except ValueError:
                req = None
            out = json.dumps(self._svc.handle(req), separators=(",", ":")).encode()
            conn.sendall(out[:MAX_REPLY] + b"\n")
        except OSError:
            pass
        finally:
            conn.close()
            self._sem.release()


def run_helper(state: Path) -> int:
    from .helper_backends import load_config

    try:
        cfg = load_config(state)
        nonces = P.NonceStore(state / "helper-nonce.db")
    except (HelperError, ValueError) as e:
        print(f"orch-hub helper: refused: {e}", file=sys.stderr)
        return 1
    from .redact import read_logs

    svc = HelperService(
        state=state, config=cfg, auth=ReadOnlyAuth(state), nonces=nonces, logs=lambda p: read_logs(p, run_argv)
    )
    srv = HelperServer(svc, state / SOCK_FILE)
    try:
        srv.bind()
    except (HelperError, OSError) as e:
        print(f"orch-hub helper: refused: {e}", file=sys.stderr)
        return 1
    print(f"orch-hub helper ready {state / SOCK_FILE}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
    return 0
