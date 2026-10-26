"""Terminals (M7a) end to end: the dashboard server against core's host and a real runner with a real `/bin/sh`.

**Why not a host subprocess.** `orch host run --foreground` admits a runner only when the connecting PID is the runner
unit's main PID as the service manager reports it (`orch.host.runners.runner_identity`: `launchctl print` /
`systemctl show`), and the service manager is off in tests (`ORCH_NO_SERVICE_MANAGER=1`, never a launchd/systemd unit
here). Core has no dev seam for a foreground runner beside a foreground host (requested from stream A). So this test
runs core's host in a thread of this process with the `runner_identity` hook core's own H11b tests use, and core's
runner helper (supervisor, term socket, host link) in this process too: the same code paths as the services, with real
sockets, a real PTY and a real shell. Everything lives under pytest's tmp_path and core's short run directory.

The flow: open a shell (preview, then open with an idem), attach read-only, take control, type `echo <canary>`, see it,
resize, a second attachment sees `writer:false` and the new size, stop the run.
"""

import json
import sys
import uuid

import pytest
from termkit import TermWorld, key, read_until, text, until_close, until_text

pytestmark = [
    pytest.mark.realhost,
    pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only"),
]


@pytest.fixture
def tw(tmp_path, monkeypatch):
    try:
        world = TermWorld(tmp_path, monkeypatch)
    except OSError as exc:  # pragma: no cover - a sandbox without AF_UNIX bind
        pytest.skip(f"no unix sockets here: {exc}")
    yield world
    world.close()


def test_a_person_opens_reads_takes_control_types_resizes_and_stops(tw):
    canary = "canary-" + uuid.uuid4().hex[:8]
    preview = tw.post("/terminals/preview", {}).json()
    assert preview["blocked"] is None and preview["notice"] == "acts as you, not as an agent"
    run = tw.open_shell()["run"]
    assert tw.get("/runs").json()["runs"][0]["run"] == run

    with tw.connect(run) as mine:  # read-only by default
        ready = json.loads(text(mine))
        assert ready["writer"] is False and ready["state"] == "running"
        mine.send_bytes(key(b"echo ignored\n"))  # dropped: a read attachment sends nothing
    with tw.connect(run, mode="write", take=True) as mine:  # "Take control"
        assert json.loads(text(mine))["writer"] is True
        mine.send_bytes(key(f"echo {canary[:7]}''{canary[7:]}\n".encode()))
        got, _ = read_until(mine, canary.encode())
        assert canary.encode() in got and b"ignored\r\n" not in got.split(b"echo ignored")[-1]
        mine.send_text(json.dumps({"t": "resize", "cols": 90, "rows": 25}))
        with tw.connect(run) as other:  # a second tab
            second = json.loads(text(other))
            assert second["writer"] is False
            _, seen = read_until(other, canary.encode())  # the ring replays to the second viewer
            resized = {"t": "resize", "cols": 90, "rows": 25}
            if (second["cols"], second["rows"]) != (90, 25) and resized not in seen:
                assert until_text(other, "resize") == resized
        res = tw.post(f"/runs/{run}/stop")
        assert res.status_code == 200
        code, texts = until_close(mine)
        assert code == 1000 and texts[-1]["t"] == "end"
    listed = {r["run"]: r for r in tw.get("/runs").json()["runs"]}
    assert listed[run]["state"] in ("exited", "running")  # the stop may still be settling
