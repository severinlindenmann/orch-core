"""Clean stop timing (#160): the child gets time for the relay's last release; an id-less protocol answer is fatal."""
import asyncio
import inspect
import sys
import time

import pytest

from orch.remote.transport import Child, TransportError

# A fake relay: after EOF it finishes a pending poll for `hold` seconds, then writes a marker and exits.
HOLD_CHILD = r"""
import json, sys, time
hold = float(sys.argv[1]); marker = sys.argv[2]
print(json.dumps({"event": "ready", "holder": "x", "lease_s": 40}), flush=True)
for line in sys.stdin:
    pass
time.sleep(hold)
open(marker, "w").write("released")
"""
BAD_CHILD = r"""
import json, sys, time
print(json.dumps({"event": "ready"}), flush=True)
sys.stdin.readline()
print(json.dumps({"id": None, "ok": False, "code": "protocol", "message": "not JSON"}), flush=True)
time.sleep(5)
"""


def test_close_waits_for_the_last_release_of_a_poll_in_flight(tmp_path):
    marker = tmp_path / "m"

    async def main():
        c = Child([sys.executable, "-c", HOLD_CHILD, "0.4", str(marker)], tmp_path)
        await c.start()
        await c.close(timeout=3)
    asyncio.run(main())
    assert marker.read_text() == "released"


def test_close_terminates_a_child_that_outlasts_the_wait(tmp_path):
    marker = tmp_path / "m"

    async def main():
        c = Child([sys.executable, "-c", HOLD_CHILD, "30", str(marker)], tmp_path)
        await c.start()
        t = time.monotonic()
        await c.close(timeout=0.3)
        return time.monotonic() - t
    assert asyncio.run(main()) < 5
    assert not marker.exists()


def test_a_normal_stop_is_not_slow(tmp_path):
    async def main():
        c = Child([sys.executable, "-c", HOLD_CHILD, "0", str(tmp_path / "m")], tmp_path)
        await c.start()
        t = time.monotonic()
        await c.close()  # the real default wait; the child exits at once
        return time.monotonic() - t
    assert asyncio.run(main()) < 3


def test_the_default_wait_covers_the_relays_30_second_poll_wait():
    assert inspect.signature(Child.close).parameters["timeout"].default >= 31


def test_an_id_less_protocol_answer_is_fatal_for_the_link(tmp_path):
    async def main():
        c = Child([sys.executable, "-c", BAD_CHILD], tmp_path)
        await c.start()
        t = time.monotonic()
        with pytest.raises(TransportError) as e:
            await c.call("poll", timeout=4)
        assert e.value.code == "protocol" and time.monotonic() - t < 3
        assert not c.alive
        with pytest.raises(TransportError):
            await c.call("poll", timeout=4)
        await c.close(timeout=0.2)
    asyncio.run(main())


def test_the_dashboard_stop_is_bounded():
    from orch.dashboard import app
    assert app.BRIDGE_STOP_S >= 5 + 10 + 35 + 2
    assert "wait_for(bridge.stop(), BRIDGE_STOP_S)" in open(app.__file__).read()
