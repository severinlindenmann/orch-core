"""M7a against core's in-process host and runner (`termkit`): the routes, the lazy attach, the bridge and the lease."""

import json
import sys

import pytest
from termkit import TermWorld, key, read_until, text, until_close

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")


@pytest.fixture
def tw(tmp_path, monkeypatch):
    try:
        world = TermWorld(tmp_path, monkeypatch)
    except OSError as exc:  # pragma: no cover - a sandbox without AF_UNIX bind
        pytest.skip(f"no unix sockets here: {exc}")
    yield world
    world.close()


def test_open_attach_type_and_read_back(tw):
    run = tw.open_shell()
    assert run["kind"] == "shell" and run["as_person"] is True and run["state"] == "running"
    listed = tw.get("/runs").json()
    assert [r["run"] for r in listed["runs"]] == [run["run"]] and listed["runner"]["connected"] is True
    with tw.connect(run["run"], mode="write", take=True) as ws:
        ready = json.loads(text(ws))
        assert ready["t"] == "ready" and ready["writer"] is True and ready["state"] == "running"
        assert set(ready) == {"t", "start", "offset", "cols", "rows", "writer", "state"}
        ws.send_bytes(key(b"echo canary-$((6*7))\n"))
        got, _ = read_until(ws, b"canary-42")
        assert b"canary-42" in got


def test_read_attachment_drops_input_and_sees_writer_false(tw):
    run = tw.open_shell()["run"]
    with tw.connect(run, mode="write", take=True) as writer:
        assert json.loads(text(writer))["writer"] is True
        with tw.connect(run) as reader:  # read-only by default
            ready = json.loads(text(reader))
            assert ready["writer"] is False
            reader.send_bytes(key(b"echo from-reader-$((2*3))\n"))  # dropped at the sink
            writer.send_bytes(key(b"echo from-writer-$((3*3))\n"))
            got, _ = read_until(reader, b"from-writer-9")
            assert b"from-reader-6" not in got


def test_stop_ends_the_attachment_with_1000(tw):
    run = tw.open_shell()["run"]
    with tw.connect(run) as ws:
        assert json.loads(text(ws))["t"] == "ready"
        res = tw.post(f"/runs/{run}/stop")
        assert res.status_code == 200, res.text
        assert res.json()["cleanup"] in ("complete", "incomplete", "unknown", None)
        code, texts = until_close(ws)
        assert code == 1000 and texts[-1]["t"] == "end"
        assert texts[-1]["reason"] in ("run.stopped", "exited")
