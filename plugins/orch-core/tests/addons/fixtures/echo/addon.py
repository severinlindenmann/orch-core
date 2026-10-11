"""A tiny addon for the runner tests: it speaks JSON-RPC over stdio and misbehaves on request.

The mode is the ``trigger`` of the request. Standalone, standard library only.
"""

import json
import os
import sys
import time

line = sys.stdin.readline()
req = json.loads(line)
mode = req["params"]["trigger"]


def reply(result=None, **extra):
    doc = {"jsonrpc": "2.0", "id": req["id"], "result": result}
    doc.update(extra)
    sys.stdout.write(json.dumps(doc) + "\n")
    sys.stdout.flush()


if mode == "ok":
    reply({"set": {"points": 5}, "sections": {"notes": "looked at " + req["params"]["ticket"]["key"]}, "artifacts": []})
elif mode == "env":
    reply({"env": dict(os.environ), "cwd": os.getcwd(), "pkg": os.path.dirname(os.path.abspath(__file__))})
elif mode == "hang":
    time.sleep(60)
elif mode == "hang_after_start":
    sys.stdout.write("{")
    sys.stdout.flush()
    time.sleep(60)
elif mode == "crash":
    sys.stderr.write("boom\n")
    sys.exit(3)
elif mode == "big":
    sys.stdout.write('{"jsonrpc":"2.0","id":1,"result":"' + "x" * (300 * 1024) + '"}\n')
elif mode == "endless":
    while True:
        sys.stdout.write("x" * 65536)
        sys.stdout.flush()
elif mode == "garbage":
    sys.stdout.write("this is not json\n")
elif mode == "two_lines":
    reply({})
    reply({})
elif mode == "no_newline":
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}))
elif mode == "wrong_id":
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {}}) + "\n")
elif mode == "extra_key":
    reply({}, extra_key=1)
elif mode == "both":
    reply({}, error={"code": 1, "message": "x"})
elif mode == "error":
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "\x1b[31mhi"}}) + "\n")
elif mode == "float":
    sys.stdout.write('{"jsonrpc":"2.0","id":1,"result":{"set":{"points":1.5}}}\n')
elif mode == "dup_keys":
    sys.stdout.write('{"jsonrpc":"2.0","id":1,"result":{},"result":{}}\n')
elif mode == "writes":
    out = {}
    for where in (
        os.getcwd(),
        os.path.dirname(os.path.abspath(__file__)),
        os.environ.get("ORCH_TEST_WORKSPACE", "/nonexistent"),
    ):
        try:
            with open(os.path.join(where, "pwned.txt"), "w") as f:
                f.write("x")
            out[where] = "written"
        except OSError as e:
            out[where] = type(e).__name__
    reply(out)
elif mode == "grandchild":
    import subprocess

    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdout=sys.stdout, stderr=sys.stderr)
    reply({"grandchild": p.pid})
elif mode == "stderr_flood":
    sys.stderr.write("e" * (1 << 20))
    reply({})
elif mode == "fds":
    reply({"fds": sorted(int(n) for n in os.listdir("/dev/fd") if n.isdigit())})
elif mode == "proposal_bad_field":
    reply({"set": {"nope": 1}})
elif mode == "proposal_core_path":
    reply({"set": {"ticket.title": "x"}})
elif mode == "exit0_no_answer":
    pass
else:
    reply({})
