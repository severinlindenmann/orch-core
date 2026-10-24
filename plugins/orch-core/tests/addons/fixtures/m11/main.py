"""A stdlib-only add-on for the API 1.1 runner tests. The mode is ``params.mode``; answers are JSON-RPC lines."""

import json
import os
import sys

req = json.loads(sys.stdin.readline())
params = req["params"]
mode = params.get("mode", "params")


def reply(result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}) + "\n")
    sys.stdout.flush()


if mode == "params":
    shown = dict(params)
    if "credential" in shown:  # never echo it (the runner refuses that): show a digest
        import hashlib

        shown["credential"] = hashlib.sha256(shown["credential"].encode()).hexdigest()
    reply({"method": req["method"], "params": shown})
elif mode == "flags":
    import sysconfig

    reply(
        {
            "exe": sys.executable,
            "argv": sys.argv,
            "flags": [
                sys.flags.dont_write_bytecode,
                sys.flags.ignore_environment,
                sys.flags.no_user_site,
                sys.flags.no_site,
            ],
            "site": "site" in sys.modules,
            "scheme": bool(sysconfig),
            "env": dict(os.environ),
        }
    )
elif mode == "import_orch":
    try:
        import orch  # noqa: F401

        reply({"imported": True})
    except ImportError:
        reply({"imported": False})
elif mode == "echo_credential":
    reply({"note": params["credential"]})
elif mode == "echo_credential_escaped":
    sys.stdout.write(
        '{"jsonrpc":"2.0","id":1,"result":{"n":"' + "".join(f"\\u{ord(c):04x}" for c in params["credential"]) + '"}}\n'
    )
elif mode == "echo_credential_key":
    reply({params["credential"]: 1})
elif mode == "stderr_credential":
    sys.stderr.write("log: " + params["credential"] + " end\n")
    reply({})
elif mode == "stderr_cut":
    sys.stderr.write("x" * 10 + params["credential"])
    reply({})
elif mode == "crash_credential":
    sys.stderr.write("boom " + json.dumps(params["credential"]) + "\n")
    sys.exit(3)
elif mode == "env_args":
    reply({"env": dict(os.environ), "argv": sys.argv})
else:
    reply({})
