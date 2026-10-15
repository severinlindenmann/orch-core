"""R6a host wiring: the ``r6`` and ``link_source`` seams over a real registered workspace (its WSK, its genesis, its
state directory): the first card verifies under the owner key from the workspace's own genesis, the key store passes
the D-P2-17 custody check, an unsafe store refuses the worker start, convenience history refuses the card."""

from __future__ import annotations

import errno
import os

import pytest

from orch import crypto
from orch.host import custody, relayr6
from orch.host.custody import write_private
from orch.host.errors import HostError
from orch.host.relaylink import link_path
from orch.identity import members, wscard
from orch.relay.keys import BridgeKeys
from orch.relay.r6.keystore import MembershipUnsafe
from tests.host.lifecycle_helpers import UID, Lab

ORIGIN = "https://relay.example"


@pytest.fixture
def lab(tmp_path):
    value = Lab(tmp_path)
    try:
        yield value
    except OSError as exc:  # pragma: no cover - a sandbox without AF_UNIX bind
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    finally:
        value.close()


def _linked(lab, h, c):
    res = lab.register(c)
    assert res["ok"], res
    wid = res["result"]["data"]["workspace_id"]
    service = h.host.services[wid]
    link_path(lab.state, wid).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(link_path(lab.state, wid), {"v": 1, "workspace_id": wid, "origin": ORIGIN, "state": "linking"})
    return wid, service


def test_link_source_makes_the_first_card_from_the_workspaces_own_genesis(lab):
    with lab.host() as h, h.client() as c:
        wid, service = _linked(lab, h, c)
        try:
            src = relayr6.link_source(service)
            owner = members.check_genesis(service.store.genesis_event())["owner_pk_pub"]
            assert src.owner_pk_pub() == owner
            card = src.card()
            vc = wscard.verify_card(card, owner)
            assert (vc.workspace_id, vc.card_seq, vc.relay_url) == (wid, 1, ORIGIN)
            assert crypto.b64u(vc.wsk_pub) == service.store.genesis_event()["wsk_pub"]
            assert relayr6.link_source(service).card() == card  # identical, same store instance
            custody.check_workspace_files(lab.state, wid, lab.root, UID)
            epochs, view = relayr6.r6(service)
            assert epochs is relayr6.keys_for(service) and view.member_list() is None
            BridgeKeys(epochs, bytes.fromhex(wid))  # R2 accepts it as its EpochKeySource
        finally:
            relayr6.close(service)


def test_a_wider_membership_key_file_fails_the_custody_check(lab):
    with lab.host() as h, h.client() as c:
        wid, service = _linked(lab, h, c)
        try:
            relayr6.link_source(service).card()
            p = relayr6.membership_path(service) / "keys" / "ck-1.json"
            os.chmod(p, 0o640)
            with pytest.raises(HostError):
                custody.check_workspace_files(lab.state, wid, lab.root, UID)
        finally:
            relayr6.close(service)


def test_an_unsafe_store_refuses_the_worker_start(lab):
    with lab.host() as h, h.client() as c:
        _, service = _linked(lab, h, c)
        relayr6.link_source(service).card()
        relayr6.close(service)
        (relayr6.membership_path(service) / "head.json").unlink()
        try:
            with pytest.raises(MembershipUnsafe):
                relayr6.r6(service)
            with pytest.raises(MembershipUnsafe):
                relayr6.link_source(service)
        finally:
            relayr6.close(service)


def test_no_link_record_refuses_the_link_source(lab):
    with lab.host() as h, h.client() as c:
        res = lab.register(c)
        service = h.host.services[res["result"]["data"]["workspace_id"]]
        with pytest.raises(HostError):
            relayr6.link_source(service)


def test_the_card_is_refused_while_convenience_would_refuse_the_link(lab, monkeypatch):
    with lab.host() as h, h.client() as c:
        _, service = _linked(lab, h, c)
        try:
            monkeypatch.setattr(relayr6, "_convenience_block", lambda svc: "convenience_events_present")
            with pytest.raises(ValueError, match="convenience"):
                relayr6.link_source(service).card()
            assert os.listdir(relayr6.membership_path(service) / "keys") == []
        finally:
            relayr6.close(service)
