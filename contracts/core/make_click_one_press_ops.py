"""Regenerate with: uv run --directory packages/core python ../../contracts/core/make_click_one_press_ops.py."""

import json
from pathlib import Path

from orch.click.protocol import ONE_PRESS_OPS

Path(__file__).with_name("click_one_press_ops.json").write_text(json.dumps(sorted(ONE_PRESS_OPS), indent=2) + "\n")
