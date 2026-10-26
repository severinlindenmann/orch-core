#!/usr/bin/env bash
# Refresh the vendored hub contract vectors in tests/hub/vectors/ from an orch-hub ref (contract H0, V3-D8):
#   vectors_hub.json         the hub's vectors/vectors_hub.json (byte for byte)
#   vectors_hub_policy.json  the hub's vectors/vectors_hub_policy.json (orch.hub-policy/1, D14)
#   SOURCE                   hub_repo, hub_commit, vectors_hub_sha256, vectors_hub_policy_sha256
# Land it as one PR that changes nothing else; tests/hub/test_vectors_hub.py checks SOURCE against the file.
# Usage: scripts/sync-hub-vectors.sh [ref] (default origin/develop); ORCH_HUB_DIR overrides the clone path.
set -euo pipefail
ref="${1:-origin/develop}"
hub="${ORCH_HUB_DIR:-$HOME/orch-dev/orch-hub}"
dest="$(cd "$(dirname "${BASH_SOURCE[0]}")/../tests/hub/vectors" && pwd)"
git -C "$hub" fetch -q
sha="$(git -C "$hub" rev-parse "$ref^{commit}")"
tmp="$(mktemp "$dest/.sync.XXXXXX")"
tmp2="$(mktemp "$dest/.sync.XXXXXX")"
trap 'rm -f "$tmp" "$tmp2"' EXIT
git -C "$hub" show "$sha:vectors/vectors_hub.json" > "$tmp"  # a git failure leaves the vendored file untouched
git -C "$hub" show "$sha:vectors/vectors_hub_policy.json" > "$tmp2"
digest="$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$tmp")"
pdigest="$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$tmp2")"
mv "$tmp" "$dest/vectors_hub.json"
mv "$tmp2" "$dest/vectors_hub_policy.json"
printf 'hub_repo: orch-hub\nhub_commit: %s\nvectors_hub_sha256: %s\nvectors_hub_policy_sha256: %s\n' "$sha" "$digest" "$pdigest" > "$dest/SOURCE"
echo "synced tests/hub/vectors from orch-hub $sha"
