"""Every refusal code is documented in F1 §10.4a and produced by the model (or named as the store's)."""

import re
from pathlib import Path

from orch.model import Code
from orch.model.state import ChainError

DOC = Path(__file__).parents[4] / "docs" / "architecture" / "orch-v2-ticket-format.md"
STORE_ONLY = {Code.CHAIN_DIVERGED}  # §5.10: the store compares checkpoints


def test_codes_are_unique_and_documented():
    text = DOC.read_text(encoding="utf-8")
    section = text[text.index("### 10.4a") : text.index("### 10.5")]
    documented = set(re.findall(r"^\| `([a-z_.]+)` \|", section, re.M))
    assert documented == {c.value for c in Code}
    assert len({c.value for c in Code}) == len(list(Code))


def test_every_code_is_produced_by_the_model():
    src = "".join(
        p.read_text() for p in (Path(__file__).parents[2] / "src/orch/model").glob("*.py") if p.name != "codes.py"
    )
    unused = [c for c in Code if c not in STORE_ONLY and f"Code.{c.name}" not in src]
    assert not unused


def test_chain_error_uses_the_enum():
    assert ChainError("workspace", 3, Code.CHAIN_BROKEN.value, "x").code == "chain.broken"
