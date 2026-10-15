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
