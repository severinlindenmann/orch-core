"""C13 cross-repo: the core space client against orch-hub's real hub (a subprocess under the hub's own environment,
``ORCH_HUB_SRC``; skipped without it): put -> get byte for byte, refused authors, the share rule, revocation, a removed
member, no backfill for a later member, and a selective workspace's owner accept."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from orch.crypto import suite
from orch.crypto.encoding import unb64u
from orch.hubclient import wire
from orch.hubclient.errors import HubRefused
from orch.hubclient.spaceclient import SpaceClient, SpaceState
from orch.hubclient.tlslink.client import LinkClient

HUB_SRC = os.environ.get("ORCH_HUB_SRC", "")
pytestmark = pytest.mark.skipif(
    not (HUB_SRC and (Path(HUB_SRC) / "src" / "orch_hub" / "spaces").is_dir() and shutil.which("uv")),
    reason="ORCH_HUB_SRC not set to an orch-hub checkout with shared spaces",
)


class Hub:
    def __init__(self, a_mode: str, b_mode: str) -> None:
        self.p = subprocess.Popen(
            ["uv", "run", "--project", HUB_SRC, "python", str(Path(__file__).with_name("hub_space_proc.py")), HUB_SRC,
             a_mode, b_mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )  # fmt: skip
        line = self.p.stdout.readline()
        if not line:
            pytest.skip("hub server did not start")
        self.info = json.loads(line)

    def admin(self, **cmd) -> dict:
        self.p.stdin.write(json.dumps(cmd) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def close(self) -> None:
        self.p.stdin.close()
        try:
            self.p.wait(10)
        except subprocess.TimeoutExpired:
            self.p.kill()


class Host:
    def __init__(self, hub: Hub, who: str) -> None:
        d = hub.info[who]
        self.hub, self.ws, self.join = hub, d["workspace_id"], d["join"]
        self.wsk = suite.private_key_from_scalar(d["wsk_scalar"])
        self.wxk = suite.private_key_from_scalar(d["wxk_scalar"])
        self.pins: dict[str, bytes] = {self.ws: suite.public_bytes(self.wsk)}
        self._link: LinkClient | None = None
        held = wire.verify_card(hub.info["hub_card"], now_ms=int(time.time() * 1000), dev_mode=True)
        self.client = SpaceClient(
            me=self.ws, sign=self.sign, wxk_priv=self.wxk, wxk_version=1, hub_id=hub.info["hub_id"], dev_mode=True,
            held=lambda: held, revoked_op_certs=lambda: frozenset(), pins=lambda: self.pins, state=SpaceState(),
            save=lambda s: SpaceState.from_json(s.to_json()), link=self.link,
        )  # fmt: skip

    def sign(self, msg: bytes) -> bytes:
        return suite.sign(self.wsk, msg)

    def _open(self, first: bool = False) -> LinkClient:
        i = self.hub.info
        return LinkClient.open(
            "127.0.0.1", i["port"], tls_spki_sha256=i["spki"], hub_id=i["hub_id"], workspace_id=self.ws,
            sign=self.sign, hk_op_pub=unb64u(i["hk_op_pub"], 65), mode="full",
            wsk_pub=suite.public_bytes(self.wsk) if first else None, timeout=10,
        )  # fmt: skip

    def link(self) -> LinkClient:
        if self._link is None:
            self._link = self._open()
        return self._link

    def do_join(self) -> None:
        with self._open(first=True) as c:
            c.call("join", self.join)

    def pin(self, other: Host) -> None:
        self.pins[other.ws] = suite.public_bytes(other.wsk)

    def close(self) -> None:
        if self._link is not None:
            self._link.close()


@pytest.fixture
def world() -> Iterator[tuple[Hub, Host, Host, Host]]:
    hub = Hub("full", "full")
    a, b, c = Host(hub, "a"), Host(hub, "b"), Host(hub, "c")
    for h in (a, b, c):
        h.do_join()
    for x in (a, b, c):
        for y in (a, b, c):
            if x is not y:
                x.pin(y)
    try:
        yield hub, a, b, c
    finally:
        for h in (a, b, c):
            h.close()
        hub.close()


def members_of(host: Host, sid: str) -> list[str]:
    """What the owner is shown when they look at the space right now (the verified record)."""
    return sorted(next(x for x in host.client.list_spaces() if x["space_id"] == sid)["members"])


def make_space(hub: Hub, *members: Host) -> str:
    rec = hub.admin(cmd="create", members=[m.ws for m in members])
    assert "error" not in rec, rec
    return rec["space_id"]


def test_put_then_get_gives_the_same_bytes_and_a_non_member_is_refused(world) -> None:
    hub, a, b, c = world
    sid = make_space(hub, a, b)
    assert a.client.share(sid, sorted([a.ws, b.ws]), understood=True) == sorted([a.ws, b.ws])
    data = os.urandom(5000)
    put = a.client.put(sid, "report.bin", data, by="person:p_test")
    items, refused = b.client.read_items(sid)
    assert refused == 0 and [i["body"]["name"] for i in items] == ["report.bin"]
    assert (
        unb64u(items[0]["body"]["data"]) == data
        and items[0]["author"] == a.ws
        and items[0]["item_id"] == put["item_id"]
    )
    assert [s["space_id"] for s in b.client.list_spaces()] == [sid]
    with pytest.raises(HubRefused) as e:
        c.client.read_items(sid)  # Finance-like: not a member
    assert e.value.code == "not_member"
    assert c.client.list_spaces() == []  # it learns nothing about spaces it is not in
    with pytest.raises(HubRefused) as e:
        c.client.sync_current(sid)
    assert e.value.code == "not_member"


def test_an_author_the_reader_never_pinned_is_refused_not_delivered(world) -> None:
    hub, a, b, _ = world
    sid = make_space(hub, a, b)
    a.client.share(sid, members_of(a, sid), understood=True)
    a.client.put(sid, "x.txt", b"hello", by="person:p_test")
    del b.pins[a.ws]  # no pin for the author: the item cannot be verified
    items, refused = b.client.read_items(sid)
    assert items == [] and refused == 1


def test_put_needs_a_share_rule_and_a_grown_member_set_is_members_changed(world) -> None:
    hub, a, b, c = world
    sid = make_space(hub, a, b)
    with pytest.raises(HubRefused) as e:
        a.client.put(sid, "x", b"1", by="person:p_test")
    assert e.value.code == "not_shared"
    a.client.share(sid, members_of(a, sid), understood=True)
    a.client.put(sid, "ok", b"1", by="person:p_test")
    assert "error" not in hub.admin(cmd="add", space_id=sid, ws=c.ws)
    with pytest.raises(HubRefused) as e:
        a.client.put(
            sid, "late", b"2", by="person:p_test"
        )  # a member the owner never approved: refused locally, nothing sent
    assert e.value.code == "members_changed"
    a.client.share(sid, members_of(a, sid), understood=True)  # the owner re-approves the grown set
    a.client.put(sid, "late", b"2", by="person:p_test")
    assert [i["body"]["name"] for i in c.client.read_items(sid)[0]] == ["late"]  # c: no backfill of epoch 1


def test_revoke_and_a_removed_member_loses_later_epochs(world) -> None:
    hub, a, b, _ = world
    sid = make_space(hub, a, b)
    a.client.share(sid, members_of(a, sid), understood=True)
    first = a.client.put(sid, "one", b"1", by="person:p_test")
    a.client.revoke(sid, first["item_id"])
    items, _ = b.client.read_items(sid)
    assert [i for i in items if not i["revoked"]] == []
    assert "error" not in hub.admin(cmd="remove", space_id=sid, ws=b.ws)
    a.client.share(sid, members_of(a, sid), understood=True)
    second = a.client.put(sid, "two", b"2", by="person:p_test")
    assert second["epoch"] == 2
    for call in (lambda: b.client.read_items(sid), lambda: b.client.put(sid, "x", b"y", by="person:p_test")):
        with pytest.raises(HubRefused) as e:
            call()
        assert e.value.code in ("not_member", "not_shared")


def test_a_stale_epoch_put_is_refreshed_and_resealed(world) -> None:
    hub, a, b, c = world
    sid = make_space(hub, a, b)
    a.client.share(sid, members_of(a, sid), understood=True)
    a.client.put(sid, "one", b"1", by="person:p_test")
    hub.admin(cmd="remove", space_id=sid, ws=b.ws)  # epoch 2 behind a's back
    a.client.share(sid, members_of(a, sid), understood=True)
    assert a.client.put(sid, "two", b"2", by="person:p_test")["epoch"] == 2


def test_selective_workspace_joins_a_space_only_with_the_owners_accept() -> None:
    hub = Hub("full", "selective")
    a, b = Host(hub, "a"), Host(hub, "b")
    try:
        a.do_join()
        b.do_join()
        a.pin(b)
        b.pin(a)
        sid = make_space(hub, a)
        refused = hub.admin(cmd="add", space_id=sid, ws=b.ws)
        assert refused["error"] == "not_accepted"
        need = refused["need"]
        assert need["epoch"] == 2 and need["workspaces"] == [b.ws]
        b.client.accept(sid, epoch=2, prev_members=[a.ws], members=sorted([a.ws, b.ws]))
        added = hub.admin(cmd="add", space_id=sid, ws=b.ws)
        assert "error" not in added and added["members"] == sorted([a.ws, b.ws])
        b.client.share(sid, members_of(b, sid), understood=True)
        b.client.put(sid, "from-selective", b"s", by="person:p_test")
        a.client.share(sid, members_of(a, sid), understood=True)
        assert [i["body"]["name"] for i in a.client.read_items(sid)[0]] == ["from-selective"]
    finally:
        a.close()
        b.close()
        hub.close()


def test_share_stores_what_the_owner_was_shown_not_what_the_hub_has_now(world) -> None:
    hub, a, b, c = world
    sid = make_space(hub, a, b)
    shown = members_of(a, sid)  # the owner looks: a and b
    assert "error" not in hub.admin(cmd="add", space_id=sid, ws=c.ws)  # the hub adds c before the owner confirms
    with pytest.raises(HubRefused) as e:
        a.client.share(sid, shown, understood=True)
    assert e.value.code == "members_changed" and sid not in a.client.st.rules
    with pytest.raises(HubRefused) as e:
        a.client.put(sid, "x", b"1", by="person:p_test")
    assert e.value.code == "not_shared"
    a.client.share(sid, members_of(a, sid), understood=True)  # looked again: now a, b and c
    assert a.client.st.rules[sid]["members"] == sorted([a.ws, b.ws, c.ws])


def test_space_list_carries_the_display_name_and_kind_from_the_hub(world) -> None:
    hub, a, b, c = world
    sid = make_space(hub, a, b)
    (row,) = a.client.list_spaces()
    assert row["space_id"] == sid and row["name"] == "launch" and row["kind"] == ""
    assert c.client.list_spaces() == []
