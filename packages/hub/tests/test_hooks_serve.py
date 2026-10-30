"""#1285: `serve` wires the webhook inbox only when both flags are given, and the admin web API is not the listener."""

import http.client
import io
import re
import threading
import time

import pytest
from publish_helpers import FakeUpstream

from orch_hub import serve as SV
from orch_hub.keys import HubKeys

HOOKS = re.compile(r"orch-hub hooks port=(\d+)\n")
WEB = re.compile(r"orch-hub web port=(\d+)\n")


def start(tmp_path, **kw):
    state = tmp_path / "hub"
    HubKeys.init(state, relay_url="http://127.0.0.1:9", real=False)
    up = FakeUpstream()
    stop, out, result = threading.Event(), io.StringIO(), []
    t = threading.Thread(
        target=lambda: result.append(
            SV.serve(state, "127.0.0.1:0", up.url, [], web_listen="127.0.0.1:0", stop=stop, out=out, **kw)
        ),
        daemon=True,
    )
    t.start()
    return state, out, stop, t, result, up


def test_one_flag_alone_is_refused(tmp_path):
    for kw in ({"hooks_listen": "127.0.0.1:0"}, {"public_hooks_url": "http://127.0.0.1:9"}):
        state = tmp_path / ("s" + next(iter(kw)))
        HubKeys.init(state, relay_url="http://127.0.0.1:9", real=False)
        up = FakeUpstream()
        with pytest.raises(ValueError, match="go together"):
            SV.serve(state, "127.0.0.1:0", up.url, [], **kw, stop=threading.Event(), out=io.StringIO())
        up.close()


def test_off_by_default_and_on_with_both(tmp_path):
    _state, out, stop, t, result, up = start(tmp_path)
    end = time.monotonic() + 10
    while not WEB.search(out.getvalue()) and time.monotonic() < end:
        time.sleep(0.05)
    assert WEB.search(out.getvalue()) and not HOOKS.search(out.getvalue())
    stop.set()
    t.join(15)
    up.close()
    assert result == [0]

    _state, out, stop, t, result, up = start(
        tmp_path / "on", hooks_listen="127.0.0.1:0", public_hooks_url="http://127.0.0.1:9"
    )
    end = time.monotonic() + 10
    while not HOOKS.search(out.getvalue()) and time.monotonic() < end:
        time.sleep(0.05)
    port, web = int(HOOKS.search(out.getvalue())[1]), int(WEB.search(out.getvalue())[1])
    assert port != web
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/h/hh_" + "U" * 43, body=b"{}")
    assert c.getresponse().status == 404
    c.close()
    # the hook path does not exist on the admin API listener, and the admin path does not exist on the hook listener
    c = http.client.HTTPConnection("127.0.0.1", web, timeout=5)
    c.request("POST", "/h/hh_" + "U" * 43, body=b"{}", headers={"Content-Type": "application/json"})
    r = c.getresponse()
    assert r.status != 204
    c.close()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/api/v1/whoami")
    assert c.getresponse().status == 404
    c.close()
    stop.set()
    t.join(15)
    up.close()
    assert result == [0]
