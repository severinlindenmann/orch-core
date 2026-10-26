"""The test seams of the passphrase backend are not reachable from production code."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "orch"
SEAMS = ("_kdf", "_min_n", "_passphrase_provider")
# the only place that may mention them: the constructor that defines the seams
ALLOWED = {SRC / "custody" / "passphrase.py"}


def test_no_production_module_passes_or_reads_a_seam():
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        for seam in SEAMS:
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(seam)}\b", text) and path not in ALLOWED:
                offenders.append((path.relative_to(SRC).as_posix(), seam))
    assert offenders == []


def test_recovery_kdf_seam_is_private_and_unused_in_production():
    for path in SRC.rglob("*.py"):
        if path.name != "recovery.py":
            assert not re.search(r"(?<![A-Za-z0-9_])(_key_at|recovery\._scalar)\b", path.read_text()), path


def _calls(tree):
    import ast

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            yield name, node


def _passes_a_passphrase(call) -> bool:
    import ast

    return any(
        kw.arg == "passphrase" and not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
        for kw in call.keywords
    )


def test_the_answering_backend_and_dashboard_passphrases_have_one_caller_each():
    """M2 (Opus review of #539): text that never came from /dev/tty opens a device key only on one path. The answering
    backend is built only in ``custody/select.py``; a non-None ``passphrase=`` reaches a custody transition only from
    the click signer, and only the conversion to convenience (the dashboard never re-keys: H2)."""
    import ast

    transitions = {"convert_to_convenience", "stage_convenience", "downgrade_in_place", "turn_off_convenience"}
    answering, dashboard = [], []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "answering" and rel != "custody/passphrase.py":
                if isinstance(node.value, ast.Name) and node.value.id in ("PassphraseBackend", "cls"):
                    answering.append(rel)
        for name, call in _calls(tree):
            if name in transitions and _passes_a_passphrase(call):
                dashboard.append((rel, name))
    assert answering == ["custody/select.py"]
    generated = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute)
        and node.attr == "generated"
        and path.name != "passphrase.py"
        and isinstance(node.value, ast.Name)
        and node.value.id == "PassphraseBackend"
    ]
    assert generated == ["custody/select.py"]  # B9: the setup process's own generated passphrase, one caller
    # B9: ``orch setup`` converts with the passphrase ITS OWN process generated, or (Finish setup) one the person typed
    # on the setup terminal with echo off (``Terminal.ask_secret``); never text from a browser or an argument
    setup_callers = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute)
        and node.attr == "setup_backend"
        and path.relative_to(SRC).as_posix() != "custody/select.py"
    )
    assert setup_callers == ["setup/steps.py", "setup/steps.py"]  # the generated-passphrase backend: setup's two sites
    assert sorted(dashboard) == [
        ("click/signer.py", "stage_convenience"),
        ("click/signer.py", "turn_off_convenience"),  # V3-D31: the dashboard's new factor, only while the flag is on
        ("custody/select.py", "downgrade_in_place"),  # only inside turn_off_convenience
        ("setup/steps.py", "stage_convenience"),
    ]
