# A10 real-host helper for market-real.spec.ts (dev sandbox workspace with the S13 test marketplace; needs ~/orch-dev/.worktrees/s13 kit state in ~/orch-dev/state/s13).
# Run it with: <server venv python> -I e2e/market-real-env.py up|link|sign|cli|down BASE  (ORCH_CORE_DIR = this checkout)
"""A10 real-host env: dev sandbox workspace + dashboard addon + the S13 test marketplace added. up|link|sign|cli|down BASE."""
import json, os, signal, subprocess, sys
from pathlib import Path

CORE = Path(os.environ["ORCH_CORE_DIR"]).resolve()

STATE = Path.home() / "orch-dev/state/s13"
for p in (CORE / "addons/dashboard/server/tests", CORE / "addons/dashboard/server/src", CORE / "plugins/orch-core", CORE / "plugins/orch-core/src"):
    sys.path.insert(0, str(p))
from orch import devsandbox  # noqa: E402
from realhost_kit import ADDON, Pty, Workspace  # noqa: E402

SANDBOX_ID = "5130aaaa"
meta = json.loads((STATE / "commits.json").read_text())


def load(base):
    ws = Workspace(Path(base) / "ws", prefix="SBX", dirname="ws", owner=devsandbox.owner_name(SANDBOX_ID))
    ws.base = Path(base) / "ws"
    plain = ws.orch
    ws.orch = lambda *a: [*plain(*a), "--dev-sim-convenience"] if a[:3] == ("host", "run", "--foreground") else plain(*a)
    g = ws.base / "grant"
    ws.grant_secret = g.read_text() if g.exists() else None
    return ws


def pid_write(ws):
    (ws.base / "host.pid").write_text(str(ws.host.pid))


def up(base):
    ws = load(base)
    ws.make_dirs()
    root = ws.base
    devsandbox.write_manifest(root, SANDBOX_ID)
    ws.init()
    wid = ws.ws_id.removeprefix("w_")
    devsandbox.write_manifest(root, SANDBOX_ID, person_id=ws.person, workspaces=[(wid, ws.root)])
    (ws.state / "host").mkdir(mode=0o700, exist_ok=True)
    devsandbox.write_dev_json(root, SANDBOX_ID, "https://relay.dev.example")
    devsandbox.write_marker(ws.root, SANDBOX_ID, wid)
    ws.grant()
    (ws.base / "grant").write_text(ws.grant_secret)
    ws.install_dashboard()
    p = ws.state / "host" / "dev.json"
    doc = json.loads(p.read_text())
    doc["market"] = {"url": meta["url"], "keys": [meta["publisher"]]}
    p.write_text(json.dumps(doc, sort_keys=True)); p.chmod(0o600)
    ws.start_host(); pid_write(ws)
    ws.register()
    out = ws.human("market", "add", meta["url"], "--commit", meta["c1"], timeout=180)
    print(out[-800:], file=sys.stderr)
    print(json.dumps({"ws": ws.ws_id, "base": str(base)}))


def denv(ws):
    env = ws.env(); env["PYTHONPATH"] = str(ADDON / "server" / "src"); return env


def stop_pid(path, *needles):
    if path.exists():
        pid = int(path.read_text())
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
        if all(n in out for n in needles):
            os.kill(pid, signal.SIGTERM)
        path.unlink(missing_ok=True)


def main():
    cmd, base = sys.argv[1], sys.argv[2]
    if cmd == "up":
        return up(base)
    ws = load(base)
    if cmd == "link":
        run = Pty([sys.executable, "-m", "orch_dashboard", "open", *sys.argv[3:4], "--print-url"], denv(ws), ws.root, [], 40).run()
        print(next((l.strip() for l in run.output.splitlines() if l.startswith("http://")), run.output)); return
    if cmd == "sign":
        print(ws.sign(sys.argv[3])[-600:]); return
    if cmd == "cli":
        print(getattr(ws, sys.argv[3])(*sys.argv[4:])); return
    if cmd == "down":
        subprocess.run([sys.executable, "-m", "orch_dashboard", "stop"], env=denv(ws), capture_output=True)
        stop_pid(ws.base / "host.pid", "orch", "host")


main()
