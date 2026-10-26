"""``python -m orch.host …`` is ``orch host …``."""

import sys

from .cli import main

raise SystemExit(main(sys.argv[1:]))
