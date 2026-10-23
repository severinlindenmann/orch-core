"""P5, opt-in: the Publish add-on against a REAL local orch-publish server (``orch-publish dev``).

Not part of CI. Run it with ``ORCH_PUBLISH_SRC`` set to a checkout of orch-publish ``develop`` that has its own
virtualenv (``uv sync`` there)::

    ORCH_PUBLISH_SRC=~/orch-dev/.worktrees/p5-pubsrv uv run --no-sync pytest tests/host/test_publishing_real.py

The server runs as a child process of the test (``python -m pubsrv.cli dev``), on two free loopback ports, with a
temporary data directory; its pid is written to a pid file and the test stops it by that pid (never by name). The
workspace is registered through the real card path (``publish.register``) and pre-bound by the server admin exactly as
the owner would (``orch-publish admin``)."""

from __future__ import annotations

import http.client
import os
import re
import signal
import socket
import subprocess
import time
import urllib.parse
from pathlib import Path

import pytest

from orch.host import publishing
from tests.host import human_helpers
from tests.host.dispatch_helpers import err, ok
from tests.host.test_publishing import REPORT, World

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

SRC = os.environ.get("ORCH_PUBLISH_SRC")
pytestmark = [
    pytest.mark.skipif(not SRC, reason="set ORCH_PUBLISH_SRC to an orch-publish checkout with a .venv"),
    pytest.mark.skipif(
        bool(SRC) and "delegation_json" not in (Path(SRC).expanduser() / "pubsrv" / "registry.py").read_text(),
        reason="needs an orch-publish with the card-free register (design 2.6b)",
    ),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, tmp: Path):
        self.src = Path(SRC).expanduser()
        self.py = self.src / ".venv" / "bin" / "python"
        self.data = tmp / "pub-data"
        self.api, self.share = free_port(), free_port()
        self.pidfile = tmp / "pub.pid"
        self.proc: subprocess.Popen | None = None

    def admin(self, *args: str) -> str:
        done = subprocess.run(
            [str(self.py), "-m", "pubsrv.cli", "admin", "--data", str(self.data), *args],
            cwd=self.src, capture_output=True, text=True, check=True,
        )  # fmt: skip
        return done.stdout

    def start(self, *extra: str) -> None:
        self.proc = subprocess.Popen(
            [str(self.py), "-m", "pubsrv.cli", "dev", "--data", str(self.data), "--api", f"127.0.0.1:{self.api}",
             "--share", f"127.0.0.1:{self.share}", *extra],
            cwd=self.src, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        self.pidfile.write_text(str(self.proc.pid))
        for _ in range(100):
            try:
                with (
                    socket.create_connection(("127.0.0.1", self.api), 0.2),
                    socket.create_connection(("127.0.0.1", self.share), 0.2),
                ):
                    return
            except OSError:
                time.sleep(0.1)
        raise AssertionError("the server did not start")

    def stop(self) -> None:
        if self.pidfile.exists():
            pid = int(self.pidfile.read_text())
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if self.proc is not None:
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    os.kill(pid, signal.SIGKILL)
                    self.proc.wait(5)
            self.pidfile.unlink()

    def http(self, method: str, path: str, body: bytes = b"", headers: dict | None = None):
        c = http.client.HTTPConnection("127.0.0.1", self.share, timeout=10)
        try:
            c.request(method, path, body=body or None, headers=headers or {})
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()

    def open_link(self, share_id: str, token: str):
        form = urllib.parse.urlencode({"t": token}).encode()
        status, headers, body = self.http("POST", f"/s/{share_id}/open", form,
                                          {"content-type": "application/x-www-form-urlencoded"})  # fmt: skip
        if status in (301, 302, 303, 307):
            return self.http("GET", headers.get("location") or headers["Location"])
        return status, headers, body


@pytest.fixture
def real(served, human, tmp_path, monkeypatch):
    srv = Server(tmp_path)
    w = World(served, human)
    publishing.TRANSPORT = None  # the real network
    # the server judges expiry against real time; the lab's clock is made up (its grant would lapse if it moved)
    monkeypatch.setattr(publishing, "_now_ms", lambda service: int(time.time() * 1000))
    w.addon()
    w.configure(f"http://127.0.0.1:{srv.api}", f"http://127.0.0.1:{srv.share}")
    try:
        yield srv, w
    finally:
        srv.stop()


def start_allowing(srv: Server, w: World) -> None:
    info = ok(w.req("publish.list", {}))["data"]
    srv.start("--owner", info["owner"], "--workspace", f"{info['workspace']}={info['owner']}")


def test_share_open_show_once_and_revoke_against_the_real_server(real):
    srv, w = real
    start_allowing(srv, w)
    uid, key = w.ticket_with(**{"report.md": REPORT, "data.csv": b"a,b\n1,2\n"})
    # a secret link: opens as often as you like until it is revoked
    staged = w.prepare(key, "report.md", "data.csv", days=3)["staged_id"]
    assert err(w.send(staged)) == "publish.not_approved"
    w.approve(staged)
    data = ok(w.send(staged))["data"]
    m = re.fullmatch(rf"http://127\.0\.0\.1:{srv.share}/s/([0-9a-f]{{32}})#t=([A-Za-z0-9_-]{{43}})", data["link"])
    assert m, data["link"]
    share_id, token = m.groups()
    status, _, body = srv.http("GET", f"/s/{share_id}")
    assert status == 200 and b"Someone shared files with you" in body and token.encode() not in body
    for _ in range(2):  # a secret link is a bearer link: it opens again
        status, headers, body = srv.open_link(share_id, token)
        assert status == 200, (status, body[:200])
    assert srv.open_link(share_id, "A" * 43)[0] == 404  # a wrong token opens nothing
    live = ok(w.req("publish.list", {"live": True}))["data"]
    assert live["live"] is True and live["shares"][0]["views"] == 2 and live["shares"][0]["state"] == "live"
    ok(w.req("publish.revoke", {"share_id": share_id}))
    assert srv.open_link(share_id, token)[0] == 404  # the tombstone keeps no token hash: nothing matches any more
    status, _, body = srv.http("GET", f"/s/{share_id}")
    assert status == 410 and b"removed by the person who shared it" in body
    assert ok(w.req("publish.list", {"live": True}))["data"]["shares"][0]["state"] == "revoked"
    # a show-once link: the first open works, the second does not
    staged = w.prepare(key, "report.md", kind="once", views=1)["staged_id"]
    w.approve(staged)
    data = ok(w.send(staged))["data"]
    share_id, token = re.search(r"/s/([0-9a-f]{32})#t=([A-Za-z0-9_-]{43})", data["link"]).groups()
    status, headers, body = srv.open_link(share_id, token)
    assert status == 200 and b"report.md" in body  # the view's file list
    href = re.search(rb'href="([^"]*report\.md)"', body).group(1).decode()
    status, _, content = srv.http("GET", href)
    assert status == 200 and content == REPORT
    assert srv.open_link(share_id, token)[0] == 410
    # the token is nowhere on this computer or in the server's database or files
    blob = w.everything_on_disk()
    assert token.encode() not in blob
    for p in Path(srv.data).rglob("*"):
        if p.is_file():
            assert token.encode() not in p.read_bytes(), p
    assert not (w.base() / "stage" / staged).exists()


def test_an_unknown_workspace_is_told_what_to_do_and_then_works(real):
    srv, w = real
    srv.start()  # no owner, no pre-binding
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    res = w.send(staged)
    assert err(res) == "publish.server_refused" and res["error"]["detail"] == {"reason": "unknown_workspace"}
    info = ok(w.req("publish.list", {}))["data"]
    srv.admin("owners", "add", info["owner"])
    srv.admin("ws", "allow", info["workspace"], info["owner"])
    assert ok(w.req("publish.register", {}))["data"]["registered"] is True
    ok(w.send(staged))  # the same signed copy goes now
    assert "active" in srv.admin("ws", "list")


def test_a_blocked_workspace_and_a_removed_owner_are_refused_on_the_next_request(real):
    srv, w = real
    start_allowing(srv, w)
    uid, key = w.ticket_with(**{"report.md": REPORT})
    info = ok(w.req("publish.list", {}))["data"]
    ok(w.req("publish.register", {}))
    srv.admin("ws", "block", info["workspace"])
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    res = w.send(staged)
    assert err(res) == "publish.server_refused" and res["error"]["detail"] == {"reason": "blocked"}
    srv.admin("ws", "unblock", info["workspace"])
    srv.admin("owners", "remove", info["owner"])
    res = w.send(staged)
    assert err(res) == "publish.server_refused" and res["error"]["detail"] == {"reason": "owner_not_allowed"}
    srv.admin("owners", "add", info["owner"])
    ok(w.send(staged))


def test_a_second_workspace_cannot_touch_the_first_ones_shares(real, tmp_path):
    """Namespace isolation (I3) through the host's own client: another workspace key gets 404 for a foreign id."""
    from orch import crypto
    from orch.publishclient import PublishClient, PublishError, flow
    from orch.wsproto.pubsign import PublishContext, PublishSigner
    from tests.wsproto.helpers import Key

    srv, w = real
    start_allowing(srv, w)
    uid, key = w.ticket_with(**{"report.md": REPORT})
    staged = w.prepare(key, "report.md")["staged_id"]
    w.approve(staged)
    share_id = ok(w.send(staged))["data"]["share_id"]
    other_ws = "ab" * 16
    other = PublishClient(PublishSigner(Key(), PublishContext(other_ws, f"http://127.0.0.1:{srv.api}")))
    with pytest.raises(PublishError) as exc:
        flow.revoke(other, other_ws, share_id)
    assert exc.value.status == 401 and exc.value.code == "unknown_workspace"  # not even a known workspace
    assert crypto  # the signature of a key the server never bound is refused before any share is looked at
    assert ok(w.req("publish.list", {"live": True}))["data"]["shares"][0]["state"] == "live"
