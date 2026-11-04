#!/bin/bash
# Real end-to-end run of `orch stop` / `orch start` / `orch services` on a throw-away HOME (not collected by pytest).
#   STOP_E2E_BASE=~/orch-dev packages/core/tests/e2e_stop/run.sh   # needs uv, python3.14, git, network
# Installs orch-core + the dashboard add-on as a uv tool, hub/relay/publish as real background services (launchd on a
# Mac, systemd --user or pidfile elsewhere; their labels carry a hash of the throw-away state folder, so no label of this
# machine is touched), two demo workspaces made by `orch setup`; then stop in a folder, stop all, start all and the table.
# The environment is ALWAYS cleared (env -i). Heavy: run it through the machine's lease (lease.sh run heavy-test ...).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
BASE=${STOP_E2E_BASE:-$HOME}   # short path: unix sockets hold ~100 bytes
T=$(mktemp -d "$BASE/.tstop-XXXX")
echo "e2e stop: throw-away folder $T (remove it with scripts/rmtmp.sh when done)"
mkdir -p "$T/home"
set +e
env -i HOME="$T/home" PATH="$PATH" USER="${USER:-tester}" LANG="${LANG:-en_US.UTF-8}" TERM=xterm \
  STOP_E2E_TMP="$T" python3.14 "$HERE/driver.py"
rc=$?
echo "e2e stop: exit $rc (folder $T kept for inspection)"
exit $rc
