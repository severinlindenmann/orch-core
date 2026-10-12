"""Served operations under the host capabilities and the audit hook (the H3a acceptance row), in a subprocess:
nothing the handlers do opens outside the roots, spawns, or breaks on a late import."""

import json
import subprocess
import sys
import textwrap

SCRIPT = textwrap.dedent(
    """
    import io, json, sys, time
    spec = json.loads(sys.argv[1])
    from orch.host import audit
    from orch.ops.caps import Capabilities
    import orch.ops.base as base
    from orch.cli.main import main
    audit.warm()
    import orch.host.ownership as own
    # direct-mode machinery (reads the agent pins outside the roots); the host has no use for it
    own.DirectGuard = lambda *a, **k: None
    import orch.hostclient as hostclient
    hostclient.locate = lambda *a, **k: None  # H4b mode lookup reads pins and the registry: a client's business
    host = Capabilities.host((spec["root"], spec["state"]))
    base.Capabilities.direct = classmethod(lambda cls, env=None, clock=time.time: host)  # H4 does this for real
    audit.install([spec["root"], spec["state"]])
    out = {}
    for argv in spec["argvs"]:
        o, e = io.StringIO(), io.StringIO()
        code = main(argv + ["--json"], env=spec["env"], stdout=o, stderr=e, now=lambda: spec["now"])
        out[" ".join(argv)] = [code, o.getvalue(), e.getvalue()]
    print("RESULT" + json.dumps(out))
    """
)

ARGVS = [
    ["status"],
    ["list"],
    ["show", "DEMO-0001"],
    ["check"],
    ["doctor"],
    ["doctor", "--repair"],
    ["instructions", "sync"],
    ["log", "-m", "a note under the hook"],
    ["artifact", "add", "nope.txt"],
]


def test_served_ops_run_under_host_caps_and_the_hook(ws, cli, tmp_path):
    assert cli("new", "one", "-m", "text").code == 0
    assert cli("claim", "DEMO-0001").code == 0
    spec = {"root": str(ws.root), "state": str(ws.host_state), "env": ws.env(), "argvs": ARGVS, "now": ws.clock[0]}
    done = subprocess.run(
        [sys.executable, "-c", SCRIPT, json.dumps(spec)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, (done.stderr[-1500:], done.stdout[-500:])
    results = json.loads(done.stdout.split("RESULT", 1)[1])
    for argv, (_code, out, err) in results.items():
        assert "host audit" not in out + err, (argv, out, err)
        assert '"internal"' not in out, (argv, out)
    assert results["status"][0] == 0
    assert results["list"][0] == 0
    assert results["show DEMO-0001"][0] == 0
    assert results["log -m a note under the hook"][0] == 0, results["log -m a note under the hook"]
    # a client path is refused with a declared error, not read
    assert json.loads(results["artifact add nope.txt"][1])["error"]["code"] == "invalid.input"
