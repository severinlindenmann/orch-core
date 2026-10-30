"""Shared loader for the contract vectors (``contracts/hub/vectors_remote.json``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DOC: dict[str, Any] = json.loads((Path(__file__).resolve().parents[4] / "contracts" / "hub" / "vectors_remote.json").read_text())
X = DOC["keys"]["x25519"]


def priv(name: str) -> bytes:
    return bytes.fromhex(X[name]["priv_hex"])


def pub(name: str) -> bytes:
    return bytes.fromhex(X[name]["pub_hex"])


def cases(check: str) -> list[dict[str, Any]]:
    return [c for c in DOC["cases"] if c["check"] == check]
