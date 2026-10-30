"""R4 T3 (#1592): remote access end to end through a REAL hub (in-process from packages/hub/src: real ``LinkServer``,
``StreamRouter``, ``RemoteEndpoint``, ``Authz``, ``Channels``) and the REAL R3 host session behind a real TLS
``LinkClient`` with the pump running. The device side is the R3 test client (``remote_port``, the independent
reading of the contract) carried over ALPN ``orch-remote/1`` with the R4 client protocol. Nothing of the hub is
stubbed except the dashboard, which is played through ``remote.attach`` like the R3 ops tests do."""

from __future__ import annotations

import base64
import logging
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orch.canon.jcs import dumps, loads_strict
from orch.crypto.encoding import b64u, unb64u
from orch.host import hubops, peerops
from orch.host.remote import pump
from orch.hubclient.errors import HubRefused
from tests.host import human_helpers
from tests.host.dispatch_helpers import ok
from tests.host.remote_world import Client, World, frame, head
from tests.host.test_remote_ops import call

HUB_SRC = Path(__file__).resolve().parents[3] / "hub" / "src"
pytestmark = pytest.mark.skipif(not (HUB_SRC / "orch_hub" / "remote").is_dir(), reason="packages/hub not present")

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

PW = "correct horse battery"
MARKER = "zq-marker-7f3a91c2"


class _Invites:
    def check(self, invite, now_ms):
        return None

    def consume(self, invite, workspace_id):
        pass


@pytest.fixture(autouse=True)
def dev_workspace(monkeypatch):
    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)


@pytest.fixture
def hub(served, monkeypatch):
    """A real hub in this process with two users: ``ann`` (gets a role in the workspace, so it is in her rail) and
    ``bob`` (nothing in his rail)."""
    monkeypatch.syspath_prepend(str(HUB_SRC))
    from orch_hub.access import Access
    from orch_hub.audit import open_log
    from orch_hub.channels import Channels
    from orch_hub.channels.service import totp_code
    from orch_hub.keys import HubKeys
    from orch_hub.link import LinkServer, Registry
    from orch_hub.link import tls as T
    from orch_hub.link.wkseal import WkSeals
    from orch_hub.remote import Authz, RemoteEndpoint, StreamRouter

    now = lambda: int(served.lab.ws.clock[0] * 1000)  # noqa: E731 - the lab's clock: the card must be valid then
    with tempfile.TemporaryDirectory(prefix="hs") as d:
        state = Path(d) / "hub"
        keys = HubKeys.init(state, now_ms=now)
        access = Access(str(state / "access.db"), now_ms=now)
        audit = open_log(state, keys, now_ms=now, dev_mode=True)
        channels = Channels(keys, access, state / "channels.db", now_ms=now, audit=audit, dev_mode=True)
        users, secrets = {}, {}
        for name in ("ann", "bob"):
            u = keys.issue_uk(None)["o"]["user_id"]
            access.add_user(u, name)
            channels.set_password(u, PW)
            s = channels.new_totp(u)["secret"]
            secrets[u] = base64.b32decode(s + "=" * (-len(s) % 8))
            users[name] = u
        reg = Registry()
        seals = WkSeals(":memory:", hub_id=keys.hub_id, hxk_pub=keys.hxk_pub, exchange=keys.hxk_exchange, now_ms=now)
        authz = Authz.from_hub(channels, reg, access)
        router = StreamRouter(hub_id=keys.hub_id, allowed=authz.may_open, count_op=reg.count_op).start()
        endpoint = RemoteEndpoint(router, authz)
        srv = LinkServer.from_keys(
            keys, identity=T.TlsIdentity.generate(), registry=reg, invites=_Invites(), dev_mode=True, now_ms=now,
            wk_seals=seals, streams=router, remote=endpoint,
        ).start()  # fmt: skip

        tokens: dict[str, str] = {}

        def token(name: str) -> str:  # one sign-in per user: a TOTP code is single use
            if name not in tokens:
                u = users[name]
                tokens[name] = channels.sign_in(u, PW, totp_code(secrets[u], now() // 30_000), scope="remote")["token"]
            return tokens[name]

        card = keys.card()
        rig = SimpleNamespace(
            card=card,
            addr=f"127.0.0.1:{srv.address[1]}",
            port=srv.address[1],
            spki=card["o"]["tls_spki_sha256"],
            registry=reg,
            access=access,
            router=router,
            users=users,
            token=token,
            state=state,
            hub_id=keys.hub_id,
            T=T,
            server=srv,
        )
        try:
            yield rig
        finally:
            router.close_all()
            srv.close()
            channels.close()
            access.close()
            audit.store.close()


class Dashboard:
    """The dashboard's side of the R3 adapter, through the ``remote.attach`` op."""

    def __init__(self, served, w: World) -> None:
        self.served, self.w = served, w
        self.c = served.client()  # one connection: the host caps them

    def attach(self, send: list[bytes] | None = None, sid: str | None = None) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"attach_id": self.w.ad.attach_id}
        if send:
            args["send"] = [{"stream_id": sid, "bytes": b64u(b)} for b in send]
        return ok(self.c.req("remote.attach", args, idem=True))["data"]["frames"]

    def frames(self, n: int, timeout: float = 8.0) -> list[dict[str, Any]]:
        got: list[dict[str, Any]] = []
        end = time.time() + timeout
        while len([m for m in got if m["t"] == "frame"]) < n and time.time() < end:
            got += self.attach()
            time.sleep(0.05)
        return got


class HubClient(Client):
    """The device side over the R4 client protocol (ALPN ``orch-remote/1``, one stream per connection)."""

    def __init__(self, w: World, rig: Any, token: str, **kw: Any) -> None:
        super().__init__(w, "0" * 32, **kw)
        self.rig, self.token = rig, token
        self.conn: Any = None
        self.hub_id: str = rig.hub_id
        self.closed: str | None = None
        self.wire: list[str] = []  # every b64u data string that crossed the client's connection

    def _framing(self):
        from orch_hub.link import framing

        return framing

    def _put(self, obj: dict[str, Any]) -> None:
        self._framing().write_frame(self.conn, obj, timeout=5, max_bytes=1 << 17)

    def _get(self, timeout: float = 10.0) -> dict[str, Any]:
        return self._framing().read_frame(self.conn, first_timeout=timeout, rest_timeout=5, max_bytes=1 << 17)

    def open_stream(self, kind: str = "session") -> str | None:
        """Dial, hello, open. ``None``: opened (``self.sid`` is the hub's id). Else the refusal code."""
        self.conn = self.rig.T.connect_pinned("127.0.0.1", self.rig.port, self.rig.spki, alpn=(self.rig.T.REMOTE_ALPN,))
        self._put({"t": "hello", "v": 1, "token": self.token})
        r = self._get()
        if r["t"] != "ready":
            return r["error"]
        self.hub_id = r["hub_id"]
        self._put({"t": "open", "workspace_id": self.w.ws, "device_id": self.did, "kind": kind})
        r = self._get()
        if r["t"] != "opened":
            return r["error"]
        self.sid = r["stream_id"]
        return None

    def connect(self) -> dict[str, Any]:
        code = self.open_stream()
        assert code is None, f"open refused: {code}"
        self.handshake()
        self.say(dumps(self.open_msg()))
        (reply,) = self.hear()
        return loads_strict(reply)

    def msg1(self, **kw: Any) -> None:
        kw.setdefault("hub", self.hub_id)
        super().msg1(**kw)

    def send(self, raw: bytes) -> None:
        self.wire.append(b64u(raw))
        self._put({"t": "data", "b": b64u(raw)})

    def inbox(self, timeout: float = 10.0) -> list[bytes]:
        """The next data frame (a ``close`` frame sets ``self.closed`` and returns nothing)."""
        f = self._get(timeout)
        if f["t"] == "close":
            self.closed = f["reason"]
            return []
        assert f["t"] == "data", f
        self.wire.append(f["b"])
        return [unb64u(f["b"])]

    def wait_closed(self, timeout: float = 10.0) -> str:
        end = time.time() + timeout
        while self.closed is None and time.time() < end:
            f = self._get(max(0.1, end - time.time()))
            if f["t"] == "close":
                self.closed = f["reason"]
            else:
                self.wire.append(f.get("b", ""))
        assert self.closed is not None, "no close frame"
        return self.closed

    def request(
        self, dash: Dashboard, path: str = "/api/w/x/runs", ident: int = 1, headers: list | None = None
    ) -> None:
        self.say(frame("HEAD", ident, dumps({"headers": headers or [], "method": "GET", "path": path, "query": ""})))
        self.say(frame("END", ident))

    def hang_up(self) -> None:
        if self.conn is not None:
            self.conn.close()


class Pump:
    """The pump on the real shared hub link, like the host runs it."""

    def __init__(self, service) -> None:
        self.service = service
        self.stop = threading.Event()
        self.t: threading.Thread | None = None

    def start(self) -> Pump:
        self.stop = threading.Event()
        self.t = threading.Thread(target=pump.run, args=(self.service, self.link, self.stop, 0.05), daemon=True)
        self.t.start()
        return self

    def link(self):
        return peerops.shared_link(self.service)

    def halt(self) -> None:
        self.stop.set()
        if self.t is not None:
            self.t.join(5)


@pytest.fixture
def world(human, served, hub):
    """The served workspace joined (full mode) and connected to the real hub, remote access on, one enrolled device,
    the pump running on the real link. A group grant puts the workspace in ``ann``'s rail."""
    human.run("hub.join", {"hub_card": hub.card, "mode": "full"})  # first: a full join turns remote access off (D93)
    args = {"invite": "inv", "hub_card": hub.card, "addr": hub.addr}
    assert ok(served.client().req("hub.connect", args, idem=True))["data"]["status"] == "bound"
    w = World(human, served)  # then the owner turns it on and enrolls the device
    hub.access.add_group("team")  # ann holds a role in the workspace: it is in her rail (bob has none)
    hub.access.add_member("team", hub.users["ann"])
    hub.access.grant(w.ws, "team", "edit")
    w.pump_thread = Pump(w.service).start()
    w.dash = Dashboard(served, w)
    w.client_ = lambda token=None: HubClient(w, hub, token or hub.token("ann"))
    try:
        yield w
    finally:
        w.pump_thread.halt()
        with peerops._MU:  # noqa: SLF001
            rt = peerops._RUNTIMES.get(peerops._key(w.service))  # noqa: SLF001
        if rt is not None:
            peerops._retire(w.service, rt)  # noqa: SLF001


def hub_client(world, **kw) -> HubClient:
    return world.client_(**kw)


# --- the happy path -------------------------------------------------------------------------------


def test_e2e_session_opens_through_hub(world, hub):
    c = hub_client(world)
    assert c.connect() == {"t": "opened", "api": 1}
    assert len(c.sid) == 32 and c.hub_id == hub.hub_id
    c.request(world.dash)
    got = world.dash.frames(2)  # the open notice, then head and end
    assert got[0]["t"] == "open" and got[0]["stream_id"] == c.sid and got[0]["device_id"] == world.did
    answer = [frame("HEAD", 1, dumps({"headers": [], "status": 200})), frame("BODY", 1, b"{}"), frame("END", 1)]
    world.dash.attach(answer, c.sid)
    heard: list[bytes] = []
    while len(heard) < 3:
        heard += c.hear()
    assert heard == answer
    assert [e["type"] for e in world.log("remote.session_opened")] == ["remote.session_opened"]
    c.hang_up()


# --- the hub cannot break the session undetected --------------------------------------------------


def test_e2e_flipped_byte_client_to_host_closes_session(world):
    c = hub_client(world)
    c.connect()
    world.dash.attach()
    ct = bytearray(c.ini.c_i2r.seal(b"", head(1)))
    ct[len(ct) // 2] ^= 1
    c.send(bytes(ct))
    reason = c.wait_closed()
    assert reason == "handshake"
    deadline = time.time() + 5
    while world.table().is_open(c.sid) and time.time() < deadline:
        time.sleep(0.05)
    assert not world.table().is_open(c.sid)
    deadline = time.time() + 5
    while not world.log("remote.session_closed") and time.time() < deadline:
        time.sleep(0.05)
    (line,) = world.log("remote.session_closed")
    assert line["stream_id"] == c.sid and line["reason"] == "handshake"
    assert [m for m in world.dash.attach() if m["t"] == "frame"] == []  # nothing was forwarded to the dashboard


def test_e2e_flipped_byte_by_hub_client_to_host_closes_session(world, hub, monkeypatch):
    c = hub_client(world)
    c.connect()
    orig = hub.router.send

    def flip(stream_id, data):
        b = bytearray(data)
        b[len(b) // 2] ^= 1
        return orig(stream_id, bytes(b))

    monkeypatch.setattr(hub.router, "send", flip)
    c.say(head(1))
    assert c.wait_closed() == "handshake"
    assert not world.table().is_open(c.sid)


def test_e2e_flipped_byte_by_hub_host_to_client_is_refused_by_the_device(world, hub, monkeypatch):
    c = hub_client(world)
    c.connect()
    orig = hub.router.on_frame

    def flip(ws, port, o, cost):
        if o["op"] == "stream_data":
            raw = bytearray(unb64u(o["body"]["bytes"]))
            raw[-1] ^= 1
            o = {**o, "body": {**o["body"], "bytes": b64u(bytes(raw))}}
        return orig(ws, port, o, cost)

    monkeypatch.setattr(hub.router, "on_frame", flip)
    c.say(frame("PING", 0))
    with pytest.raises(HubRefused):  # the PONG's tag does not verify: the device ends the session
        c.hear()


# --- limits, opt-out, authorization ---------------------------------------------------------------


def test_e2e_limits_enforced_end_to_end(world):
    clients = [hub_client(world) for _ in range(5)]
    for c in clients[:4]:
        assert c.open_stream() is None
    assert clients[4].open_stream() == "remote.busy"  # 4 streams per device
    assert world.table().count_device(world.did) == 4
    clients[0].hang_up()  # the hub notices, tells the host, the slot is free again
    deadline = time.time() + 8
    while world.table().count_device(world.did) > 3 and time.time() < deadline:
        time.sleep(0.05)
    again = hub_client(world)
    assert again.open_stream() is None
    for c in clients[1:4] + [again]:
        c.hang_up()


def test_e2e_oversize_client_frame_closes_the_stream(world):
    c = hub_client(world)
    c.connect()
    c._put({"t": "data", "b": b64u(b"x" * 65_536)})  # noqa: SLF001
    assert c.wait_closed() == "too_large"
    deadline = time.time() + 5
    while world.table().is_open(c.sid) and time.time() < deadline:
        time.sleep(0.05)
    assert not world.table().is_open(c.sid)


def test_e2e_opted_out_workspace_remote_off_reaches_the_client(world):
    assert call(world.served, "remote.off")["ok"]
    c = hub_client(world)
    assert c.open_stream() == "remote.off"
    assert world.table().count_workspace() == 0


def test_e2e_another_users_workspace_is_not_allowed(world, hub):
    c = hub_client(world, token=hub.token("bob"))
    assert c.open_stream() == "not_allowed"
    assert world.table().count_workspace() == 0


def test_e2e_a_browser_scope_token_is_refused_at_the_endpoint(world, hub):
    c = hub_client(world, token="not-a-token")
    assert c.open_stream() == "no_session"


# --- what the hub keeps ---------------------------------------------------------------------------


def test_e2e_hub_logs_no_plaintext_marker(world, hub, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    token = hub.token("ann")
    c = hub_client(world, token=token)
    c.connect()
    c.request(world.dash, headers=[["x-note", MARKER]])
    world.dash.frames(2)
    answer = [frame("HEAD", 1, dumps({"headers": [["x-reply", MARKER]], "status": 200})), frame("END", 1)]
    world.dash.attach(answer, c.sid)
    heard: list[bytes] = []
    while len(heard) < 2:
        heard += c.hear()
    c.hang_up()
    time.sleep(0.5)  # the hub logs the close
    out, err = capfd.readouterr()
    files = b"".join(p.read_bytes() for p in hub.state.rglob("*") if p.is_file())
    text = (caplog.text + out + err).encode() + files
    assert b"stream_open" in text and c.sid.encode() in text  # the grep looks at something real
    needles = [MARKER, b64u(MARKER.encode()), token, world.did, *c.wire]
    for n in needles:
        assert n.encode() not in text, n[:12]
    for raw in (MARKER.encode(), unb64u(c.wire[0])):
        assert raw not in text


# --- F3 / F4 against the real link ----------------------------------------------------------------


def test_e2e_stream_burst_during_a_call_keeps_the_link(world, hub):
    c = hub_client(world)
    assert c.open_stream() is None
    world.pump_thread.halt()  # the host is busy elsewhere: nobody drains the stream frames
    link = peerops.shared_link(world.service)
    for _ in range(100):
        c.send(b"x" * 40)
    time.sleep(1.0)  # the hub has forwarded them
    try:
        link.call("snapshot_get", {})  # the frames are read inside this call; 64 fit, the rest overflow
    except Exception as e:  # noqa: BLE001 - the hub may answer with an error; the link must stay open either way
        assert type(e).__name__ == "LinkError"
    assert link.is_open
    assert peerops.shared_link(world.service, dial=False) is link  # the same link, not a redial
    assert link.take_stream_overflow() == [c.sid]  # frames were lost to the full queue: this stream is the casualty
    link._overflow.append(c.sid)  # noqa: SLF001 - hand it back: the pump below is what closes it
    world.pump_thread.start()
    assert c.wait_closed() == "remote.busy"  # that stream ends; the link and the host live
    assert link.is_open
    deadline = time.time() + 10
    while link._streams and time.time() < deadline:  # noqa: SLF001 - the stale frames of the ended stream drain
        time.sleep(0.05)
    other = hub_client(world)
    assert other.connect() == {"t": "opened", "api": 1}  # and a new session works on the same link
    assert peerops.shared_link(world.service, dial=False) is link


def test_e2e_link_change_frees_host_streams(world, hub):
    clients = [hub_client(world) for _ in range(3)]
    for c in clients:
        assert c.open_stream() is None
    assert world.table().count_device(world.did) == 3
    old = peerops.shared_link(world.service, dial=False)
    old.close()  # the link dies; the host redials, the pump sees a new link
    deadline = time.time() + 10
    while world.table().count_workspace() and time.time() < deadline:
        time.sleep(0.05)
    assert world.table().count_workspace() == 0
    for c in clients:
        assert c.wait_closed() == "closed"  # the hub told every client when its side of the link went
    fresh = [hub_client(world) for _ in range(4)]
    deadline = time.time() + 10
    results: list[str | None] = []
    while time.time() < deadline:
        results = [f.open_stream() for f in fresh]
        if all(r is None for r in results):
            break
        for f in fresh:
            f.hang_up()
        time.sleep(0.3)
    assert results == [None] * 4
