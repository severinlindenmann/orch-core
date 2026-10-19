"""B1: ``host.relay_link`` / ``orch host relay link`` (11A.5 step 1, 11A.15.11 step 7): relay-info first, the
reservation, the genesis-pin step, then the start; ``host.relay_status``; the status says the pin is needed."""

from __future__ import annotations

import errno
import io
from types import SimpleNamespace

import pytest

from orch.host import paircli
from orch.host.relaylink import link_path
from tests.host.lifecycle_helpers import Lab
from tests.host.test_b1_genesis_pin import Backend, _call
from tests.relay.mock import MockRelay, json_reply

MIB4 = 4 << 20


@pytest.fixture
def lab(tmp_path):
    value = Lab(tmp_path)
    try:
        yield value
    except OSError as exc:  # pragma: no cover
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    finally:
        value.close()


@pytest.fixture
def relay():
    m = MockRelay()
    m.fallback = lambda s: json_reply(200, {"v": 1, "reader": 1}) if s.path == "/v2/relay-info" else json_reply(404, {})
    try:
        yield m
    finally:
        m.close()


def _register(lab, c):
    return lab.register(c)["result"]["data"]["workspace_id"]


def test_link_reads_relay_info_reserves_and_starts_nothing(lab, relay):
    with lab.host() as h, h.client() as c:
        wid = _register(lab, c)
        call = _call(c)
        data = call("host.relay_link", {"workspace_id": wid, "origin": relay.origin(), "max_publication_bytes": MIB4})
        assert data["state"] == "linking" and data["origin"] == relay.origin()
        assert [s.path for s in relay.seen] == ["/v2/relay-info"]  # the reader level first
        assert link_path(lab.state, wid).is_file()
        st = call("host.relay_status", {"workspace_id": wid})["relay"]
        assert st["link"] == "linking" and st["worker"] == "off"
        assert st["genesis_pin"] == "needed" and st["attention"][0]["actionable"] is True
        res = c.call("host.relay_link", {"workspace_id": wid, "origin": relay.origin(), "max_publication_bytes": MIB4})
        assert res["ok"] is False and res["error"]["code"] == "invalid.input"
        assert "relay.link_exists" in res["error"]["message"]


def test_an_unreachable_relay_links_nothing(lab):
    with lab.host() as h, h.client() as c:
        wid = _register(lab, c)
        res = c.call("host.relay_link", {"workspace_id": wid, "origin": "http://127.0.0.1:1",
                                         "max_publication_bytes": MIB4})  # fmt: skip
        assert res["ok"] is False and res["error"]["code"] == "invalid.input"
        assert not link_path(lab.state, wid).exists()
        bad = c.call("host.relay_link", {"workspace_id": wid, "origin": "ftp://x", "max_publication_bytes": MIB4})
        assert bad["ok"] is False
        small = c.call(
            "host.relay_link", {"workspace_id": wid, "origin": "http://127.0.0.1:1", "max_publication_bytes": 5}
        )
        assert small["ok"] is False


def test_the_cli_link_reserves_asks_for_the_pin_then_starts_and_reports_where_it_stopped(lab, relay, monkeypatch):
    from orch.relay import worker as _w6

    monkeypatch.setattr(_w6, "R6_READY", False)  # the closed gate is what this test is about
    with lab.host() as h, h.client() as c:
        wid = _register(lab, c)
        call = _call(c)
        backend = Backend(lab.ws.owner)
        asked: list[tuple[str, str]] = []

        def pin_run(call_, entry, state):
            return paircli.sign_genesis_pin(
                call_, wid, backend=backend, key_id="dk", name="demo", confirm=lambda t, w: asked.append((t, w)) or True
            )

        monkeypatch.setattr(paircli, "_genesis_pin_run", pin_run)
        args = SimpleNamespace(relay=relay.origin(), max_publication_bytes=MIB4, ca_file=None, proxy=None)
        out = io.StringIO()
        entry = {"workspace_id": wid}
        data = paircli._link_run(call, entry, lab.state, args, out, lambda s: None)  # noqa: SLF001
        assert "permanently relay-linked" in out.getvalue()
        assert asked and asked[0][1] == f"{wid[-8:]}/GENESIS"
        # the R6 gate is closed in this test: nothing starts, and the CLI says so instead of hanging
        assert data["link"] == "linking" and data["worker"] == "off" and data["start"] == "none"
        assert call("host.relay_status", {"workspace_id": wid})["relay"]["genesis_pin"] == "signed"
        with pytest.raises(paircli.HostError):
            paircli._link_run(call, entry, lab.state, SimpleNamespace(relay=None, max_publication_bytes=None,  # noqa: SLF001
                          ca_file=None, proxy=None), out, lambda s: None)  # fmt: skip


def test_the_tty_ask_works_on_a_real_terminal(tmp_path):
    """Found by the real dev-pair run: ``open('/dev/tty', 'r+')`` raises UnsupportedOperation on macOS, so the SAS
    prompt of ``orch host relay pair`` could never be answered. A child with a pseudo-terminal as its controlling
    terminal asks and gets the typed line."""
    import os
    import select
    import subprocess
    import sys
    import time

    master, slave = os.openpty()
    code = (
        "import fcntl, os, sys, termios\n"
        "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
        "from orch.host import paircli\n"
        "print('GOT:' + paircli._tty_ask('code: '), flush=True)\n"  # noqa: SLF001
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=slave, stdout=slave, stderr=slave,
                            start_new_session=True)  # fmt: skip
    os.close(slave)
    out = b""
    sent = False
    end = time.monotonic() + 30
    try:
        while time.monotonic() < end and b"GOT:" not in out:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    out += os.read(master, 4096)
                except OSError:
                    break
            if not sent and b"code: " in out:
                os.write(master, b"ABC234\n")
                sent = True
        assert b"GOT:ABC234" in out, out
    finally:
        proc.kill()
        proc.wait()
        os.close(master)
