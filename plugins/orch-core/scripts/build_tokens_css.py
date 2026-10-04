"""Regenerate src/orch/dashboard/static/tokens.css from tokens.json (pass --check to only compare).
Same as `python -m orch.dashboard.design.build`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from orch.dashboard.design.build import main  # noqa: E402

raise SystemExit(main())
