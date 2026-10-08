#!/bin/sh
# The Orch Remote end-to-end run (issue #94, docs/remote-validation.md): two dashboards started with the remote flag,
# a local TIX server from an orch-tix checkout, one Chromium. Never installs anything machine-wide: the packages go
# into a scratch environment (E2E_VENV, default ../.e2e-venv next to this plugin).
#
#   ORCH_TIX_DIR=/path/to/orch-tix plugins/orch-core/scripts/e2e-remote.sh [pytest arguments]
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
venv=${E2E_VENV:-$here/.e2e-venv}
if ! command -v uv >/dev/null 2>&1; then
  echo "e2e-remote: uv is needed to build the scratch environment (https://docs.astral.sh/uv/)" >&2
  exit 5
fi
if [ ! -x "$venv/bin/python" ]; then
  uv venv "$venv" --python 3.13
  uv pip install --python "$venv/bin/python" -e "$here[dashboard,dev]" pywebpush pytest-playwright
fi
cd "$here"
exec "$venv/bin/python" -m pytest tests/e2e_remote -m e2e_remote -p no:xdist "$@"
