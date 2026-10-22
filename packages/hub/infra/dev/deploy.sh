#!/usr/bin/env bash
# shellcheck disable=SC2029
# Deploy orch-hub (dev link server on 127.0.0.1:8105) to a dev host (ssh alias "orch-edge-deploy", user "deploy").
# Run from the dev machine: infra/dev/deploy.sh      (modeled on orch-publish infra/dev/deploy.sh)
# Refuses a dirty git tree unless ORCH_DEPLOY_ALLOW_DIRTY=1 (release is then named ...-dirty-<hash>).
# The unit (orch-hub) comes from the orch-dev-kit provisioning scripts (vps/provision-edge.sh).
# Health: the hub has no HTTP port; the unit must be active and have printed its "orch-hub ready" line since the restart.
# A failed check rolls back to the previous release. First deploy: the hub keys do not exist yet (`orch-hub keys init`
# is a manual step as user orch-hub), so run once with ORCH_HUB_SKIP_RESTART=1 (installs and switches, no restart).
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

rm -rf dist
uv build --wheel
wheel="$(ls dist/orch_hub-*.whl)"
wheel_name="$(basename "$wheel")"
version="$(printf '%s' "$wheel_name" | cut -d- -f2)"
wheel_sha="$(shasum -a 256 "$wheel" | cut -d' ' -f1)"
if [ -n "$dirty" ]; then
    release="hub-${version}-dirty-${wheel_sha:0:8}"
else
    release="hub-${version}-${sha}"
fi
rdir="${BASE}/releases/${release}"
echo "deploying ${release}"

ssh "$HOST" "mkdir -p '${rdir}' '${BASE}/hub'"
rsync -a "$wheel" "${HOST}:${rdir}/${wheel_name}"

# Everything below runs on the host under the shared deploy lock.
ssh "$HOST" bash -s -- "$rdir" "$wheel_name" "$wheel_sha" "$BASE" "$KEEP" "${ORCH_HUB_SKIP_RESTART:-0}" <<'REMOTE'
set -euo pipefail
rdir="$1"; wheel_name="$2"; wheel_sha="$3"; base="$4"; keep="$5"; skip_restart="$6"
units="orch-hub"

exec 9>"$base/.deploy.lock"
flock -n 9 || { echo "another deploy is running" >&2; exit 1; }

if [ "$(cat "$rdir/.wheel.sha256" 2>/dev/null || true)" != "$wheel_sha" ] \
        || [ ! -x "$rdir/venv/bin/orch-hub" ]; then
    py="$(command -v python3.14 || command -v python3)"
    rm -rf "$rdir/venv" "$rdir/.wheel.sha256"
    uv venv --python "$py" "$rdir/venv"
    uv pip install --python "$rdir/venv/bin/python" "$rdir/$wheel_name"
    printf '%s' "$wheel_sha" > "$rdir/.wheel.sha256"
fi
mkdir -p "$rdir/bin"
ln -sfn "$rdir/venv/bin/orch-hub" "$rdir/bin/orch-hub"
touch "$rdir"

switch() {
    ln -sfn "$1" "$base/hub/.current.new"
    mv -T "$base/hub/.current.new" "$base/hub/current"
}
restart_all() {
    local rc=0 u
    for u in $units; do
        sudo orch-unit enable "$u" || rc=1
        sudo orch-unit restart "$u" || rc=1
    done
    return "$rc"
}
check_all() {
    local i out pid
    for i in 1 2 3 4 5 6 7 8 9 10; do
        # only the ready line of the CURRENT main process counts (a line from before the restart is stale)
        pid="$(systemctl show -p MainPID --value orch-hub 2>/dev/null || true)"
        if [ -n "$pid" ] && [ "$pid" != 0 ] && sudo orch-unit status orch-hub >/dev/null 2>&1 \
                && out="$(sudo orch-unit logs orch-hub 50 2>/dev/null | grep -F "orch-hub[${pid}]:" | grep -F 'orch-hub ready' | tail -n 1)" \
                && [ -n "$out" ]; then
            echo "orch-hub: active, ready line seen"
            return 0
        fi
        sleep 1
    done
    echo "health check failed: orch-hub is not active/ready" >&2
    return 1
}

prev="$(readlink "$base/hub/current" || true)"
switch "$rdir"
if [ "$skip_restart" = 1 ]; then
    echo "installed ${rdir} (ORCH_HUB_SKIP_RESTART=1: no restart)"
elif restart_all && check_all; then
    :
else
    echo "deploy failed" >&2
    for u in $units; do sudo orch-unit logs "$u" 50 || true; done
    if [ -n "$prev" ] && [ "$prev" != "$rdir" ]; then
        echo "rolling back to $prev" >&2
        switch "$prev"
        restart_all || true
    fi
    exit 1
fi

# shellcheck disable=SC2012
ls -1dt "$base"/releases/hub-* | tail -n +"$((keep + 1))" | while read -r old; do
    [ "$old" = "$rdir" ] || [ "$old" = "$prev" ] || rm -rf "$old"
done
REMOTE
