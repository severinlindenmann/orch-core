"""The Simulator dev gate (orch-v2-host §11A.17, owner option 2 of 15 Oct 2026): a **dev host** started with
``orch host run --foreground --dev-sim-convenience`` accepts ``auth:"convenience"`` on column R for
``question.answered`` only, and only on a throwaway ``dev-pair`` **sandbox workspace**. Off by default; refused on
every real workspace.

* :func:`check_launch` and :func:`prove` run once before the control socket binds (17.2). Any failure is the
  start-up refusal ``host.dev_refused`` with a reason; the host never falls back to a normal host.
* :func:`sandbox_reason` is S1 to S6 of 17.3; :func:`dev_gate` the one function that answers "may column R accept
  convenience for this workspace now" (fresh, under the store lock); :func:`gated` its lock-free cached form (P5 and
  ``required_reader``).
* :func:`check_admit` is the dev side of the one registration choke point :func:`orch.host.registry.admit`: a dev
  host admits sandbox workspaces only (R5), a normal host refuses every ``SBX`` genesis and every workspace with the
  marker (F2).
* :func:`refuse_link` and :func:`refuse_push`: ``relay.link`` to another origin on a dev host (``dev_origin``), a
  records push from a dev host (``dev_host``), both for an ``SBX`` workspace on a normal host (``sandbox_workspace``).

``ORCH_NO_CONVENIENCE`` (any non-empty value) makes :func:`dev_gate` return ``None`` (narrowing, 11A.15.11).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orch import devsandbox
from orch import layout as layout_mod
from orch.devsandbox import RESERVED_PREFIX, SandboxRefused

from .errors import HostError

LOG = logging.getLogger("orch.host.devmode")

__all__ = [
    "FLAG",
    "REASONS",
    "DevGate",
    "DevHost",
    "View",
    "check_admit",
    "check_launch",
    "dev_gate",
    "gated",
    "no_convenience",
    "prove",
    "refuse_link",
    "refuse_push",
    "refused",
    "sandbox_reason",
    "service_view",
    "status_line",
]

FLAG = "--dev-sim-convenience"
#: the closed reasons of ``host.dev_refused`` (§0.6)
REASONS = (
    "not_foreground",
    "service_managed",
    "service_dir",
    "no_manifest",
    "state_not_in_root",
    "home_not_in_root",
    "no_dev_json",
    "dev_json_mismatch",
    "no_person",
    "custody_convenience",
    "not_sandbox",
    "sandbox_workspace",
    "dev_origin",
    "dev_host",
)
#: systemd's markers of a service-manager launch (§12.3.3 "As built in H11a", the ``--dev-runner`` rule)
SYSTEMD_MARKERS = ("INVOCATION_ID", "LISTEN_PID", "JOURNAL_STREAM")
#: the one event type a convenience-signed column-R event may have (DECIDED with orch-b8: answers only)
GATED_TYPE = "question.answered"
SANDBOX_FIX = "this is a throwaway dev-pair sandbox; start a real workspace"
_WS_DIR = re.compile(r"ws(-[0-9]{1,4})?")


def refused(reason: str, message: str, *, hint: str | None = None) -> HostError:
    assert reason in REASONS, reason
    return HostError("host.dev_refused", f"{reason}: {message}", hint=hint, detail={"reason": reason})


def no_convenience(env: Mapping[str, str] | None = None) -> bool:
    """``ORCH_NO_CONVENIENCE`` set to any non-empty value (``0`` and ``false`` too)."""
    return bool((os.environ if env is None else env).get("ORCH_NO_CONVENIENCE"))


@dataclass(frozen=True)
class DevHost:
    """The result of a passed start-up proof, fixed for the life of the process."""

    root: Path
    root_id: tuple[int, int]
    sandbox_id: str
    person_id: str
    relay_origin: str

    @property
    def id8(self) -> str:
        return self.sandbox_id[:8]


@dataclass(frozen=True)
class DevGate:
    """The token :func:`dev_gate` returns: passed from L2 of a column-R job down to the append (L8), where a fresh
    :func:`dev_gate` call must return an equal one (F6)."""

    sandbox_id: str
    workspace_id: str


@dataclass(frozen=True)
class View:
    """What S1 to S6 read of one workspace: its verified genesis, members, former members and link origin."""

    genesis: Mapping[str, Any]
    members: frozenset[str]
    former: frozenset[str] = field(default_factory=frozenset)
    link_origin: str | None = None


# -- the start (17.2) --------------------------------------------------------------------------------------------------


def check_launch(foreground: bool, env: Mapping[str, str], run_dir: Path, state_dir: Path) -> None:
    """Check 1: foreground only, never under a service manager's label or markers, never with a service run
    directory (by identity). Reading the environment here only narrows."""
    from orch.runner import devmode as runner_devmode
    from orch.runner.service import LAUNCHD_LABEL as RUNNER_LABEL

    from .units import LAUNCHD_LABEL

    if not foreground:
        raise refused("not_foreground", f"{FLAG} needs --foreground (a person's terminal, never a service unit)")
    if env.get("XPC_SERVICE_NAME") in (LAUNCHD_LABEL, RUNNER_LABEL):
        raise refused("service_managed", f"{FLAG} is never for a service-managed host (launchd)")
    for marker in SYSTEMD_MARKERS:
        if env.get(marker):
            raise refused("service_managed", f"{FLAG} is never for a service-managed host ({marker} is set)")
    for path in (run_dir, state_dir):
        if runner_devmode.is_service_run_dir(path):
            raise refused("service_dir", f"{FLAG} is never for a service directory ({path})")


def _same(dir_fd: int, name: str, path: str | os.PathLike[str]) -> bool:
    try:
        here = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        there = os.lstat(path)
    except OSError:
        return False
    return (here.st_dev, here.st_ino) == (there.st_dev, there.st_ino) and os.path.isdir(path)


def _custody_convenience(state: Path) -> bool:
    """Check 5: any ``<state>/person/<ws>/custody.json`` that selects convenience or is moving to it (an unreadable
    one counts: refuse rather than guess)."""
    root = state / "person"
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    for name in names:
        path = root / name / "custody.json"
        if not os.path.lexists(path):
            continue
        try:
            doc = json.loads(path.read_bytes())
        except (OSError, ValueError):
            return True
        if not isinstance(doc, dict):
            return True
        transition = doc.get("transition")
        if doc.get("custody") != "passphrase" or (
            isinstance(transition, dict) and transition.get("to") != "passphrase"
        ):
            return True
    return False


def prove(state_dir: Path, env: Mapping[str, str]) -> DevHost:
    """Checks 2, 3 and 5 of 17.2 (check 4's registry part runs in :meth:`orch.host.service.Host._prepare`, once the
    registry is loaded): ``ORCH_STATE_DIR`` is ``<root>/state`` and ``HOME`` is ``<root>/home`` by ``st_dev``/``st_ino``
    through the pinned root descriptor, ``<root>`` holds a valid manifest, ``dev.json`` (read through the pinned
    state directory) names the same sandbox and root, the manifest names the sandbox person, and no person custody
    selects convenience."""
    from orch.wsproto import wire

    state = Path(os.path.abspath(state_dir))
    root = state.parent
    try:
        fd, st = devsandbox.open_root(root)
    except SandboxRefused as exc:
        raise refused("no_manifest", f"{root} is not a dev-pair sandbox root ({exc})") from None
    sfd = hfd = -1
    try:
        try:
            manifest = devsandbox.manifest_at(fd, st)
        except SandboxRefused as exc:
            raise refused("no_manifest", f"{root}: {exc}") from None
        if state.name != "state" or not _same(fd, "state", state):
            raise refused("state_not_in_root", f"the state directory must be {root}/state, not {state}")
        home = env.get("HOME") or ""
        if not home or not _same(fd, "home", home):
            raise refused("home_not_in_root", f"HOME must be {root}/home")
        try:
            sfd = os.open("state", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            hfd = os.open("host", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=sfd)
            raw = devsandbox.read_private(hfd, devsandbox.DEV_JSON)
        except (OSError, SandboxRefused) as exc:
            raise refused("no_dev_json", f"{state}/host/dev.json is missing or not private ({exc})") from None
        try:
            dev = devsandbox.parse_dev_json(raw)
        except SandboxRefused as exc:
            raise refused("dev_json_mismatch", str(exc)) from None
        root_pin = (dev["root"]["st_dev"], dev["root"]["st_ino"])
        if dev["sandbox_id"] != manifest["id"] or root_pin != (st.st_dev, st.st_ino):
            raise refused("dev_json_mismatch", "dev.json names another sandbox or root than the manifest")
        try:
            wire.check_origin(dev["relay_origin"])
        except Exception:  # noqa: BLE001 - WireError or a type error: not an origin
            raise refused(
                "dev_json_mismatch", "dev.json's relay_origin is not an origin (scheme://host[:port])"
            ) from None
        person = manifest.get("person_id")
        if not isinstance(person, str):
            raise refused("no_person", "the manifest names no sandbox person (person_id, written after orch init)")
        if _custody_convenience(state):
            raise refused("custody_convenience", "a person custody selects convenience: the dev path never mixes it in")
        return DevHost(root, (st.st_dev, st.st_ino), manifest["id"], person, dev["relay_origin"])
    finally:
        for d in (hfd, sfd, fd):
            if d >= 0:
                os.close(d)


def status_line(dev: DevHost) -> str:
    return f"DEV HOST: Simulator convenience answers ON for sandbox {dev.id8} (throwaway workspaces only)"


# -- S1 to S6 (17.3) ---------------------------------------------------------------------------------------------------


def sandbox_reason(
    dev: DevHost, workspace_id: str, ws_root: str | os.PathLike[str], view: View, *, s6: bool = True
) -> str | None:
    """The first of S1 to S6 that fails for this workspace on ``dev`` (a short text naming it), or ``None``. Reads
    the manifest again through a fresh root descriptor (a change since the start is seen)."""
    try:
        fd, st = devsandbox.open_root(dev.root)
    except SandboxRefused as exc:
        return f"S1: the sandbox root is gone ({exc})"
    try:
        if (st.st_dev, st.st_ino) != dev.root_id:
            return "S1: the sandbox root was replaced"
        try:
            manifest = devsandbox.manifest_at(fd, st)
        except SandboxRefused as exc:
            return f"S2: {exc}"
        if manifest["id"] != dev.sandbox_id or manifest.get("person_id") != dev.person_id:
            return "S2: the manifest changed since the start"
        row = next((r for r in manifest.get("workspaces", []) if r["id"] == workspace_id), None)
        if row is None:
            return "S2: the manifest does not list this workspace"
        path = Path(os.path.abspath(ws_root))
        try:
            parent = os.stat(path.parent)
        except OSError:
            parent = None
        if parent is None or (parent.st_dev, parent.st_ino) != dev.root_id or not _WS_DIR.fullmatch(path.name):
            return "S1: the workspace root is not <root>/ws or <root>/ws-<n>"
        try:
            here = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            there = os.lstat(path)
        except OSError:
            return "S1: the workspace root cannot be reached from the sandbox root"
        if (here.st_dev, here.st_ino) != (row["st_dev"], row["st_ino"]) or (there.st_dev, there.st_ino) != (
            here.st_dev,
            here.st_ino,
        ):
            return "S1: the workspace root is not the one the manifest recorded (moved or copied)"
    finally:
        os.close(fd)
    genesis = view.genesis
    owner = genesis.get("owner") if isinstance(genesis.get("owner"), Mapping) else {}
    if genesis.get("prefix") != RESERVED_PREFIX:
        return "S3: the genesis prefix is not SBX"
    if owner.get("name") != devsandbox.owner_name(dev.sandbox_id):
        return "S3: the genesis owner name is not the sandbox's"
    if owner.get("person") != dev.person_id:
        return "S3: the genesis owner is not the sandbox person"
    if view.members != frozenset({dev.person_id}) or view.former:
        return "S4: the sandbox person is not the only member"
    try:
        marker = devsandbox.read_marker(ws_root)
    except SandboxRefused as exc:
        return f"S5: {exc}"
    if marker["sandbox_id"] != dev.sandbox_id or marker["workspace_id"] != workspace_id:
        return "S5: the workspace marker names another sandbox or workspace"
    if s6 and view.link_origin is not None and view.link_origin != dev.relay_origin:
        return "S6: the relay link is not dev.json's relay_origin"
    return None


def _link_origin(state_dir: Path, workspace_id: str, host_uid: int) -> str | None:
    """The workspace's ``link.json`` origin, ``None`` without a link; an unreadable link is a sentinel that never
    equals an origin (S6 then fails)."""
    from .custody import read_json
    from .relaylink import link_path

    path = link_path(state_dir, workspace_id)
    if not os.path.lexists(path):
        return None
    try:
        doc = read_json(path, host_uid)
    except Exception:  # noqa: BLE001 - unreadable: S6 fails
        return "\x00unreadable"
    origin = doc.get("origin") if isinstance(doc, dict) else None
    return origin if isinstance(origin, str) else "\x00unreadable"


def service_view(service: Any) -> View | None:
    """The served workspace's view, from its open store (call it on the worker thread, under the store lock)."""
    store = service.store
    if store is None:
        return None
    genesis = store.genesis_event()
    if genesis is None:
        return None
    ws = store.state.workspace
    return View(
        genesis,
        frozenset(ws.members),
        frozenset(ws.former),
        _link_origin(Path(service.state_dir), service.workspace_id, service.host_uid),
    )


def dev_gate(service: Any) -> DevGate | None:
    """**The dev gate** (17.1): a :class:`DevGate` when the host is a dev host, ``ORCH_NO_CONVENIENCE`` is unset and
    the workspace passes S1 to S6 now (on fresh state: the store lock is taken, which re-reads it); else ``None``.
    Runs on the workspace's worker thread."""
    dev = getattr(service, "dev", None)
    if dev is None or no_convenience():
        return None
    store = getattr(service, "store", None)
    if store is None or getattr(service, "state", "unavailable") == "unavailable":
        return None
    try:
        with store.locked():
            view = service_view(service)
            if view is None:
                return None
            reason = sandbox_reason(dev, service.workspace_id, service.entry.root, view)
    except Exception:  # noqa: BLE001 - any doubt: no gate
        return None
    if reason is not None:
        return None
    return DevGate(dev.sandbox_id, service.workspace_id)


def gated(service: Any) -> bool:
    """The lock-free form for P5 and ``required_reader``: a dev host whose last fresh check of this workspace
    (at serve, and at every L2) passed, and ``ORCH_NO_CONVENIENCE`` unset. Never an admission by itself: L2 and L8
    call :func:`dev_gate` again."""
    return (
        getattr(service, "dev", None) is not None and getattr(service, "dev_ok", False) is True and not no_convenience()
    )


# -- admission, link, push (17.3 R5, F2, F5) ---------------------------------------------------------------------------


def check_admit(dev: DevHost | None, workspace_id: str, ws_root: str | os.PathLike[str], view: View) -> None:
    """The dev part of :func:`orch.host.registry.admit`: a dev host admits a workspace only when S1 to S5 hold
    (``not_sandbox``); a normal host refuses an ``SBX`` genesis or a workspace with the marker
    (``sandbox_workspace``)."""
    if dev is None:
        if view.genesis.get("prefix") == RESERVED_PREFIX or devsandbox.marker_present(ws_root):
            raise refused("sandbox_workspace", f"workspace {workspace_id} is a dev-pair sandbox", hint=SANDBOX_FIX)
        return
    reason = sandbox_reason(dev, workspace_id, ws_root, view, s6=False)
    if reason is not None:
        raise refused(
            "not_sandbox",
            f"a dev host serves dev-pair sandbox workspaces only; {workspace_id} fails {reason}",
            hint="register real workspaces with your normal host (orch host run without --dev-sim-convenience)",
        )


def _is_sandbox_genesis(service: Any) -> bool:
    store = getattr(service, "store", None)
    genesis = None
    with contextlib.suppress(Exception):
        genesis = store.genesis_event() if callable(getattr(store, "genesis_event", None)) else None
    if isinstance(genesis, Mapping) and genesis.get("prefix") == RESERVED_PREFIX:
        return True
    entry = getattr(service, "entry", None)
    return entry is not None and (entry.prefix == RESERVED_PREFIX or devsandbox.marker_present(entry.root))


def refuse_link(service: Any, origin: str | None) -> None:
    """``relay.link`` (F5): on a dev host only to ``dev.json``'s ``relay_origin`` (``dev_origin``); on a normal host
    never for a sandbox workspace (``sandbox_workspace``). Called before anything is written."""
    dev = getattr(service, "dev", None)
    if dev is not None:
        if origin != dev.relay_origin:
            raise refused("dev_origin", "a dev host links a sandbox only to the dev relay dev-pair chose (dev.json)")
        return
    if _is_sandbox_genesis(service):
        raise refused("sandbox_workspace", "a dev-pair sandbox is never linked from a normal host", hint=SANDBOX_FIX)


def refuse_push(service: Any) -> None:
    """A records push (F5): never from a dev host (``dev_host``), never of a sandbox workspace
    (``sandbox_workspace``)."""
    if getattr(service, "dev", None) is not None:
        raise refused("dev_host", "a dev host never pushes records")
    if _is_sandbox_genesis(service):
        raise refused("sandbox_workspace", "a dev-pair sandbox's records are never pushed", hint=SANDBOX_FIX)


# -- the start-time registry check (17.2 check 4, 17.3 D3) -------------------------------------------------------------


def _raw_prefix(root: str) -> str | None:
    """The ``prefix`` of the first line of the workspace log, unverified: used only to narrow (skip)."""
    with contextlib.suppress(OSError, ValueError, TypeError, AttributeError, layout_mod.LayoutError):
        with open(layout_mod.store_of(root) / "events" / "workspace.jsonl", "rb") as handle:
            first = json.loads(handle.readline(1 << 20))
        prefix = first.get("prefix") if isinstance(first, dict) else None
        return prefix if isinstance(prefix, str) else None
    return None


def entry_view(state_dir: Path, workspace_id: str, root: str, host_uid: int) -> View | None:
    """A registered workspace's verified view (its pinned genesis), or ``None`` when it cannot be opened now."""
    from orch.store import Store
    from orch.store.pins import HostPins

    try:
        pins = HostPins(state_dir, workspace_id)
        try:
            pin = pins.genesis()
        finally:
            pins.close()
        if pin is None:
            return None
        store = Store.open(
            root, expected_workspace_id=workspace_id, expected_genesis=pin, host_state_dir=None, load="lazy"
        )
    except Exception:  # noqa: BLE001 - the caller decides (fail closed on a dev host, narrow on a normal one)
        return None
    try:
        genesis = store.genesis_event()
        if genesis is None:
            return None
        ws = store.state.workspace
        return View(
            genesis, frozenset(ws.members), frozenset(ws.former), _link_origin(state_dir, workspace_id, host_uid)
        )
    finally:
        store.close()


def check_registry(dev: DevHost | None, state_dir: Path, entries: Iterable[Any], host_uid: int) -> list[str]:
    """At start, before anything is served. On a dev host every registered workspace must pass S1 to S5, else the
    start is refused (``not_sandbox``). On a normal host an ``SBX`` workspace (the unverified first
    line or the registry's prefix: either of them) is skipped, never served; returns those ids for the alarm
    ``sandbox_workspace_registered``."""
    skipped: list[str] = []
    for entry in entries:
        if dev is not None:
            view = entry_view(state_dir, entry.workspace_id, entry.root, host_uid)
            reason = (
                "it cannot be opened"
                if view is None
                else sandbox_reason(dev, entry.workspace_id, entry.root, view, s6=False)
            )
            if reason is not None:
                raise refused(
                    "not_sandbox",
                    f"the state directory holds workspace {entry.workspace_id}, which is not a sandbox workspace "
                    f"({reason})",
                )
            continue
        # narrowing only, and cheap at every start: the prefix register recorded from the verified genesis, and the
        # genesis line itself (a tampered first line breaks the chain, so the workspace is unavailable anyway)
        if RESERVED_PREFIX in {entry.prefix, _raw_prefix(entry.root)}:
            LOG.warning(
                "alarm sandbox_workspace_registered: workspace %s is a dev-pair sandbox (SBX); not served",
                entry.workspace_id,
            )
            skipped.append(entry.workspace_id)
    return skipped
