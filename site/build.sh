#!/usr/bin/env bash
# Assembles the GitHub Pages site into _site/: the page from site/, the images from docs/images/v2 (shared with the README).
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf _site && mkdir -p _site/images/v2
cp site/index.html site/favicon.svg _site/
cp docs/images/v2/*.png docs/images/v2/*.jpg _site/images/v2/
touch _site/.nojekyll
