"""H12d-2: ``orch keys reader declare``'s signing path (orch-v2-host §0.8.6 item 9.2, 9.3): the runtime KAT over the
package corpus, the passphrase prompt's lines for a declaration, the convenience backend's refusal, and the hosted and
enrolment declarations verifying where they are used."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch import canon, crypto
from orch.custody import readerdecl
from orch.custody.base import CustodyError
from orch.custody.convenience import ConvenienceBackend, FakeStore
from orch.custody.describe import describe_payload
from orch.identity import reader
from orch.ops import readers
from tests.host import human_helpers
from tests.host.lifecycle_helpers import UID
from tests.identity.helpers import Person

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human
VECTOR = Path(__file__).resolve().parents[1] / "vectors" / "f1" / "reader_kat_1.json"
WS = "705d40abbb8c1c90354a1acaa94c935c"


class KeyBackend:
    """A device key that signs after building the prompt's lines (what the passphrase backend shows)."""

    def __init__(self, p: Person) -> None:
        self.p, self.shown = p, []

    def public_key(self, key_id: str) -> bytes:
        return self.p.sig_pub

    def sign(self, key_id: str, payload: bytes, *, action: str) -> bytes:
        self.shown.append(describe_payload(payload))
        return crypto.sign(self.p.dk_sig, payload)


def test_the_package_corpus_is_the_vector_and_the_self_test_declares_level_1(tmp_path):
    assert readerdecl.CORPUS.read_bytes() == VECTOR.read_bytes()
    assert readerdecl.self_test() == (1, {"1": {"log": reader.KAT_DIGESTS["1"]["log"]}})
    broken = json.loads(VECTOR.read_bytes())
    broken["log"]["cases"][2]["event"]["auth"] = "passphrase"  # the bogus case no longer fails: another digest
    path = tmp_path / "kat.json"
    path.write_text(json.dumps(broken))
    assert readerdecl.self_test(path) == (0, {})


def test_the_prompt_shows_every_field_of_a_declaration_and_refuses_another_shape():
    p = Person()
    o = readerdecl.build(
        workspace_id=WS,
        dk_sig_pub=p.sig_pub,
        dk_hash=crypto.b64u(crypto.dk_hash(p.sig_pub, p.kx_pub)),
        person_hex=p.ref[2:],
        via="declare",
        created_ms=1,
        level=1,
        kat={"1": {"log": reader.KAT_DIGESTS["1"]["log"]}},
    )
    lines = dict(describe_payload(reader.LABEL + canon.cj_checked(o)))
    assert lines["signs"] == "reader-decl" and lines["reader"] == "1" and lines["bind.via"] == "declare"
    assert (
        lines["workspace_id"] == WS and lines["who.person_id"] == p.ref[2:] and lines["kat.1.log"].startswith("sha256")
    )
    with pytest.raises(CustodyError):
        describe_payload(reader.LABEL + canon.cj_checked({**o, "extra": 1}))


def test_a_convenience_device_key_never_signs_a_reader_declaration():
    p = Person()
    store = FakeStore()
    store.put(WS, p.device, crypto.private_scalar(p.dk_sig))
    backend = ConvenienceBackend(store, WS, p.device, p.sig_pub)
    with pytest.raises(CustodyError, match="does not sign reader declarations"):
        readerdecl.sign(backend, {"v": 1, "reader": 1})
    assert backend.signatures == 0


def test_an_enrolment_declaration_verifies_as_member_add_checks_it():
    p = Person()
    backend = KeyBackend(p)
    d = readerdecl.enrol(backend, WS, p.cert(), 1_790_000_000_000)
    got = readers.verify_admission(
        d, workspace_id=WS, cert=p.cert(), person=p.ref, via="enrol", surfaces=("log",), now_ms=1_790_000_000_000
    )
    assert got.level == 1 and backend.shown and dict(backend.shown[0])["bind.via"] == "enrol"
    with pytest.raises(ValueError):
        readerdecl.enrol(backend, WS, Person().cert(), 1)  # a certificate of another key


def test_a_hosted_declaration_goes_through_reader_declare(served):
    from orch.host.client import Connection, Pin
    from orch.host.control_client import new_idem

    owner = served.lab.ws.owner
    conn = Connection(Pin(UID, served.wid, served.lab.run / served.wid / "host.sock"))
    try:
        answer = readerdecl.declare_hosted(
            conn, KeyBackend(owner), served.wid, int(served.service.clock() * 1000), new_idem()
        )
    finally:
        conn.close()
    assert answer["device"] == owner.device and answer["reader"] == 1 and answer["level"] == 1
