# Real-host helper for followups-real.spec.ts: a dev sandbox workspace (prefix SBX, Schedules ON, the dashboard add-on installed,
# a runner whose harness writes nothing) with one ticket whose agent set a follow-up two days ahead (pending) and one whose follow-up falls due in about a
# minute and whose run writes nothing (the host records `failed`). Reuses core's own follow-up e2e environment.
# Run it with: <server venv python> -I e2e/followups-real-env.py up|link|sign|cli|state|down BASE   (ORCH_CORE_DIR = this checkout)
import json, os, re, signal, subprocess, sys, time
from pathlib import Path

CORE = Path(os.environ["ORCH_CORE_DIR"]).resolve()
for p in (CORE / "packages/addons/dashboard/server/tests", CORE / "packages/addons/dashboard/server/src", CORE / "packages/core", CORE / "packages/core/src", CORE / "packages/core/tests/host"):
    sys.path.insert(0, str(p))
from orch import devsandbox  # noqa: E402
from realhost_kit import ADDON, Pty  # noqa: E402
import test_followups_real_host as core_e2e  # noqa: E402

SANDBOX_ROOT = (Path.home() / "orch-dev").resolve()


def checked_base(base, new=False):
    """BASE must be under ~/orch-dev/ (never a mistyped ~ or /); for `up` it must be new or empty."""
    b = Path(base).expanduser().resolve()
    if SANDBOX_ROOT not in b.parents:
        sys.exit(f"refusing BASE {b}: it must be inside {SANDBOX_ROOT}/")
    if new and b.exists() and any(b.iterdir()):
        sys.exit(f"refusing BASE {b}: not empty")
    return b


# A harness that ends at once and writes nothing: the host records the run as `failed` (no_writes), a Today card.
core_e2e.HARNESS = "#!@PY@\nimport sys\nsys.exit(0)\n"


def env_of(base):
    return core_e2e.Env(Path(base))


def denv(ws):
    env = ws.env(); env["PYTHONPATH"] = str(ADDON / "server" / "src"); return env


def up(base):
    e = env_of(base)
    ws, kit = e.ws, e.kit
    Path(base).mkdir(parents=True, exist_ok=True)
    e.up()  # sandbox workspace, Schedules ON, the host with the dev timer, a runner running the silent harness
    ws.install_dashboard()
    (Path(base) / "host.pid").write_text(str(ws.host.pid))
    (Path(base) / "runner.pid").write_text(str(e.runner.pid))
    waiting = e.in_progress("Backfill tariff data")
    soon = e.in_progress("Reconcile settlement files")
    state = {
        "waiting": waiting,
        "waiting_fu": e.set_fu(waiting, "2d", "Check whether backfill 8812 finished.\n\n- If yes: run the reconcile.\n- If not: ask Mara."),
        "stuck": soon,
        "stuck_fu": e.set_fu(soon, "1m", "Continue with the settlement files."),
        "ws": ws.ws_id,
    }
    (Path(base) / "followups.json").write_text(json.dumps(state))
    print(json.dumps(state))


def stop_pid(path, *needles):
    """SIGTERM the recorded host only while its command line still names THIS sandbox state (a reused PID is left alone)."""
    if path.exists():
        pid = int(path.read_text())
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
        if all(n in out for n in needles):
            os.kill(pid, signal.SIGTERM)
        path.unlink(missing_ok=True)


def main():
    cmd = sys.argv[1]
    base = str(checked_base(sys.argv[2], new=cmd == "up"))
    if cmd == "up":
        return up(base)
    e = env_of(base)
    ws = e.ws
    ws.base = Path(base) / "ws"
    if cmd == "link":
        run = Pty([sys.executable, "-m", "orch_dashboard", "open", *sys.argv[3:4], "--print-url"], denv(ws), ws.root, [], 40).run()
        print(next((l.strip() for l in run.output.splitlines() if l.startswith("http://")), run.output)); return
    if cmd == "sign":
        print(ws.sign(sys.argv[3])[-600:]); return
    if cmd == "state":
        print((Path(base) / "followups.json").read_text()); return
    if cmd == "cli":
        if sys.argv[3] not in ("human", "agent", "unattended"):
            sys.exit("cli takes only human, agent or unattended")
        print(getattr(ws, sys.argv[3])(*sys.argv[4:])); return
    if cmd == "down":
        subprocess.run([sys.executable, "-m", "orch_dashboard", "stop"], env=denv(ws), capture_output=True)
        stop_pid(Path(base) / "runner.pid", "orch", "runner", str(ws.state))
        stop_pid(Path(base) / "host.pid", "orch", "host", str(ws.state))


main()
