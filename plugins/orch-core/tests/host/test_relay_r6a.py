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
from orch.identity.epochs import MembershipUnsafe
from orch.relay.keys import BridgeKeys
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


# -- round 2 (Opus review of #554) ------------------------------------------------------------------------------


def _commit_list(mk, wid, devices, person):
    from orch import canon
    from orch.identity.memberlist import member_list_o

    lhw, ehw = mk.high_water()
    e, seq = ehw + 1, lhw + 1
    o = member_list_o(wid, e, seq, 1, mk.new_epoch_key(e),
                      [{"device_id": d, "person_id": person, "scopes": ["look", "decide"]} for d in devices],
                      owner_person_id=person, first=seq == 1)  # fmt: skip
    h = "sha256:" + crypto.sha256(canon.dumps(o)).hex()
    mk.expose(list_seq=seq, epoch=e, body=canon.dumps(o), digest=h, adds=devices)
    assert mk.commit(list_seq=seq, epoch=e, list_hash=h, list_obj=o, at_ms=1)
    return e


def test_r6_installs_the_store_hook_and_rebuilds_the_gate_from_f1(lab):
    """F4: a listed device that F1 does not hold as a current device raises the gate before the worker starts;
    the store's relay_gate is installed."""
    with lab.host() as h, h.client() as c:
        wid, service = _linked(lab, h, c)
        try:
            mk = relayr6.keys_for(service)
            person = members.check_genesis(service.store.genesis_event())["delegation"]["owner_person_id"]
            _commit_list(mk, wid, ["ab" * 16], person)
            assert not mk.gate_up()
            epochs, _ = relayr6.r6(service)
            assert service.store.relay_gate is not None
            assert epochs.gate_up() and epochs.seal_state().gated
        finally:
            relayr6.close(service)


def test_a_lost_membership_directory_of_a_linked_workspace_refuses_the_start(lab):
    """F2: link.json says linked, membership/ is gone: H7, never a second WK_1."""
    import shutil

    with lab.host() as h, h.client() as c:
        wid, service = _linked(lab, h, c)
        relayr6.link_source(service).card()
        relayr6.close(service)
        write_private(link_path(lab.state, wid), {"v": 1, "workspace_id": wid, "origin": ORIGIN, "state": "linked"})
        shutil.rmtree(relayr6.membership_path(service))
        try:
            with pytest.raises(MembershipUnsafe):
                relayr6.r6(service)
        finally:
            relayr6.close(service)


def test_the_custody_check_walks_r6cs_subdirectories(lab):
    """F7: grants/<e>/, devices/ and pairing/ (R6c) are checked by the same rule: 0700 directories, 0600 files,
    at most three deep."""
    with lab.host() as h, h.client() as c:
        wid, service = _linked(lab, h, c)
        try:
            relayr6.keys_for(service)
            root = relayr6.membership_path(service)
            (root / "grants" / "1").mkdir(mode=0o700, parents=True)
            os.chmod(root / "grants", 0o700)
            f = root / "grants" / "1" / ("ab" * 16 + ".json")
            f.write_text("{}")
            os.chmod(f, 0o600)
            custody.check_workspace_files(lab.state, wid, lab.root, UID)
            os.chmod(f, 0o644)
            with pytest.raises(HostError):
                custody.check_workspace_files(lab.state, wid, lab.root, UID)
            os.chmod(f, 0o600)
            (root / "grants" / "1" / "deeper").mkdir(mode=0o700)
            with pytest.raises(HostError):
                custody.check_workspace_files(lab.state, wid, lab.root, UID)
        finally:
            relayr6.close(service)


# -- round 3 (Opus delta review) ----------------------------------------------------------------------------------


class _FakeLiveStore:
    """locked() and a state snapshot for the rebuild (``bad``: the devices whose standing is lost; every other device
    is the listed person's, ok). ``depth`` records whether the prune ran inside the lock span."""

    def __init__(self, bad=(), persons=None):
        import types

        self.depth = 0
        bad = {"d_" + b for b in bad}
        self.persons = persons or {}

        class Devices(dict):
            def get(inner, dev, default=None):  # noqa: N805
                person = self.persons.get(dev)
                return types.SimpleNamespace(person=person, removed=dev in bad, revoked=False) if person else None

        self.state = types.SimpleNamespace(workspace=types.SimpleNamespace(devices=Devices()))

    def lock_held(self):
        return self.depth > 0

    def locked(self):
        import contextlib

        @contextlib.contextmanager
        def span():
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1

        return span()


def test_the_rebuild_covers_the_exposed_rotation_and_malformed_entries(tmp_path, monkeypatch):
    """X6 / N11: an exposed rotation's member whose standing is lost, and a malformed entry, raise the gate."""
    import types

    from tests.wsproto.r6kit import DEVICE, expose_rotation, open_keys, rotate

    other = "ab" * 16
    mk = open_keys(tmp_path)
    try:
        rotate(mk, devices=[DEVICE])
        expose_rotation(mk, devices=[DEVICE, other])
        from tests.wsproto.r6kit import PERSON

        live = _FakeLiveStore([other], persons={"d_" + d: "p_" + PERSON for d in (DEVICE, other)})
        relayr6._rebuild_gate(types.SimpleNamespace(store=live), mk)  # noqa: SLF001
        assert mk.gate_up() and {"device": other, "reason": "f1_standing"} in mk.head()["gate"]["reasons"]
        calls = []
        fake = types.SimpleNamespace(
            listed_devices=lambda: [("", "")], raise_gate=lambda d, r: calls.append((d, r)),
            prune_stale_reasons=lambda ok: 0,
        )  # fmt: skip
        relayr6._rebuild_gate(types.SimpleNamespace(store=_FakeLiveStore()), fake)  # noqa: SLF001
        assert calls == [("", "h7")]
    finally:
        mk.close()


def test_previously_linked_reads_link_card_and_fails_closed(tmp_path):
    """N16 / N17."""
    import types

    ws = "a" * 32
    relay = tmp_path / "hosts" / ws / "relay"
    relay.mkdir(parents=True, mode=0o700)
    svc = types.SimpleNamespace(state_dir=tmp_path, workspace_id=ws, host_uid=UID)
    assert relayr6._previously_linked(svc) is False  # noqa: SLF001 - nothing linked
    (relay / "link-card.json").write_text("{}")
    assert relayr6._previously_linked(svc) is True  # noqa: SLF001 - a first card may be at the relay
    (relay / "link-card.json").unlink()
    (relay / "link.json").write_text("{}")
    os.chmod(relay / "link.json", 0o644)  # unsafe mode: the checked read refuses
    assert relayr6._previously_linked(svc) is True  # noqa: SLF001 - fail closed
    os.chmod(relay / "link.json", 0o600)
    write_private(relay / "link.json", {"v": 1, "state": "linked"})
    assert relayr6._previously_linked(svc) is True  # noqa: SLF001


def test_r6_rebinds_after_the_store_is_replaced(lab, monkeypatch):
    from orch.relay import worker as _w6

    monkeypatch.setattr(_w6, "R6_READY", False)  # the closed gate is what this test is about
    import types

    """X5 (R3): the key store follows the live service.store; a replaced store (reopen) gates until r6 runs again."""
    with lab.host() as h, h.client() as c:
        wid, service = _linked(lab, h, c)
        try:
            mk = relayr6.keys_for(service)
            person = members.check_genesis(service.store.genesis_event())["delegation"]["owner_person_id"]
            dev = members.check_genesis(service.store.genesis_event())["device_cert"]["device_id"]
            _commit_list(mk, wid, [dev], person)
            relayr6.r6(service)
            assert not mk.seal_state().gated
            old = service.store
            service.submit(service.open).result(30)  # replaces service.store (on the worker thread)
            assert service.store is not old and mk.seal_state().gated
            relayr6.r6(service)
            assert not mk.seal_state().gated
            live = service.store
            service.store = types.SimpleNamespace(relay_gate=None)  # replaced, the old one NOT closed (still hooked)
            try:
                assert mk.seal_state().gated  # bound to service.store as it is now, not to the store it saw
            finally:
                service.store = live
            assert not mk.seal_state().gated
        finally:
            relayr6.close(service)


# -- round 5 (Opus delta 3) -----------------------------------------------------------------------------------


def test_r6_prunes_a_stale_reason_of_a_device_that_kept_its_standing(lab):
    """F-1 end to end: a durable f1_standing reason for a listed device that is ok in the live store (an append that
    failed before its commit point) is dropped by the start-up rebuild; a reason for a device F1 does not hold as ok
    stays."""
    with lab.host() as h, h.client() as c:
        wid, service = _linked(lab, h, c)
        try:
            mk = relayr6.keys_for(service)
            g = members.check_genesis(service.store.genesis_event())
            dev = g["device_cert"]["device_id"]
            _commit_list(mk, wid, [dev], g["delegation"]["owner_person_id"])
            mk.raise_gate(dev, "f1_standing")
            assert mk.seal_state().gated
            relayr6.r6(service)
            assert mk.head()["gate"] is None and not mk.seal_state().gated
            mk.raise_gate("ab" * 16, "f1_standing")  # not an F1 device of this workspace: never ok
            relayr6.r6(service)
            assert mk.seal_state().gated
        finally:
            relayr6.close(service)


def test_r01_the_rebuild_prunes_inside_its_one_lock_span(tmp_path):
    import types

    from tests.wsproto.r6kit import DEVICE, PERSON, open_keys, rotate

    mk = open_keys(tmp_path)
    try:
        rotate(mk, devices=[DEVICE])
        live = _FakeLiveStore(persons={"d_" + DEVICE: "p_" + PERSON})
        depths = []
        real = mk.prune_stale_reasons

        def prune(ok):
            depths.append(live.depth)
            return real(ok)

        mk.prune_stale_reasons = prune
        relayr6._rebuild_gate(types.SimpleNamespace(store=live), mk)  # noqa: SLF001
        assert depths == [1]
    finally:
        mk.close()
