#!/usr/bin/env bash
# Refresh tests/vectors/vectors_v2.json and tests/vectors/SOURCE from an orch-relay ref.
# Usage: scripts/sync-vectors.sh [ref] (default origin/develop); ORCH_RELAY_DIR overrides the clone path.
set -euo pipefail
ref="${1:-origin/develop}"
relay="${ORCH_RELAY_DIR:-$HOME/orch-dev/orch-relay}"
dest="$(cd "$(dirname "${BASH_SOURCE[0]}")/../tests/vectors" && pwd)"
git -C "$relay" fetch -q
sha="$(git -C "$relay" rev-parse "$ref")"
git -C "$relay" show "$sha:tests/vectors_v2.json" > "$dest/vectors_v2.json"
echo "$sha" > "$dest/SOURCE"
echo "synced vectors_v2.json from orch-relay $sha"
