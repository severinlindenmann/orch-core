"""§11A.17 (D1, owner option 2): the Simulator dev gate. A dev host (``orch host run --foreground
--dev-sim-convenience`` plus a dev-pair sandbox's manifest and ``dev.json``) accepts ``auth:"convenience"`` on column R
for ``question.answered`` only, and only on an ``SBX`` sandbox workspace passing S1 to S6; every real workspace and
every normal host answer exactly as before; a normal host refuses every sandbox workspace."""

from __future__ import annotations

import ast
import io
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from orch import devsandbox
from orch.host import devmode
from orch.host.control import _genesis_view
from orch.host.control_client import new_idem
from orch.host.errors import DETAIL_KEYS, HostError
from tests.host.devgate_helpers import ORIGIN, SbxLab
from tests.host.dispatch_helpers import Served, ok
from tests.host.lifecycle_helpers import UID, Lab
from tests.store.helpers import WS

SRC = Path(__file__).resolve().parents[2] / "src" / "orch"


@pytest.fixture
def sbx(tmp_path):
    lab = SbxLab(tmp_path)
    try:
        yield lab
    finally:
        lab.close()


def _write_registry(state: Path, *entries: Any) -> None:
    """The registry edited on disk (it bypasses admit): what the start-time check must catch."""
    from orch.host.custody import write_private
    from orch.host.registry import registry_path

    write_private(registry_path(state), {"v": 1, "workspaces": [e.to_doc() for e in entries]})


def _reason(exc: pytest.ExceptionInfo) -> str:
    assert exc.value.code == "host.dev_refused", exc.value
    return exc.value.detail["reason"]


def _refused(fn: Any) -> str:
    with pytest.raises(HostError) as exc:
        fn()
    return _reason(exc)


# --- the start flag and the start-up proof (17.2) ---------------------------------------------------------------------


def test_dev_flag_requires_foreground(sbx):
    assert _refused(lambda: devmode.check_launch(False, sbx.env(), sbx.run, sbx.state)) == "not_foreground"
    from orch.host.cli import main

    out = io.StringIO()
    code = main(
        ["run", "--dev-sim-convenience", "--state", str(sbx.state), "--run", str(sbx.run)], env=sbx.env(), out=out
    )
    assert code == 3 and "not_foreground" in out.getvalue()
    assert not (sbx.run / "control").exists()  # nothing bound


@pytest.mark.parametrize(
    "env",
    [
        {"XPC_SERVICE_NAME": "io.orch.host"},
        {"XPC_SERVICE_NAME": "io.orch.runner"},
        {"INVOCATION_ID": "abc"},
        {"LISTEN_PID": "12"},
        {"JOURNAL_STREAM": "8:1234"},
    ],
)
def test_dev_flag_refused_under_service_manager_env(sbx, env):
    assert _refused(lambda: devmode.check_launch(True, sbx.env(**env), sbx.run, sbx.state)) == "service_managed"


@pytest.mark.parametrize("value", ["0", "application.com.apple.Terminal.123", ""])
def test_dev_flag_accepted_with_terminal_xpc_service_name_0(sbx, value):
    devmode.check_launch(True, sbx.env(XPC_SERVICE_NAME=value), sbx.run, sbx.state)  # no refusal
    assert sbx.dev().sandbox_id == sbx.sandbox_id


@pytest.mark.parametrize("where", ["run", "state"])
def test_dev_flag_refused_for_service_run_dir(sbx, where, monkeypatch):
    from orch.runner import devmode as runner_devmode

    service_root = sbx.tmp / "svc"
    service_root.mkdir()
    monkeypatch.setattr(runner_devmode, "SERVICE_RUN_ROOTS", (service_root,))
    run = service_root / "501" if where == "run" else sbx.run
    state = service_root / "state" if where == "state" else sbx.state
    assert _refused(lambda: devmode.check_launch(True, sbx.env(), run, state)) == "service_dir"


def test_unit_writers_refuse_dev_flag(tmp_path):
    from orch.host import units
    from orch.runner import service as runner_service

    py, rel, st, run = Path("/usr/bin/python3"), Path("/opt/orch/r"), tmp_path / "s", tmp_path / "r"
    for flag in ("--dev-sim-convenience", "--dev-sim-convenience=1", "--dev-runner", "--foreground"):
        with pytest.raises(HostError) as e:
            units.launchd_plist(py, rel, st, run_dir=run, extra_args=(flag,))
        assert e.value.code == "invalid.input"
        with pytest.raises(HostError):
            units.systemd_unit(py, rel, st, run_dir=run, extra_args=(flag,))
        with pytest.raises(HostError):
            runner_service.launchd_plist(py, rel, st, run, extra_args=(flag,))
        with pytest.raises(HostError):
            runner_service.systemd_unit(py, rel, st, run, extra_args=(flag,))
    # the units actually written carry no dev flag at all
    for text in (units.launchd_plist(py, rel, st, run_dir=run).decode(), units.systemd_unit(py, rel, st, run_dir=run)):
        assert "--dev-" not in text and "--foreground" not in text


def test_dev_flag_without_dev_json_refuses_start(tmp_path):
    lab = SbxLab(tmp_path, dev_json=False)
    try:
        assert _refused(lab.dev) == "no_dev_json"
    finally:
        lab.close()


def test_dev_json_without_flag_is_ignored(sbx):
    """A normal host (no flag) ignores dev.json and the manifest: it is no dev host, and the SBX workspace is refused
    (stray_dev_json_in_real_state_changes_nothing is the real-workspace side)."""
    with sbx.host() as h:
        assert h.host.dev is None
        res = sbx.register(h.client())
        assert not res["ok"] and res["error"]["code"] == "host.dev_refused"
        assert res["error"]["detail"]["reason"] == "sandbox_workspace"


def test_dev_flag_with_real_state_dir_refuses_start(tmp_path):
    lab = Lab(tmp_path)
    try:
        assert _refused(lambda: devmode.prove(lab.state, {"HOME": str(tmp_path)})) == "no_manifest"
    finally:
        lab.close()


def test_dev_flag_manifest_mismatch_refuses_start(sbx):
    sbx.write_manifest(sandbox_id="f" * 32)
    assert _refused(sbx.dev) == "dev_json_mismatch"
    devsandbox.write_dev_json(sbx.sroot, "f" * 32, ORIGIN)
    assert sbx.dev().sandbox_id == "f" * 32
    doc = json.loads((sbx.state / "host" / "dev.json").read_text())
    doc["root"]["st_ino"] += 1
    (sbx.state / "host" / "dev.json").write_text(json.dumps(doc))
    assert _refused(sbx.dev) == "dev_json_mismatch"


def test_manifest_id_format_enforced(sbx):
    for bad in ("ABCDEF12", "abc", "abcdefg", "abcdefgh", "12345678 ", ""):
        sbx.write_manifest(sandbox_id=bad)
        with pytest.raises(devsandbox.SandboxRefused):
            devsandbox.check_manifest(sbx.sroot)
        assert _refused(sbx.dev) == "no_manifest"
    sbx.write_manifest(sandbox_id="0123abcd")
    assert devsandbox.check_manifest(sbx.sroot)["id"] == "0123abcd"


def test_dev_flag_with_home_outside_root_refuses_start(sbx, tmp_path):
    other = tmp_path / "home2"
    other.mkdir()
    assert _refused(lambda: devmode.prove(sbx.state, {"HOME": str(other)})) == "home_not_in_root"
    assert _refused(lambda: devmode.prove(sbx.state, {})) == "home_not_in_root"
    link = tmp_path / "homelink"
    link.symlink_to(sbx.home)
    assert _refused(lambda: devmode.prove(sbx.state, {"HOME": str(link)})) == "home_not_in_root"


def test_dev_flag_needs_the_sandbox_person_and_no_convenience_custody(sbx):
    devsandbox.write_manifest(sbx.sroot, sbx.sandbox_id, workspaces=[(WS, sbx.root)])  # no person_id
    assert _refused(sbx.dev) == "no_person"
    sbx.write_manifest()
    sel = sbx.state / "person" / WS
    sel.mkdir(parents=True)
    (sel / "custody.json").write_text(json.dumps({"v": 1, "custody": "convenience"}))
    assert _refused(sbx.dev) == "custody_convenience"


def test_dev_host_never_falls_back_to_normal(sbx, tmp_path):
    """A refused proof ends the start: the CLI exits with host.dev_refused and nothing binds."""
    from orch.host.cli import main

    (sbx.state / "host" / "dev.json").unlink()
    out = io.StringIO()
    argv = ["run", "--foreground", "--dev-sim-convenience", "--state", str(sbx.state), "--run", str(sbx.run)]
    assert main(argv, env=sbx.env(), out=out) == 3
    assert json.loads(out.getvalue())["error"]["code"] == "host.dev_refused"
    assert not (sbx.run / "control").exists()


# --- registration: the one choke point (17.3 R5, F2, F5, D3) ---------------------------------------------------------


def test_sandbox_workspace_registers_and_is_served_on_a_dev_host(sbx):
    served = Served(sbx, dev=sbx.dev())
    try:
        assert served.service.dev_ok is True
        assert devmode.dev_gate(served.service) == devmode.DevGate(sbx.sandbox_id, WS)
        listed = ok(served.control.call("host.list"))["data"]
        assert listed["dev"] == {"sim_convenience": True, "sandbox": sbx.sandbox_id[:8]}
        assert listed["workspaces"][0]["sandbox"] is True and "alarms" not in listed
        status = ok(served.client().req("host.status"))["data"]
        assert status["dev"] == {"sim_convenience": True, "sandbox": sbx.sandbox_id[:8]}
    finally:
        served.close()


def test_real_workspace_on_dev_host_refuses_convenience(sbx, tmp_path):
    """Registration refused (the admit check: not_sandbox; through host.register the missing genesis pin refuses
    first); with the registry written directly the start refuses; dev_gate on it is None and a column-R convenience
    submit answers op.not_allowed (test_devgate_relay)."""
    real = Lab(tmp_path / "real")
    try:
        dev = sbx.dev()
        view = devmode.View(
            {"prefix": "DEMO", "owner": {"person": real.owner, "name": "Owner"}}, frozenset({real.owner})
        )
        assert _refused(lambda: devmode.check_admit(dev, WS, real.root, view)) == "not_sandbox"
        with sbx.host(dev=dev) as h:
            res = h.client().call("host.register", {"root": real.root, "peers": real.peers()}, idem=new_idem())
            assert not res["ok"] and h.host.registry.entries() == []
        from orch.host.registry import Entry, group_name

        _write_registry(sbx.state, Entry("b" * 32, "DEMO", real.root, UID, group_name("b" * 32), real.peers()))
        h = sbx.host(dev=dev)
        with pytest.raises(HostError) as e:
            h.__enter__()
        h.stop()
        assert _reason(e) == "not_sandbox"
    finally:
        real.close()


def test_dev_host_bootstrap_of_non_sandbox_refused(sbx):
    """host.bootstrap's validation calls the admit check before the genesis is appended: a new root is in no
    manifest, so a dev host refuses it (real prefix or SBX alike); nothing is created."""
    with sbx.host(dev=sbx.dev()) as h:
        root = sbx.sroot / "ws-2"
        os.mkdir(root, 0o700)
        for prefix in ("DEMO", "SBX"):
            event = {"prefix": prefix, "owner": {"person": sbx.owner, "name": devsandbox.owner_name(sbx.sandbox_id)}}
            with pytest.raises(HostError) as e:
                devmode.check_admit(h.host.dev, "c" * 32, root, _genesis_view(event))
            assert _reason(e) == "not_sandbox"
        assert os.listdir(root) == [] and len(h.host.registry.entries()) == 0


@pytest.mark.parametrize("path", ["register", "bootstrap", "restore", "adopt", "records.push"])
def test_normal_host_refuses_sbx_on_every_admit_path(sbx, path):
    """Restore and adopt reach the registry only through host.register (a restored or adopted root is registered):
    both are the register row with the marker removed (the genesis alone refuses)."""
    from types import SimpleNamespace

    if path in ("restore", "adopt"):
        (Path(sbx.root) / ".orch" / ".state" / devsandbox.WS_MARKER).unlink()
    with sbx.host() as h:
        if path in ("register", "restore", "adopt"):
            res = sbx.register(h.client())
            assert not res["ok"] and res["error"]["detail"]["reason"] == "sandbox_workspace"
            assert "start a real workspace" in res["error"]["hint"]
        elif path == "bootstrap":
            event = {"prefix": "SBX", "owner": {"person": sbx.owner, "name": devsandbox.owner_name(sbx.sandbox_id)}}
            with pytest.raises(HostError) as e:
                devmode.check_admit(h.host.dev, "c" * 32, sbx.sroot / "new", _genesis_view(event))
            assert _reason(e) == "sandbox_workspace"
        else:
            svc = SimpleNamespace(dev=None, store=None, entry=SimpleNamespace(prefix="SBX", root=sbx.root))
            assert _refused(lambda: devmode.refuse_push(svc)) == "sandbox_workspace"
        assert h.host.registry.entries() == []


def test_sbx_genesis_without_marker_refused_on_normal_host(sbx):
    """A records rebuild or a relay restore carries the signed genesis but not .state/: the genesis alone refuses."""
    (Path(sbx.root) / ".orch" / ".state" / devsandbox.WS_MARKER).unlink()
    with sbx.host() as h:
        res = sbx.register(h.client())
        assert res["error"]["code"] == "host.dev_refused" and res["error"]["detail"]["reason"] == "sandbox_workspace"


def test_sandbox_workspace_on_normal_host_refuses_convenience(tmp_path):
    """The marker alone refuses a real (non-SBX) workspace on a normal host; with it removed the workspace registers
    and convenience is still refused on column R (no gate on a normal host)."""
    lab = SbxLab(tmp_path, prefix="DEMO")
    try:
        with lab.host() as h:
            res = lab.register(h.client())
            assert res["error"]["detail"]["reason"] == "sandbox_workspace"
        (Path(lab.root) / ".orch" / ".state" / devsandbox.WS_MARKER).unlink()
        served = Served(lab)
        try:
            assert devmode.dev_gate(served.service) is None and devmode.gated(served.service) is False
        finally:
            served.close()
    finally:
        lab.close()


def test_normal_host_skips_and_alarms_sbx_in_registry_at_start(sbx, tmp_path):
    from orch.host.registry import Entry, Registry, group_name

    real = Lab(tmp_path / "real")
    try:
        with real.host() as h:
            ok(real.register(h.client()))
        reg = Registry(real.state, UID)
        reg.load()
        _write_registry(
            real.state, *reg.entries(), Entry("e" * 32, "SBX", sbx.root, UID, group_name("e" * 32), sbx.peers())
        )
        with real.host() as h:
            assert "e" * 32 not in h.host.services
            assert len(h.host.services) == 1
            listed = ok(h.client().call("host.list"))["data"]
            assert listed["alarms"] == [{"code": "sandbox_workspace_registered", "workspace_id": "e" * 32}]
            assert "dev" not in listed and all("sandbox" not in row for row in listed["workspaces"])
    finally:
        real.close()


def _calls(fn: ast.AST) -> list[ast.Call]:
    """The calls in ``fn``'s own body (a nested function is its own row)."""
    out, todo = [], list(ast.iter_child_nodes(fn))
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            out.append(node)
        todo.extend(ast.iter_child_nodes(node))
    return out


def test_registry_add_paths_table():
    """Every registry add goes through orch.host.registry.admit (§11A.17.3 R5; structural through the admit token,
    this AST table is the extra check): Registry._add is called nowhere else in the package, and admit's callers are
    exactly these paths. Restore and relay adopt reach the registry only
    through host.register."""
    adds, admits = [], []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        imports_admit = "from .registry import" in text and " admit" in text
        for fn in ast.walk(ast.parse(text)):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in _calls(fn):
                if isinstance(node.func, ast.Attribute) and node.func.attr in ("add", "_add"):
                    if ast.unparse(node.func.value).endswith("registry"):
                        adds.append((path.name, fn.name))
                if imports_admit and isinstance(node.func, ast.Name) and node.func.id == "admit":
                    admits.append((path.name, fn.name))
    assert adds == [("registry.py", "admit")]
    paths = {"run": "host.register", "_finish_bootstrap": "host.bootstrap (live and its start-up recovery)"}
    assert sorted(admits) == sorted(("control.py", fn) for fn in paths)


# --- S1 to S6, each failing alone (17.3) -----------------------------------------------------------------------------


def _view(sbx, **kw) -> devmode.View:
    genesis = {"prefix": "SBX", "owner": {"person": sbx.owner, "name": devsandbox.owner_name(sbx.sandbox_id)}}
    base = {"genesis": genesis, "members": frozenset({sbx.owner}), "former": frozenset()}
    return devmode.View(**{**base, **kw})


def test_s1_to_s6_pass_for_the_sandbox(sbx):
    assert devmode.sandbox_reason(sbx.dev(), WS, sbx.root, _view(sbx)) is None


def test_s1_moved_root(sbx):
    dev = sbx.dev()
    moved = sbx.tmp / "ws"
    os.rename(sbx.root, moved)
    assert devmode.sandbox_reason(dev, WS, moved, _view(sbx)).startswith("S1")
    os.rename(moved, sbx.sroot / "ws-7")  # back under the root, under another name: the registered root is gone
    assert devmode.sandbox_reason(dev, WS, sbx.root, _view(sbx)).startswith("S1")


def test_s1_copied_root(sbx):
    import shutil

    dev = sbx.dev()
    copy = sbx.sroot / "ws-3"
    shutil.copytree(sbx.root, copy, symlinks=True)
    assert devmode.sandbox_reason(dev, WS, copy, _view(sbx)).startswith("S1")
    elsewhere = sbx.tmp / "ws"
    shutil.copytree(sbx.root, elsewhere, symlinks=True)
    assert devmode.sandbox_reason(dev, WS, elsewhere, _view(sbx)).startswith("S1")


def test_s2_unlisted_workspace_id(sbx):
    dev = sbx.dev()
    sbx.write_manifest(listed=False)
    assert devmode.sandbox_reason(dev, WS, sbx.root, _view(sbx)).startswith("S2")
    sbx.write_manifest()
    assert devmode.sandbox_reason(dev, "d" * 32, sbx.root, _view(sbx)).startswith("S2")


@pytest.mark.parametrize("change", ["prefix", "name", "person"])
def test_s3_other_genesis(sbx, change):
    dev = sbx.dev()
    g = _view(sbx).genesis
    other = {
        "prefix": {**g, "prefix": "DEMO"},
        "name": {**g, "owner": {**g["owner"], "name": "orch dev sandbox 00000000"}},
        "person": {**g, "owner": {**g["owner"], "person": "p_" + "9" * 32}},
    }[change]
    assert devmode.sandbox_reason(dev, WS, sbx.root, _view(sbx, genesis=other)).startswith("S3")


def test_s4_a_second_member(sbx):
    dev = sbx.dev()
    two = _view(sbx, members=frozenset({sbx.owner, "p_" + "8" * 32}))
    assert devmode.sandbox_reason(dev, WS, sbx.root, two).startswith("S4")
    gone = _view(sbx, former=frozenset({"p_" + "8" * 32}))
    assert devmode.sandbox_reason(dev, WS, sbx.root, gone).startswith("S4")


def test_s5_missing_or_mismatched_marker(sbx):
    dev = sbx.dev()
    marker = Path(sbx.root) / ".orch" / ".state" / devsandbox.WS_MARKER
    marker.unlink()
    assert devmode.sandbox_reason(dev, WS, sbx.root, _view(sbx)).startswith("S5")
    devsandbox.write_marker(sbx.root, "0" * 32, WS)
    assert devmode.sandbox_reason(dev, WS, sbx.root, _view(sbx)).startswith("S5")


def test_forged_manifest_naming_real_person_refused(tmp_path):
    """A real workspace moved into <root>/ws with a perfect forged manifest, marker and dev.json still fails S3."""
    lab = SbxLab(tmp_path, prefix="DEMO", name="Owner")
    try:
        dev = lab.dev()
        g = {"prefix": "DEMO", "owner": {"person": lab.owner, "name": "Owner"}}
        assert devmode.sandbox_reason(dev, WS, lab.root, _view(lab, genesis=g)).startswith("S3")
        with lab.host(dev=dev) as h:
            res = lab.register(h.client())
            assert res["error"]["detail"]["reason"] == "not_sandbox" and "S3" in res["error"]["message"]
    finally:
        lab.close()


def test_stray_dev_json_in_real_state_changes_nothing(tmp_path):
    lab = Lab(tmp_path)
    try:
        host = lab.state / "host"
        host.mkdir(mode=0o700, exist_ok=True)
        (host / "dev.json").write_text(json.dumps({"v": 1, "sim_convenience": True}))
        os.chmod(host / "dev.json", 0o600)
        served = Served(lab)
        try:
            assert served.h.host.dev is None and devmode.dev_gate(served.service) is None
            assert "dev" not in ok(served.client().req("host.status"))["data"]
        finally:
            served.close()
    finally:
        lab.close()


def test_stray_workspace_marker_only_narrows(tmp_path):
    lab = Lab(tmp_path)
    try:
        devsandbox.write_marker(lab.root, "a" * 32, WS)
        with lab.host() as h:
            res = lab.register(h.client())
            assert res["error"]["detail"]["reason"] == "sandbox_workspace"
    finally:
        lab.close()


# --- the click signer on a dev host (17.2 F3, D2) --------------------------------------------------------------------


def test_dev_host_refuses_convenience_on_after_start(sbx):
    from orch.host import convenience

    served = Served(sbx, dev=sbx.dev())
    try:
        with pytest.raises(HostError) as e:
            convenience.set_enabled(sbx.state, WS, True, UID)
        assert e.value.code == "sign.convenience_unavailable" and e.value.detail["reason"] == "dev_host"
        convenience.set_enabled(sbx.state, WS, False, UID)  # the way back stays open
    finally:
        served.close()


def test_dev_host_refuses_convert_to_convenience(sbx):
    from orch.custody import select

    with pytest.raises(select.DevHostRefused) as e:
        select.convert_to_convenience(sbx.state, WS)
    assert e.value.code == "sign.convenience_unavailable" and str(e.value).startswith("dev_host")


def test_dev_host_refuses_custody_convenience(sbx, monkeypatch):
    """``orch keys custody convenience`` against the sandbox state, from any shell environment."""
    from orch.custody import cli as custody_cli

    monkeypatch.setattr(custody_cli, "has_terminal", lambda: True)
    with sbx.host(dev=sbx.dev()) as h:
        ok(sbx.register(h.client()))
    out, err = io.StringIO(), io.StringIO()
    code = custody_cli.main(
        ["custody", "convenience", "--workspace", WS, "--state", str(sbx.state), "--json"],
        env={"HOME": "/nonexistent-d1"},
        out=out,
        err=err,
    )
    assert code != 0 and "sign.convenience_unavailable" in out.getvalue() and "dev_host" in out.getvalue()


def test_custody_convenience_refused_with_home_under_sandbox_root(tmp_path):
    """No dev.json in the state directory, but HOME lies under a sandbox root: refused too (D2)."""
    from orch.devsandbox import click_refusal

    lab = SbxLab(tmp_path, dev_json=False)
    try:
        other_state = tmp_path / "elsewhere"
        other_state.mkdir()
        assert click_refusal(other_state, {"HOME": str(lab.home)}) == "dev_host"
        assert click_refusal(other_state, {"HOME": str(tmp_path)}) is None
        assert click_refusal(lab.state, {"HOME": str(tmp_path)}) is None  # no dev.json: only HOME narrows here
    finally:
        lab.close()


def test_init_refuses_sbx_for_existing_person(sbx, tmp_path):
    from orch.ops.errors import OrchError
    from orch.ops.workspace_init import check_reserved_prefix

    root = sbx.sroot / "ws-2"
    name = devsandbox.owner_name(sbx.sandbox_id)
    fresh = tmp_path / "fresh-state"
    check_reserved_prefix(root, fresh, "SBX", name)  # a fresh state: accepted
    (sbx.state / "hosts" / WS / "person").mkdir()  # the device key of the sandbox's own workspace: a person exists
    with pytest.raises(OrchError) as e:
        check_reserved_prefix(root, sbx.state, "SBX", name)  # the sandbox state already holds a person
    assert e.value.code == "invalid.input" and "SBX is reserved" in e.value.message
    check_reserved_prefix(tmp_path / "anywhere", sbx.state, "DEMO", "Owner")  # other prefixes: untouched


def test_init_refuses_sbx_with_other_owner_name(sbx, tmp_path):
    from orch.ops.errors import OrchError
    from orch.ops.workspace_init import check_reserved_prefix

    for name in ("Owner", "orch dev sandbox 00000000", devsandbox.owner_name(sbx.sandbox_id) + " "):
        with pytest.raises(OrchError) as e:
            check_reserved_prefix(sbx.sroot / "ws-2", tmp_path / "fresh", "SBX", name)
        assert e.value.code == "invalid.input"
    with pytest.raises(OrchError):  # no sandbox manifest at all
        check_reserved_prefix(tmp_path / "ws", tmp_path / "fresh", "SBX", devsandbox.owner_name(sbx.sandbox_id))


def test_init_refuses_sbx_with_entered_recovery_code(tmp_path, monkeypatch):
    """Only a recovery code this init run generated (``_dialogue``, ``generated=True``) may sign an SBX genesis; a
    typed, restored or imported code is refused before any key is made. Both init paths pass the generated code."""
    import inspect

    from orch.identity import generate_recovery_code
    from orch.ops import workspace_init
    from orch.ops.errors import OrchError

    made: list[Any] = []

    def backend(directory: Any) -> Any:
        made.append(directory)
        raise RuntimeError("stop here")

    monkeypatch.setattr(workspace_init, "open_backend", backend)
    code = generate_recovery_code()
    with pytest.raises(OrchError) as e:
        workspace_init._signed_genesis(None, tmp_path, "a" * 32, b"", "SBX", "n", "pw", code)
    assert e.value.code == "invalid.input" and made == []
    with pytest.raises(RuntimeError):  # generated: past the guard, to the device key
        workspace_init._signed_genesis(None, tmp_path, "a" * 32, b"", "SBX", "n", "pw", code, generated=True)
    assert len(made) == 1
    source = inspect.getsource(workspace_init)
    assert source.count("generated=True") == 2 and source.count("code = generate_recovery_code()") == 1


def test_host_bootstrap_reserved_prefix_on_a_dev_host(sbx):
    with sbx.host(dev=sbx.dev()) as h:
        ops = h.host.control
        with pytest.raises(HostError) as e:
            ops._check_reserved_prefix({"prefix": "SBX", "owner": {"person": sbx.owner, "name": "Owner"}})
        assert e.value.code == "invalid.input"
        ops._check_reserved_prefix(
            {"prefix": "SBX", "owner": {"person": sbx.owner, "name": devsandbox.owner_name(sbx.sandbox_id)}}
        )
    with sbx.host() as h:  # a normal host never accepts an SBX bootstrap
        with pytest.raises(HostError):
            h.host.control._check_reserved_prefix(
                {"prefix": "SBX", "owner": {"person": sbx.owner, "name": devsandbox.owner_name(sbx.sandbox_id)}}
            )


# --- P5 in the bridge's lock-free checks, the alarm (17.4, 17.7) ------------------------------------------------------


def test_no_convenience_env_refuses_dev_gate(sbx, monkeypatch):
    served = Served(sbx, dev=sbx.dev())
    try:
        assert devmode.gated(served.service) is True
        for value in ("1", "0", "false", "yes"):
            monkeypatch.setenv("ORCH_NO_CONVENIENCE", value)
            assert devmode.dev_gate(served.service) is None and devmode.gated(served.service) is False
        monkeypatch.delenv("ORCH_NO_CONVENIENCE")
        assert devmode.dev_gate(served.service) is not None
    finally:
        served.close()


def test_the_new_code_is_in_both_catalogs_and_the_spec():
    from orch.host.errors import HOST_CODES
    from orch.ops.errors import ERRORS

    assert HOST_CODES["host.dev_refused"] == (3, False) and ERRORS["host.dev_refused"].exit == 3
    spec = (SRC.parents[3] / "docs" / "architecture" / "orch-v2-host.md").read_text(encoding="utf-8")
    assert "| `host.dev_refused` |" in spec
    assert set(devmode.REASONS) == set(DETAIL_KEYS["host.dev_refused"]["reason"])


def test_dev_host_logs_its_line_at_start(sbx, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="orch.host.devmode")
    served = Served(sbx, dev=sbx.dev())
    try:
        line = (
            f"DEV HOST: Simulator convenience answers ON for sandbox {sbx.sandbox_id[:8]} (throwaway workspaces only)"
        )
        deadline = time.monotonic() + 10
        while line not in caplog.text and time.monotonic() < deadline:
            time.sleep(0.05)
        assert line in caplog.text
        assert devmode.status_line(sbx.dev()) == line
    finally:
        served.close()


def test_normal_host_skips_sbx_when_the_registry_prefix_was_edited(sbx, tmp_path):
    """The registry's prefix says DEMO, the workspace's own genesis line says SBX: skipped and alarmed all the same."""
    from orch.host.registry import Entry, Registry, group_name

    real = Lab(tmp_path / "real")
    try:
        with real.host() as h:
            ok(real.register(h.client()))
        reg = Registry(real.state, UID)
        reg.load()
        edited = Entry("e" * 32, "DEMO", sbx.root, UID, group_name("e" * 32), sbx.peers())
        _write_registry(real.state, *reg.entries(), edited)
        with real.host() as h:
            assert "e" * 32 not in h.host.services and len(h.host.services) == 1
            listed = ok(h.client().call("host.list"))["data"]
            assert listed["alarms"] == [{"code": "sandbox_workspace_registered", "workspace_id": "e" * 32}]
    finally:
        real.close()


def test_registry_add_needs_the_admit_token(tmp_path):
    """The choke point is structural: Registry has no public add; _add and _save refuse a new entry without the
    single-use token that only admit creates (an alias, a lambda or Registry._add(r, e) cannot bypass it)."""
    from orch.host import registry as reg_mod
    from orch.host.registry import AdmitToken, Entry, Registry, group_name

    reg = Registry(tmp_path / "state", UID)
    (tmp_path / "state").mkdir()
    reg.load()
    entry = Entry("c" * 32, "DEMO", str(tmp_path / "ws"), UID, group_name("c" * 32), {})
    assert not hasattr(reg, "add") and not hasattr(Registry, "add")
    for token in (None, object(), "admit"):
        with pytest.raises(HostError) as e:
            Registry._add(reg, entry, token)
        assert e.value.code == "internal"
    with pytest.raises(HostError):
        AdmitToken(object(), entry)  # only admit holds the key
    with pytest.raises(HostError):
        reg._save({entry.workspace_id: entry})  # a new entry through _save: refused
    with pytest.raises(HostError):  # an unspent token does not open _save either
        reg._save({entry.workspace_id: entry}, token=AdmitToken(reg_mod._ADMIT_KEY, entry))
    assert reg.entries() == [] and not reg.path.exists()
    token = AdmitToken(reg_mod._ADMIT_KEY, entry)  # what admit makes (the test reaches the private key)
    other = Entry("d" * 32, "DEMO", str(tmp_path / "ws2"), UID, group_name("d" * 32), {})
    with pytest.raises(HostError):
        reg._add(other, token)  # bound to its entry
    reg._add(entry, token)
    assert [e.workspace_id for e in reg.entries()] == ["c" * 32]
    reg.remove(entry.workspace_id)
    with pytest.raises(HostError):
        reg._add(entry, token)  # single use
    with pytest.raises(HostError):
        reg._save({entry.workspace_id: entry}, token=token)  # spent and persisted: never again through _save
    assert reg.entries() == []


@pytest.mark.parametrize("argv", [["run", "--dev-sim"], ["run", "--fore"], ["run", "--dev-sim-c"], ["run", "--dev-r"]])
def test_host_cli_takes_no_abbreviated_flag(argv):
    from orch.host.cli import _parser

    with pytest.raises(SystemExit):
        _parser().parse_args(argv)
    full = {"--dev-sim": "--dev-sim-convenience", "--dev-sim-c": "--dev-sim-convenience", "--fore": "--foreground"}
    if argv[1] in full:
        _parser().parse_args(["run", full[argv[1]]])  # the exact flag still parses


def test_runner_cli_takes_no_abbreviated_flag():
    from orch.runner.cli import _parser

    with pytest.raises(SystemExit):
        _parser().parse_args(["run", "--fore"])
    _parser().parse_args(["run", "--foreground"])


@pytest.mark.parametrize("arg", ["--dev-sim", "--fore", "--dev", "--dev-r", "--dev-sim=1", "--foreg=x"])
def test_unit_writers_refuse_dev_flag_prefixes(tmp_path, arg):
    from orch.host import units
    from orch.runner import service as runner_service

    py, rel, st, run = Path("/usr/bin/python3"), Path("/opt/orch/r"), tmp_path / "s", tmp_path / "r"
    with pytest.raises(HostError):
        units.launchd_plist(py, rel, st, run_dir=run, extra_args=(arg,))
    with pytest.raises(HostError):
        runner_service.systemd_unit(py, rel, st, run, extra_args=(arg,))
    units.launchd_plist(py, rel, st, run_dir=run, extra_args=("--x", "--"))  # not a prefix of a dev flag


def test_cli_marks_the_dev_process_after_the_proof(sbx, monkeypatch):
    """After prove() the CLI marks the process: the click signer's refusal is structural in-process too."""
    from orch.host import cli, release, service

    monkeypatch.setattr(devsandbox, "_PROCESS_DEV_HOST", False)
    monkeypatch.setattr(release, "preload", lambda: None)

    class FakeHost:
        def __init__(self, *a: Any, **k: Any) -> None:
            assert k["dev"] is not None and devsandbox._PROCESS_DEV_HOST is True  # marked before the host exists

        async def run(self) -> None:
            return None

    monkeypatch.setattr(service, "Host", FakeHost)
    argv = ["run", "--foreground", "--dev-sim-convenience", "--state", str(sbx.state), "--run", str(sbx.run)]
    assert cli.main(argv, env=sbx.env(), out=io.StringIO()) == 0
    assert devsandbox.click_refusal(sbx.tmp / "anywhere", {}) == "dev_host"
