"""The committed ``contracts/hub/vectors_hub_backup.json`` is what its generator writes (so a hand edit of the file, or a
change of the generator without a rewrite, fails here)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "hub"


def test_the_vectors_file_is_what_the_generator_builds() -> None:
    spec = importlib.util.spec_from_file_location("make_vectors_hub_backup", CONTRACTS / "make_vectors_hub_backup.py")
    assert spec is not None and spec.loader is not None
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    want = json.dumps(gen.build(), indent=1, sort_keys=True) + "\n"
    assert (CONTRACTS / "vectors_hub_backup.json").read_text() == want, "rewrite it: see the generator's docstring"
