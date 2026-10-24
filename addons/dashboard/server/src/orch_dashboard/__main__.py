"""Private IPC launch, one random loopback listener, and port-ownership proof."""

import argparse
import errno
import fcntl
import functools
import getpass
import hashlib
import hmac
import html
import json
import os
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

from .app import create_app
from .auth import CODE_TTL, SETUP_CODE_TTL, TUNNEL_CODE_TTL, Auth
from .gateway import Gateway, RunDirectoryError, run_dir, state_dir
from .hostlink import check_uid, encode, receive

#: seconds a stopping server waits for open connections (a tab's live stream) before it cuts them
GRACE = 3
#: seconds `restart` waits for the old server to free its control socket
RESTART_WAIT = 10.0
AGENT_ENV = ("ORCH_GRANT", "ORCH_SESSION")
# Seconds before the code's expiry after which the launcher writes no redirect file (and removes one it just wrote):
# covers the gap between the server's monotonic timer and the wall clock both sides read.
EXPIRY_MARGIN = 1
#: the port a headless machine listens on unless told otherwise, so the printed `ssh -L` line stays true across
#: restarts (#1198 R1-1). A desktop keeps a random port: nobody types it. Loopback only in both cases.
HEADLESS_PORT = 47821
#: file in the dashboard state directory that fixes the port (one decimal number), on any machine
PORT_FILE = "port"


class AgentRefused(RuntimeError):
    """``human_only``: the dashboard acts as the person, so it never opens or serves under an agent's environment."""

    def __init__(self):
        super().__init__(
            "human_only: the dashboard is a person's; an agent never opens it "
            "(unset ORCH_GRANT and ORCH_SESSION in your own terminal)"
        )


def refuse_agent(environ=None):
    """Refuse (#542 item 1) when ORCH_GRANT or ORCH_SESSION is set and non-empty: the same code, exit and message
    as core's ``orch dashboard`` (and ``orch sign``'s ``human_only``). A speed bump in tier A (same UID), not a
    boundary."""
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


def tunnel_line(port, environ=None, user=None, host=None):
    """The command to run on the person's own computer: ``ssh -L <port>:127.0.0.1:<port> <user>@<host>``. The host is
    the address this very SSH session reached us on when there is one, else this machine's name."""
    environ = os.environ if environ is None else environ
    if host is None:
        parts = (environ.get("SSH_CONNECTION") or "").split()
        host = parts[2] if len(parts) == 4 else ""
        if not _plain(host):
            host = socket.getfqdn() or socket.gethostname()
    user = user or getpass.getuser()
    if not (_plain(host) and _plain(user)):
        raise RuntimeError("cannot name this machine or user for an ssh command; build the tunnel line yourself")
    return f"ssh -L {port}:127.0.0.1:{port} {user}@{host}"


def _plain(value):
    return bool(value) and all(c.isalnum() or c in "-_.:" for c in value)


def serve(lock_held=False, port=None):
    refuse_agent()
    run_dir(state_dir())
    directory = state_dir() / "dashboard"
    with private_directory(directory) as fd:
        if not lock_held:
            with locked(fd) as lock_fd:
                return _serve(directory, lock_fd=lock_fd, port=port)
        return _serve(directory, port=port)


def _serve(directory, lock_fd=None, port=None):
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
    auth = Auth(directory=redirects)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    wanted = listen_port(directory, port=port)
    try:
        listener.bind(("127.0.0.1", wanted))
    except OSError as exc:
        if wanted and exc.errno == errno.EADDRINUSE:
            raise RuntimeError(
                f"port {wanted} is already in use; free it or set another in {directory / PORT_FILE}"
            ) from None
        raise
    listener.listen(128)
    port = listener.getsockname()[1]
    config = uvicorn.Config(
        create_app(port, gateway=gateway, auth=auth),
        proxy_headers=False,
        access_log=False,
        log_level="warning",
        # The terminal WebSocket (section 12.6.6): max_size 64 KiB, no permessage-deflate, no subprotocol.
        ws="websockets",
        ws_max_size=65536,
        ws_per_message_deflate=False,
        # A browser tab's live stream never ends by itself: after `stop`, cut open connections after GRACE seconds.
        timeout_graceful_shutdown=GRACE,
    )
    server = uvicorn.Server(config)
    ctl = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        ctl.bind(str(ctl_path))
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
                    check_uid(conn, os.getuid())
                    req = receive(conn)
                    if req == {"command": "launch"}:
                        conn.sendall(encode({**auth.launch(port), "redirect": str(redirects)}))
                    elif req == {"command": "launch", "kind": "tunnel"}:
                        conn.sendall(encode({**auth.launch(port, tunnel=True), "redirect": str(redirects)}))
                    elif req == {"command": "launch", "kind": "setup"}:
                        conn.sendall(encode({**auth.launch(port, setup=True), "redirect": str(redirects)}))
                    elif req == {"command": "hello"}:
                        conn.sendall(encode({"release": release}))
                    elif req == {"command": "stop"}:
                        conn.sendall(encode({"ok": True}))
                        server.should_exit = True
                        # Free the control socket now, not after the shutdown: until then a launcher would connect
                        # to a server that no longer answers (and time out) instead of starting a new one.
                        retire()
                        break
                except Exception:
                    pass  # never log the launch payload

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
        server.run(sockets=[listener])
    finally:
        server.should_exit = True
        retire()
        thread.join(timeout=3)
        listener.close()


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


def open_dashboard(path="/", print_url=False, emit_url_fd=None, no_browser=False):
    refuse_agent()  # before anything is started or minted
    path = safe_path(path)
    kind = "setup" if path == "/setup" else None  # setup's nonce opens no normal session
    headless = is_headless()
    if kind is None and print_url and headless:
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
        ctl = directory / "ctl.sock"
        launch = None
        try:
            running = _running_release(ctl)
            if running is None:
                raise FileNotFoundError(errno.ENOENT, "no dashboard running")
            if running != code_digest():
                _stop_stale(ctl)  # raises RuntimeError when it will not stop; prints the one-line notice
            else:
                launch = control(ctl, "launch", kind)
        except EOFError:
            # The server closed the connection: it does not know this request (an older release). Replace it, once.
            _stop_stale(ctl)
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                raise
        if launch is None:
            stale_socket(ctl)
            child = subprocess.Popen(
                [sys.executable, "-m", "orch_dashboard", "serve", "--launcher-lock"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            for _ in range(100):
                if child.poll() is not None:
                    raise RuntimeError("dashboard could not start")
                try:
                    launch = control(ctl, "launch", kind)
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    time.sleep(0.05)
                except EOFError:
                    raise RuntimeError("the new dashboard closed the connection; run it again") from None
            else:
                raise RuntimeError("dashboard did not become ready")
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
    if print_url:
        ttl = CODE_TTL if kind is None else TUNNEL_CODE_TTL  # setup's nonce lives as long as the tunnel one
        print(
            f"Single-use dashboard link; expires in {ttl // 60} min."
            if ttl >= 60
            else f"Single-use dashboard link; expires in {ttl} s."
        )
        if headless:
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
    parser.add_argument("command", nargs="?", choices=["open", "launch", "serve", "stop", "restart"], default="open")
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
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--launcher-lock", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if not (args.stop or args.command == "stop"):
            refuse_agent()  # stop passes: it mints nothing and only ends the person's own server
        if args.command == "serve" and not args.stop:
            return serve(args.launcher_lock, port=args.port)
        if args.stop or args.command == "stop":
            control(state_dir() / "dashboard/ctl.sock", "stop")
        elif args.command == "restart":
            restart_dashboard(args.path, print_url=args.print_url)
        else:
            open_dashboard(
                args.path, print_url=args.print_url, emit_url_fd=args.emit_url_fd, no_browser=args.no_browser
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
