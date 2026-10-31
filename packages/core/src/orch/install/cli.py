"""``orch install hub|relay|publish|status|remove`` and the pieces ``orch setup`` uses for "Full server"."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from orch import childenv
from orch.wsproto import wire
from orch.wsproto.localdial import dial_host

from . import parts as P
from .helper import remove_helper, sync_helper, wait_for_socket
from .parts import InstallError
from .server import CADDY_DIR, ROLES, derive_names
from .service import Runner, pick_runner
from .state import Layout, load, save

__all__ = ["Ctx", "main", "add_workspace", "choose_role", "full_server", "default_ctx"]

USAGE = (
    "usage: orch install server --role hub|edge [--domain BASE] [--domain-hub N] [--domain-relay N]\n"
    "                    [--domain-publish N] [--hub-vpn-only] [--vpn-ip IP[/PREFIX]]\n"
    "                    [--allow-non-cgnat] [--caddy-dir DIR] [--behind-proxy]\n"
    "                    [--backup-drop DIR]\n"
    "                    [--port-base N] [--yes]\n"
    "usage: orch install phone [--user ID]\n"
    "usage: orch install {hub|relay|publish} [--address A] [--port N] [--domain D] [--source PATH] [--ref REF]\n"
    "                    [--publish-url U] [--relay-url U] [--port-base N] [--dev] [--yes]\n"
    "                    | status [--json] | remove PART [--purge]\n"
)
# Git ref the parts are installed from. Prototype: develop carries the current work; this moves to release tags later.
DEFAULT_REF = "develop"  # only when the installed orch-core has no known commit (e.g. an editable dev install)
AGENT_ENV = ("ORCH_GRANT", "ORCH_SESSION")


def _run(argv: Sequence[str], env: Mapping[str, str] | None = None) -> tuple[int, str]:
    r = subprocess.run(list(argv), env=childenv.sanitize(env if env else os.environ, absolute_pythonpath=True),
                       capture_output=True, text=True)  # fmt: skip
    return r.returncode, (r.stdout + r.stderr)


def core_commit() -> str | None:
    """The git commit the installed orch-core was installed from (``direct_url.json``), or None (editable, wheel)."""
    from importlib import metadata

    try:
        d = metadata.distribution("orch-core")
        commit = (json.loads(d.read_text("direct_url.json") or "{}").get("vcs_info") or {}).get("commit_id")
    except (metadata.PackageNotFoundError, ValueError, OSError):
        return None
    return commit if isinstance(commit, str) and len(commit) == 40 else None


def _resolve_ref(url: str, ref: str) -> str:
    rc, out = _run(["git", "ls-remote", url, ref])
    line = out.splitlines()[0] if rc == 0 and out.strip() else ""
    if not line:
        raise InstallError(f"cannot resolve {ref} of {url} (is it reachable? use --source PATH for a local checkout)")
    return line.split()[0]


def _health(host: str, port: int, timeout: float = 2.0) -> bool:
    """One short probe; :func:`wait_ready` repeats it until its deadline."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


READY_DEADLINE = 60.0


def wait_ready(ctx: Ctx, probe: Callable[[], bool]) -> bool:
    """Poll ``probe`` until it is true or the deadline passes (``ctx.ready_deadline`` seconds)."""
    end = ctx.clock() + ctx.ready_deadline
    while True:
        if probe():
            return True
        if ctx.clock() >= end:
            return False
        ctx.sleep(0.5)


@dataclass
class Ctx:
    """Everything that touches the machine, as seams (the tests replace them)."""

    env: Mapping[str, str]
    out: TextIO
    err: TextIO
    interactive: bool = False
    ask: Callable[[str, str], str] = lambda prompt, default: default
    run: Callable[..., tuple[int, str]] = _run
    resolve_ref: Callable[[str, str], str] = _resolve_ref
    core_commit: Callable[[], str | None] = core_commit
    health: Callable[..., bool] = _health  # (host, port[, timeout]): something listens there
    detect: Callable[[], list[tuple[str, str]]] = P.detect_addresses
    port_free: Callable[[int, str], bool] = P.port_free
    runner: Runner | None = None
    token: Callable[[], str] = lambda: secrets.token_urlsafe(18)
    dashboard_entry: Callable[[], list[str] | None] = lambda: _dashboard_entry()
    mint_login: Callable[[list[str], Mapping[str, str]], str | None] = lambda argv, env: _mint_login(argv, env)
    qr: Callable[[str], str | None] = lambda link: _qr(link)
    publish_setup: Callable[[str], str | None] = lambda url: None  # configure the workspace; returns a hint on failure
    port_base: int | None = None  # --port-base N: hub N, relay N+1, publish N+2, hub link N+5 (None: as before, 8100)
    ready_deadline: float = READY_DEADLINE  # seconds a part gets to answer (polled, not a single check)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    helper_ready: Callable[[Path], bool] = (
        wait_for_socket  # the service helper's socket shows up (seconds, not forever)
    )
    layout: Layout = field(init=False)

    def __post_init__(self) -> None:
        self.layout = Layout(self.env)
        if self.runner is None:
            self.runner = pick_runner(self.layout, self.env)

    def say(self, line: str = "") -> None:
        self.out.write(line + "\n")


def _dashboard_entry() -> list[str] | None:
    from orch.cli.dashboard import find_entry_point

    return find_entry_point(os.environ)


def _mint_login(argv: list[str], env: Mapping[str, str]) -> str | None:
    """Run ``orch-dashboard open --no-browser --emit-url-fd N`` and return the single-use login URL it writes."""
    from orch.childenv import sanitize

    r, w = os.pipe()
    try:
        proc = subprocess.run(
            [*argv, "open", "--no-browser", "--emit-url-fd", str(w)],
            env=sanitize(env, absolute_pythonpath=True), pass_fds=(w,), capture_output=True, timeout=30,
        )  # fmt: skip
        os.close(w)
        w = -1
        data = os.read(r, 8192).decode(errors="replace") if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        os.close(r)
        if w >= 0:
            os.close(w)
    line = data.splitlines()[0].strip() if data.strip() else ""
    return line if line.startswith("http://") else None


#: setup's Finish: how long the dashboard service may take to answer (the page's proxy gives up after 15 s)
SERVICE_WAIT = 6.0


class LoginUnavailable(Exception):
    """The dashboard service should serve this workspace but gave no login link (setup's Finish says: try again)."""


def workspace_login_url(ctx: Ctx, directory: str, *, wait: float = SERVICE_WAIT) -> str | None:
    """Setup's "Open your workspace": register the workspace, make sure the dashboard SERVICE (not the stopped
    launcher of the setup page) runs, and return a fresh single-use login URL on it.
    ``None`` when this machine has no dashboard service (the setup page's own dashboard is the destination then).
    :class:`LoginUnavailable` when there is one but it gave no link: it did not answer (started once more, waited
    ``wait`` s each time), or minting failed. Each call mints a new link, so a retry never reuses a spent one."""
    try:
        if add_workspace(ctx, directory) is None:
            return None
        dash = load(ctx.layout).get("dashboard")
        entry = ctx.dashboard_entry()
        if not dash or not entry:
            return None
        if not ctx.health("127.0.0.1", dash["port"], wait):
            start_dashboard(ctx, dash["bind"], dash["port"])
            if not ctx.health("127.0.0.1", dash["port"], wait):
                raise LoginUnavailable(f"the dashboard did not answer on port {dash['port']}")
        env = {
            "HOME": str(ctx.layout.home),
            "PATH": ctx.env.get("PATH", os.defpath),
            "ORCH_STATE_DIR": str(ctx.layout.root.parent),
        }
        url = ctx.mint_login(entry, env)
    except LoginUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 - Finish must answer in words, never with a traceback
        raise LoginUnavailable(f"the dashboard could not be started ({type(e).__name__})") from e
    if not url:
        raise LoginUnavailable("the dashboard gave no login link")
    return url


def default_ctx(env: Mapping[str, str], out: TextIO, err: TextIO) -> Ctx:
    tty = sys.stdin.isatty()
    return Ctx(
        env=env,
        out=out,
        err=err,
        interactive=tty,
        ask=lambda prompt, default: (input(f"{prompt} [{default}]: ").strip() or default) if tty else default,
        publish_setup=_publish_setup,
    )


def _publish_setup(url: str) -> str | None:
    """Sign the workspace's publish settings (server = share = the part's address): the person's terminal signs."""
    from orch.cli import dashboard

    # -P: the current directory is not on sys.path; PYTHONPATH reduced to absolute entries (the launcher's sanitizer)
    env = dashboard.launch_env(os.environ)
    rc, out = _run([sys.executable, "-P", "-m", "orch", "publish", "setup", "--server", url, "--share", url], env)
    return None if rc == 0 else (out.strip().splitlines() or ["failed"])[-1]


# ---------------------------------------------------------------------------------------------------- address / port


def choose_address(ctx: Ctx, given: str | None, st: dict[str, Any]) -> str:
    if given:
        return given
    if st.get("address"):
        return st["address"]
    found = ctx.detect()
    if sys.platform == "darwin" and any(_is_cgnat(ip) for ip, _ in found):
        ctx.say("Note: on this computer, open it at 127.0.0.1 (a Mac cannot open its own private-network address).")
    if not ctx.interactive or len(found) == 1:
        return found[0][0]
    ctx.say("Which address should the parts listen on?")
    for i, (ip, label) in enumerate(found, 1):
        ctx.say(f"  {i}  {ip}   ({label})")
    ctx.say("  or type your own address (an IP; a domain is only noted)")
    ans = ctx.ask("address", "1")
    return found[int(ans) - 1][0] if ans.isdigit() and 1 <= int(ans) <= len(found) else ans


def _is_cgnat(ip: str) -> bool:
    """100.64.0.0/10: where NetBird addresses live."""
    try:
        a, b, *_ = (int(x) for x in ip.split("."))
    except ValueError:
        return False
    return a == 100 and 64 <= b <= 127


def _taken(st: dict[str, Any], skip: str = "") -> set[int]:
    t = {p["port"] for n, p in st["parts"].items() if n != skip}
    t |= {p["link_port"] for p in st["parts"].values() if p.get("link_port")}
    return t | {w["port"] for w in st["workspaces"]}


def ports_of(ctx: Ctx, st: dict[str, Any]) -> dict[str, int]:
    """The port block in force: this run's ``--port-base``, else the one an earlier run saved, else 8100."""
    return P.port_block(ctx.port_base or st.get("port_base") or P.DEFAULT_PORT_BASE)


def check_block(ctx: Ctx, parts: Sequence[str]) -> None:
    """``--port-base``: validate the block and bind-test the ports of ``parts`` BEFORE anything is installed. A port
    held by a part of THIS install (a rerun) is fine; one held by anything else is refused, never freed."""
    if ctx.port_base is None:
        return
    st = load(ctx.layout)
    block = P.port_block(ctx.port_base)
    ours = {p["port"] for p in st["parts"].values()} | {
        p["link_port"] for p in st["parts"].values() if p.get("link_port")
    }
    need = [(part, block[part]) for part in parts] + ([("hub link", block["link"])] if "hub" in parts else [])
    for what, port in need:
        if port not in ours and not ctx.port_free(port, P.ADDRESS_ALL):
            raise InstallError(
                f"port {port} ({what}) is already used by another program on this computer; pick another --port-base"
            )


def choose_port(ctx: Ctx, part: str, given: int | None, st: dict[str, Any], address: str) -> int:
    if given is None and ctx.port_base is not None:  # an explicit block: exactly these ports (check_block tested them)
        port = ports_of(ctx, st)[part]
        if port in _taken(st, part):
            raise InstallError(f"port {port} is already used by another part of this install")
        return port
    if given is not None:
        port = P.valid_port(given)
        if port in _taken(st, part):
            raise InstallError(f"port {port} is already used by another part of this install")
        return port
    if part in st["parts"]:
        return st["parts"][part]["port"]
    start = ports_of(ctx, st)[part]
    host = address if address not in ("localhost",) else "127.0.0.1"
    return P.free_port(start, _taken(st), lambda p: ctx.port_free(p, host if not _is_domain(host) else "127.0.0.1"))


def _is_domain(a: str) -> bool:
    return not a.replace(".", "").isdigit()


def _listen_address(address: str) -> str:
    return P.ADDRESS_LOOPBACK if address == "localhost" or _is_domain(address) else address


# ------------------------------------------------------------------------------------------------------------ install


def hub_address_kind(address: str) -> str:
    """The hub settings kind of an address: private (VPN), office (local network), server (this computer), public."""
    if address in ("localhost", P.ADDRESS_LOOPBACK):
        return "server"
    label = P.classify(address)
    return {"private network": "private", "local network": "office"}.get(label, "public")


def write_hub_settings(
    data: Path,
    *,
    address: str,
    domain: str | None,
    port: int,
    link_port: int = P.HUB_LINK_PORT,
    web_origin: str | None = None,
    link_host: str | None = None,
    vpn_only: bool = False,
) -> None:
    """Seed ``hub-settings.json`` (the hub's own words and the addresses it advertises) with the address chosen now.

    The hub validates the same shape when it reads it (``orch_hub.settings``). A re-run keeps the name and company an
    admin already set (a damaged, linked or foreign old file resets them to the defaults)
    and replaces only the addresses; nothing secret goes in. The file is 0600, written atomically."""
    f = data / "hub-settings.json"
    name, company = "Hub", ""
    try:  # no-follow open; a link, a foreign owner or a non-regular file is not trusted: defaults
        fd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            if stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid() and not st.st_mode & 0o077:
                with os.fdopen(fd, closefd=False) as fh:
                    old = json.load(fh)
                if isinstance(old.get("name"), str) and isinstance(old.get("company"), str):
                    name, company = old["name"], old["company"]
        finally:
            os.close(fd)
    except (OSError, ValueError, AttributeError):
        pass
    link = link_port
    loop = address in ("localhost", P.ADDRESS_LOOPBACK)
    if link_host:  # server role: the card advertises the VPN address for the link and the https name for the console
        own = P.ADDRESS_LOOPBACK if loop else address  # no name: the console answers on the VPN address itself
        web = f"{web_origin}:443" if web_origin else f"http://{own}:{port}"
        # one entry: behind Caddy every allowed origin must be https (hub config); the settings shape wants host:port,
        # the hub drops the default port when it builds the allowed Origin (settings.origin_of)
        addrs = [{"kind": "private" if vpn_only else "public", "web": web, "link": f"{link_host}:{link}"}]
        return _save_hub_settings(f, {"v": 1, "name": name, "company": company, "addresses": addrs})
    host = P.ADDRESS_LOOPBACK if loop else (f"[{address}]" if ":" in address else address)
    addrs = [{"kind": hub_address_kind(address), "web": f"http://{host}:{port}", "link": f"{host}:{link}"}]
    if not loop:  # a network install also answers on the computer itself
        addrs.append(
            {"kind": "server", "web": f"http://{P.ADDRESS_LOOPBACK}:{port}", "link": f"{P.ADDRESS_LOOPBACK}:{link}"}
        )
    if domain:  # the scheme of a domain address (http here) is revisited in S2 with the address picker
        addrs.append({"kind": "public", "web": f"http://{domain}:{port}", "link": None})
    _save_hub_settings(f, {"v": 1, "name": name, "company": company, "addresses": addrs})


def _save_hub_settings(f: Path, body: dict[str, Any]) -> None:
    tmp = f.with_name(f".{f.name}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(json.dumps(body, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, f)


def install_part(
    ctx: Ctx,
    part: str,
    *,
    address: str | None = None,
    port: int | None = None,
    domain: str | None = None,
    source: str | None = None,
    ref: str | None = None,
    publish_url: str | None = None,
    relay_url: str | None = None,
    dev: bool = False,
    sync: bool = True,  # False: the caller (full_server) syncs the service helper once at the end
    link_address: str | None = None,  # server role, hub: the VPN address the link binds (the console stays on loopback)
    web_origin: str | None = None,  # server role, hub: https://<hub name>, advertised in the card
    vpn_only: bool = False,
    public_origin: str | None = None,  # server role, publish: https://<publish name> (server = share)
    workspace_side: bool = True,
    behind_edge: bool = False,  # server role with TLS: Caddy on this computer is the edge in front of the part
    exact_bind: bool = False,  # server role: the part listens on exactly ``address`` (not on every interface)
    trusted_proxy: str | None = None,  # behind the private network's proxy: its networks (rate key only)
) -> dict[str, Any]:
    spec = P.PARTS[part]
    source = source or ctx.env.get(f"ORCH_INSTALL_SOURCE_{part.upper()}")  # a local checkout (dev machines, tests)
    lay = ctx.layout
    st = load(lay)
    if part in st["parts"] or ctx.runner.info(part)["running"]:
        # a re-run replaces this user's own running part (its port is then free again), also when an earlier run died
        # before it recorded the part: that port is ours, not another program's
        ctx.runner.stop(part)
    address = choose_address(ctx, address, st)
    if _is_domain(address) and address != "localhost":
        domain, address = address, P.ADDRESS_LOOPBACK  # a domain is only noted; the part listens on this computer
    bind = _listen_address(address)
    port = choose_port(ctx, part, port, st, bind if bind == P.ADDRESS_LOOPBACK else P.ADDRESS_ALL)
    lay.ensure()
    tool_env = {**ctx.env, "UV_TOOL_DIR": str(lay.tools), "UV_TOOL_BIN_DIR": str(lay.bin)}
    if source:
        pkg, sha = str(Path(source).resolve()), "local:" + str(Path(source).resolve())
    else:
        if ref is None:
            # the parts must match the orch-core that runs them: its own commit, not whatever develop is today
            sha = ctx.core_commit()
            if sha is None:
                ctx.say(f"  this orch has no known commit (dev install): taking the latest {DEFAULT_REF} of the parts")
                sha = ctx.resolve_ref(P.REPO_URL, DEFAULT_REF)
        else:
            sha = ctx.resolve_ref(P.REPO_URL, ref)
        pkg = spec.package_url(sha)
    ctx.say(f"orch install {part}: installing {spec.binary} ({sha[:12] if not source else 'local checkout'}) ...")
    rc, out = ctx.run(["uv", "tool", "install", "--force", "--python", "3.14", pkg], tool_env)
    if rc != 0:
        raise InstallError(f"uv tool install failed:\n{out.strip()[-600:]}")
    binary = lay.bin / spec.binary
    data = lay.data(part)
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    pubs = st["parts"].get("publish")
    rels = st["parts"].get("relay")
    if part == "hub":
        publish_url = publish_url or (pubs and _dial_url(pubs))
        relay_url = relay_url or (rels and _dial_url(rels))
        if relay_url and not _hub_accepts(relay_url):
            ctx.say(f"  note: the hub does not accept {relay_url} as a relay address: left out, push stays off")
            relay_url = None
        fresh = not any(data.iterdir())
        if fresh:
            cmd = (
                [str(binary), "keys", "--state", str(data), "init"]
                + ([] if dev else ["--real"])  # a hub without --dev is a real one: real workspaces join it
                + (["--relay-url", relay_url] if relay_url else [])
            )
            rc, out = ctx.run(cmd, tool_env)
            if rc != 0:
                shutil.rmtree(data, ignore_errors=True)  # a half-made key store would refuse the next try
                raise InstallError(f"orch-hub keys init failed:\n{out.strip()[-600:]}")
        token = (st["parts"].get("hub") or {}).get("setup_token") or ctx.token()
        if fresh:  # the hub's console reads this file once: the first-admin page consumes it
            tf = data / "setup-token"
            tf.write_text(token + "\n")
            tf.chmod(0o600)
    if part == "hub":
        write_hub_settings(
            data,
            address=address,
            domain=domain,
            port=port,
            link_port=ports_of(ctx, st)["link"],
            web_origin=web_origin,
            link_host=link_address,
            vpn_only=vpn_only,
        )
    argv = P.command_for(
        part,
        binary=binary,
        data=data,
        address=bind,
        port=port,
        publish_url=publish_url,
        relay_url=relay_url,
        dev=dev,
        link_address=link_address,
        public_origin=public_origin,
        link_port=ports_of(ctx, st)["link"],
        publish_port=ports_of(ctx, st)["publish"],
        behind_edge=behind_edge,
        exact_bind=exact_bind or bool(link_address),
    )
    log = lay.logs / f"{part}.log"
    started = ctx.runner.start(
        part, argv, P.env_for(part, data, behind_edge=behind_edge, trusted_proxy=trusted_proxy), log
    )
    # a part on every interface is dialled on loopback (a computer cannot dial its own overlay IP). A part that binds
    # exactly one address (the server role: the VPN IP) answers ONLY there: probe that address, never 127.0.0.1.
    check_host = bind if (exact_bind or link_address) else dial_host(bind)

    # a computer often cannot dial its own overlay IP (NetBird): then a held port on that IP and a live process count
    def answers() -> bool:
        return ctx.health(check_host, port) or (
            ctx.runner.info(part)["running"] and not ctx.port_free(port, check_host)
        )

    if not wait_ready(ctx, answers):
        tail = log.read_text()[-500:] if log.exists() else ""
        raise InstallError(
            f"{part} did not answer on {check_host}:{port} within {ctx.ready_deadline:.0f} seconds; log {log}\n{tail}"
        )
    url = f"http://{bind}:{port}"
    rec: dict[str, Any] = {
        "port": port,
        "address": address,
        "bind": bind,
        "url": url,
        "domain": domain,
        "ref": sha,
        "source": source,
        "binary": str(binary),
        "dev": dev,
        "service": started["service"],
        "runner": ctx.runner.kind,
        "pid": started.get("pid"),
        "started": int(time.time()),
    }
    if part == "hub":
        rec["link_port"] = ports_of(ctx, st)["link"]
        if link_address:
            rec["link_host"] = link_address
        if web_origin:
            rec["console_url"] = web_origin + "/"
        rec["setup_token"] = token
    st = load(lay)
    st["address"], st["domain"] = address, domain or st.get("domain")
    if ctx.port_base is not None:
        st["port_base"] = ctx.port_base
    st["parts"][part] = rec
    save(lay, st)
    _version(ctx, rec, spec.binary, tool_env)
    save(lay, {**st, "parts": {**st["parts"], part: rec}})
    _report(ctx, part, rec)
    if part == "publish":
        url = public_origin or url  # server = share = the https name when there is one
        if not workspace_side and not public_origin:
            ctx.say(
                "  publish needs a name and https before the Macs can use it (shares need a secure address). "
                "This follows once DNS is in: run this command again with --domain <your domain>."
            )
        elif not workspace_side:
            ctx.say(f"  on each Mac, once: orch publish setup --server {url} --share {url}")
        else:
            hint = ctx.publish_setup(url)
            if hint:
                ctx.say(
                    f"  publish settings: not set yet ({hint}); run: orch publish setup --server {url} --share {url}"
                )
            else:
                ctx.say(f"  publish settings: server = share = {url}")
    if sync:
        sync_helper(ctx)
    return rec


def _dial_url(rec: dict[str, Any]) -> str:
    """How the hub on this computer dials a part: its own address."""
    return str(rec["url"])


def _hub_accepts(url: str) -> bool:
    """The hub (card and ``--publish-url``) takes https, or http on loopback, private and CGNAT addresses."""
    try:
        wire.check_origin(url)
    except wire.WireError:
        return False
    return True


def _version(ctx: Ctx, rec: dict[str, Any], name: str, env: Mapping[str, str]) -> None:
    rc, out = ctx.run(["uv", "tool", "list"], env)
    rec["version"] = "unknown"
    for line in out.splitlines():
        if line.startswith(name + " "):
            rec["version"] = line.split()[1].lstrip("v")


def _report(ctx: Ctx, part: str, rec: dict[str, Any]) -> None:
    ctx.say(f"installed {part} {rec['version']} at {rec['url']}  ({rec['runner']}: {rec['service']})")
    if part == "hub":
        ctx.say(f"  hub console:  {hub_console_url(rec)}")
        ctx.say(f"  first admin:  {hub_console_url(rec)}#setup={rec['setup_token']}")
        if rec["bind"] != P.ADDRESS_LOOPBACK:
            ctx.say(f"  on this computer: http://{P.ADDRESS_LOOPBACK}:{rec['port']}/#setup={rec['setup_token']}")
        ctx.say(f"  workspaces connect to the hub link at {rec.get('link_host') or rec['bind']}:{rec['link_port']}")
    if part != "hub" and rec["bind"] != P.ADDRESS_LOOPBACK:
        ctx.say(f"  on this computer: http://{P.ADDRESS_LOOPBACK}:{rec['port']}")
    if rec.get("domain"):
        ctx.say(
            f"  domain {rec['domain']}: point its DNS at this computer yourself and put your own TLS in front of "
            f"{rec['bind']}:{rec['port']}; orch does not do that for you."
        )


def command_failed(e: subprocess.CalledProcessError) -> str:
    """Plain words for a system command that failed (the command, its last output line, the usual fix)."""
    cmd = " ".join(str(x) for x in e.cmd) if isinstance(e.cmd, (list, tuple)) else str(e.cmd)
    raw = e.stderr or e.output or b""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    last = " ".join(text.strip().splitlines()[-1:]) if text.strip() else f"exit code {e.returncode}"
    last = "".join(ch if ch.isprintable() else " " for ch in last)[:200]
    hint = ""
    if cmd.startswith("systemctl --user"):
        hint = (
            " If this user has no systemd session: ask an administrator to run the linger line of the root block, "
            "then log in with `sudo machinectl shell <user>@` (or set XDG_RUNTIME_DIR=/run/user/<id>) and run this "
            "command again."
        )
    return f"a step failed: `{cmd}` ({last}).{hint}"


def hub_console_url(rec: dict[str, Any]) -> str:
    if rec.get("console_url"):
        return rec["console_url"]
    return f"http://{rec['bind']}:{rec['port']}/"


# ------------------------------------------------------------------------------------------------------------ status


def status(ctx: Ctx, as_json: bool) -> int:
    st = load(ctx.layout)
    rows = []
    for name in P.PARTS:
        rec = st["parts"].get(name)
        if rec is None:
            rows.append({"part": name, "state": "not installed"})
            continue
        info = ctx.runner.info(name)
        rows.append(
            {
                "part": name,
                "state": "running" if info["running"] else "stopped",
                "version": rec.get("version"),
                "ref": rec.get("ref"),
                "url": rec["url"],
                "service": rec["service"],
                "pid": info.get("pid"),
            }
        )
    dash = st.get("dashboard")
    dver, dref = installed_version(ctx, "orch-dashboard") if dash else (None, None)
    for w in st["workspaces"]:
        rows.append(
            {
                "part": "workspace",
                "version": (dash or {}).get("version") or dver,
                "ref": (dash or {}).get("ref") or dref,
                "state": ("running" if ctx.runner.info("dashboard")["running"] else "stopped")
                if dash
                else "registered",
                "url": dash["url"] if dash else f"http://{_ws_host(st)}:{w['port']}/",
                "service": dash["service"] if dash else None,
                "dir": w["dir"],
            }
        )
    if as_json:
        ctx.say(json.dumps({"address": st.get("address"), "parts": rows}, indent=2))
        return 0
    ctx.say(f"{'part':<10}{'state':<15}{'version':<12}{'sha':<14}{'address':<28}{'service'}")
    for r in rows:
        where = r.get("service") or r.get("dir") or ""
        ver, sha, url = r.get("version") or "-", _short(r.get("ref")), r.get("url") or "-"
        ctx.say(f"{r['part']:<10}{r['state']:<15}{ver:<12}{sha:<14}{url:<28}{where}")
    if dash:
        ctx.say(f"workspace dashboard login link (single use, 5 min):  {dashboard_login_hint(dash)}")
    return 0


def installed_version(ctx: Ctx, dist: str) -> tuple[str | None, str | None]:
    """(version, git commit) of an installed Python distribution, read from its metadata (the dashboard runs from the
    same tool environment as ``orch``; its commit is in ``direct_url.json`` when it was installed from git)."""
    from importlib import metadata

    for name in (dist, "orch-core"):
        try:
            d = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        commit = None
        try:
            commit = (json.loads(d.read_text("direct_url.json") or "{}").get("vcs_info") or {}).get("commit_id")
        except (ValueError, OSError):
            pass
        return d.version, commit
    return None, None


def _short(ref: Any) -> str:
    return str(ref)[:12] if ref else "-"


def _ws_host(st: dict[str, Any]) -> str:
    return st.get("address") or P.ADDRESS_LOOPBACK


def remove(ctx: Ctx, part: str, purge: bool) -> int:
    st = load(ctx.layout)
    if part == "dashboard":
        ctx.runner.stop("dashboard")
        st.pop("dashboard", None)
        save(ctx.layout, st)
        ctx.say("removed the workspace dashboard service")
        return 0
    if part not in P.PARTS:
        raise InstallError(f"unknown part {part!r}: hub, relay, publish or dashboard")
    if part not in st["parts"]:
        ctx.say(f"{part} is not installed")
        return 0
    if part == "hub":
        remove_helper(ctx, st)  # first: its helper.json lists this hub; the helper only exists for a hub
    ctx.runner.stop(part)
    env = {**ctx.env, "UV_TOOL_DIR": str(ctx.layout.tools), "UV_TOOL_BIN_DIR": str(ctx.layout.bin)}
    ctx.run(["uv", "tool", "uninstall", P.PARTS[part].binary], env)
    del st["parts"][part]
    save(ctx.layout, st)
    if part != "hub":
        sync_helper(ctx)  # drops this part from the helper's list
    if purge:
        shutil.rmtree(ctx.layout.root / "parts" / part, ignore_errors=True)
        ctx.say(f"removed {part} and its data")
    else:
        ctx.say(f"removed {part}; its data is kept in {ctx.layout.data(part)} (--purge deletes it)")
    return 0


# -------------------------------------------------------------------------------------------------------------- phone


def _json_of(out: str) -> Any:
    starts = [i for i in (out.find("{"), out.find("[")) if i >= 0]
    try:
        return json.loads(out[min(starts) :]) if starts else json.loads(out)
    except ValueError:
        raise InstallError("the hub answered something unreadable") from None


def phone_origin(rec: dict[str, Any]) -> str:
    """The origin a phone dials: the advertised (LAN/NetBird) address or the domain, never loopback."""
    if rec.get("domain"):
        return f"https://{rec['domain']}"
    host = rec.get("address") or rec.get("bind") or P.ADDRESS_LOOPBACK
    if host in ("localhost", P.ADDRESS_LOOPBACK, "::1") or host.startswith("127."):
        raise InstallError(
            "this hub only listens on this computer (127.0.0.1), a phone cannot reach it. "
            "Reinstall it on a LAN or private-network address: orch install hub --address <your address>"
        )
    return f"http://{host}:{rec['port']}"


def phone(ctx: Ctx, user: str | None) -> int:
    """``orch install phone``: mint a one-time phone pairing link for the hub on this computer; shown, never stored."""
    rec = load(ctx.layout)["parts"].get("hub")
    if not rec:
        raise InstallError("no hub is installed on this computer; run: orch install hub")
    origin = phone_origin(rec)
    binary, data = rec["binary"], str(ctx.layout.data("hub"))
    if not user:
        rc, out = ctx.run([binary, "users", "--state", data, "list"], dict(ctx.env))
        if rc != 0:
            raise InstallError(f"cannot list the hub's users:\n{out.strip()[-400:]}")
        users = _json_of(out)
        admins = [u for u in users if "hub-admin" in (u.get("groups") or [])]
        if len(admins) != 1:
            names = ", ".join(f"{u['user_id']} ({u.get('name', '')})" for u in users) or "none yet"
            raise InstallError(f"say which user: orch install phone --user <id>   (users: {names})")
        user = admins[0]["user_id"]
    rc, out = ctx.run(
        [binary, "channels", "--state", data, "pair-code", "--user", user, "--link", "--api-origin", origin],
        dict(ctx.env),
    )
    if rc != 0:
        raise InstallError(f"the hub refused to make a pairing link:\n{out.strip()[-400:]}")
    got = _json_of(out)
    link = got.get("link") if isinstance(got, dict) else None
    if not link:
        raise InstallError("the hub made no pairing link")
    ctx.say("Open the orch app on your phone and scan this, or open the link (one use, valid 10 minutes):")
    ctx.say(link)
    qr = ctx.qr(link)
    if qr is None:
        ctx.say("(no QR here: brew install qrencode, then run this again)")
    else:
        ctx.say(qr)
    return 0


def _qr(link: str) -> str | None:
    exe = shutil.which("qrencode")
    if not exe:
        return None
    r = subprocess.run([exe, "-t", "ANSIUTF8", link], capture_output=True, text=True, env=childenv.minimal(os.environ))
    return r.stdout if r.returncode == 0 else None


# ------------------------------------------------------------------------------------------------- setup integration


def choose_role(ctx: Ctx, given: str | None) -> tuple[str, list[str]]:
    """``(role, parts)``: role is workspace | full | pick; parts are the server parts to install."""
    if given:
        role = given
    elif not ctx.interactive:
        role = "workspace"
    else:
        ctx.say("What should this computer be?")
        ctx.say("  1  Workspace only      one workspace, nothing else (the default)")
        ctx.say("  2  Full server         hub + relay + publish + this workspace")
        ctx.say("  3  Pick parts          choose which of them to install")
        role = {"1": "workspace", "2": "full", "3": "pick"}.get(ctx.ask("choice", "1"), "workspace")
    if role == "full":
        return role, ["publish", "relay", "hub"]
    if role == "pick":
        chosen = [
            p
            for p in ("publish", "relay", "hub")
            if ctx.ask(f"install {P.PARTS[p].what}? (y/n)", "n").lower().startswith("y")
        ]
        return role, chosen
    return "workspace", []


def full_server(
    ctx: Ctx,
    parts: Sequence[str],
    *,
    address: str | None = None,
    source_for: Mapping[str, str] | None = None,
    ref: str | None = None,
    dev: bool = False,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not parts:  # a workspace-only setup installs no part, so it has no address to ask for
        return out
    check_block(ctx, parts)
    st = load(ctx.layout)
    address = choose_address(ctx, address, st)
    for p in parts:
        out[p] = install_part(ctx, p, address=address, source=(source_for or {}).get(p), ref=ref, dev=dev, sync=False)
    sync_helper(ctx)
    return out


def add_workspace(ctx: Ctx, directory: str) -> int | None:
    """Record a new workspace on the next free port (8151, 8152, ...). Only on a machine with an install state."""
    st = load(ctx.layout)
    if not st["parts"] and not st["workspaces"] and not ctx.layout.file.exists():
        return None
    for w in st["workspaces"]:
        if w["dir"] == directory:
            return w["port"]
    host = _listen_address(st.get("address") or P.ADDRESS_LOOPBACK)
    # one dashboard per person serves every workspace (it lists them all): the first workspace picks its port
    dash = st.get("dashboard")
    port = dash["port"] if dash else P.free_port(P.WORKSPACE_FIRST_PORT, _taken(st), lambda p: ctx.port_free(p, host))
    st["workspaces"].append({"dir": directory, "port": port})
    save(ctx.layout, st)
    if not dash:
        start_dashboard(ctx, host, port)
    return port


def start_dashboard(ctx: Ctx, bind: str, port: int) -> dict[str, Any] | None:
    """Run ``orch-dashboard serve`` as a user service on ``bind:port`` (a network address adds its listener to the
    loopback one, same port). Prototype: plain http; the link to log in comes from ``orch dashboard open --network``."""
    entry = ctx.dashboard_entry()
    if not entry:
        ctx.say("  the dashboard add-on is not installed: no dashboard service started (see: orch dashboard)")
        return None
    lay = ctx.layout
    lay.ensure()
    env = {"HOME": str(lay.home), "PATH": ctx.env.get("PATH", os.defpath), "ORCH_STATE_DIR": str(lay.root.parent)}
    # setup's "Open your workspace" may already have started the launcher dashboard (random loopback port, owns the
    # control socket): the service would then exit with "dashboard already running" and nothing listens on ``port``.
    ctx.run([*entry, "stop"], {**ctx.env, **env})
    started = ctx.runner.start(
        "dashboard", P.dashboard_command(entry, address=bind, port=port), env, lay.logs / "dashboard.log"
    )
    check = "127.0.0.1"

    def up() -> bool:
        return ctx.health(check, port) or (ctx.runner.info("dashboard")["running"] and not ctx.port_free(port, check))

    if not wait_ready(ctx, up):
        ctx.say(
            f"  the dashboard did not answer on {bind}:{port} within {ctx.ready_deadline:.0f} seconds; "
            f"log {lay.logs / 'dashboard.log'}"
        )
    rec = {
        "port": port,
        "bind": bind,
        "url": f"http://{bind}:{port}/",
        "service": started["service"],
        "runner": ctx.runner.kind,
        "pid": started.get("pid"),
        "started": int(time.time()),
    }
    st = load(lay)
    st["dashboard"] = rec
    save(lay, st)
    if bind != P.ADDRESS_LOOPBACK:
        ctx.say(f"  workspace dashboard: http://{bind}:{port}/   on this computer: http://{P.ADDRESS_LOOPBACK}:{port}/")
    return rec


def dashboard_login_hint(rec: dict[str, Any]) -> str:
    return "orch dashboard open --print-url" + ("" if rec["bind"] == P.ADDRESS_LOOPBACK else " --network")


def workspace_port_preview(ctx: Ctx) -> int:
    st = load(ctx.layout)
    return P.free_port(P.WORKSPACE_FIRST_PORT, _taken(st), lambda p: ctx.port_free(p, "127.0.0.1"))


# ----------------------------------------------------------------------------------------------------------- command


def main(argv: Sequence[str], *, env: Mapping[str, str], out: TextIO, err: TextIO, ctx: Ctx | None = None) -> int:
    if any(env.get(k) for k in AGENT_ENV):
        err.write("orch install: installing parts is a person's job; an agent never does it\n")
        return 3
    p = argparse.ArgumentParser(prog="orch install", add_help=False)
    p.add_argument("cmd", choices=("hub", "relay", "publish", "status", "remove", "phone", "server"))
    p.add_argument("rest", nargs="*")
    p.add_argument("--address")
    p.add_argument("--port", type=int)
    p.add_argument("--domain")
    p.add_argument("--source")
    p.add_argument("--ref", default=None)
    p.add_argument("--publish-url")
    p.add_argument("--relay-url")
    p.add_argument("--dev", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--purge", action="store_true")
    p.add_argument("--user")
    p.add_argument("--role", choices=tuple(ROLES))
    p.add_argument("--domain-hub")
    p.add_argument("--domain-relay")
    p.add_argument("--domain-publish")
    p.add_argument("--hub-vpn-only", action="store_true")
    p.add_argument("--vpn-ip")
    p.add_argument("--allow-non-cgnat", action="store_true")
    p.add_argument("--behind-proxy", action="store_true")
    p.add_argument("--port-base", type=int)
    p.add_argument("--caddy-dir", default=CADDY_DIR)
    p.add_argument("--backup-drop")
    try:
        a = p.parse_args(list(argv))
    except SystemExit:
        err.write(USAGE)
        return 2
    ctx = ctx or default_ctx(env, out, err)
    if a.yes:
        ctx.interactive = False
    if a.port_base is not None:
        ctx.port_base = a.port_base
    try:
        if a.cmd == "status":
            return status(ctx, a.json)
        if a.cmd == "phone":
            return phone(ctx, a.user)
        if a.cmd == "server":
            from .serverflow import server_setup

            if not a.role or a.rest:
                err.write(USAGE)
                return 2
            server_setup(
                ctx,
                a.role,
                names=derive_names(a.domain, a.domain_hub, a.domain_relay, a.domain_publish),
                hub_vpn_only=a.hub_vpn_only,
                vpn_ip=a.vpn_ip,
                allow_non_cgnat=a.allow_non_cgnat,
                address=a.address,
                caddy_dir=a.caddy_dir,
                backup_drop=a.backup_drop,
                source=a.source,
                ref=a.ref,
                dev=a.dev,
                behind_proxy=a.behind_proxy,
            )
            return 0
        if a.cmd == "remove":
            if len(a.rest) != 1:
                err.write(USAGE)
                return 2
            return remove(ctx, a.rest[0], a.purge)
        if a.rest:
            err.write(USAGE)
            return 2
        if ctx.port_base is not None:
            P.port_block(ctx.port_base)
            check_block(ctx, [a.cmd])
        address = a.address or a.domain
        install_part(
            ctx,
            a.cmd,
            address=address,
            port=a.port,
            source=a.source,
            ref=a.ref,
            publish_url=a.publish_url,
            relay_url=a.relay_url,
            dev=a.dev,
        )
        return 0
    except InstallError as e:
        err.write(f"orch install: {e}\n")
        return 1
    except subprocess.CalledProcessError as e:  # never a raw traceback from a setup step
        err.write(f"orch install: {command_failed(e)}\n")
        return 1
