"""B1: the genesis pin's person path (11A.2, ``relay.genesis_pin``): the signer's check refuses what it cannot
recompute, the host keeps only the exact pin its own state asks for, ``link_source().genesis()`` returns it, and the
pin passes R1's ``Directory.check_genesis_pin``."""

from __future__ import annotations

import copy
import errno
import os
from types import SimpleNamespace

import pytest

from orch import canon, crypto
from orch.crypto.labels import L
from orch.custody.base import DEVICE_KEY_LABELS, HOST_LABELS, PERSON_KEY_LABELS
from orch.host import paircli, relayr6
from orch.host.custody import write_private
from orch.host.relaylink import link_path
from orch.host.signer import SignerRefused
from orch.identity import genesispin
from orch.identity.errors import Refused
from orch.relay.directory import Directory
from orch.relay.r6.firstcard import GenesisPathMissing
from tests.host.lifecycle_helpers import Lab

ORIGIN = "https://relay.example"


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


class Backend:
    """The genesis device's custody backend (a test person's key)."""

    def __init__(self, person) -> None:
        self.person, self.signed = person, []

    def public_key(self, key_id):
        return self.person.sig_pub

    def sign(self, key_id, payload, *, action):
        self.signed.append((payload, action))
        return self.person.dk_sign(payload)


def _linked(lab, h, c):
    res = lab.register(c)
    wid = res["result"]["data"]["workspace_id"]
    service = h.host.services[wid]
    link_path(lab.state, wid).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(link_path(lab.state, wid), {"v": 1, "workspace_id": wid, "origin": ORIGIN, "state": "linking",
                                              "max_publication_bytes": 4 << 20})  # fmt: skip
    return wid, service


def _call(c):
    def call(op, args):
        res = c.call(op, args)
        assert res["ok"], res
        return res["result"]["data"]

    return call


# -- the signer's check -----------------------------------------------------------------------------------------------


def _facts(lab, h, c):
    wid, service = _linked(lab, h, c)
    return wid, service, _call(c)("host.relay_genesis_pin_facts", {"workspace_id": wid})["facts"]


def test_the_signer_recomputes_the_pin_from_the_genesis_event_and_refuses_anything_else(lab):
    with lab.host() as h, h.client() as c:
        wid, service, facts = _facts(lab, h, c)
        owner = lab.ws.owner
        v = genesispin.check_facts(facts, workspace_id=wid, device_pub=owner.sig_pub)
        ev = service.store.genesis_event()
        assert v.o == genesispin.pin_o(wid, crypto.unb64u(ev["wsk_pub"]), canon.event_head(ev))
        assert v.origin == ORIGIN and v.owner_pk_pub == owner.pk_pub
        other_ws = "0" * 32
        bad = [
            (dict(facts), dict(workspace_id=other_ws)),  # the signer asked about another workspace
            ({**facts, "workspace_id": other_ws}, {}),  # the host claims another workspace
            ({**facts, "extra": 1}, {}),  # any extra field
            ({k: x for k, x in facts.items() if k != "origin"}, {}),
            ({**facts, "v": 2}, {}),
            ({**facts, "origin": "x\x1b[2Jy"}, {}),  # a control sequence never reaches the review
            (dict(facts), dict(device_pub=crypto.public_bytes(crypto.generate_private_key()))),  # not the device
        ]
        for f, kw in bad:
            with pytest.raises(Refused):
                genesispin.check_facts(f, **{"workspace_id": wid, **kw})
        for field, value in (("wsk_pub", crypto.b64u(crypto.public_bytes(crypto.generate_private_key()))),
                             ("prefix", "ZZZ"), ("workspace_id", other_ws)):  # fmt: skip
            tampered = copy.deepcopy(facts)
            tampered["genesis_event"][field] = (
                value  # a host swapping the key or the hash: the event no longer verifies
            )
            with pytest.raises(Refused):
                genesispin.check_facts(tampered, workspace_id=wid)


def test_the_hash_is_the_events_own_so_a_wrong_genesis_hash_cannot_be_signed(lab):
    with lab.host() as h, h.client() as c:
        wid, service, facts = _facts(lab, h, c)
        v = genesispin.check_facts(facts, workspace_id=wid)
        wrong = {**v.o, "genesis": "sha256:" + "0" * 64}
        backend = Backend(lab.ws.owner)
        # the signer builds the object itself; a host that sends its own pin is simply not read
        pin = genesispin.sign_pin(backend, "dk", v)
        assert pin["o"] == v.o != wrong
        res = c.call("host.relay_genesis_pin_submit", {"workspace_id": wid,
                                                         "pin": {"o": wrong, "sig": pin["sig"]}})  # fmt: skip
        assert res["ok"] is False and res["error"]["code"] == "invalid.input"
        res = c.call(
            "host.relay_genesis_pin_submit", {"workspace_id": wid, "pin": {"o": v.o, "sig": crypto.b64u(bytes(64))}}
        )
        assert res["ok"] is False and res["error"]["code"] == "invalid.input"  # a signature that does not verify


# -- the whole path ---------------------------------------------------------------------------------------------------


def test_sign_submit_keep_and_link_source_returns_a_pin_the_relay_directory_accepts(lab):
    with lab.host() as h, h.client() as c:
        wid, service = _linked(lab, h, c)
        try:
            src = relayr6.link_source(service)
            with pytest.raises(GenesisPathMissing):
                src.genesis()  # nothing signed yet
            backend = Backend(lab.ws.owner)
            shown: list[tuple[str, str]] = []

            def confirm(text, word):
                shown.append((text, word))
                return True

            out = paircli.sign_genesis_pin(_call(c), wid, backend=backend, key_id="dk", name="demo", confirm=confirm)
            assert out["workspace_id"] == wid
            ((text, word),) = shown
            assert word == f"{wid[-8:]}/GENESIS" and ORIGIN in text and out["genesis"] in text
            payload, action = backend.signed[0]
            assert payload.startswith(L["sig_genesis_pin"]) and "genesis" in action
            pin, cert, genesis = relayr6.link_source(service).genesis()
            assert genesis == out["genesis"] and pin["o"]["genesis"] == genesis
            d = Directory(SimpleNamespace(workspace_id=wid), owner_pk_pub=lab.ws.owner.pk_pub,
                          wsk_pub=crypto.unb64u(service.store.genesis_event()["wsk_pub"]))  # fmt: skip
            d.check_genesis_pin(pin, cert, genesis)  # R1's own check of the relay's view of it
            assert (
                oct(os.stat(service.state_dir / "hosts" / wid / "relay" / "genesis-pin.json").st_mode & 0o777)
                == "0o600"
            )
        finally:
            relayr6.close(service)


def test_a_declined_review_signs_nothing_and_another_workspaces_facts_are_refused_before_the_prompt(lab):
    with lab.host() as h, h.client() as c:
        wid, service = _linked(lab, h, c)
        backend = Backend(lab.ws.owner)
        with pytest.raises(SignerRefused, match="not confirmed"):
            paircli.sign_genesis_pin(_call(c), wid, backend=backend, key_id="dk", name="d", confirm=lambda t, w: False)
        assert backend.signed == []
        asked = []

        def lying(op, args):
            data = _call(c)(op, args)
            if op.endswith("facts"):
                data["facts"]["workspace_id"] = "f" * 32
            return data

        with pytest.raises(SignerRefused, match="refusing to sign"):
            paircli.sign_genesis_pin(lying, wid, backend=backend, key_id="dk", name="d",
                                     confirm=lambda t, w: asked.append(t) or True)  # fmt: skip
        assert asked == [] and backend.signed == []
        wrong_key = Backend(SimpleNamespace(sig_pub=crypto.public_bytes(crypto.generate_private_key()), dk_sign=None))
        with pytest.raises(SignerRefused, match="genesis device"):
            paircli.sign_genesis_pin(_call(c), wid, backend=wrong_key, key_id="dk", name="d", confirm=lambda t, w: True)


def test_the_passphrase_prompt_shows_the_pins_fields_and_refuses_a_pin_with_anything_else(lab):
    from orch.custody.base import CustodyError
    from orch.custody.describe import describe_payload

    with lab.host() as h, h.client() as c:
        wid, service, facts = _facts(lab, h, c)
        v = genesispin.check_facts(facts, workspace_id=wid)
        lines = dict(describe_payload(genesispin.pin_payload(v.o)))
        assert lines["signs"] == "genesis-pin" and lines["workspace_id"] == wid and lines["genesis"] == v.o["genesis"]
        assert lines["wsk_pub"] == v.o["wsk_pub"]
        with pytest.raises(CustodyError):
            describe_payload(genesispin.pin_payload({**v.o, "extra": 1}))


def test_the_label_is_an_owner_device_label_and_nobody_elses():
    raw = L["sig_genesis_pin"]
    assert raw in DEVICE_KEY_LABELS and raw not in HOST_LABELS and raw not in PERSON_KEY_LABELS


def test_a_stored_pin_of_another_genesis_is_not_returned(lab):
    with lab.host() as h, h.client() as c:
        wid, service = _linked(lab, h, c)
        try:
            paircli.sign_genesis_pin(_call(c), wid, backend=Backend(lab.ws.owner), key_id="dk", name="d",
                                     confirm=lambda t, w: True)  # fmt: skip
            path = service.state_dir / "hosts" / wid / "relay" / "genesis-pin.json"
            import json

            rec = json.loads(path.read_text())
            rec["pin"]["o"]["genesis"] = "sha256:" + "1" * 64
            write_private(path, rec)
            with pytest.raises(GenesisPathMissing):
                relayr6.link_source(service).genesis()
        finally:
            relayr6.close(service)
