#!/usr/bin/env bash
# Run one orch-core e2e scenario: e2e/run.sh <scenario>.  `e2e/run.sh --list` lists them.
#
# publish-isolation (#296 exit criterion): two sandbox workspaces (own hosts, keys, state; temp dirs only, never real
# data, the keychain or ~/.config/orch) against one REAL local `orch-publish dev` on loopback. Signing is the terminal
# path (human.prepare, device-key signature, human.submit), never one-click.
#
# Needs an orch-publish `develop` checkout with its own .venv:  ORCH_PUBLISH_SRC=<dir> e2e/run.sh publish-isolation
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE="$HERE/../plugins/orch-core"
SCENARIOS="publish-isolation"

case "${1:-}" in
  --list) tr ' ' '\n' <<<"$SCENARIOS"; exit 0 ;;
  publish-isolation)
    : "${ORCH_PUBLISH_SRC:?set ORCH_PUBLISH_SRC to an orch-publish develop checkout with a .venv (uv sync there)}"
    [[ -x "$ORCH_PUBLISH_SRC/.venv/bin/python" ]] || { echo "no .venv in $ORCH_PUBLISH_SRC (run uv sync there)" >&2; exit 2; }
    # an isolated HOME: the run never reads or writes anything of the real user
    tmp_home="$(mktemp -d /tmp/hkXXXX)"
    trap 'rm -rf "$tmp_home"' EXIT
    echo "== e2e: publish-isolation"
    cd "$CORE"
    HOME="$tmp_home" ORCH_PUBLISH_SRC="$ORCH_PUBLISH_SRC" \
      uv run --no-sync pytest tests/host/test_publish_isolation_e2e.py -q -p no:xdist -rA
    echo "== e2e: publish-isolation passed"
    ;;
  *) echo "usage: e2e/run.sh <scenario> | --list   (scenarios: $SCENARIOS)" >&2; exit 2 ;;
esac
