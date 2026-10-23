"""C9, D79: orch core always works standalone. Static: nothing in orch-core (src, add-ons) imports or depends on hub
code. Behavioural: with no hub, relay or publish configured and every outbound connection failing, a fresh workspace
inits, runs a host, makes a ticket, passes a gate signed by the test human and reads it back; pairing and publish fail
closed with their existing codes and never try to connect."""

from __future__ import annotations

import ast
import errno
import ipaddress
import re
import socket
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

from tests.host import human_helpers
from tests.host.dispatch_helpers import Served
from tests.host.human_helpers import Human, ok

lab = human_helpers.lab

CORE = Path(__file__).resolve().parents[1]
REPO = CORE.parents[1]
SKIP_DIRS = {".venv", "node_modules", "__pycache__", ".git", "dist", "build"}
HUB_NAME = re.compile(r"^(orch[_.-]?hub|orchhub)(\.|$)", re.I)
DYNAMIC_IMPORTERS = {"import_module", "__import__"}


def python_files() -> list[Path]:
    roots = [CORE / "src", REPO / "addons"]
    found = []
    for root in roots:
        for p in root.rglob("*.py"):
            if not SKIP_DIRS & set(p.relative_to(root).parts):
                found.append(p)
    return sorted(found)


def hub_imports(source: str) -> list[tuple[int, str]]:
    """(line, module) of each import of hub code: ``import x``, ``from x import y``, relative ``from . import orch_hub``
    and a constant name given to ``importlib.import_module`` / ``__import__``."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names = [base, *(f"{base}.{a.name}" if base else a.name for a in node.names)]
        elif isinstance(node, ast.Call):
            fn = node.func
            called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if called in DYNAMIC_IMPORTERS and node.args and isinstance(node.args[0], ast.Constant):
                if isinstance(node.args[0].value, str):
                    names = [node.args[0].value]
        hits += [(node.lineno, n) for n in names if HUB_NAME.match(n)][:1]  # one finding per statement
    return sorted(hits)


def test_the_guard_itself_sees_every_form_of_hub_import():
    src = (
        "import orch_hub\nfrom orch_hub.x import y\nfrom . import orch_hub\nimport importlib\n"
        "importlib.import_module('orch_hub.z')\n__import__('orch.hub')\nimport os\nfrom orch import store\n"
    )
    assert [line for line, _ in hub_imports(src)] == [1, 2, 3, 5, 6]


def test_no_module_in_core_or_addons_imports_hub_code():
    files = python_files()
    assert len(files) > 100, "the walk found almost nothing: wrong roots"
    offenders = [
        f"{p.relative_to(REPO)}:{line}: {mod}" for p in files for line, mod in hub_imports(p.read_text("utf-8"))
    ]
    assert not offenders, "D79: orch core must not import hub code:\n" + "\n".join(offenders)


def test_core_metadata_and_lock_have_no_hub_dependency():
    declared = tomllib.loads((CORE / "pyproject.toml").read_text())
    deps = [*declared["project"]["dependencies"]]
    for group in declared.get("dependency-groups", {}).values():
        deps += [d for d in group if isinstance(d, str)]
    deps += [d for ds in declared["project"].get("optional-dependencies", {}).values() for d in ds]
    try:
        deps += metadata.requires("orch-core") or []
    except metadata.PackageNotFoundError:
        pass  # not installed as a distribution: the pyproject above is the source
    assert not [d for d in deps if re.match(r"\s*orch[-_.]?hub\b", d, re.I)], deps
    lock = CORE / "uv.lock"
    if lock.exists():
        names = {p["name"] for p in tomllib.loads(lock.read_text()).get("package", [])}
        assert not {n for n in names if re.match(r"orch[-_.]?hub", n, re.I)}, names


# --- behaviour with the network down ------------------------------------------------------------------------------


@pytest.fixture
def offline(monkeypatch):
    """Every outbound connection fails; unix sockets and loopback (the in-process host's own) keep working. The
    attempts are recorded, so a connection that is tried and swallowed still fails the test."""
    attempts: list[object] = []
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def outbound(sock, address) -> bool:
        if sock.family == socket.AF_UNIX:
            return False
        host = address[0] if isinstance(address, tuple) else address
        try:
            return not ipaddress.ip_address(host).is_loopback
        except ValueError:
            return True

    def connect(self, address):
        if outbound(self, address):
            attempts.append(address)
            raise OSError(errno.ENETUNREACH, "network unreachable (D79 test)")
        return real_connect(self, address)

    def connect_ex(self, address):
        if outbound(self, address):
            attempts.append(address)
            return errno.ENETUNREACH
        return real_connect_ex(self, address)

    def refuse(*a, **k):
        attempts.append(a[:1])
        raise OSError(errno.ENETUNREACH, "network unreachable (D79 test)")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    return attempts


@pytest.fixture
def served(lab, offline):
    """The host starts only after ``offline`` is active (it is a dependency), so a connect at startup is recorded."""
    s = Served(lab)
    s.offline_when_started = socket.socket.connect.__qualname__.endswith("<locals>.connect")  # the patched one
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def human(served):
    return Human(served)


def test_a_fresh_workspace_works_with_no_hub_relay_or_publish(human: Human, served, offline):
    assert served.offline_when_started, "the network block must be active before the host starts"
    assert not hasattr(served.service, "relay_starter")  # V3-D36: a workspace host has no relay client
    uid, key = human.ticket("standalone")
    for sid in ("context", "requirements", "out_of_scope"):
        human.run("section.set", {"ref": key, "section": sid, "message": f"the {sid}"})
    human.run("ac.add", {"ref": key, "text": "it works offline"})
    prepared, res, signed = human.run("approve", {"ref": key, "gate": "requirements"})  # local signing, test human
    assert res["data"] == {"gate": "requirements"} and signed.events
    events = served.lab.ws.events(key)
    assert events[-1]["type"] == "gate.approved"
    shown = ok(human.c.req("show", {"ref": key}))
    assert key in repr(shown) and "standalone" in repr(shown)

    # publish is off (add-on not enabled) or unconfigured, and says so; no send is attempted
    res = human.c.req("publish.list", {})
    assert res["ok"] is False and res["error"]["code"] in {"runner.disabled", "publish.not_configured"}, res
    res = human.c.req("publish.prepare", {"ref": key, "artifacts": ["a.md"], "days": 1, "kind": "once", "views": 1})
    assert res["ok"] is False, res

    assert offline == [], f"outbound connection attempted: {offline}"


def test_no_relay_client_module_exists():
    """V3-D36 / D69: a workspace never has a direct relay path, so core carries no relay client at all."""
    pkgs = [p.relative_to(CORE / "src").as_posix() for p in (CORE / "src" / "orch").iterdir() if p.is_dir()]
    mods = [p.stem for p in (CORE / "src" / "orch").glob("*.py")]
    banned = [n for n in (*pkgs, *mods) if n.startswith(("orch/relay", "relay"))]
    assert banned == [], banned
    host = [p.name for p in (CORE / "src" / "orch" / "host").glob("relay*.py")]
    assert host == [], host
    for p in python_files():
        for line, mod in [
            (n.lineno, a)
            for n in ast.walk(ast.parse(p.read_text()))
            if isinstance(n, ast.ImportFrom)
            for a in [n.module or ""]
            if a == "orch.relay" or a.startswith(("orch.relay.", "orch.relay_owner"))
        ]:
            pytest.fail(f"{p}:{line} imports {mod}")


def test_a_served_workspace_holds_no_relay_state_or_url(human: Human, served, offline):
    """The host's state for a workspace has no relay directory, link or origin, whatever mode it is in."""
    state = Path(served.service.state_dir) / "hosts" / served.wid
    assert not (state / "relay").exists()
    for f in state.rglob("*.json"):
        text = f.read_text(errors="replace")
        assert "link_origin" not in text and "relay_origin" not in text, f
