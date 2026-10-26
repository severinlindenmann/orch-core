#!/bin/sh
# Regenerate the plugin's skills/ and hooks/ from orch.instructions (tests/instructions/test_harness.py checks them).
set -eu
cd "$(dirname "$0")/.."
uv run python -m orch.instructions write-plugin .
