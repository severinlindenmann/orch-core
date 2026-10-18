"""H7: the golden suites in host mode. Every case of ``tests/ops/golden`` (agent ops, ``wait`` included) and
``tests/ops/golden_human`` (person ops through the terminal signer) runs on the same fork, registered with a real host
on its own thread, through the thin CLI (``orch.hostclient``), and must match the **same** golden file byte for byte
as direct mode. Nothing is masked beyond what the direct-mode goldens already normalise.

A case whose parity needs a host group that is not merged yet is ``xfail(strict=True)`` with the PR it waits for, so
it fails loudly (XPASS) the day that PR lands and the mark must go."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from orch.host.pins import AgentPins
from tests.host.lifecycle_helpers import UID, HostThread, remove_tree, short_dir
from tests.ops import test_golden as agent_golden
from tests.ops import test_golden_human as human_golden
from tests.ops.helpers import Ws

pytestmark = pytest.mark.slow  # a host per case

#: agent cases whose host-mode parity waits for an unmerged group (case name -> reason). None today: the fixed
#: workspace links no repository; the cases that need one are in ``test_parity_pending.py``.
AGENT_PENDING: dict[str, str] = {}
#: person cases whose host-mode parity waits for an unmerged group
HUMAN_PENDING: dict[str, str] = {}

#: H3b2 (task.done contract 2): in host mode the host never runs ``verify.cmd``; the CLI reads it from the host, says
#: on stderr what it is about to run (the host's answer decides what runs here) and labels the receipt. The one designed
#: difference; everything else in every output is the golden byte for byte.
ATTESTED = "note: attested by you (the agent): this ran on your machine, the host does not verify it"
RUNNING = "running the verify command the host gave for DEMO-0001/T3: echo joined"
DESIGNED: dict[str, Any] = {
    "task-done-run.txt": lambda text: text.replace("seq=17\n", f"seq=17\n{ATTESTED}\n", 1).replace(
        "--- stderr\n", f"--- stderr\n{RUNNING}\n", 1
    ),
}


@contextlib.contextmanager
def served(ws: Ws, *, release: bool = False, fake_git: bool = True) -> Iterator[dict[str, str]]:
    """``ws`` registered with a host (state dir = the fork's, run directory short enough for a socket path), mapped
    to its owner; yields the CLI environment that names the run directory (where to look, never a trust input).
    ``fake_git`` gives the service the test git reader the golden cases expect; ``False`` keeps the host's own.
    ``release`` unregisters it at the end (a base that forks are copied from must not stay in the registry)."""
    from orch.host.control_client import new_idem
    from orch.ops.caps import Capabilities
    from tests.host.test_dispatch_review import FakeReader

    short = short_dir()
    pins_dir = ws.tmp / "pins"
    pins_dir.mkdir(exist_ok=True)
    host = HostThread(ws.host_state, short / "r", clock=lambda: ws.clock[0], pins=AgentPins(pins_dir, owner=UID))
    try:
        host.__enter__()
        with host.client() as control:
            owner = ws.owner.ref if getattr(ws, "owner", None) is not None else (ws.tmp / "owner.ref").read_text()
            peers = {str(UID): {"class": "person", "person": owner}}
            res = control.call("host.register", {"root": os.path.realpath(ws.root), "peers": peers}, idem=new_idem())
            assert res["ok"], res
            wid = res["result"]["data"]["workspace_id"]
        service = host.host.services[wid]

        def reader() -> None:  # the production host has a git reader; a test host has none
            if service.caps is not None:
                service.caps.close()
            service.caps = Capabilities.host((service.entry.root,), env={}, clock=service.clock, git=FakeReader())

        if fake_git:
            service.submit(reader).result(10)
        yield {"ORCH_HOST_RUN_DIR": str(short / "r")}
        if release:
            with host.client() as control:
                res = control.call("host.unregister", {"workspace_id": wid}, idem=new_idem())
                assert res["ok"], res
    finally:
        host.stop()
        remove_tree(short)


def _check(path: Path, text: str) -> None:
    assert path.exists(), f"missing {path.name}: the direct-mode golden suite writes it"
    want = path.read_text()
    if path.name in DESIGNED:
        changed = DESIGNED[path.name](want)
        assert changed != want, f"the designed difference no longer applies to {path.name}"
        want = changed
    assert want == text, f"{path.name} differs in host mode"


def _params(cases: dict[str, Any], pending: dict[str, str]) -> list[Any]:
    out = []
    for name in sorted(cases):
        marks = [pytest.mark.xfail(strict=True, reason=pending[name])] if name in pending else []
        out.append(pytest.param(name, marks=marks, id=name))
    return out


@pytest.mark.parametrize("name", _params(agent_golden.CASES, AGENT_PENDING))
@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_agent_golden_in_host_mode(name, as_json, request):
    base = request.getfixturevalue("agent_base")
    text = agent_golden.run(base, name, as_json, mode=served)
    _check(agent_golden.GOLDEN / f"{name}.{'json' if as_json else 'txt'}", text)


@pytest.fixture(autouse=True)
def _utc():
    """Times in the text are this computer's (the host runs in this process): the goldens are in UTC."""
    import os
    import time

    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


@pytest.mark.parametrize("name", _params(human_golden.CASES, HUMAN_PENDING))
@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_human_golden_in_host_mode(name, as_json, request, monkeypatch):
    base = request.getfixturevalue("human_base")
    text = human_golden.run(base, name, as_json, monkeypatch, mode=served)
    _check(human_golden.GOLDEN / f"{name}.{'json' if as_json else 'txt'}", text)


def building(ws: Ws) -> contextlib.AbstractContextManager[dict[str, str]]:
    return served(ws, release=True)


@pytest.fixture(scope="module")
def agent_base():
    """The base built through the host: the same script as direct mode, in host mode from the start."""
    yield from agent_golden.make_base(building)


@pytest.fixture(scope="module")
def human_base():
    yield from human_golden.make_base(building)
