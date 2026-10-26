# ruff: noqa: F811
"""V3 C11 delta fixes (L2-C11d-1, L2-C11d-2a): the full-mode refusal sits at the one custody choke point
(``select.stage_convenience``) that the CLI, the dashboard switch and Finish setup all pass through, and a full
``hub.joined`` re-checks the custody under the lock at the append."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from orch.custody import select
from orch.host import convenience
from orch.host.errors import HostError
from tests.host import human_helpers
from tests.host.human_helpers import err
from tests.host.lifecycle_helpers import UID
from tests.host.test_hub_only import append, full_joined, join_args, log_len, owner_event
from tests.host.test_hub_ops import CARD, connect_args, dev_workspace, keystore, stub  # noqa: F401

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human
SRC = Path(select.__file__).resolve().parents[1]


def test_stage_convenience_is_refused_in_full_mode_before_the_key_store_is_opened(human, served, stub, keystore):
    full_joined(human, served)

    def boom(*a, **k):
        raise AssertionError("the key store was opened")

    with pytest.raises(HostError) as ei:
        select.stage_convenience(served.lab.state, served.wid, store=type("S", (), {"exists": boom, "put": boom})())
    assert ei.value.code == "sign.hub_only"
    with pytest.raises(HostError):
        select.convert_to_convenience(served.lab.state, served.wid)


def test_finish_convenience_is_refused_after_a_full_join_landed_since_stage(human, served, stub, keystore, monkeypatch):
    """L2-C11e-3: stage_convenience passed in standalone; a full join then landed in the gap before the transition was
    written. finish_convenience refuses before the store is opened and the selection stays passphrase custody."""
    staged = select.Selection({"custody": "passphrase", "transition": {"to": "convenience", "step": "written"}})
    full_joined(human, served)
    monkeypatch.setattr(select, "read_selection", lambda state, wid: staged)
    written = []
    monkeypatch.setattr(select, "_write_selection", lambda *a, **k: written.append(a))

    def boom(*a, **k):
        raise AssertionError("the key store was opened")

    with pytest.raises(HostError) as ei:
        select.finish_convenience(
            served.lab.state, served.wid, staged, store=type("S", (), {"get": boom, "put": boom})()
        )
    assert ei.value.code == "sign.hub_only"
    assert written == []  # custody not changed


def test_stage_convenience_passes_the_full_mode_gate_in_standalone_and_for_unregistered(human, served):
    select.refuse_full_mode(served.lab.state, served.wid)  # standalone: nothing to refuse
    select.refuse_full_mode(served.lab.state, "ws_not_registered")


def test_the_setup_port_turns_one_click_on_through_refuse_turn_on(human, served, stub, keystore):
    from orch.setup.steps import RealPorts

    full_joined(human, served)
    ports = RealPorts({}, served.lab.state, UID)
    with pytest.raises(HostError) as ei:
        ports.convenience_on(served.wid)
    assert ei.value.code == "sign.hub_only"
    assert not convenience.enabled(served.lab.state, served.wid, UID)  # the flag stayed off
    with pytest.raises(HostError):
        ports.stage(served.wid, "p", "pw")  # Finish setup's conversion: refused too, key untouched
    assert select.read_selection(served.lab.state, served.wid) is None or (
        select.read_selection(served.lab.state, served.wid)["custody"] == "passphrase"
    )


def _calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            yield name, node


def test_every_path_into_convenience_custody_goes_through_the_refusal():
    """The invariant (L2-C11d-1): a key enters convenience custody only through ``select.stage_convenience``, and that
    function runs ``refuse_full_mode`` before the key store is opened. Callers outside select.py are listed, so a new
    one fails here and must be added on purpose; Finish setup's flag path calls ``refuse_turn_on``."""
    callers = set()
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        for name, _ in _calls(ast.parse(path.read_text())):
            if name in ("stage_convenience", "convert_to_convenience") and rel != "custody/select.py":
                callers.add((rel, name))
    assert callers == {
        ("click/signer.py", "stage_convenience"),
        ("custody/cli.py", "convert_to_convenience"),
        ("host/cli.py", "stage_convenience"),  # `orch host convenience on` moves a passphrase key (run 9 fb 3.7)
        ("setup/steps.py", "stage_convenience"),
    }
    tree = ast.parse((SRC / "custody/select.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "stage_convenience")
    order = [(n.lineno, name) for name, n in _calls(fn) if name in ("refuse_full_mode", "_store", "read_selection")]
    assert [name for _, name in sorted(order)][0] == "refuse_full_mode"
    conv = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "convert_to_convenience")
    assert "stage_convenience" in {name for name, _ in _calls(conv)}
    fin = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "finish_convenience")
    order = [(n.lineno, name) for name, n in _calls(fin) if name in ("refuse_full_mode", "_store", "read_selection")]
    assert [name for _, name in sorted(order)][0] == "refuse_full_mode"
    steps = ast.parse((SRC / "setup/steps.py").read_text())
    port = next(n for n in ast.walk(steps) if isinstance(n, ast.FunctionDef) and n.name == "convenience_on" and n.body
                and not isinstance(n.body[0], ast.Expr | ast.Pass))  # fmt: skip
    assert "refuse_turn_on" in {name for name, _ in _calls(port)}


def test_a_full_join_rechecks_custody_at_the_append(human, served, monkeypatch):
    """L2-C11d-2a: prepare passed (passphrase custody); the key then moved to convenience before the submit."""
    human.prepare("hub.join", join_args(mode="full"))  # prepare-time check passes
    monkeypatch.setattr(select, "read_selection", lambda state, wid: {"custody": "convenience", "transition": None})
    before = log_len(served)
    landed, error = append(served, [owner_event(served, "hub.joined", mode="full")])
    assert error.code == "sign.hub_only" and landed == [] and log_len(served) == before
    assert err(human.prepare_res("hub.join", join_args(mode="full"))) == "sign.hub_only"
