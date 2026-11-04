"""What the hub knows about its neighbours, for the console's Services page (run-11 feedback 12).

Read only. The hub cannot start or stop other services (relay and publish are separate processes, maybe on other
machines), so this offers a re-probe and says so; the console shows the commands to run. A probe is one short HTTP GET:
relay and publish answer ``/healthz`` with their version; an older publish without that route still counts as up on
any HTTP answer, with no version. Probes run in parallel, are bounded by ``timeout_s`` and never raise.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Callable
from typing import Any

from .. import __version__
from ..localdial import dialling_locally
from ..publish.forwarder import _endpoint

#: (base_url, path, timeout_s) -> (http status, parsed json object or None); raises OSError / ValueError when not reachable
Probe = Callable[[str, str, float], tuple[int, dict | None]]


def http_probe(base_url: str, path: str, timeout_s: float) -> tuple[int, dict | None]:
    scheme, host, port = _endpoint(base_url)
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    conn = dialling_locally(cls(host, port, timeout=timeout_s))
    try:
        conn.request("GET", path, headers={"Accept": "application/json"})
        r = conn.getresponse()
        data = r.read(64 * 1024)
    finally:
        conn.close()
    try:
        obj = json.loads(data)
    except ValueError:
        obj = None
    return r.status, obj if type(obj) is dict else None


class Services:
    def __init__(
        self,
        *,
        relay_url: str | None,
        publish_url: str | None,
        behind_edge: bool = False,
        edge_url: str | None = None,
        hub_url: str | None = None,
        now_ms: Callable[[], int] | None = None,
        probe: Probe = http_probe,
        timeout_s: float = 2.0,
    ) -> None:
        self._urls = {"relay": relay_url, "publish": publish_url}
        # Edge is probed only when the hub runs behind one and its public address is known; no address = configuration only.
        self._edge_state = "up" if behind_edge else "not_set_up"
        self._urls["edge"] = edge_url if behind_edge else None
        self._hub_url = hub_url
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._probe, self._timeout = probe, timeout_s
        self._started = self._now()
        self._last_ok: dict[str, int] = {}
        self._mu = threading.Lock()

    @property
    def publish_url(self) -> str | None:
        """Where orch-publish listens (None = not set up): the Published page reads its admin list there."""
        return self._urls["publish"]

    def _check(self, name: str, path: str) -> dict[str, Any]:
        url = self._urls[name]
        out: dict[str, Any] = {"name": name, "url": url, "version": None, "last_ok_ms": None}
        if not url:
            return {**out, "state": self._edge_state if name == "edge" else "not_set_up"}
        now = self._now()
        try:
            status, obj = self._probe(url, path, self._timeout)
        except OSError, ValueError:
            state = "down"
        else:
            # http.client never follows redirects, so a probe cannot be sent to another host. Publish and Edge count as
            # up on any answer that is not a server error (an older publish has no /healthz; Edge answers whatever the hub does).
            state = "up" if (status == 200 or (name in ("publish", "edge") and status < 500)) else "down"
            ver = (obj or {}).get("version")
            if state == "up" and type(ver) is str and len(ver) <= 40:
                out["version"] = ver
        with self._mu:
            if state == "up":
                self._last_ok[name] = now
            out["last_ok_ms"] = self._last_ok.get(name)
        return {**out, "state": state, "checked_ms": now}

    def snapshot(self) -> dict[str, Any]:
        """``{hub, relay, publish, edge}``; the two probes run at the same time."""
        res: dict[str, dict[str, Any]] = {}

        def run(name: str, path: str) -> None:
            res[name] = self._check(name, path)

        ts = [
            threading.Thread(target=run, args=("relay", "/healthz")),
            threading.Thread(target=run, args=("publish", "/healthz")),
            threading.Thread(target=run, args=("edge", "/")),
        ]
        for t in ts:
            t.start()
        for t in ts:
            t.join(self._timeout + 2)
        now = self._now()
        for name in ("relay", "publish", "edge"):  # a thread that did not finish in time counts as down
            res.setdefault(
                name, {"name": name, "url": self._urls[name], "version": None, "last_ok_ms": None, "state": "down"}
            )
        return {
            "now_ms": now,
            "hub": {"version": __version__, "started_ms": self._started, "state": "up", "url": self._hub_url},
            "relay": res["relay"],
            "publish": res["publish"],
            "edge": res["edge"],
        }
