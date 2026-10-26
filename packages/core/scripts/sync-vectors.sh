#!/usr/bin/env bash
# Refresh the vendored orch-relay corpus in tests/wsproto/vectors/ from an orch-relay ref (contract 11A.10):
#   vectors_v2.json.xz  the relay's tests/vectors_v2.json, xz-compressed (only the compressed file is committed)
#   http_fixtures/      MANIFEST.json and every file it lists
#   SOURCE              relay_commit, http_fixtures_manifest_revision, vectors_v2_sha256 (over the DECOMPRESSED bytes)
# Then bump PINNED_RELAY_COMMIT in tests/wsproto/test_corpus.py and land it as one PR that changes nothing else.
# Usage: scripts/sync-vectors.sh [ref] (default origin/develop); ORCH_RELAY_DIR overrides the clone path.
set -euo pipefail
ref="${1:-origin/develop}"
relay="${ORCH_RELAY_DIR:-$HOME/orch-dev/orch-relay}"
dest="$(cd "$(dirname "${BASH_SOURCE[0]}")/../tests/wsproto/vectors" && pwd)"
git -C "$relay" fetch -q
sha="$(git -C "$relay" rev-parse "$ref^{commit}")"
work="$(mktemp -d "$dest/.sync.XXXXXX")"
trap 'rm -rf "$work"' EXIT
git -C "$relay" show "$sha:tests/vectors_v2.json" > "$work/vectors_v2.json"
git -C "$relay" show "$sha:tests/http_fixtures/MANIFEST.json" > "$work/MANIFEST.json"
mkdir "$work/http_fixtures"
python3 - "$work" "$relay" "$sha" <<'PY'
import hashlib, json, lzma, pathlib, subprocess, sys

work, relay, sha = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
raw = (work / "vectors_v2.json").read_bytes()
xz = lzma.compress(raw, format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64, preset=9 | lzma.PRESET_EXTREME)
assert lzma.decompress(xz) == raw
(work / "vectors_v2.json.xz").write_bytes(xz)
manifest = json.loads((work / "MANIFEST.json").read_text())
for name in manifest["files"]:
    data = subprocess.run(
        ["git", "-C", relay, "show", f"{sha}:tests/http_fixtures/{name}"], capture_output=True, check=True
    ).stdout
    (work / "http_fixtures" / name).write_bytes(data)
(work / "http_fixtures" / "MANIFEST.json").write_bytes((work / "MANIFEST.json").read_bytes())
(work / "SOURCE").write_text(
    "relay_repo: orch-relay\n"
    f"relay_commit: {sha}\n"
    f"http_fixtures_manifest_revision: {manifest['revision']}\n"
    f"vectors_v2_sha256: {hashlib.sha256(raw).hexdigest()}\n"
)
PY
rm -rf "$dest/http_fixtures"
mv "$work/http_fixtures" "$dest/http_fixtures"
mv "$work/vectors_v2.json.xz" "$dest/vectors_v2.json.xz"
mv "$work/SOURCE" "$dest/SOURCE"
rm -f "$dest/vectors_v2.json" # never commit the raw 23 MB file
echo "synced tests/wsproto/vectors from orch-relay $sha; now bump PINNED_RELAY_COMMIT in tests/wsproto/test_corpus.py"
