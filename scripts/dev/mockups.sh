#!/bin/bash
# mockups.sh start|stop|status : serve every browser mockup of this repo on 127.0.0.1 (see docs/mockups/README.md).
#   start   dashboard mockup (Vite, mock transport) and the hub mockup (static file) on free ports in 8780-8799
#   stop    stop exactly the processes this script started (PIDs from its own pid file)
#   status  print the URLs of the running mockups
# Needs `npm ci` once in packages/addons/dashboard. PORT_FROM/PORT_TO override the range.
set -u
ROOT=$(cd "$(dirname "$0")/../.." && pwd -P)
DASH=$ROOT/packages/addons/dashboard
STATE=${XDG_CACHE_HOME:-$HOME/.cache}/orch-mockups
PIDS=$STATE/$(echo "$ROOT" | cksum | cut -d' ' -f1).pids
FROM=${PORT_FROM:-8780}; TO=${PORT_TO:-8799}
mkdir -p "$STATE"

free_port() { # next port in the range nobody listens on (127.0.0.1 only), skipping the ones in $1
  local p
  for p in $(seq "$FROM" "$TO"); do
    case " $1 " in *" $p "*) continue;; esac
    lsof -nP -iTCP@127.0.0.1:"$p" -sTCP:LISTEN >/dev/null 2>&1 || { echo "$p"; return 0; }
  done
  return 1
}
wait_up() { local _; for _ in $(seq 1 40); do curl -s -o /dev/null "http://127.0.0.1:$1/" && return 0; sleep 0.5; done; return 1; }
alive() { [ -n "$1" ] && kill -0 "$1" 2>/dev/null; }

status() {
  [ -f "$PIDS" ] || { echo "no mockups running (from $ROOT)"; return 0; }
  while read -r name pid port path; do
    if alive "$pid"; then echo "$name  http://127.0.0.1:$port$path  (pid $pid)"; else echo "$name  stopped"; fi
  done < "$PIDS"
}

case ${1:-start} in
  start)
    if [ -f "$PIDS" ]; then status; echo "already started: run '$0 stop' first"; exit 1; fi
    [ -x "$DASH/node_modules/.bin/vite" ] || { echo "run 'npm ci' in $DASH first" >&2; exit 2; }
    P1=$(free_port "") || { echo "no free port in $FROM-$TO" >&2; exit 1; }
    P2=$(free_port "$P1") || { echo "no free port in $FROM-$TO" >&2; exit 1; }
    LOG1=$STATE/dashboard.log; LOG2=$STATE/hub.log
    # node runs vite directly so the PID written is the server itself (npx would leave a child behind).
    (cd "$DASH" && exec node node_modules/vite/bin/vite.js --port "$P1" --strictPort --host 127.0.0.1 >"$LOG1" 2>&1) &
    D=$!
    python3 -m http.server "$P2" --bind 127.0.0.1 --directory "$ROOT/docs/mockups/hub" >"$LOG2" 2>&1 &
    H=$!
    printf 'dashboard %s %s /\nhub %s %s /index.html\n' "$D" "$P1" "$H" "$P2" > "$PIDS"
    wait_up "$P1" || echo "dashboard did not answer, see $LOG1" >&2
    wait_up "$P2" || echo "hub mockup did not answer, see $LOG2" >&2
    status
    echo "more entry points: http://127.0.0.1:$P1/preview-setup.html  /remote-client.html  /__layout/<scenario>"
    ;;
  stop)
    [ -f "$PIDS" ] || { echo "nothing to stop"; exit 0; }
    while read -r name pid port path; do alive "$pid" && kill "$pid" && echo "stopped $name (pid $pid)"; done < "$PIDS"
    rm -f "$PIDS"
    ;;
  status) status ;;
  *) echo "usage: $0 start|stop|status" >&2; exit 2 ;;
esac
