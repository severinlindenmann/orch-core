"""``orch runner …``: the machine switch, the runner service and the runner process itself.

* ``orch runner enable --workspace W [--no-terminals] [--dev] [--replace]`` (person, own terminal only): shows the
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


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch runner", description="the orch runner (dashboard execution)")
    sub = p.add_subparsers(dest="cmd", required=True)
    en = sub.add_parser("enable", help="turn on dashboard execution for a workspace and start the runner service")
    en.add_argument("--workspace", required=True, help="workspace id or prefix")
    en.add_argument("--no-terminals", action="store_true", help="hide terminals (does not make agents safe)")
    en.add_argument("--replace", action="store_true", help="replace a runner service installed for another setup")
    en.add_argument("--dev", action="store_true", help="allow an interpreter or orch package from a source checkout")
    en.add_argument("--state", type=Path)
    en.add_argument("--run", type=Path)
    dis = sub.add_parser("disable", help="turn dashboard execution off for a workspace")
    dis.add_argument("--workspace", required=True)
    dis.add_argument("--state", type=Path)
    st = sub.add_parser("status")
    st.add_argument("--state", type=Path)
    st.add_argument("--run", type=Path)
    st.add_argument("--json", action="store_true")
    run = sub.add_parser("run", help="run the runner (the service manager runs this)")
    run.add_argument("--foreground", action="store_true")
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


def _workspace(state: Path, uid: int, wanted: str) -> tuple[str, str]:
    from orch.host.custody import read_json
    from orch.host.registry import registry_path

    reg = read_json(registry_path(state), uid) or {"workspaces": []}
    match = [w for w in reg.get("workspaces") or [] if wanted in (w.get("workspace_id"), w.get("prefix"))]
    if len(match) != 1:
        raise HostError("not_found", f"no single registered workspace {wanted}")
    return str(match[0]["workspace_id"]), str(match[0].get("prefix") or "")


def _write(out: TextIO, doc: dict[str, Any]) -> None:
    out.write(json.dumps(doc, sort_keys=True) + "\n")


def _fail(code: str, message: str, exit_: int, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "exit": exit_, "error": {"code": code, "message": message[:400], "retryable": False}, **extra}


def enable(args: argparse.Namespace, env: Mapping[str, str], out: TextIO) -> dict[str, Any]:
    from orch.host import cli as host_cli
    from orch.host import enable as svc
    from orch.host.custody import ensure_private_dir
    from orch.host.release import ReleaseError, _package_dir, install_release, releases_dir

    from . import service

    if env.get("ORCH_GRANT"):
        return _fail("human_only", "dashboard execution is a person's decision; an agent never turns it on", 3)
    uid = os.getuid()
    state = _state(args.state, env)
    wid, prefix = _workspace(state, uid, args.workspace)
    run_dir = svc.run_dir_for(state, args.run, env)
    try:
        unit = service.plan(env, state, run_dir, releases_dir(dict(env)) / "current")
    except svc.Unsupported as exc:
        return _fail("unsupported.platform", str(exc), 3)
    except ValueError as exc:
        return _fail("invalid.input", f"the unit cannot hold this path: {exc}", 5)
    if not args.dev:
        why = svc.dev_source(Path(sys.executable), _package_dir())
        if why is not None:
            return _fail(
                "invalid.input",
                f"{why}: the runner would keep running whatever that tree holds next. Run orch runner enable from an "
                "installed orch, or pass --dev",
                5,
            )
    manager = host_cli.SERVICE_MANAGER(unit)
    differs = svc.differences(unit)
    if not args.replace and differs:
        return _fail(
            "host.busy",
            f"a runner service ({unit.label}) is already installed for another setup ({', '.join(differs)} differ); "
            "replacing it ends its runs. To do that on purpose: orch runner enable --replace",
            4,
            differs=differs,
        )
    terminals = not args.no_terminals
    text = enablement.risk_text(prefix or wid[:8], wid, terminals)
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
        changed = manager.install()
        ran = manager.start(changed=changed)
    except (OSError, HostError) as exc:
        return _fail("runner.unavailable", f"the {unit.kind} runner service did not start: {exc}", 9)
    enablement.set_enabled(state, wid, True, uid, terminals=terminals, text=text)
    data = {"workspace_id": wid, "enabled": True, "terminals": terminals, "service": unit.kind,
            "unit": str(unit.unit_path), "changed": changed, "ran": ran, "card": enablement.CARD_TEXT}  # fmt: skip
    return {"ok": True, "result": {"data": data}}


def _run(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    from orch.host import enable as svc

    from .link import HostLink, next_generation
    from .supervise import Supervisor
    from .term import TermListener

    state = _state(args.state, env)
    run_dir = svc.run_dir_for(state, args.run, env)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uid = os.getuid()
    sup = Supervisor(state)
    swept = sup.open()
    log = logging.getLogger("orch.runner")
    for item in swept:
        log.warning("run %s was lost (runner restart); swept %d", item["run"], item["swept"])
    generation = next_generation(Path(state) / "runner", uid)
    term = TermListener(Path(run_dir) / f"term-{uid}" / "term.sock", uid)
    try:
        term.open()
    except OSError as exc:
        log.warning("the terminal socket is not served: %s", exc)
        term = None  # type: ignore[assignment]
    # tier A: the host runs as this UID; the link refuses a socket whose peer is another UID
    link = HostLink(sup, Path(run_dir) / "runner" / "host.sock", host_uid=uid, generation=generation, term=term)
    link.start()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())
    try:
        stop.wait()
    finally:
        link.stop()
        if term is not None:
            term.close()
        sup.close()
    return 0


def main(argv: list[str], *, env: Mapping[str, str] | None = None, out: TextIO | None = None) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "enable":
            doc = enable(args, env, out)
            _write(out, doc)
            return 0 if doc.get("ok") else int(doc.get("exit", 1))
        if args.cmd == "disable":
            state = _state(args.state, env)
            wid, _ = _workspace(state, os.getuid(), args.workspace)
            enablement.set_enabled(state, wid, False, os.getuid())
            _write(out, {"ok": True, "result": {"data": {"workspace_id": wid, "enabled": False}}})
            return 0
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
        unit = service.plan(env, state, svc.run_dir_for(state, args.run, env), releases_dir(dict(env)) / "current")
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
