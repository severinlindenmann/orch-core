"""Private IPC launch, one random loopback listener, and port-ownership proof."""

import argparse
import asyncio
import errno
import fcntl
import functools
import hashlib
import hmac
import html
import ipaddress
import json
import math
import os
import pwd
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from contextlib import contextmanager, suppress
from pathlib import Path

import uvicorn
from orch import childenv

from .adapters.setup import connect_setup
from .app import create_app
from .auth import CODE_TTL, SETUP_CODE_TTL, Auth
from .front import Front, PeerChecked
from .gateway import Gateway, RunDirectoryError, run_dir, state_dir
from .hostlink import check_uid, encode, peer_uid, receive
from .passkey_routes import ENROLL_TTL
from .policy import REMOTE, remote_policy

#: seconds a stopping server waits for open connections (a tab's live stream) before it cuts them
GRACE = 3
#: seconds `restart` waits for the old server to free its control socket
RESTART_WAIT = 10.0
# A stop while a setup page is bound keeps the loopback page up until the setup ends (r11-P01), at most this long.
SETUP_HOLD = 180.0
SETUP_POLL = 0.5
AGENT_ENV = ("ORCH_GRANT", "ORCH_SESSION")
# Seconds before the code's expiry after which the launcher writes no redirect file (and removes one it just wrote):
# covers the gap between the server's monotonic timer and the wall clock both sides read.
EXPIRY_MARGIN = 1
#: the port a headless machine listens on unless told otherwise, so the printed `ssh -L` line stays true across
#: restarts (#1198 R1-1). A desktop keeps a random port: nobody types it. Loopback only in both cases.
HEADLESS_PORT = 47821
#: file in the dashboard state directory that fixes the port (one decimal number), on any machine
PORT_FILE = "port"
#: file in the dashboard state directory that turns remote access on: {"name": "<dns name>", "front_uid": <int>}
#: (written by `orch remote enable`, R1-7). Absent: remote access is off and no front socket is opened.
REMOTE_FILE = "remote.json"
#: the front socket's file name
FRONT_SOCKET = "front.sock"
#: where the front socket lives: `<FRONT_RUN_ROOT>/<person uid>/front.sock`. The directory `<uid>` is made by root
#: (`orch remote enable`, systemd-tmpfiles): the person's, group `orch-front`, mode 0750. So the front's user reaches
#: the socket without `<state>/run` (0700) or `/run/orch/<uid>` (the agent's group) being opened, and nobody else
#: (the agent user, an add-on, another person) can even look inside. The peer UID check stays the gate.
FRONT_RUN_ROOT = Path("/run/orch/front")
#: users the front may never be: an agent or add-on user is the very process the front keeps out
FORBIDDEN_FRONT_PREFIXES = ("orch-agent-", "orch-addon-")


class AgentRefused(RuntimeError):
    """``human_only``: the dashboard acts as the person, so it never opens or serves under an agent's environment."""

    def __init__(self):
        super().__init__(
            "human_only: the dashboard is a person's; an agent never opens it "
            "(unset ORCH_GRANT and ORCH_SESSION in your own terminal)"
        )


def refuse_agent(environ=None):
    """Refuse (#542 item 1) when ORCH_GRANT or ORCH_SESSION is set and non-empty: the same code, exit and message
    as core's ``orch dashboard``. A speed bump in tier A (same UID), not a boundary."""
    environ = os.environ if environ is None else environ
    if any(environ.get(name) for name in AGENT_ENV):
        raise AgentRefused()


@contextmanager
def private_directory(path, parents=True):
    path.mkdir(parents=parents, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        s = os.fstat(fd)
        if s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o700:
            raise RuntimeError("dashboard state directory must be owned by you, mode 0700")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def locked(directory_fd):
    fd = os.open("lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077:
            raise RuntimeError("unsafe dashboard lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


def control(path, command, kind=None):
    """One request on ctl.sock. ``launch`` mints a code, so it refuses under an agent's environment first (#542 item 1;
    the socket itself cannot tell an agent of the same UID from the person: the accepted tier-A residual)."""
    if command == "launch":
        refuse_agent()
        asked = time.time()  # the mint is later: asked + CODE_TTL never outlives the code
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(str(path))
        check_uid(sock, os.getuid())
        sock.sendall(encode({"command": command, **({"kind": kind} if kind else {})}))
        reply = receive(sock)
    if command == "launch" and isinstance(reply, dict) and "expires_at" not in reply:
        reply["expires_at"] = asked + CODE_TTL  # an older server (30 s codes for every kind): a conservative bound
    return reply


@functools.lru_cache(maxsize=1)
def code_digest():
    """12 hex of a digest of this package's files: a server reports it (``hello``) and a launcher with another digest
    knows the running server is from another release."""
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for file in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(file.relative_to(root).as_posix().encode() + b"\0")
        with suppress(OSError):
            h.update(file.read_bytes())
    return h.hexdigest()[:12]


def stale_socket(path):
    """Call only while holding the startup lock. Never unlink a responding or foreign socket."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
        raise RuntimeError("unsafe control socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        try:
            sock.connect(str(path))
        except FileNotFoundError:
            return  # a stopping server removed it between lstat and connect: already gone
        except OSError as exc:
            if exc.errno != errno.ECONNREFUSED:
                raise
        else:
            raise RuntimeError("dashboard already running")
    path.unlink(missing_ok=True)


def redirect_directory(fallback):
    """The one place that picks the redirect directory; the server decides and tells the launcher.

    XDG_RUNTIME_DIR is used only if it already exists as a real directory owned by this user, mode 0700.
    It is never created. Only the `orch-dashboard` leaf is created. Otherwise use the private fallback.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime.startswith("/"):
        try:
            st = Path(runtime).lstat()
            if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o700:
                target = Path(runtime) / "orch-dashboard"
                with private_directory(target, parents=False):
                    return target
        except (OSError, RuntimeError):
            pass
    return fallback


def sweep_redirects(directory, now=None):
    now = time.time() if now is None else now
    for path in directory.glob("launch-*.html"):
        st = path.lstat()
        if (
            stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and now - st.st_mtime >= SETUP_CODE_TTL
        ):  # the longest code life: a live setup file stays
            path.unlink(missing_ok=True)


def is_headless(environ=None, platform=None):
    """No desktop to open a browser on: Linux or macOS reached over SSH, or Linux with no display."""
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    if environ.get("SSH_CONNECTION") and platform in ("linux", "darwin"):
        return True
    return platform.startswith("linux") and not (environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"))


def listen_port(directory, headless=None, port=None):
    """The loopback port to bind: ``port`` (``serve --port``), else the number in ``<dashboard>/port``, else
    HEADLESS_PORT on a headless machine, else 0 (the system picks). A bad file is an error, never a silent fallback."""
    if port is None:
        path = directory / PORT_FILE
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            fd = None
        except OSError:
            raise RuntimeError(f"cannot read {path}: it must be a plain file") from None
        if fd is not None:
            with os.fdopen(fd) as file:
                st = os.fstat(file.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
                    raise RuntimeError(f"{path} must be your own file, not writable by others")
                text = file.read(32).strip()
            try:
                port = int(text)
            except ValueError:
                raise RuntimeError(f"{path} must hold one port number") from None
    if port is None:
        return HEADLESS_PORT if (is_headless() if headless is None else headless) else 0
    if not 1024 <= port <= 65535:
        raise RuntimeError("the dashboard port must be between 1024 and 65535")
    return port


def remote_config(directory):
    """``(policy, front_uid)`` from ``<dashboard>/remote.json``, or None when the file is absent (remote access off).
    Anything wrong with a file that is there refuses the start (host spec 10.2: missing any of it keeps remote mode
    off, and the server says which), never a silent fall back to loopback only: the administrator asked for remote
    access and must see that it is not on. The file is read with the checks of the port file: a plain file of ours that
    nobody else can write (writing it would choose who may use the front socket)."""
    path = directory / REMOTE_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise RuntimeError(f"remote.not_configured: {path} must be a plain file") from None
    with os.fdopen(fd) as file:
        st = os.fstat(file.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022 or st.st_size > 4096:
            raise RuntimeError(f"remote.not_configured: {path} must be your own small file, not writable by others")
        text = file.read(4097)
    try:
        data = json.loads(text)
        name, uid = data["name"], data["front_uid"]
        if set(data) != {"name", "front_uid"} or type(uid) is not int:
            raise ValueError
        policy = remote_policy(name)  # the name must be a public DNS name
    except (ValueError, KeyError, TypeError):
        raise RuntimeError(
            f"remote.not_configured: {path} must hold exactly a public DNS name and the front's user id"
        ) from None
    if uid <= 0 or uid == os.getuid():
        # the front is its own system user: a front that shares our UID is no boundary (any process of ours passes)
        raise RuntimeError("remote.not_configured: front_uid must be a user other than the person's and not root")
    if any(n.startswith(FORBIDDEN_FRONT_PREFIXES) for n in user_names(uid)):
        # an agent or add-on user as the front would let exactly the processes it must keep out reach the dashboard
        raise RuntimeError("remote.not_configured: front_uid must not be an agent or add-on user")
    return policy, uid


def user_names(uid):
    """Every name the user database gives ``uid`` (aliases too, so a second name cannot hide an agent user)."""
    try:
        return [e.pw_name for e in pwd.getpwall() if e.pw_uid == uid]
    except OSError:  # pragma: no cover - a broken user database: nothing to compare with
        return []


def front_group(uid):
    """The primary group of the front's user: the one group allowed into the front socket's directory."""
    try:
        return pwd.getpwuid(uid).pw_gid
    except KeyError:
        raise RuntimeError(f"remote.not_configured: there is no user with id {uid} for the front") from None


def front_socket_path(front_uid):
    """``<FRONT_RUN_ROOT>/<our uid>/front.sock``. The directory is made by root and is not created here (we could not
    give it the front's group): it must be our own, group = the front's group, no group write and nothing for others,
    and not a link. A missing or wrong directory means remote access stays off (`orch remote enable` makes it)."""
    directory = Path(FRONT_RUN_ROOT) / str(os.getuid())
    try:
        st = directory.lstat()
    except OSError:
        raise RuntimeError(f"remote.not_configured: {directory} is missing; run `orch remote enable`") from None
    if (
        not stat.S_ISDIR(st.st_mode)
        or st.st_uid != os.getuid()
        or st.st_mode & 0o027
        or st.st_gid != front_group(front_uid)
    ):
        raise RuntimeError(
            f"remote.not_configured: {directory} must be your own directory in the front's group, mode 0750"
        )
    return directory / FRONT_SOCKET


def bind_front(path, front_uid):
    """The front socket: bound at ``path`` (mode 0666: who may connect is decided by the peer UID check of every
    accepted connection, not by file permissions; the directory above decides who can reach it at all)."""
    stale_socket(path)
    sock = PeerChecked(front_uid)
    old = os.umask(0o000)
    try:
        sock.bind(str(path))
    except BaseException:
        sock.close()
        raise
    finally:
        os.umask(old)
    os.chmod(path, 0o666)
    st = path.lstat()
    sock.identity = (st.st_ino, st.st_ctime_ns)  # which file is ours: close_front removes nothing else (as ctl.sock)
    sock.listen(128)
    sock.setblocking(False)
    return sock


def close_front(front, path):
    """Close the front socket and remove its file, once: only the socket this process bound (same inode and change
    time, like ctl.sock), never a file or another process's socket that took the name since."""
    if front is not None:
        front.close()
        if path is not None:
            with suppress(OSError):
                st = path.lstat()
                if stat.S_ISSOCK(st.st_mode) and getattr(front, "identity", None) == (st.st_ino, st.st_ctime_ns):
                    path.unlink()


def server_config(app, front=False):
    """The one place that builds a uvicorn config, for the loopback server and the front alike. Both run on asyncio's
    selector loop (`loop="asyncio"`, never uvloop): the front's `PeerChecked.accept` must run for every connection, and
    `FrontServer` refuses to start on any other loop. The front adds its own limits (the front's Caddy already holds
    unauthenticated connections to 32; these bound what gets past it)."""
    extra = (
        {
            "lifespan": "off",  # the loopback server owns the app's lifespan
            "limit_concurrency": 64,
            "timeout_keep_alive": 5,
            "h11_max_incomplete_event_size": 16384,
        }
        if front
        else {}
    )
    return uvicorn.Config(
        app,
        proxy_headers=False,
        access_log=False,
        log_level="warning",
        loop="asyncio",
        # The terminal WebSocket (section 12.6.6): max_size 64 KiB, no permessage-deflate, no subprotocol.
        ws="websockets",
        ws_max_size=65536,
        ws_per_message_deflate=False,
        # A browser tab's live stream never ends by itself: after `stop`, cut open connections after GRACE seconds.
        timeout_graceful_shutdown=GRACE,
        **extra,
    )


def require_selector_loop(loop):
    """The peer check lives in `socket.accept`, which only asyncio's selector loops call."""
    if not isinstance(loop, asyncio.selector_events.BaseSelectorEventLoop):
        raise RuntimeError("remote.not_configured: the front needs asyncio's selector event loop")


class FrontServer(uvicorn.Server):
    async def startup(self, sockets=None):
        require_selector_loop(asyncio.get_running_loop())
        await super().startup(sockets=sockets)


def tunnel_line(port, environ=None, user=None, host=None):
    """The command to run on the person's own computer, the same one core prints (orch.setup.tunnel):
    ``ssh -o ExitOnForwardFailure=yes -L 127.0.0.1:<port>:127.0.0.1:<port> USER@SERVER``. A name that fails the
    closed patterns (or is unknown) shows as the ``USER`` / ``SERVER`` placeholder, never as part of the command."""
    from orch.setup.tunnel import tunnel_line as core_tunnel_line

    return core_tunnel_line(port, os.environ if environ is None else environ, user=user, server=host)


def bind_listener(wanted):
    """The loopback listener. SO_REUSEADDR so a restart on a fixed port is not refused while the old connections sit
    in TIME_WAIT (on Linux it never lets anyone take a port that is still listening)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", wanted))
    except BaseException:
        listener.close()
        raise
    return listener


def serve(lock_held=False, port=None, listen=None):
    refuse_agent()
    run_dir(state_dir())  # refuses a bad run directory before anything is created
    from orch.update.guard import hold_state_root

    # An unfinished update refuses; the shared format lock is held while serving. A machine with no state folder yet
    # has no data to protect (the host stamps it when it first starts), so the dashboard starts without the check.
    _state_root = hold_state_root(state_dir()) if state_dir().is_dir() else None
    directory = state_dir() / "dashboard"
    with private_directory(directory) as fd:
        if not lock_held:
            with locked(fd) as lock_fd:
                return _serve(directory, lock_fd=lock_fd, port=port, listen=listen)
        return _serve(directory, port=port, listen=listen)


def network_address(listen, port):
    """``serve --listen ADDR``: one literal IP of this computer (no wildcard, no name), served on the same port as the
    loopback listener, over plain http (dev prototype: the owner reaches the dashboard over NetBird)."""
    try:
        ip = ipaddress.ip_address(listen)
    except ValueError:
        raise RuntimeError("--listen takes one literal IP address of this computer") from None
    if ip.is_unspecified or ip.is_multicast:
        raise RuntimeError("--listen refuses a wildcard address")
    if not port:
        raise RuntimeError("--listen needs --port (the loopback and the network listener share it)")
    return str(ip)


def _serve(directory, lock_fd=None, port=None, listen=None):
    # The digest of the code this process LOADED, taken now and never again: an in-place reinstall changes the files
    # on disk, and a server that hashed them at request time would report the new release while running the old code.
    release = code_digest()
    # For a child, its launcher holds the startup lock until ctl.sock answers.
    gateway = Gateway()
    ctl_path = directory / "ctl.sock"
    stale_socket(ctl_path)
    redirects = redirect_directory(directory)
    sweep_redirects(directory)
    if redirects != directory:
        sweep_redirects(redirects)
    auth = Auth(directory=redirects, trust_file=state_dir() / "dashboard-trusted-browsers.json")
    # A wrong remote.json (or a front socket that cannot be opened) switches remote access off and says why
    # (`remote.not_configured` in /api/session); the loopback dashboard keeps serving: the person at the machine must
    # be able to see and fix it.
    remote_problem = None
    try:
        remote = remote_config(directory)
    except RuntimeError as exc:
        remote, remote_problem = None, str(exc)
    wanted = listen_port(directory, port=port)
    try:
        listener = bind_listener(wanted)
    except OSError as exc:
        if wanted and exc.errno == errno.EADDRINUSE:
            raise RuntimeError(
                f"port {wanted} is already in use; free it or set another in {directory / PORT_FILE}"
            ) from None
        raise
    listener.listen(128)
    port = listener.getsockname()[1]
    net_listener = None
    network = None
    if listen:
        try:
            addr = network_address(listen, wanted)
            net_listener = socket.socket(socket.AF_INET6 if ":" in addr else socket.AF_INET, socket.SOCK_STREAM)
            net_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            net_listener.bind((addr, port))
            net_listener.listen(128)
        except BaseException:
            listener.close()
            if net_listener is not None:
                net_listener.close()
            raise
        network = f"{addr}:{port}"
    front = None
    front_path = None
    net_server = net_thread = None
    try:
        if remote:
            try:
                front_path = front_socket_path(remote[1])
                front = bind_front(front_path, remote[1])
            except (RuntimeError, OSError) as exc:
                remote, remote_problem = (
                    None,
                    str(exc)
                    if "remote.not_configured" in str(exc)
                    else (f"remote.not_configured: the front socket could not be opened ({exc.__class__.__name__})"),
                )
        app = create_app(
            port,
            gateway=gateway,
            auth=auth,
            remote=remote[0] if remote else None,
            remote_problem=remote_problem,
            alt_hosts=(network,) if network else (),
            remote_streams=True,
        )
    except BaseException:
        listener.close()
        close_front(front, front_path)
        raise
    server = uvicorn.Server(server_config(app))
    front_server = front_thread = None
    if front is not None:
        front_server = FrontServer(server_config(Front(app), front=True))
    ctl = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        ctl.bind(str(ctl_path))
    except BaseException:
        ctl.close()
        listener.close()
        close_front(front, front_path)
        raise
    finally:
        os.umask(old)
    os.chmod(ctl_path, 0o600)
    # Linux hands a removed socket's inode number to the next one: its change time tells them apart.
    st = ctl_path.lstat()
    ctl_id = (st.st_ino, st.st_ctime_ns)
    retired = threading.Event()
    ctl.listen(8)
    ctl.settimeout(0.5)
    if lock_fd is not None:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def ipc():
        while not server.should_exit:
            try:
                conn, _ = ctl.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with conn:
                try:
                    conn.settimeout(2)
                    caller = peer_uid(conn)
                    if caller not in (os.getuid(), 0):
                        continue  # nobody else gets an answer
                    req = receive(conn)
                    if caller == 0 and req != {"command": "enroll"}:
                        continue  # root (sudo orch remote ...) may mint an enrollment code and do nothing else here
                    if req == {"command": "launch"}:
                        conn.sendall(encode({**auth.launch(port), "redirect": str(redirects), "network": network}))
                    elif req == {"command": "launch", "kind": "tunnel"}:
                        conn.sendall(
                            encode({**auth.launch(port, tunnel=True), "redirect": str(redirects), "network": network})
                        )
                    elif req == {"command": "launch", "kind": "setup"}:
                        conn.sendall(
                            encode({**auth.launch(port, setup=True), "redirect": str(redirects), "network": network})
                        )
                    elif req == {"command": "enroll"}:
                        # Root only: a same-UID process (an agent in tier A) could otherwise mint a code, register its
                        # own passkey and keep remote access. The code never goes to the person's own UID.
                        conn.sendall(
                            encode(mint_enrollment(app) if caller == 0 else {"error": "enroll.root_only: use sudo"})
                        )
                    elif req == {"command": "hello"}:
                        conn.sendall(encode({"release": release}))
                    elif req == {"command": "stop"}:
                        conn.sendall(encode({"ok": True}))
                        # Free the control socket now, not after the shutdown: until then a launcher would connect
                        # to a server that no longer answers (and time out) instead of starting a new one.
                        retire()
                        if setup_bound():
                            # r11-P01: setup's Finish starts the dashboard service, which stops this server while it
                            # still serves the setup page (and that very request): only the loopback server keeps
                            # serving, until setup ends; the network and front listeners stop now (r11-P01b).
                            exit_remote()
                            threading.Thread(target=exit_after_setup, daemon=True).start()
                        else:
                            exit_now()
                        break
                except Exception:
                    pass  # never log the launch payload

    setup_sock = state_dir() / "setup" / "setup.sock"

    def setup_bound():
        """A setup page is bound here and its setup process still answers on its socket (a real connect: a stale
        file, a socket nobody listens on or a slow one count as gone). Fail closed: any doubt ends the hold."""
        return auth.has_setup() and setup_alive(setup_sock)

    def exit_remote():
        if net_server is not None:
            net_server.should_exit = True
        if front_server is not None:
            front_server.should_exit = True

    def exit_now():
        server.should_exit = True
        exit_remote()

    def exit_after_setup():
        deadline = time.monotonic() + SETUP_HOLD
        try:
            while not server.should_exit and setup_bound() and time.monotonic() < deadline:
                time.sleep(SETUP_POLL)
        finally:  # whatever goes wrong in the check, the hold ends
            exit_now()

    def retire():
        """Close the control socket and remove its path if it is still ours (a new server may have bound it). Once."""
        if retired.is_set():
            return
        retired.set()
        ctl.close()
        with suppress(OSError):
            st = ctl_path.lstat()
            if stat.S_ISSOCK(st.st_mode) and (st.st_ino, st.st_ctime_ns) == ctl_id:
                ctl_path.unlink()

    thread = threading.Thread(target=ipc, daemon=True)
    thread.start()
    try:
        if front_server is not None:
            front_thread = threading.Thread(target=front_server.run, kwargs={"sockets": [front]}, daemon=True)
            front_thread.start()
            for _ in range(200):  # the front is up, or the start is refused (a half-open remote mode is not offered)
                if front_server.started or not front_thread.is_alive():
                    break
                time.sleep(0.05)
            if not front_server.started:
                # loopback keeps serving; remote access is off and says why. "Off" means off before loopback serves:
                # the front thread is joined and the remote policy leaves the listener registry (fail closed).
                front_server.should_exit = True
                front_thread.join(timeout=GRACE + 2)
                app.state.policies.pop(REMOTE, None)
                app.state.remote_problem = "remote.not_configured: the front socket did not start"
                close_front(front, front_path)
                front = front_server = front_thread = None
        if net_listener is not None:
            net_server = uvicorn.Server(server_config(app, front=True))
            net_thread = threading.Thread(target=net_server.run, kwargs={"sockets": [net_listener]}, daemon=True)
            net_thread.start()
        server.run(sockets=[listener])
    finally:
        if net_listener is not None and net_server is not None:
            net_server.should_exit = True
        server.should_exit = True
        if front_server is not None:
            front_server.should_exit = True
        retire()
        thread.join(timeout=3)
        if front_thread is not None:
            front_thread.join(timeout=GRACE + 2)
        close_front(front, front_path)
        listener.close()
        if net_thread is not None:
            net_thread.join(timeout=GRACE + 2)
        if net_listener is not None:
            net_listener.close()


def setup_alive(path, timeout=0.5):
    """The setup process listens on ``path``, with the same checks as the setup proxy (``connect_setup``: a 0600 socket
    of this user in a private folder, peer is this user), closed at once. Anything else or any error -> False."""
    try:
        connect_setup(path, timeout).close()
        return True
    except Exception:
        return False


def safe_path(value):
    parsed = urllib.parse.urlsplit(value)
    path = parsed.path or "/"
    if not path.startswith("/") or path.startswith("//") or "\\" in path or any(ord(c) < 32 for c in value):
        raise ValueError("expected a dashboard path or URL")
    return path + ("?" + parsed.query if parsed.query else "")


def verify_probe(launch, fetch=None):
    nonce = secrets.token_urlsafe(32)
    url = f"http://127.0.0.1:{launch['port']}/auth/probe?n={nonce}"
    expected = hmac.new(
        bytes.fromhex(launch["probe_key"]), (nonce + launch["instance"]).encode(), hashlib.sha256
    ).hexdigest()
    if fetch is None:
        # Never send the probe through environment proxies or follow redirects.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(url, timeout=2) as reply:
            actual = reply.read(129).decode("ascii")
    else:
        actual = fetch(url)
    if not hmac.compare_digest(actual, expected):
        raise RuntimeError("dashboard port ownership proof failed; browser was not opened")


#: second line on the --emit-url-fd pipe when no browser was (or could be) opened
NO_BROWSER_MARKER = b"no-browser"


def check_url_fd(fd):
    """The caller's pipe/socket for the code-bearing URL: not argv, not the environment, not a file. Anything else
    (a tty, a regular file) is refused, so the code cannot land in a log through a wrong fd."""
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        mode = 0
    if fd < 3 or not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
        raise RuntimeError("--emit-url-fd needs an open pipe")


def _running_release(ctl):
    """The release digest of the server on ``ctl``; ``None`` when none is running (nothing there / refused);
    ``"?"`` when one answers but does not know ``hello`` (an older release closes the connection)."""
    try:
        reply = control(ctl, "hello")
    except (FileNotFoundError, ConnectionRefusedError):
        return None
    except EOFError:
        return "?"
    release = reply.get("release") if isinstance(reply, dict) else None
    return release if isinstance(release, str) and release else "?"


def _stop_stale(ctl, wait=RESTART_WAIT):
    """Stop the server on ``ctl`` through its own ``stop`` command (never a signal), wait (bounded) until the socket
    is free, then say so. A server that is already gone is fine."""
    try:
        control(ctl, "stop")
    except (FileNotFoundError, ConnectionRefusedError, EOFError):
        pass
    except TimeoutError:
        raise RuntimeError(
            "an older dashboard is running but does not answer; find it with `pgrep -fl orch_dashboard` and stop it"
        ) from None
    deadline = time.monotonic() + wait
    while not _free(ctl):
        if time.monotonic() >= deadline:
            raise RuntimeError("the older dashboard did not stop in time; try again in a moment")
        time.sleep(0.1)
    print("restarted the dashboard on the new release")


def mint_enrollment(app):
    """The reply to ctl ``enroll`` from root: a fresh one-time enrollment code (R1-5) for the configured name, or the
    reason remote access is off. Never logged: the code goes back over ctl.sock to the one root process that asked."""
    service = getattr(app.state, "passkeys", None)
    if service is None or app.state.enroll is None or app.state.remote_problem:
        return {"error": app.state.remote_problem or "remote.not_configured: remote access is off (orch remote enable)"}
    code = app.state.enroll.mint()
    return {"code": code, "name": service.rp_id, "expires_at": int(time.time()) + ENROLL_TTL}


def start_dashboard():
    """`orch-dashboard up`: make sure a server runs (it reads remote.json at start). Mints nothing."""
    refuse_agent()
    directory = state_dir() / "dashboard"
    run_dir(state_dir())
    with private_directory(directory) as fd, locked(fd):
        control_or_start(directory, "hello")


def control_or_start(directory, command, kind=None):
    """The reply to ``command`` from the dashboard server of ``directory``, starting one when none runs and replacing
    one of another release. Call while holding the startup lock."""
    ctl = directory / "ctl.sock"
    launch = None
    try:
        running = _running_release(ctl)
        if running is None:
            raise FileNotFoundError(errno.ENOENT, "no dashboard running")
        if running != code_digest():
            _stop_stale(ctl)  # raises RuntimeError when it will not stop; prints the one-line notice
        else:
            launch = control(ctl, command, kind)
    except EOFError:
        # The server closed the connection: it does not know this request (an older release). Replace it, once.
        _stop_stale(ctl)
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
            raise
    if launch is None:
        stale_socket(ctl)
        child = subprocess.Popen(
            [sys.executable, "-P", "-m", "orch_dashboard", "serve", "--launcher-lock"],
            env=childenv.sanitize(os.environ, absolute_pythonpath=True),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(100):
            if child.poll() is not None:
                raise RuntimeError("dashboard could not start")
            try:
                launch = control(ctl, command, kind)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(0.05)
            except EOFError:
                raise RuntimeError("the new dashboard closed the connection; run it again") from None
        else:
            raise RuntimeError("dashboard did not become ready")
    return launch


def open_dashboard(path="/", print_url=False, emit_url_fd=None, no_browser=False, network=False):
    refuse_agent()  # before anything is started or minted
    path = safe_path(path)
    kind = "setup" if path == "/setup" else None  # setup's nonce opens no normal session
    headless = is_headless()
    if network:
        kind = kind or "tunnel"  # a 5 min code: it is opened on another computer
    elif kind is None and print_url and headless:
        kind = "tunnel"  # a 5 min code: the person needs time to open the tunnel
    if emit_url_fd is not None:
        check_url_fd(emit_url_fd)  # before anything is started or minted
    if no_browser and emit_url_fd is None:
        raise RuntimeError("--no-browser needs --emit-url-fd")
    if print_url and not sys.stdout.isatty():
        raise RuntimeError("--print-url requires a terminal")  # before anything is started or minted
    run_dir(state_dir())
    directory = state_dir() / "dashboard"
    with private_directory(directory) as fd, locked(fd):
        launch = control_or_start(directory, "launch", kind)
    # Control can become ready just before the HTTP accept loop.
    for attempt in range(20):
        try:
            verify_probe(launch)
            break
        except (urllib.error.URLError, TimeoutError):
            if attempt == 19:
                raise
            time.sleep(0.05)
    fragment = urllib.parse.urlencode({"launch": launch["code"], "to": path})
    url = f"http://127.0.0.1:{launch['port']}/#{fragment}"
    if network:
        if not launch.get("network"):
            raise RuntimeError("this dashboard has no network address (it was started without --listen)")
        url = f"http://{launch['network']}/#{fragment}"
    if print_url:
        left = math.ceil(float(launch["expires_at"]) - time.time())  # what the server really granted
        print(
            f"Single-use dashboard link; expires in {left // 60} min."
            if left >= 60
            else f"Single-use dashboard link; expires in {max(left, 0)} s."
        )
        if headless and not network:
            print(f"On your own computer, first run:  {tunnel_line(launch['port'])}")
            print("then open this link in its browser:")
        print(url)
        return
    if emit_url_fd is not None:
        os.write(emit_url_fd, (url + "\n").encode())
        if no_browser:
            os.write(emit_url_fd, NO_BROWSER_MARKER + b"\n")
            return
    # Write exactly the file the server recorded for this code, so its exchange and timer delete that same path.
    if launch.get("redirect_file"):
        redirect = Path(launch["redirect_file"])
        # Only the server's own hashed name, in the private dashboard directory or an `orch-dashboard` runtime
        # directory (the server's XDG_RUNTIME_DIR may differ from ours); private_directory() below also requires
        # it to be ours, 0700 and not a symlink. Anything else would put the code where nothing deletes it.
        expected = Auth(directory=redirect.parent).redirect_path(launch["code"]).name
        if not (redirect.parent == directory or redirect.parent.name == "orch-dashboard") or redirect.name != expected:
            raise SystemExit("orch-dashboard: the server named an unexpected launch file; not writing the code")
        directory = redirect.parent
    else:
        directory = Path(launch["redirect"]) if launch.get("redirect") else redirect_directory(directory)
        redirect = Auth(directory=directory).redirect_path(launch["code"])
    # The server's timer invalidates the code and unlinks its file at `expires_at`. A launcher paused past it must not
    # write (or keep) a file afterwards: the code in it would be dead, but nothing would delete it before a restart.
    expires_at = float(launch.get("expires_at", 0))
    if time.time() >= expires_at - EXPIRY_MARGIN:
        raise RuntimeError("the launch code expired before the browser could be opened; run it again")
    with private_directory(directory, parents=False) as directory_fd:
        fd = os.open(redirect.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as file:
                file.write(
                    '<!doctype html><meta name="referrer" content="no-referrer">'
                    # ONE navigation: the script; the meta refresh only without JavaScript (both at once load the
                    # page twice, and the second load presents an already spent single-use code)
                    f"<script>location.replace({json.dumps(url)});</script>"
                    f'<noscript><meta http-equiv="refresh" content="0;url={html.escape(url, quote=True)}"></noscript>'
                )
            if time.time() >= expires_at - EXPIRY_MARGIN:  # paused between the check and the write
                raise RuntimeError("the launch code expired before the browser could be opened; run it again")
            # Only the file's path goes to the browser's argv, never the code: argv is readable by every UID (ps).
            if not webbrowser.open(redirect.resolve().as_uri()):
                if emit_url_fd is not None:  # the caller already has the link: say the browser did not open
                    redirect.unlink(missing_ok=True)
                    os.write(emit_url_fd, NO_BROWSER_MARKER + b"\n")
                    return
                raise RuntimeError("no browser could be opened; use --print-url in your own terminal")
        except BaseException:
            redirect.unlink(missing_ok=True)
            raise


def _free(path) -> bool:
    """Is the control socket free for a new server: gone, or no one accepts on it?"""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError):
            return True
        except OSError:
            return False
    return False


def restart_dashboard(path="/", print_url=False, wait=RESTART_WAIT):
    """`orch-dashboard restart`: stop the running server (if any), wait until it has let go of its control socket,
    then open a new one. A server that does not answer `stop` (an older, hung one) is named, not killed."""
    ctl = state_dir() / "dashboard" / "ctl.sock"
    try:
        control(ctl, "stop")
    except (FileNotFoundError, ConnectionRefusedError):
        pass  # none running: just open
    except TimeoutError:
        raise RuntimeError(
            "a dashboard is running but does not answer; find it with `pgrep -fl orch_dashboard` and stop it with kill"
        ) from None
    deadline = time.monotonic() + wait
    while not _free(ctl):
        if time.monotonic() >= deadline:
            raise RuntimeError("the running dashboard did not stop in time; try again in a moment")
        time.sleep(0.1)
    open_dashboard(path, print_url=print_url)


def main():
    if sys.argv[1:2] == ["addon-manifest"]:
        from .manifest import main as manifest_main

        return manifest_main(sys.argv[2:])
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", nargs="?", choices=["open", "launch", "serve", "stop", "restart", "up"], default="open"
    )
    parser.add_argument("path", nargs="?", default="/")
    parser.add_argument(
        "--print-url",
        action="store_true",
        help="print a single-use URL to a terminal (30 s; 5 min on a headless machine, with the ssh -L line)",
    )
    parser.add_argument("--emit-url-fd", type=int, help=argparse.SUPPRESS)  # the caller's pipe gets the URL
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--port", type=int, help="serve on this loopback port (default: the port file, else fixed when headless)"
    )
    parser.add_argument("--listen", help="serve: also listen on this literal IP (same --port), plain http (dev)")
    parser.add_argument("--network", action="store_true", help="open/--print-url: a link for the --listen address")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--launcher-lock", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if not (args.stop or args.command == "stop"):
            refuse_agent()  # stop passes: it mints nothing and only ends the person's own server
        if args.command == "serve" and not args.stop:
            return serve(args.launcher_lock, port=args.port, listen=args.listen)
        if args.stop or args.command == "stop":
            control(state_dir() / "dashboard/ctl.sock", "stop")
        elif args.command == "up":
            start_dashboard()
        elif args.command == "restart":
            restart_dashboard(args.path, print_url=args.print_url)
        else:
            open_dashboard(
                args.path,
                print_url=args.print_url,
                emit_url_fd=args.emit_url_fd,
                no_browser=args.no_browser,
                network=args.network,
            )
    except AgentRefused as exc:
        print(f"orch-dashboard: {exc}", file=sys.stderr)
        return 3
    except RunDirectoryError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except EOFError:
        print("orch-dashboard: the dashboard closed the connection; run `orch-dashboard restart`", file=sys.stderr)
        return 1
    except (RuntimeError, OSError) as exc:
        # A one-line refusal for the person at the terminal; nothing was started or minted on the refusals above.
        print(f"orch-dashboard: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
