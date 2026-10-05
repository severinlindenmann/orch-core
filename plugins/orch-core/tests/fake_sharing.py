"""A stand-in for the sharing CLI in tests: `space show`, `bridge-key` and the `bridge-host` pipe protocol
(orch-tix#90), with no network. Driven by the directory in FAKE_SHARING_DIR:

  space.json        what `space show --json` prints (exit 0); {"error": code, "exit": n} makes it fail
  key               the hex K_ws `bridge-key` prints; key-exit holds an exit code to fail with instead
  queue/*.json      consumed in name order: {"rid", "body"} is delivered by the next poll;
                    {"op", "code"} makes the next command of that op fail with that code;
                    {"op", "delay"} delays the next answer of that op; {"op": "...", "exit": n} exits instead
  log.jsonl         written: the argv first, then every command received, then {"eof": true}

Polls wait at most FAKE_POLL_MAX seconds (default 0.05) whatever `wait` says.
"""
import json
import os
import sys
import time
from pathlib import Path

DIR = Path(os.environ["FAKE_SHARING_DIR"])
POLL_MAX = float(os.environ.get("FAKE_POLL_MAX", "0.05"))


def log(obj):
    with open(DIR / "log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def take(op=None):
    """The first queue item for `op` (an injection), or for op None the first request; removed."""
    q = DIR / "queue"
    for p in sorted(q.glob("*.json")) if q.is_dir() else []:
        try:
            item = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (op is None and "rid" in item) or (op is not None and item.get("op") == op):
            p.unlink()
            return item
    return None


def bridge_host():
    if sys.stdout.isatty() or sys.stdin.isatty():
        print("refused: a pipe only", file=sys.stderr)
        return 6
    print(json.dumps({"event": "ready", "holder": "fake", "lease_s": 40}), flush=True)
    for line in sys.stdin:
        try:
            cmd = json.loads(line)
        except ValueError:
            print(json.dumps({"id": None, "ok": False, "code": "protocol", "message": "not JSON"}), flush=True)
            continue
        log(cmd)
        cid, op = cmd.get("id"), cmd.get("op")
        inj = take(op)
        if inj and "delay" in inj:
            time.sleep(float(inj["delay"]))
            inj = None
        if inj and "exit" in inj:
            return int(inj["exit"])
        if inj and "code" in inj:
            print(json.dumps({"id": cid, "ok": False, "code": inj["code"], "message": "injected"}), flush=True)
            continue
        if op == "poll":
            deadline = time.monotonic() + min(float(cmd.get("wait", 0)), POLL_MAX)
            got = []
            while True:
                item = take()
                while item is not None:
                    got.append({"rid": item["rid"], "body": item["body"]})
                    item = take()
                if got or time.monotonic() >= deadline:
                    break
                time.sleep(0.005)
            print(json.dumps({"id": cid, "ok": True, "requests": got, "lease_s": 40}), flush=True)
        elif op in ("respond", "heartbeat", "goodbye", "release"):
            print(json.dumps({"id": cid, "ok": True}), flush=True)
        else:
            print(json.dumps({"id": cid, "ok": False, "code": "protocol", "message": "unknown op"}), flush=True)
    log({"eof": True})
    return 0


def main(argv):
    log({"argv": argv})
    if argv[:2] == ["space", "show"]:
        data = json.loads((DIR / "space.json").read_text(encoding="utf-8"))
        if "error" in data:
            print(json.dumps({"error": data["error"], "detail": "fake"}))
            return int(data.get("exit", 1))
        print(json.dumps(data))
        return 0
    if argv[:1] == ["bridge-key"]:
        if sys.stdout.isatty():
            return 6
        code = DIR / "key-exit"
        if code.exists():
            return int(code.read_text().strip())
        print((DIR / "key").read_text().strip())
        return 0
    if argv[:1] == ["bridge-host"]:
        return bridge_host()
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
