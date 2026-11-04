#!/bin/bash
# Real end-to-end run of `orch host prune` on a throw-away HOME (not collected by pytest).
#   packages/core/tests/e2e_prune/run.sh            # needs: uv, python3.14, git; network for `uv tool install`
# What it does: installs orch-core + the dashboard add-on as a uv tool, hub/relay/publish as services (pidfile runner),
# makes a demo workspace with `orch setup`, runs `orch host prune --dry-run`, then `--yes --workspaces keep`, and asserts
# that the throw-away HOME holds no trace of orch and the workspace keeps the user's files.
# The environment is ALWAYS cleared (env -i): ORCH_*, XDG_* and the dev kit's markers never reach the run.
# Heavy: run it through the machine's lease (e.g. lease.sh run heavy-test prune -- <this script>).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
BASE=${PRUNE_E2E_BASE:-$HOME}   # short path: unix sockets hold ~100 bytes
T=$(mktemp -d "$BASE/.tprune-XXXX")
echo "e2e prune: throw-away folder $T (remove it with scripts/rmtmp.sh when done)"
mkdir -p "$T/home"
set +e
env -i HOME="$T/home" PATH="$PATH" USER="${USER:-tester}" LANG="${LANG:-en_US.UTF-8}" TERM=xterm \
  PRUNE_E2E_TMP="$T" python3.14 "$HERE/driver.py"
rc=$?
echo "e2e prune: exit $rc (folder $T kept for inspection)"
exit $rc
