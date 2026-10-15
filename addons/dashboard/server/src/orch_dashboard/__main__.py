"""Private IPC launch, one random loopback listener, and port-ownership proof."""

import argparse
import errno
import fcntl
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
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from .app import create_app
from .auth import CODE_TTL, Auth
from .gateway import Gateway, RunDirectoryError, run_dir, state_dir
from .hostlink import check_uid, encode, receive

AGENT_ENV = ("ORCH_GRANT", "ORCH_SESSION")


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


def control(path, command):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(str(path))
        check_uid(sock, os.getuid())
        sock.sendall(encode({"command": command}))
        return receive(sock)


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
        except OSError as exc:
            if exc.errno != errno.ECONNREFUSED:
                raise
        else:
            raise RuntimeError("dashboard already running")
    path.unlink()


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
        if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and now - st.st_mtime >= CODE_TTL:
            path.unlink(missing_ok=True)


def serve(lock_held=False):
    refuse_agent()
    run_dir(state_dir())
    directory = state_dir() / "dashboard"
    with private_directory(directory) as fd:
        if not lock_held:
            with locked(fd) as lock_fd:
                return _serve(directory, lock_fd=lock_fd)
        return _serve(directory)


def _serve(directory, lock_fd=None):
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
    listener.bind(("127.0.0.1", 0))
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
    )
    server = uvicorn.Server(config)
    ctl = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        ctl.bind(str(ctl_path))
    finally:
        os.umask(old)
    os.chmod(ctl_path, 0o600)
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
                    elif req == {"command": "stop"}:
                        conn.sendall(encode({"ok": True}))
                        server.should_exit = True
                except Exception:
                    pass  # never log the launch payload

    thread = threading.Thread(target=ipc, daemon=True)
    thread.start()
    try:
        server.run(sockets=[listener])
    finally:
        server.should_exit = True
        ctl.close()
        thread.join(timeout=3)
        listener.close()
        ctl_path.unlink(missing_ok=True)


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


def open_dashboard(path="/", print_url=False):
    refuse_agent()  # before anything is started or minted
    path = safe_path(path)
    if print_url and not sys.stdout.isatty():
        raise RuntimeError("--print-url requires a terminal")  # before anything is started or minted
    run_dir(state_dir())
    directory = state_dir() / "dashboard"
    with private_directory(directory) as fd, locked(fd):
        try:
            launch = control(directory / "ctl.sock", "launch")
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                raise
            stale_socket(directory / "ctl.sock")
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
                    launch = control(directory / "ctl.sock", "launch")
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    time.sleep(0.05)
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
        print("Single-use dashboard link; expires in 30 s.")
        print(url)
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
    with private_directory(directory, parents=False) as directory_fd:
        fd = os.open(redirect.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as file:
                file.write(
                    '<!doctype html><meta name="referrer" content="no-referrer">'
                    '<meta http-equiv="Cache-Control" content="no-store">'
                    f'<meta http-equiv="refresh" content="0;url={html.escape(url, quote=True)}">'
                    f"<script>location.replace({json.dumps(url)});</script>"
                )
            # Only the file's path goes to the browser's argv, never the code: argv is readable by every UID (ps).
            if not webbrowser.open(redirect.resolve().as_uri()):
                raise RuntimeError("no browser could be opened; use --print-url in your own terminal")
        except BaseException:
            redirect.unlink(missing_ok=True)
            raise


def main():
    if sys.argv[1:2] == ["addon-manifest"]:
        from .manifest import main as manifest_main

        return manifest_main(sys.argv[2:])
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=["open", "launch", "serve", "stop"], default="open")
    parser.add_argument("path", nargs="?", default="/")
    parser.add_argument("--print-url", action="store_true", help="print a single-use 30 s URL to a terminal")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--launcher-lock", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if not (args.stop or args.command == "stop"):
            refuse_agent()  # stop passes: it mints nothing and only ends the person's own server
        if args.command == "serve" and not args.stop:
            return serve(args.launcher_lock)
        if args.stop or args.command == "stop":
            control(state_dir() / "dashboard/ctl.sock", "stop")
        else:
            open_dashboard(args.path, print_url=args.print_url)
    except AgentRefused as exc:
        print(f"orch-dashboard: {exc}", file=sys.stderr)
        return 3
    except RunDirectoryError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (RuntimeError, OSError) as exc:
        # A one-line refusal for the person at the terminal; nothing was started or minted on the refusals above.
        print(f"orch-dashboard: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
