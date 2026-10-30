#!/bin/sh
# Builds the helper in release mode. Signs with Developer ID ONLY when ORCH_REMOTE_KEY_IDENTITY names a
# certificate present in the keychain; otherwise the build stays ad-hoc signed (unbound prototype).
set -eu
cd "$(dirname "$0")/.."
swift build -c release
BIN=.build/release/orch-remote-key
if [ -n "${ORCH_REMOTE_KEY_IDENTITY:-}" ] && security find-identity -v -p codesigning | grep -q "$ORCH_REMOTE_KEY_IDENTITY"; then
  codesign --force --options runtime --timestamp --identifier io.severin.orch.remote-key \
    --entitlements entitlements.plist --sign "$ORCH_REMOTE_KEY_IDENTITY" "$BIN"
  echo "signed (bound mode needs the provisioning profile and app bundle, see README)"
else
  codesign --force --sign - --identifier io.severin.orch.remote-key "$BIN"
  echo "ad-hoc signed (UNBOUND prototype)"
fi
echo "$BIN"
