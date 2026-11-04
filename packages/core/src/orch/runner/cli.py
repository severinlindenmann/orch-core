"""``orch runner …``: the machine switch, the runner service and the runner process itself.

* ``orch runner enable [--workspace W] [--no-terminals] [--dev] [--replace]`` (person, own terminal only): shows the
  risk, requires typing the workspace prefix, records the switch (D-P2-31) and installs and starts the runner unit
  (D-P2-24) like ``orch host enable``.
* ``orch runner disable --workspace W``: switches dashboard execution off (the service stays; it starts nothing for
  that workspace).
* ``orch runner status [--json]``: the switches and the service.
* ``orch runner run --state S --run R``: the service entry (the unit runs this; ``--foreground`` for dev).
* ``orch runner unit launchd|systemd``: print the unit.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from orch.host.errors import HostError
from orch.ops.workspace_init import Terminal

from . import enablement

__all__ = ["TERMINAL", "main"]


class RunnerTerminal(Terminal):
    def ask(self, prompt: str) -> str:
        return self._read(prompt, echo=True)


#: the person's own terminal (``/dev/tty``); tests replace it
TERMINAL: Any = RunnerTerminal()


class _Exact(argparse.ArgumentParser):
    """No abbreviated long option (``--fore`` never means ``--foreground``), in every subcommand."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        kw["allow_abbrev"] = False
        super().__init__(*args, **kw)


def _parser() -> argparse.ArgumentParser:
    p = _Exact(prog="orch runner", description="the orch runner (dashboard execution)")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Exact)
    en = sub.add_parser(
        "enable",
        help="turn on dashboard execution for a workspace and start the runner service",
        description=(
            "Turns on dashboard execution for one workspace on this account: the dashboard may then start agents, "
            "create worktrees and open terminals that run as you. The prompt on your terminal and the refusal under "
            "ORCH_GRANT only stop accidents: in tier A an agent running as you can give itself a terminal (a pty) "
            "and drop ORCH_GRANT, so it can turn this on too."
        ),
    )
    en.add_argument("--workspace", help="workspace id or prefix (default: the workspace of the current directory)")
    en.add_argument("--no-terminals", action="store_true", help="hide terminals (does not make agents safe)")
    en.add_argument("--replace", action="store_true", help="replace a runner service installed for another setup")
    en.add_argument("--dev", action="store_true", help="allow an interpreter or orch package from a source checkout")
    en.add_argument("--state", type=Path)
    en.add_argument("--run", type=Path)
    en.add_argument("--json", action="store_true", help="machine-readable output")
    dis = sub.add_parser("disable", help="turn dashboard execution off for a workspace")
    dis.add_argument("--workspace", help="workspace id or prefix (default: the current directory's)")
    dis.add_argument("--state", type=Path)
    dis.add_argument("--json", action="store_true", help="machine-readable output")
    term = sub.add_parser("terminals", help="turn terminals on or off for a workspace with dashboard execution on")
    term.add_argument("switch", choices=["on", "off"])
    term.add_argument("--workspace", help="workspace id or prefix (default: the current directory's)")
    term.add_argument("--state", type=Path)
    st = sub.add_parser("status")
    st.add_argument("--state", type=Path)
    st.add_argument("--run", type=Path)
    st.add_argument("--json", action="store_true")
    run = sub.add_parser("run", help="run the runner (the service manager runs this)")
    run.add_argument(
        "--foreground",
        action="store_true",
        help="dev and tests: announce this runner to a host started with orch host run --foreground --dev-runner",
    )
    run.add_argument("--state", type=Path)
    run.add_argument("--run", type=Path)
    unit = sub.add_parser("unit", help="print the runner's launchd or systemd unit")
    unit.add_argument("kind", choices=["launchd", "systemd"])
    unit.add_argument("--release", type=Path)
    unit.add_argument("--state", type=Path)
    unit.add_argument("--run", type=Path)
    return p


def _state(given: Path | None, env: Mapping[str, str]) -> Path:
    from orch.ops.runtime import state_dir

    return given.resolve() if given is not None else state_dir(env)


def _cwd_workspace_id(env: Mapping[str, str]) -> str:
    """The id of the workspace the current directory (or ``ORCH_WORKSPACE``) is in, as every other command finds it."""
    from orch import discovery

    explicit = env.get("ORCH_WORKSPACE")
    found = discovery.locate(explicit or os.getcwd(), explicit=bool(explicit), v1=False)
    wid = ((found.config if found is not None else None) or {}).get("workspace", {}).get("id")
    if not isinstance(wid, str):
        raise HostError("invalid.input", "name the workspace (--workspace ID or PREFIX): this directory is in none")
    return wid


def _workspace(state: Path, uid: int, wanted: str | None, env: Mapping[str, str] | None = None) -> tuple[str, str]:
    from orch.host.custody import read_json
    from orch.host.registry import registry_path

    if wanted is None:
        wanted = _cwd_workspace_id(os.environ if env is None else env)
    reg = read_json(registry_path(state), uid) or {"workspaces": []}
    match = [w for w in reg.get("workspaces") or [] if wanted in (w.get("workspace_id"), w.get("prefix"))]
    if len(match) != 1:
        raise HostError("not_found", f"no single registered workspace {wanted}")
    return str(match[0]["workspace_id"]), str(match[0].get("prefix") or "")


def _write(out: TextIO, doc: dict[str, Any]) -> None:
    out.write(json.dumps(doc, sort_keys=True) + "\n")


def _human(cmd: str, doc: dict[str, Any]) -> str:
    """N18: the result of ``enable`` or ``disable`` as plain lines for a person (``--json`` keeps the document)."""
    if not doc.get("ok"):
        from orch.host.cli import human_text

        return human_text(cmd, doc, subject="orch runner")
    data = doc["result"]["data"]
    wid = " ".join(str(data.get("workspace_id")).split())
    if cmd == "disable":
        return (
            f"Dashboard execution is off for workspace {wid}. The runner service stays installed but starts nothing.\n"
        )
    terms = "on" if data.get("terminals") else "off"
    return f"Dashboard execution is on for workspace {wid} (terminals {terms}).\n"


def _fail(code: str, message: str, exit_: int, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "exit": exit_, "error": {"code": code, "message": message[:400], "retryable": False}, **extra}


def enable(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, *, confirmed: bool = False) -> dict[str, Any]:
    """``confirmed``: the caller (``orch setup``) already showed :data:`enablement.CARD_TEXT` (the runner flag of its
    review; :func:`enablement.risk_text` embeds it) and has the person's consent for exactly that text; the typed-prefix
    prompt is skipped. Nothing else changes."""
    from orch.host import cli as host_cli
    from orch.host import enable as svc
    from orch.host.custody import ensure_private_dir
    from orch.host.release import ReleaseError, _package_dir, install_release, releases_dir

    from . import service

    if env.get("ORCH_GRANT"):
        return _fail("human_only", "dashboard execution is a person's decision; an agent never turns it on", 3)
    uid = os.getuid()
    state = _state(args.state, env)
    wid, prefix = _workspace(state, uid, args.workspace, env)
    run_dir = svc.run_dir_for(state, args.run, env)
    tier = service.agent_tier(state, uid)
    if tier.kind == "broken":
        return _fail(
            "runner.unavailable",
            f"agents are set up to run as their own user ({tier.agent_user}), but that is not safe right now: "
            f"{tier.why} Dashboard execution stays off.",
            9,
        )
    try:
        if tier.kind == "B":  # the system unit root installed: read-only here, never written by the person's CLI
            unit = service.plan_tier_b(str(tier.person), service.tier_b_release(env), uid=uid)
        else:
            unit = service.plan(env, state, run_dir, releases_dir(dict(env)) / "current")
    except svc.Unsupported as exc:
        return _fail("unsupported.platform", str(exc), 3)
    except ValueError as exc:
        return _fail("invalid.input", f"the unit cannot hold this path: {exc}", 5)
    if not args.dev:
        from orch.host.custody import read_json
        from orch.host.registry import registry_path

        try:  # as orch host enable: an interpreter inside a registered workspace is dev code too
            roots = [str(w.get("root")) for w in (read_json(registry_path(state), uid) or {}).get("workspaces") or []]
        except HostError:
            roots = []
        why = svc.dev_source(Path(sys.executable), _package_dir(), roots)
        if why is not None:
            return _fail(
                "invalid.input",
                f"{why}: the runner would keep running whatever that tree holds next. Run orch runner enable from an "
                "installed orch, or pass --dev",
                5,
            )
    manager = host_cli.SERVICE_MANAGER(unit)
    agent_user = tier.agent_user if tier.kind == "B" else None
    if agent_user is not None:
        if svc.differences(unit) is None:
            return _fail(
                "runner.unavailable",
                f"the agent runner service ({unit.label}) is not installed: run orch-os-setup again",
                9,
            )
        if manager.active() is not True:
            return _fail(
                "runner.unavailable",
                f"the agent runner service ({unit.label}) is not running. It checks the sudo and SSH rules at every "
                "start and refuses while they are not safe: run orch-os-setup again to see why",
                9,
            )
    else:  # tier A: an installed unit for another setup is never replaced silently
        differs = svc.differences(unit)
        if not args.replace and (differs or (differs is None and host_cli._loaded(manager))):
            named = differs or ["unit"]  # no unit file here, but the label is loaded: from a unit file elsewhere
            what = ", ".join(named) + " differ" if differs else "unit differs: the label is loaded from another file"
            return _fail(
                "host.busy",
                f"a runner service ({unit.label}) is already installed for another setup ({what}); replacing it ends "
                "its runs. To do that on purpose: orch runner enable --replace",
                4,
                differs=named,
            )
    terminals = not args.no_terminals
    text = enablement.risk_text(prefix or wid[:8], wid, terminals, agent_user)
    if not confirmed:
        try:
            TERMINAL.show(text)
            typed = TERMINAL.ask(f"Type the workspace prefix {prefix} to turn on dashboard execution: ")
        except Exception as exc:  # noqa: BLE001 - no terminal: refuse (never stdin, which an agent may hold)
            return _fail("human_only", f"orch runner enable needs your own terminal ({exc})", 3)
        if typed.strip() != prefix or not prefix:
            return _fail("invalid.input", "the prefix did not match; dashboard execution stays off", 5)
    try:
        install_release(target=releases_dir(dict(env)))
    except (ReleaseError, OSError) as exc:
        return _fail("internal", f"the release could not be installed: {exc}", 1)
    try:
        ensure_private_dir(Path(state), uid)
        ensure_private_dir(Path(state) / "runner", uid)  # launchd opens runner.out there before exec
        mask = os.umask(0)
        os.umask(mask)
        from orch.host.custody import write_private

        write_private(Path(state) / "runner" / "settings.json", {"v": 1, "umask": mask})  # runs start with it
        changed, ran = False, []  # tier B: the service is root's, already running; nothing to install or start
        if agent_user is None:
            changed = manager.install()
            ran = manager.start(changed=changed)
    except (OSError, HostError) as exc:
        return _fail("runner.unavailable", f"the {unit.kind} runner service did not start: {exc}", 9)
    enablement.set_enabled(state, wid, True, uid, terminals=terminals, text=text)
    from orch.host.release import code_digest

    try:  # a changed unit was just re-bootstrapped: a fresh process on the new release, nothing to restart
        stale = None if changed else service.restart_if_stale(unit, manager, state, code_digest())
    except OSError:
        stale = None
    data = {"workspace_id": wid, "enabled": True, "terminals": terminals, "service": unit.kind,
            "unit": str(unit.unit_path), "changed": changed, "ran": ran,
            "card": enablement.card_text(agent_user)}  # fmt: skip
    if agent_user is not None:
        data["agent_user"] = agent_user
    if stale is not None:
        data["runner_message"] = stale["message"]
        data["runner_restarted"] = stale["restarted"]
    return {"ok": True, "result": {"data": data}}


def terminals(args: argparse.Namespace, env: Mapping[str, str]) -> dict[str, Any]:
    """``orch runner terminals on|off --workspace W``: the person's own terminal for ``on`` (it widens what the
    dashboard may start), never under ``ORCH_GRANT``; ``off`` needs no prompt."""
    if env.get("ORCH_GRANT"):
        return _fail("human_only", "terminals are a person's decision; an agent never turns them on", 3)
    uid = os.getuid()
    state = _state(args.state, env)
    wid, prefix = _workspace(state, uid, args.workspace, env)
    on = args.switch == "on"
    text = ""
    if on:
        text = enablement.TERMINALS_ON_TEXT
        try:
            TERMINAL.show(enablement.risk_text(prefix or wid[:8], wid, True) + "\n" + text + "\n")
            typed = TERMINAL.ask(f"Type the workspace prefix {prefix} to turn terminals on: ")
        except Exception as exc:  # noqa: BLE001 - no terminal: refuse (never stdin, which an agent may hold)
            return _fail("human_only", f"orch runner terminals on needs your own terminal ({exc})", 3)
        if typed.strip() != prefix or not prefix:
            return _fail("invalid.input", "the prefix did not match; terminals stay as they were", 5)
    try:
        enablement.set_terminals(state, wid, on, uid, text=text)
    except HostError as exc:
        return {"ok": False, "exit": exc.exit, "error": exc.error()}
    return {"ok": True, "result": {"data": {"workspace_id": wid, "terminals": on}}}


def _host_endpoint(uid: int, run_dir: Path, *, pins: Any = None) -> tuple[int, Path]:
    """Where the host is and whose it is: ``(host UID, runner socket)``.

    Tier A (no root-owned agent pin for this UID): the host is this UID and the socket is ``<--run>/runner/host.sock``.
    Tier B (spec 13.3): this UID is the pinned agent user, so the host is the PERSON the root-owned pin names, at
    ``/run/orch/<person uid>/runner/host.sock``; ``--run`` must be this agent's own run directory
    (``/run/orch/agents/<user>``). A pin that cannot be trusted, or any other ``--run``, refuses to start: a runner
    never falls back to tier A while a pin exists (it would look for a host that is the agent itself)."""
    from orch.host.listener import default_run_directory
    from orch.host.pins import AgentPins
    from orch.setup.osusers import AGENT_RUN_ROOT

    pins = pins if pins is not None else AgentPins()
    doc = pins.get(uid)
    if doc is None:
        return uid, Path(run_dir) / "runner" / "host.sock"
    host = doc.get("host_uid")
    if doc.get("class") != "agent" or type(host) is not int or host in (0, uid):
        raise SystemExit(f"orch-runner: the pin of UID {uid} is not a usable agent pin; not starting")
    import pwd

    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        raise SystemExit(f"orch-runner: UID {uid} is not in the user database; not starting") from None
    if Path(run_dir) != Path(AGENT_RUN_ROOT) / user:
        raise SystemExit(f"orch-runner: an agent runner uses {AGENT_RUN_ROOT}/{user} as --run, not {run_dir}")
    return host, default_run_directory(host) / "runner" / "host.sock"


def term_peer_uid(uid: int, host_uid: int) -> int | None:
    """Whose connections the term socket serves: ``None`` (the runner's own UID) in tier A,
    the person's UID in tier B."""
    return host_uid if host_uid != uid else None


def _run(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    from orch.host import enable as svc

    from .link import HostLink, new_gen
    from .supervise import Supervisor
    from .term import TermListener

    state = _state(args.state, env)
    from orch.update.guard import hold_state_root

    _state_root = hold_state_root(state)  # refuses during an unfinished update; shared lock for the process lifetime
    run_dir = svc.run_dir_for(state, args.run, env)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uid = os.getuid()
    host_uid, host_socket = _host_endpoint(uid, run_dir)
    sup = Supervisor(state)
    swept = sup.open()
    log = logging.getLogger("orch.runner")
    for item in swept:
        log.warning("run %s was lost (runner restart); swept %d", item["run"], item["swept"])
    gen = new_gen()
    # tier B (host_uid is the person): only the person's dashboard may connect, by ACL and by peer UID (spec 13.5)
    term = TermListener(Path(run_dir) / "term" / str(uid) / "term.sock", uid, gen=gen, sup=sup,
                        peer_uid=term_peer_uid(uid, host_uid))  # fmt: skip
    try:
        term.open()
    except OSError as exc:
        log.warning("the terminal socket is not served: %s", exc)
        term = None  # type: ignore[assignment]
    from .config import read_config
    from .grants import GrantStore, RedactFilter

    grants = GrantStore()  # in memory only (D-P2-27): a restart loses every grant
    for handler in logging.getLogger().handlers:  # defence in depth: no held secret ever reaches runner.out
        handler.addFilter(RedactFilter(grants))

    announced = None
    if args.foreground:  # dev and tests: a host with --dev-runner admits this process without a service manager
        from . import devmode

        try:
            announced = devmode.announce(Path(run_dir), uid)
            log.warning("dev runner: announced in %s (only a host started with --dev-runner admits it)", announced)
        except OSError as exc:
            log.warning("dev runner: not announced: %s", exc)
    # the link refuses a socket whose peer is not the host UID: this UID in tier A, the person's in tier B
    from .service import current_release, write_loaded_release

    release = current_release()  # eager: the release this runner loaded, for the host's hello and for enable
    write_loaded_release(state, release)
    stop = threading.Event()
    from .restart import RestartGate

    gate = RestartGate(lambda: len(sup.table.live()), stop, lock=sup._spawn_lock)
    refused = threading.Event()

    def give_up() -> None:  # refused again and again: exit non-zero, `Restart=always` starts a fresh process
        refused.set()
        stop.set()

    link = HostLink(sup, host_socket, host_uid=host_uid, gen=gen, term=term,
                    config=lambda: read_config(state, uid), grants=grants, release=release,
                    restart=gate.request, restarting=lambda: gate.armed, on_denied=give_up)  # fmt: skip
    link.start()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())
    try:
        stop.wait()
    finally:
        link.stop()  # first: no new command while the runs are stopped
        try:
            # a controlled stop (D-P2-39): every managed run is stopped and verified before the runner exits
            sup.close(stop_runs=True)
        finally:
            if term is not None:
                term.close()
            if announced is not None:
                from . import devmode

                devmode.withdraw(announced, os.getpid())
    return 1 if refused.is_set() else 0


def main(argv: list[str], *, env: Mapping[str, str] | None = None, out: TextIO | None = None) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "enable":
            doc = enable(args, env, out)
            if args.json:
                _write(out, doc)
            else:
                out.write(_human("enable", doc))
            return 0 if doc.get("ok") else int(doc.get("exit", 1))
        if args.cmd == "disable":
            state = _state(args.state, env)
            wid, _ = _workspace(state, os.getuid(), args.workspace, env)
            enablement.set_enabled(state, wid, False, os.getuid())
            done = {"ok": True, "result": {"data": {"workspace_id": wid, "enabled": False}}}
            if args.json:
                _write(out, done)
            else:
                out.write(_human("disable", done))
            return 0
        if args.cmd == "terminals":
            doc = terminals(args, env)
            _write(out, doc)
            return 0 if doc.get("ok") else int(doc.get("exit", 1))
        if args.cmd == "status":
            return _status(args, env, out)
        if args.cmd == "run":
            return _run(args, env)
        if args.cmd == "unit":
            from orch.host import enable as svc
            from orch.host.release import releases_dir

            from . import service

            state = _state(args.state, env)
            release = args.release or releases_dir(dict(env)) / "current"
            run_dir = svc.run_dir_for(state, args.run, env)
            if args.kind == "launchd":
                out.write(service.launchd_plist(Path(sys.executable), release, state, run_dir).decode())
            else:
                out.write(service.systemd_unit(Path(sys.executable), release, state, run_dir))
            return 0
    except HostError as exc:
        _write(out, {"ok": False, "error": exc.error()})
        return exc.exit
    return 2


def _status(args: argparse.Namespace, env: Mapping[str, str], out: TextIO) -> int:
    from orch.host import cli as host_cli
    from orch.host import enable as svc
    from orch.host.release import releases_dir

    from . import service

    state = _state(args.state, env)
    uid = os.getuid()
    try:
        switches = enablement.read(state, uid)["workspaces"]
        problem = None
    except HostError as exc:
        switches, problem = {}, exc.message
    doc: dict[str, Any] = {
        "workspaces": {
            w: {"enabled": True, "terminals": e.get("terminals") is True, "at": e.get("at")}
            for w, e in switches.items()
            if isinstance(e, dict) and e.get("enabled") is True
        },  # fmt: skip
        "problem": problem,
        "card": enablement.CARD_TEXT,
    }
    try:
        tier = service.agent_tier(state, uid)
        if tier.kind == "B":
            unit = service.plan_tier_b(str(tier.person), service.tier_b_release(env), uid=uid)
            doc["card"] = enablement.card_text(tier.agent_user)
            doc["agent_user"] = tier.agent_user
        else:
            unit = service.plan(env, state, svc.run_dir_for(state, args.run, env), releases_dir(dict(env)) / "current")
            if tier.kind == "broken":
                doc["problem"] = problem or tier.why
        try:
            doc["service"] = host_cli.SERVICE_MANAGER(unit).status()
        except OSError as exc:
            doc["service"] = {"kind": unit.kind, "unit": str(unit.unit_path), "error": str(exc)[:200]}
    except svc.Unsupported as exc:
        doc["service"] = None
        doc["problem"] = problem or str(exc)
    if args.json:
        _write(out, {"ok": True, "result": {"data": doc}})
    else:
        for wid, entry in sorted(doc["workspaces"].items()):
            extra = "" if entry["terminals"] else " (terminals hidden)"
            out.write(f"dashboard execution on: {wid}{extra} since {entry['at']}\n")
        if not doc["workspaces"]:
            out.write("dashboard execution is off for every workspace\n")
        svc_doc = doc.get("service") or {}
        out.write(f"runner service: {svc_doc.get('unit', 'none')} running={svc_doc.get('running')}\n")
        if doc["problem"]:
            out.write(f"problem: {doc['problem']}\n")
    return 0
