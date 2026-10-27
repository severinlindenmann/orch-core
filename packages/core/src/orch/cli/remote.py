"""``orch remote enable|disable|reset``: the HTTPS front of the remote dashboard, Linux with sudo (#1198 R1-7).

``enable --name orch.example.com`` makes the dashboard reachable at ``https://<name>`` with passkey login (host spec
0.8.6 10, design-r1-remote-dashboard). The person runs it as themselves, never as root. It

1. refuses an agent's environment, root, and anything but Linux;
2. checks the name and that it points to this server, and that ports 80 and 443 are free here (nothing is changed
   until this passes: ``remote.name_mismatch``);
3. prints exactly what will change (the root steps are the dry run of the same code root will run), and asks for a
   confirm on the person's own terminal (``--dry-run`` stops here, ``--yes`` is for scripts);
4. runs the root steps through ``sudo`` and the root-owned trusted copy ``/usr/lib/orch/os-setup/orch-os-setup``
   (never the person's release as root): the user ``orch-front``, the socket directory, the Caddyfile, the unit;
5. writes ``<state>/dashboard/remote.json`` (name and the front's user id), restarts the dashboard so it opens the
   front socket, and has root (sudo) ask it for the first enrollment link: the dashboard hands a code to uid 0 only.
   The link is printed on this terminal by the root copy and is never in argv, a file or a log.

``disable`` takes access away (remote.json, the dashboard, the front) and keeps the ``orch-front`` user and its
certificates. ``reset`` drops every passkey and every remote session and prints a new enrollment link.
The Settings page does the same with plain clicks later (R1-8); this is the backup.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from orch.host.errors import HostError
from orch.posixmod import posixmod
from orch.setup import osfront
from orch.setup.oslauncher import LAUNCHER_PATH

pwd = posixmod("pwd")

__all__ = ["main"]

#: the fixed port of a headless dashboard (orch_dashboard.__main__.HEADLESS_PORT), for the printed ssh line
HEADLESS_PORT = 47821
SUDO = "/usr/bin/sudo"
PYTHON = "/usr/bin/python3"
AGENT_ENV = ("ORCH_GRANT", "ORCH_SESSION")
EXIT_REFUSED = 3


class Refused(Exception):
    def __init__(self, code: str, message: str, exit_: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.code, self.exit = code, exit_


def _resolve(name: str) -> set[str]:
    infos = socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
    return {str(i[4][0]).split("%", 1)[0] for i in infos}


def _is_local(address: str) -> bool:
    """Whether ``address`` is on one of this machine's interfaces: binding to it (an ephemeral port, closed at once)
    works only then."""
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with contextlib.closing(socket.socket(family, socket.SOCK_STREAM)) as sock:
        try:
            sock.bind((address, 0))
        except OSError:
            return False
    return True


def _port_busy(port: int) -> bool:
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            continue
    return False


def _run(argv: Sequence[str], env: Mapping[str, str]) -> int:
    """Run ``argv`` with this terminal's stdin, stdout and stderr (sudo's password prompt, the dashboard's link)."""
    return subprocess.run(list(argv), env=dict(env), check=False).returncode


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# ---------------------------------------------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch remote", description="reach the dashboard over HTTPS with a passkey")
    sub = p.add_subparsers(dest="cmd", required=True)
    en = sub.add_parser("enable", help="set up the HTTPS front (Linux, needs sudo) and print the first passkey link")
    en.add_argument("--name", required=True, help="the public DNS name of this server, such as orch.example.com")
    en.add_argument("--port", type=int, help="the fixed loopback port of the dashboard (for the SSH fallback)")
    en.add_argument(
        "--trusted-proxy",
        action="append",
        default=[],
        metavar="NETWORK",
        help="a proxy network in front of this server whose forwarded client address counts (repeatable)",
    )
    en.add_argument(
        "--public-ip",
        action="append",
        default=[],
        metavar="ADDRESS",
        help="this server's public address when it is not on the machine itself (cloud NAT); the name must point to it",
    )
    dis = sub.add_parser("disable", help="take remote access away")
    rst = sub.add_parser("reset", help="drop every passkey and session, print a new link")
    for sp in (en, dis, rst):
        sp.add_argument("--dry-run", action="store_true", help="print exactly what would change and stop")
        sp.add_argument("--yes", action="store_true", help="do not ask (scripts); the changes are still printed")
    return p


def _state(env: Mapping[str, str]) -> Path:
    from orch.ops.runtime import state_dir

    return state_dir(env)


def _dashboard_dir(env: Mapping[str, str], *, create: bool) -> Path:
    from orch.host.custody import ensure_private_dir

    state = _state(env)
    try:
        ensure_private_dir(state, os.getuid(), create=create)
        return ensure_private_dir(state / "dashboard", os.getuid(), create=create)
    except HostError as exc:
        if exc.code != "not_found":
            raise Refused("custody.unsafe_mode", f"the orch state directory is not safe: {exc}") from None
        return state / "dashboard"  # nothing there yet: remote access has never been on


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _is_root() -> bool:
    return os.geteuid() == 0


def _uid() -> int:
    return os.getuid()


def _person() -> str:
    return pwd.getpwuid(_uid()).pw_name


def _preconditions(env: Mapping[str, str]) -> None:
    if any(env.get(name) for name in AGENT_ENV):
        raise Refused("human_only", "remote access is a person's decision; an agent never changes it")
    if not _is_linux():
        raise Refused("unsupported.platform", "the HTTPS front is set up on Linux (a server) for now; nothing changed")
    if _is_root():
        raise Refused(
            "invalid.input",
            "orch does not run as root. Run this as the normal user that runs orch (sudo is asked for when needed)",
        )


def _approve(args: argparse.Namespace, out: TextIO, question: str) -> bool:
    if args.dry_run:
        out.write("Dry run: nothing was changed.\n")
        return False
    if args.yes:
        return True
    from orch.ops.errors import OrchError
    from orch.ops.human import review_prompt
    try:
        return review_prompt(question)
    except OrchError as exc:
        raise Refused(exc.code, str(exc)) from None


def _run_root(flags: list[str], env: Mapping[str, str]) -> int:
    """``sudo python3 -I -S <trusted copy> <flags>``. Root runs the root-owned copy only."""
    if not os.path.exists(LAUNCHER_PATH):
        raise Refused(
            "unsupported.platform",
            f"{LAUNCHER_PATH} is not installed. The installer puts it there (orch update / the install command)",
        )
    return _run([SUDO, PYTHON, "-I", "-S", LAUNCHER_PATH, *flags], env)


def _root_step(flags: list[str], env: Mapping[str, str], failed: str = "the dashboard was not changed") -> None:
    """The root part of enable and disable; a failure stops the run. ``failed`` says what is true afterwards."""
    rc = _run_root(flags, env)
    if rc != 0:
        raise Refused("internal", f"the root step failed (exit {rc}); {failed}", 1)


def _write_private(path: Path, body: bytes) -> None:
    """Replace ``path`` with ``body``, never readable by others at any moment (mkstemp makes the file 0600)."""
    fd, tmp = tempfile.mkstemp(prefix=".new-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _front_uid() -> int:
    """The id of the user ``orch-front`` the root step made, checked again here (the same rule as
    ``osfront._forbidden``, which runs as root; this copy runs as the person): never the person, root, or an agent
    or add-on user (the dashboard refuses such a file at start; better not to write it)."""
    try:
        entry = pwd.getpwnam(osfront.FRONT_USER)
    except KeyError:
        raise Refused("internal", f"the user '{osfront.FRONT_USER}' does not exist after the root step", 1) from None
    names = [e.pw_name for e in pwd.getpwall() if e.pw_uid == entry.pw_uid]
    if entry.pw_uid in (0, _uid()) or any(n.startswith(("orch-agent-", "orch-addon-")) for n in names):
        raise Refused(
            "internal", f"the user '{osfront.FRONT_USER}' is not usable as the front; remote access stays off", 1
        )
    return entry.pw_uid


def _stop_dashboard(env: Mapping[str, str], err: TextIO, directory: Path) -> None:
    """End the running dashboard (every session and open link with it) and wait until its control socket is free."""
    from orch.cli.dashboard import INSTALL_HINT, find_entry_point

    command = find_entry_point(env, err)
    if command is None:
        raise Refused("unsupported.platform", INSTALL_HINT)
    _run([*command, "stop"], env)  # none running is fine
    ctl = directory / "ctl.sock"
    for _ in range(100):
        if not ctl.exists() or _no_answer(ctl):
            _sleep(0.5)  # the old server still lets go of its ports after it closed the control socket
            return
        _sleep(0.1)
    raise Refused("host.busy", "the dashboard did not stop in time; try again in a moment", 1)


def _no_answer(path: Path) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError):
            return True
        except OSError:
            return False
    return False


def _start_dashboard(env: Mapping[str, str], err: TextIO) -> bool:
    """Start the dashboard again (it reads remote.json at start). Never raises: it runs after the teardown, which a
    failed start must not skip or undo. A failure is said; returns whether it started."""
    from orch.cli.dashboard import find_entry_point

    command = find_entry_point(env, err)
    if command is None or _run([*command, "up"], env) != 0:
        err.write("orch remote: the dashboard did not start again; start it with: orch dashboard up\n")
        return False
    return True


def _enroll(env: Mapping[str, str], err: TextIO) -> int:
    """Start the dashboard (it reads remote.json), then have ROOT ask it for the first link: the dashboard hands an
    enrollment code to uid 0 only (a same-UID agent must not mint one). The root copy prints the link straight to this
    terminal; it is never read back here, never in argv, a file or a log."""
    from orch.cli.dashboard import INSTALL_HINT, find_entry_point

    command = find_entry_point(env, err)
    if command is None:
        raise Refused("unsupported.platform", INSTALL_HINT)
    rc = _run([*command, "up"], env)
    if rc != 0:
        return rc
    ctl = _dashboard_dir(env, create=False) / "ctl.sock"
    return _run_root(["--front-enroll", "--person", _person(), "--ctl", str(ctl)], env)


def _tunnel_lines(name: str, env: Mapping[str, str]) -> list[str]:
    port = HEADLESS_PORT
    with contextlib.suppress(OSError, ValueError):
        port = int((_state(env) / "dashboard" / "port").read_text().strip())
    return [
        "If you are locked out, the link ran out, or you prefer SSH, the SSH tunnel always works (no passkey needed).",
        "On your computer, run:",
        f"  ssh -o ExitOnForwardFailure=yes -L {port}:127.0.0.1:{port} {_person()}@{name}",
        "Then, on the server (in that SSH session), run:  orch dashboard open --print-url",
        "and open the link it prints in your computer's browser.",
        "For a new sign-in link, run on the server:  orch remote reset",
    ]


# ---------------------------------------------------------------------------------------------------------------
def _indent(res: Any) -> str:
    """The root plan's report as indented lines (without its own heading)."""
    lines = res.report().splitlines()
    if lines and lines[0] in ("Would do:", "Done:"):
        lines = lines[1:]
    return "".join(f"  {line.strip()}\n" for line in lines if line.strip())


def _enable(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    from orch.setup.osapply import DryRunExecutor
    from orch.setup.osfacts import RealOsFacts

    _preconditions(env)
    bad = osfront.valid_name(args.name)
    if bad:
        raise Refused("invalid.input", bad, 5)
    proxies = []
    for text in args.trusted_proxy:
        net = osfront.valid_proxy(text)
        if net is None:
            raise Refused("invalid.input", f"'{text}' is not a proxy network (never 0.0.0.0/0 or ::/0)", 5)
        proxies.append(net)
    if len(proxies) > osfront.MAX_PROXIES:
        raise Refused("invalid.input", "at most 16 trusted proxies", 5)
    if args.port is not None and not 1024 <= args.port <= 65535:
        raise Refused("invalid.input", "the dashboard port must be between 1024 and 65535", 5)
    public = []
    for text in args.public_ip:
        try:
            public.append(str(ipaddress.ip_address(text)))
        except ValueError:
            raise Refused("invalid.input", f"'{text}' is not an IP address", 5) from None
    _check_name_points_here(args.name, public)
    _check_ports_free()
    person = _person()
    plan = osfront.FrontPlan(args.name, person, _uid(), tuple(proxies))
    root_plan = osfront.apply_enable(RealOsFacts(), DryRunExecutor(), plan, is_root=True)
    if not root_plan.ok:
        out.write("This is not possible yet:\n")
        out.write("".join(f"  {p}\n" for p in root_plan.problems))
        out.write("Nothing was changed.\n")
        return 1
    out.write(f"orch remote enable: https://{args.name}\n\n")
    out.write(f"Checked: {args.name} points to this server; ports 80 and 443 are free here.\n\n")
    out.write("With sudo (your password is asked for):\n")
    out.write(_indent(root_plan))
    out.write("\nAs you:\n")
    out.write("  save the secure web address and the web server's user in <state>/dashboard/remote.json\n")
    if args.port is not None:
        out.write(f"  save port {args.port} for the SSH fallback in <state>/dashboard/port\n")
    out.write("  restart the dashboard so it opens the secure web address (a running dashboard is stopped)\n")
    out.write(
        "  print a one-time first sign-in link (works once, for 15 minutes); sudo asks the dashboard for it,\n"
        "  because only the administrator may\n\n"
    )
    if not _approve(args, out, "Make these changes?"):
        return 0 if args.dry_run else 1
    flags = ["--front-enable", "--person", person, "--name", args.name]
    for net in proxies:
        flags += ["--trusted-proxy", net]
    _root_step(flags, env)
    uid = _front_uid()
    directory = _dashboard_dir(env, create=True)
    if args.port is not None:
        _write_private(directory / "port", f"{args.port}\n".encode())
    _write_private(
        directory / "remote.json", json.dumps({"name": args.name, "front_uid": uid}, sort_keys=True).encode()
    )
    _stop_dashboard(env, err, directory)
    out.write(f"\norch is reachable at https://{args.name}\n")
    out.flush()
    rc = _enroll(env, err)
    if rc != 0:
        out.write("The sign-in link could not be made. Run `orch remote reset` to get one.\n")
    out.write("Send the link to the person who will use orch. The first browser to open it wins; it works once.\n")
    out.write("\n".join(_tunnel_lines(args.name, env)) + "\n")
    out.write("After a reboot remote access stays down until the dashboard is started again (orch dashboard up).\n")
    out.write(
        "Tip: add a CAA DNS record for your domain (letsencrypt.org) so only that company may issue its certificate.\n"
    )
    return 0 if rc == 0 else 1


def _check_name_points_here(name: str, public: list[str]) -> None:
    try:
        found = _resolve(name)
    except OSError:
        raise Refused(
            "remote.name_mismatch",
            f"{name} does not resolve. Create the DNS record for this server first; nothing was changed.",
        ) from None
    if public:
        mine = set(public)
        if not found & mine:
            raise Refused(
                "remote.name_mismatch",
                f"{name} points to {', '.join(sorted(found))}, not to {', '.join(sorted(mine))}; nothing was changed.",
            )
        return
    if not any(_is_local(a) for a in found):
        raise Refused(
            "remote.name_mismatch",
            f"{name} points to {', '.join(sorted(found))}, which is not an address of this server. Fix the DNS "
            "record (or, behind a cloud NAT, name this server's public address with --public-ip); nothing was changed.",
        )


def _front_installed() -> bool:
    """Whether the Caddyfile of an earlier ``orch remote enable`` is there (the file is world-readable)."""
    try:
        return Path(osfront.CADDYFILE).read_text(encoding="utf-8").startswith(osfront.MARKER)
    except OSError:
        return False


def _check_ports_free() -> None:
    if _front_installed():  # our own Caddy already holds them: this is a re-run
        return
    for port in (80, 443):
        if _port_busy(port):
            raise Refused(
                "host.busy",
                f"port {port} on this server is already in use by another program; stop it first. Nothing was changed.",
            )


def _disable(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    from orch.setup.osapply import DryRunExecutor
    from orch.setup.osfacts import RealOsFacts

    _preconditions(env)
    directory = _dashboard_dir(env, create=False)
    have_file = (directory / "remote.json").exists()
    root_plan = osfront.apply_disable(RealOsFacts(), DryRunExecutor(), is_root=True)
    out.write("orch remote disable\n\nAs you:\n")
    out.write("  remove <state>/dashboard/remote.json\n")
    out.write("  stop the dashboard and start it again without remote access\n")
    out.write("\nWith sudo (your password is asked for):\n")
    out.write(_indent(root_plan))
    if not root_plan.ok:
        out.write("\nNothing was changed.\n")
        return 1
    if not _approve(args, out, "Take remote access away?"):
        return 0 if args.dry_run else 1
    if have_file:
        with contextlib.suppress(FileNotFoundError):
            (directory / "remote.json").unlink()
    # The teardown must run whatever happens to the dashboard, once remote.json is gone: stopping it may fail (no
    # entry point, it does not answer, an OSError, Ctrl-C), and that must never leave Caddy and the front user
    # configured. The dashboard is started again last, in an outer finally, and that never raises.
    stopped = False
    started = False
    try:
        try:
            _stop_dashboard(env, err, directory)
            stopped = True
        except Refused as exc:
            err.write(f"orch remote: {exc.code}: {exc}\n")
        finally:
            pending = sys.exc_info()[1]  # the error that is on its way out, if any (it must not be replaced)
            try:
                _root_step(
                    ["--front-disable"],
                    env,
                    "remote access may still be on. Run `orch remote disable` again (it is safe to repeat)",
                )
            except Refused as exc:
                if pending is None:
                    raise
                err.write(f"orch remote: {exc.code}: {exc}\n")
    finally:  # the person's dashboard comes back, loopback only (remote.json is gone)
        started = _start_dashboard(env, err)
    out.write("Remote access is off.\n")
    if not stopped:
        out.write("The running dashboard could not be stopped: restart it so it closes the secure web address.\n")
    elif started:
        out.write("The dashboard is running again on this computer only.\n")
    out.write("The passkeys stay stored, and enabling again brings them back.\n")
    out.write("If you suspect a passkey leaked, run `orch remote reset` to drop them all.\n")
    return 0


def _reset(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    from orch.click.passkey_store import PasskeyStore

    _preconditions(env)
    directory = _dashboard_dir(env, create=False)
    if not (directory / "remote.json").exists():
        raise Refused("remote.not_configured", "remote access is off; there is nothing to reset (orch remote enable)")
    out.write("orch remote reset\n\nAs you:\n")
    out.write("  stop the dashboard: every remote session ends and every open sign-in link dies\n")
    out.write("  delete every passkey (everybody has to add a passkey again)\n")
    out.write("  start the dashboard and print one new one-time link\n")
    if not _approve(args, out, "Drop every passkey and session?"):
        return 0 if args.dry_run else 1
    try:
        _stop_dashboard(env, err, directory)
    except Refused as exc:
        if exc.code != "unsupported.platform":
            raise
        raise Refused(
            exc.code,
            "the dashboard program was not found, so nothing was stopped: the passkeys were NOT dropped. "
            "Reinstall the dashboard, or run `orch remote disable`.",
        ) from None
    held = PasskeyStore(directory / "passkeys.json").clear()
    out.write(f"Dropped {held} passkey{'s' if held != 1 else ''} and all remote sessions.\n")
    out.flush()
    rc = _enroll(env, err)
    if rc != 0:
        out.write("The link could not be made. Run `orch remote reset` once the dashboard works.\n")
    return 0 if rc == 0 else 1


def main(
    args: Sequence[str],
    *,
    env: Mapping[str, str],
    out: TextIO,
    err: TextIO,
) -> int:
    try:
        ns = _parser().parse_args(list(args))
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        run = {"enable": _enable, "disable": _disable, "reset": _reset}[ns.cmd]
        return run(ns, env, out, err)
    except Refused as exc:
        err.write(f"orch remote: {exc.code}: {exc}\n")
        return exc.exit
    except (OSError, ValueError) as exc:
        err.write(f"orch remote: internal: {exc}\n")
        return 1
