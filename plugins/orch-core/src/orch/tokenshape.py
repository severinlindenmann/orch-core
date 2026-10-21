"""Keep GitHub token detection in one place so refusal and redaction cannot drift.

host.auth, cli.render, ops.runtime and runner.grants share these shapes. Relay framing
cannot import orch.host and is outside this boundary: phones/relay carry no GitHub ops in v1.
"""

from __future__ import annotations

import re

GITHUB_TOKEN_SHAPE = re.compile(r"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{20,}")
GITHUB_TOKEN_BYTES = re.compile(rb"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{20,}")
