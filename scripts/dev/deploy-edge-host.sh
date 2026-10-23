#!/usr/bin/env bash
# shellcheck disable=SC2029
# Install the orch-core wheel on the dev edge host (D12, S11: a Linux workspace host on the one-VPS layout).
# Run from the dev machine: scripts/dev/deploy-edge-host.sh      (modeled on orch-hub infra/dev/deploy.sh)
# Installs /opt/orch/releases/core-<version>-<sha> and switches /opt/orch/core/current. It never restarts or enables a host
# unit: the workspace host (orch-host-<ws>) is brought up by hand/the e2e scenario, because starting it creates keys.
# Refuses a dirty tree unless ORCH_DEPLOY_ALLOW_DIRTY=1.
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

rm -rf plugins/orch-core/dist
(cd plugins/orch-core && uv build --wheel)
wheel="$(ls plugins/orch-core/dist/orch_core-*.whl)"
wheel_name="$(basename "$wheel")"
version="$(printf '%s' "$wheel_name" | cut -d- -f2)"
wheel_sha="$(shasum -a 256 "$wheel" | cut -d' ' -f1)"
if [ -n "$dirty" ]; then
    release="core-${version}-dirty-${wheel_sha:0:8}"
else
    release="core-${version}-${sha}"
fi
rdir="${BASE}/releases/${release}"
echo "deploying ${release}"

ssh "$HOST" "mkdir -p '${rdir}' '${BASE}/core'"
rsync -a "$wheel" "${HOST}:${rdir}/${wheel_name}"

ssh "$HOST" bash -s -- "$rdir" "$wheel_name" "$wheel_sha" "$BASE" "$KEEP" <<'REMOTE'
set -euo pipefail
rdir="$1"; wheel_name="$2"; wheel_sha="$3"; base="$4"; keep="$5"

exec 9>"$base/.deploy.lock"
flock -n 9 || { echo "another deploy is running" >&2; exit 1; }

if [ "$(cat "$rdir/.wheel.sha256" 2>/dev/null || true)" != "$wheel_sha" ] \
        || [ ! -x "$rdir/venv/bin/orch" ]; then
    py="$(command -v python3.14)" || { echo "python3.14 missing on the host" >&2; exit 1; }
    rm -rf "$rdir/venv" "$rdir/.wheel.sha256"
    uv venv --python "$py" "$rdir/venv"
    uv pip install --python "$rdir/venv/bin/python" "$rdir/$wheel_name"
    printf '%s' "$wheel_sha" > "$rdir/.wheel.sha256"
fi
mkdir -p "$rdir/bin"
ln -sfn "$rdir/venv/bin/orch" "$rdir/bin/orch"
"$rdir/bin/orch" --help >/dev/null || { echo "orch does not start from the new release" >&2; exit 1; }
touch "$rdir"
ln -sfn "$rdir" "$base/core/.current.new"
mv -T "$base/core/.current.new" "$base/core/current"
echo "installed ${rdir}; /opt/orch/core/current switched (no unit restarted)"
# shellcheck disable=SC2012
ls -1dt "$base"/releases/core-* | tail -n +"$((keep + 1))" | while read -r old; do
    [ "$old" = "$rdir" ] || [ "$old" = "$(readlink "$base/core/current")" ] || rm -rf "$old"
done
REMOTE
