#!/usr/bin/env bash
# shellcheck disable=SC2029
# Install the orch-core wheel on the dev edge host (D12, S11: a Linux workspace host on the one-VPS layout).
# Run from the dev machine: scripts/dev/deploy-edge-host.sh      (modeled on orch-hub infra/dev/deploy.sh)
#
# Who/what: ssh alias ORCH_DEPLOY_HOST (default orch-edge-deploy) = user `deploy`, who owns /opt/orch/{core,releases}.
# No sudo is used or needed. It installs /opt/orch/releases/core-<version>-<sha> and switches /opt/orch/core/current. It never
# restarts or enables a host unit: the workspace host (orch-host-<ws>) is started by hand / the e2e scenario (it creates keys).
#
# Safety: refuses a dirty tree (ORCH_DEPLOY_ALLOW_DIRTY=1 overrides) and a HEAD that is not on any origin branch (what runs on
# the edge must be reviewable). The wheel is hashed here and verified on the host before anything is installed; dependencies
# come from the uv.lock export with --require-hashes (the wheel itself is installed with --no-deps). The deploy lock is taken
# before anything is written; the release is built into a fresh directory and the one `current` points at is never rebuilt.
# ORCH_DEPLOY_DRY_RUN=1: run the local checks and the build, print the plan, touch nothing on the host.
set -euo pipefail

HOST="${ORCH_DEPLOY_HOST:-orch-edge-deploy}"
BASE=/opt/orch
KEEP=5

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

sha="$(git rev-parse --short HEAD)"
dirty=""
if [ -n "$(git status --porcelain --untracked-files=all)" ]; then
    if [ "${ORCH_DEPLOY_ALLOW_DIRTY:-}" != 1 ]; then
        echo "working tree is dirty; commit or set ORCH_DEPLOY_ALLOW_DIRTY=1" >&2
        exit 1
    fi
    dirty=1
fi
if [ -z "$dirty" ]; then
    git fetch -q origin
    if [ -z "$(git branch -r --contains HEAD 2>/dev/null)" ]; then
        echo "HEAD ${sha} is not on any origin branch; push it first" >&2
        exit 1
    fi
fi

rm -rf packages/core/dist
(cd packages/core && uv build --wheel)
wheel="$(ls packages/core/dist/orch_core-*.whl)"
wheel_name="$(basename "$wheel")"
version="$(printf '%s' "$wheel_name" | cut -d- -f2)"
wheel_sha="$(shasum -a 256 "$wheel" | cut -d' ' -f1)"
(cd packages/core && uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file dist/requirements.txt >/dev/null)
grep -q -- '--hash=sha256:' packages/core/dist/requirements.txt || { echo "uv export produced no hashes" >&2; exit 1; }
if [ -n "$dirty" ]; then
    release="core-${version}-dirty-${wheel_sha:0:8}"
else
    release="core-${version}-${sha}"
fi
rdir="${BASE}/releases/${release}"
echo "deploying ${release} (wheel sha256 ${wheel_sha})"
if [ "${ORCH_DEPLOY_DRY_RUN:-}" = 1 ]; then
    echo "dry run: would install ${rdir} on ${HOST} and switch ${BASE}/core/current"
    exit 0
fi

# The remote script runs under the deploy lock FIRST; the wheel and the hashed requirements arrive as a tar on stdin.
remote='
set -euo pipefail
umask 022
rdir="$1"; wheel_name="$2"; wheel_sha="$3"; base="$4"; keep="$5"; version="$6"
exec 9>"$base/.deploy.lock"
flock -n 9 || { echo "another deploy is running" >&2; exit 1; }
cur="$(readlink "$base/core/current" || true)"
if [ "$cur" = "$rdir" ]; then echo "$rdir is already current; refusing to rebuild it (commit a change for a new release)" >&2; exit 1; fi
rm -rf "$rdir"
mkdir -p "$rdir" "$base/core"
tar -x -C "$rdir"
[ "$(sha256sum "$rdir/$wheel_name" | cut -d" " -f1)" = "$wheel_sha" ] || { echo "wheel sha256 mismatch on the host" >&2; exit 1; }
py="$(command -v python3.14)" || { echo "python3.14 missing on the host" >&2; exit 1; }
uv venv --python "$py" "$rdir/venv"
uv pip install --python "$rdir/venv/bin/python" --require-hashes --no-deps -r "$rdir/requirements.txt"
uv pip install --python "$rdir/venv/bin/python" --no-deps "$rdir/$wheel_name"
uv pip freeze --python "$rdir/venv/bin/python" > "$rdir/freeze.txt"
mkdir -p "$rdir/bin"
ln -sfn "$rdir/venv/bin/orch" "$rdir/bin/orch"
v="$("$rdir/bin/orch" --version)"
case "$v" in "orch $version "*|"orch $version") ;; *) echo "orch --version says [$v], expected $version" >&2; exit 1;; esac
chmod -R go-w "$rdir"
ln -sfn "$rdir" "$base/core/.current.new"
mv -T "$base/core/.current.new" "$base/core/current"
echo "installed $rdir ($v); /opt/orch/core/current switched (no unit restarted)"
# shellcheck disable=SC2012
ls -1dt "$base"/releases/core-* | tail -n +"$((keep + 1))" | while read -r old; do
    [ "$old" = "$rdir" ] || [ "$old" = "$cur" ] || rm -rf "$old"
done
'
args="$(printf '%q ' "$rdir" "$wheel_name" "$wheel_sha" "$BASE" "$KEEP" "$version")"
tar -c -C packages/core/dist "$wheel_name" requirements.txt | ssh "$HOST" "bash -c $(printf '%q' "$remote") -- ${args}"
