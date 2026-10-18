"""T2 entry point (test support, runs INSIDE the relay's own environment): ``orch-relay serve`` with the APNs
transport pointed at the test's in-test fake gateway (``ORCH_T2_FAKE_APNS``, plain HTTP/1.1 on loopback). The relay
itself has no gateway setting (it must never be redirectable in production), so the test patches the one seam its own
suite patches. The file name carries ``orch-relay`` so the harness recognises its own process group."""

import os
import sys

import httpx
from orch_relay import cli, push_send
from orch_relay.apns import HttpxTransport

BASE = os.environ["ORCH_T2_FAKE_APNS"]
assert BASE.startswith("http://127.0.0.1:"), BASE


class FakeGateway(HttpxTransport):
    def client(self, env):
        c = self.clients.get(env)
        if c is None:
            c = self.clients[env] = httpx.AsyncClient(base_url=BASE, timeout=httpx.Timeout(10.0))
        return c


push_send.HttpxTransport = FakeGateway
sys.exit(cli.main(sys.argv[1:]))
