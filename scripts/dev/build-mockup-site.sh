#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
COMMIT="$(git rev-parse --short HEAD)"
mkdir -p "$ROOT/site"
(
  cd packages/addons/dashboard
  if [[ ! -d node_modules ]]; then
    npm ci
  fi
  if [[ -n "${DEMO_SITE_URL:-}" ]]; then
    export VITE_HUB_MOCKUP_URL="${DEMO_SITE_URL%/}/hub/index.html"
  fi
  DEMO_OUT="$ROOT/site/app" npm run build:demo
)
rm -rf "$ROOT/site/hub" "$ROOT/site/publish"
cp -R docs/mockups/hub site/hub
mkdir -p site/publish
cp docs/mockups/publish/index.html site/publish/index.html
sed "s/__COMMIT__/$COMMIT/g" docs/mockups/site/index.html > site/index.html
: > site/.nojekyll
printf 'Static demo built at %s\n' "$ROOT/site"
