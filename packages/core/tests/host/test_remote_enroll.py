"""R2 T2a (#1590): enrollment, the host side (design note section 3; hub contract 10.2c "Enrollment").

Codes live in memory; a device proves the code at the handshake (shape, MAC, presence), only then a pending
confirmation exists, and only the owner's confirmation (the owner proof) adds a device. The first-message check
replays the shared contract vectors."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest

from orch.crypto import suite
from orch.crypto.encoding import b64u, unb64u
from orch.host import remote
from orch.host.remote import enrollment, wrk
from orch.hubclient.errors import HubRefused
from tests.host import human_helpers
from tests.host.dispatch_helpers import ok
from tests.host.human_helpers import err
from tests.host.test_remote_ops import call, clock_ms, model, run_on, turn_on
from tests.host.test_remote_policy import submit_with

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

CONTRACT = Path(__file__).resolve().parents[4] / "contracts" / "hub"
DOC = json.loads((CONTRACT / "vectors_remote.json").read_text())
PUB = {n: unb64u(v["pub"], 65) for n, v in DOC["keys"]["p256"].items()}


class Device:
    """A device on the other side of an enroll stream: its own transport and presence keys."""

    def __init__(self, name: str = "Severin's MacBook", seed: int = 1) -> None:
        self.name = name
        self.transport = bytes([seed]) * 32
        self.key = suite.generate_private_key()
        self.ppub = suite.public_bytes(self.key)

    def first(self, served, text: str, code_id: str, hh: bytes, challenge: bytes, **over: Any) -> dict[str, Any]:
        """The first message, built from the code text the owner handed over."""
        body = unb64u(text.split(".")[2])
        secret = body[16:32]
        kind = bytes([1])
        mac = hmac.new(secret, enrollment._ENROLL + hh + self.transport + self.ppub + kind, hashlib.sha256)  # noqa: SLF001
        msg = enrollment.open_bytes(served.service.workspace_id, code_id, hh, challenge)
        out = {
            "t": "enroll", "name": self.name, "presence_kind": "platform", "presence_pub": b64u(self.ppub),
            "mac": b64u(mac.digest()), "presence": {"form": "p256", "sig": b64u(suite.sign(self.key, msg))},
        }  # fmt: skip
        return {**out, **over}


HH, CH = bytes(range(32)), bytes(range(32, 64))
STREAM = "e" * 32


def new_code(served) -> dict[str, Any]:
    return ok(call(served, "remote.code"))["data"]


def verify(served, code, dev: Device, msg: dict[str, Any] | None = None, code_id: str | None = None):
    cid = code_id or code["code_id"]
    first = dev.first(served, code["code"], code["code_id"], HH, CH) if msg is None else msg
    return enrollment.verify(served.service, cid, STREAM, HH, dev.transport, first, CH)


def log_len(served) -> int:
    return len(list(served.service.store.events_after("workspace", 0, 1 << 30)))


def test_enroll_no_code_no_state_no_prompt(human, served):
    before = log_len(served)
    dev = Device()
    fake = {"t": "enroll"}
    with pytest.raises(HubRefused) as e:  # no code was ever issued: nothing to look at, not even the message
        enrollment.verify(served.service, "rc_" + "1" * 32, STREAM, HH, dev.transport, fake, CH)
    assert e.value.code == "remote.no_code"
    assert enrollment.outstanding(served.service, clock_ms(served)) == {}
    assert ok(call(served, "remote.pending"))["data"] == {"pending": []}  # no prompt for the owner
    assert log_len(served) == before and not wrk.path(served.service).exists()  # no line, no key made
    assert not (wrk.state_dir(served.service) / "devices.json").exists()
    code = new_code(served)  # a code that ran out is no code either
    served.lab.ws.clock[0] += 16 * 60
    with pytest.raises(HubRefused) as e:
        verify(served, code, dev)
    assert e.value.code == "remote.no_code" and ok(call(served, "remote.pending"))["data"] == {"pending": []}


def test_enroll_bad_mac_counts_and_burns_at_5(human, served):
    code, dev = new_code(served), Device()
    bad = dev.first(served, code["code"], code["code_id"], HH, CH, mac=b64u(b"\0" * 32))
    for n in range(4):
        with pytest.raises(HubRefused) as e:
            verify(served, code, dev, bad)
        assert e.value.code == "bad_signature"
        assert code["code_id"] in enrollment.outstanding(served.service, clock_ms(served)), n  # still alive
    with pytest.raises(HubRefused) as e:  # the fifth burns it
        verify(served, code, dev, bad)
    assert e.value.code == "bad_signature"
    assert enrollment.outstanding(served.service, clock_ms(served)) == {}
    with pytest.raises(HubRefused) as e:  # even the right message is too late
        verify(served, code, dev)
    assert e.value.code == "remote.no_code"
    types = [e["type"] for e, _ in served.service.store.events_after("workspace", 0, 1 << 30)]
    assert types.count("remote.code_burned") == 1 and ok(call(served, "remote.pending"))["data"] == {"pending": []}


def test_a_malformed_message_is_not_a_try_and_the_right_one_still_works(human, served):
    code, dev = new_code(served), Device()
    for _ in range(6):
        with pytest.raises(HubRefused) as e:
            verify(served, code, dev, {"t": "enroll"})
        assert e.value.code == "malformed"  # shape comes before the MAC and counts nothing
    assert verify(served, code, dev)["t"] == "pending"


def test_a_presence_failure_after_the_mac_creates_nothing_and_keeps_the_code(human, served):
    code, dev = new_code(served), Device()
    other = suite.generate_private_key()
    msg = enrollment.open_bytes(served.service.workspace_id, code["code_id"], HH, CH)
    first = dev.first(
        served, code["code"], code["code_id"], HH, CH, presence={"form": "p256", "sig": b64u(suite.sign(other, msg))}
    )
    with pytest.raises(HubRefused) as e:
        verify(served, code, dev, first)
    assert e.value.code == "bad_signature"
    assert ok(call(served, "remote.pending"))["data"] == {"pending": []}
    assert code["code_id"] in enrollment.outstanding(served.service, clock_ms(served))


def test_enroll_verified_but_unconfirmed_adds_nothing(human, served):
    turn_on(human)
    code, dev = new_code(served), Device()
    before = log_len(served)
    out = verify(served, code, dev)
    assert out["t"] == "pending" and len(out["fp"]) == 64 and len(out["words"].split()) == 6
    assert log_len(served) == before  # the log learned nothing
    r = model(served)
    assert r["devices"] == {} and r["device_version"] == 0
    assert not (wrk.state_dir(served.service) / "devices.json").exists()
    assert enrollment.outstanding(served.service, clock_ms(served)) == {}  # single use: consumed
    with pytest.raises(HubRefused) as e:
        verify(served, code, dev)
    assert e.value.code == "remote.no_code"
    pend = ok(call(served, "remote.pending"))["data"]["pending"]
    assert [(p["pending"], p["name"], p["words"]) for p in pend] == [(out["pending"], dev.name, out["words"])]
    assert enrollment.wait_added(out["pending"], timeout=0.05) is None  # nobody confirmed
    served.lab.ws.clock[0] += 16 * 60  # the pending device runs out with its code
    assert ok(call(served, "remote.pending"))["data"] == {"pending": []}
    assert enrollment.pending_source(served.service, out["pending"]) is None


def test_the_owner_confirms_and_the_waiter_gets_the_device(human, served):
    turn_on(human)
    code, dev = new_code(served), Device()
    out = verify(served, code, dev)
    prepared = human.prepare("remote.device_confirm", {"pending": out["pending"], "level_cap": "work", "days": 30})
    ev = prepared["events"][0]
    assert ev["name"] == dev.name and ev["code_id"] == code["code_id"] and ev["fingerprint"] == out["fp"]
    assert "data" in ok(submit_with(human, prepared, human.owner_proof(prepared)))
    assert enrollment.wait_added(out["pending"], timeout=2) == ev["device_id"]
    assert list(model(served)["devices"]) == [ev["device_id"]]
    assert ok(call(served, "remote.pending"))["data"] == {"pending": []}
    res = human.prepare_res("remote.device_confirm", {"pending": out["pending"], "level_cap": "work", "days": 30})
    assert err(res) == "remote.no_pending"  # a pending device is confirmed once
    assert enrollment.wait_added(out["pending"], timeout=0.05) is None  # and the answer is taken once


def test_confirm_after_stream_gone_no_pending(human, served):
    turn_on(human)
    code, dev = new_code(served), Device()
    out = verify(served, code, dev)
    enrollment.drop(served.service, STREAM)  # the enroll stream closed
    assert enrollment.wait_added(out["pending"], timeout=0.05) is None
    res = human.prepare_res("remote.device_confirm", {"pending": out["pending"], "level_cap": "view", "days": 30})
    assert err(res) == "remote.no_pending"
    assert model(served)["devices"] == {}
    assert (
        err(human.prepare_res("remote.device_confirm", {"pending": "rp_" + "0" * 32, "level_cap": "view", "days": 30}))
        == "remote.no_pending"
    )


def test_still_pending_is_true_only_for_the_right_code_while_open_and_in_time(human, served):
    """The real `enrollment.still_pending` (not a stub): the confirm's last check under the store lock."""
    turn_on(human)
    code, dev = new_code(served), Device()
    cid = code["code_id"]
    assert not enrollment.still_pending(served.service, cid)  # nothing verified yet
    verify(served, code, dev)
    assert enrollment.still_pending(served.service, cid)
    assert not enrollment.still_pending(served.service, "c_" + "0" * 26)  # another code id
    enrollment.drop(served.service, STREAM)
    assert not enrollment.still_pending(served.service, cid)  # the stream closed
    # expiry: a second device, then the clock moves past its expires_ms
    code2 = new_code(served)
    verify(served, code2, Device(seed=2))
    assert enrollment.still_pending(served.service, code2["code_id"])
    served.lab.ws.clock[0] += 16 * 60
    assert not enrollment.still_pending(served.service, code2["code_id"])


def test_a_confirmation_needs_the_owner_proof(human, served):
    turn_on(human)
    code, dev = new_code(served), Device()
    out = verify(served, code, dev)
    prepared = human.prepare("remote.device_confirm", {"pending": out["pending"], "level_cap": "work", "days": 30})
    assert err(submit_with(human, prepared, None)) == "remote.proof_invalid"
    assert model(served)["devices"] == {}  # nothing added without the proof; the pending device is still there
    assert enrollment.pending_source(served.service, out["pending"]) is not None


def test_a_fido2_device_signature_counter_is_recorded_for_r3(human, served):
    from orch.host.custody import read_json

    turn_on(human)
    st = enrollment._ws(served.service)  # noqa: SLF001
    p = enrollment.Pending(
        pid="rp_" + "3" * 32, workspace_id=served.service.workspace_id, code_id="rc_" + "3" * 32, stream_id="s" * 32,
        name="Key", presence_kind="fido2", transport_pub=b64u(b"\x03" * 32), presence_pub=b64u(Device().ppub),
        fingerprint="0" * 64, words="", sign_count=41, expires_ms=clock_ms(served) + 60_000,
    )  # fmt: skip
    st.pending[p.pid] = p
    run_on(
        served,
        remote.on_commit,
        served.service,
        "workspace",
        {"type": "remote.device_added", "code_id": p.code_id, "device_id": "rd_" + "4" * 32},
    )
    doc = read_json(wrk.state_dir(served.service) / "counters.json", served.service.host_uid)
    assert doc == {"v": 1, "fido2": {"rd_" + "4" * 32: 41}}


# --- the contract vectors against the real check ---

ENROLL = [c for c in DOC["cases"] if c["check"] == "enroll"]


@pytest.mark.parametrize("c", ENROLL, ids=[c["name"] for c in ENROLL])
def test_enroll_vector(c):
    a = c["args"]
    try:
        got: dict[str, Any] = {
            "ok": enrollment.check_enroll(
                a["msg"],
                secret=bytes.fromhex(a["secret_hex"]),
                workspace_id=a["workspace_id"],
                code_id=a["code_id"],
                hh=bytes.fromhex(a["handshake_hash_hex"]),
                transport_pub=bytes.fromhex(a["transport_pub_hex"]),
                challenge=bytes.fromhex(a["challenge_hex"]),
            )  # fmt: skip
        }
    except HubRefused as e:
        got = {"error": e.code}
    assert got == c["expect"], c.get("note")


def test_code_and_key_vectors():
    c = next(c for c in DOC["cases"] if c["check"] == "code_bytes")["args"]
    pub = bytes.fromhex(c["wrk_pub_hex"])
    text = enrollment.code_text("0" * 32, c["code_id"], bytes.fromhex(c["secret_hex"]), pub)
    want = next(x for x in DOC["cases"] if x["check"] == "code_bytes")["expect"]["hex"]
    assert unb64u(text.split(".")[2]).hex() == want and unb64u(text.split(".")[3]) == pub
    f = next(x for x in DOC["cases"] if x["check"] == "wrk_fp")
    assert wrk.wrk_fp(bytes.fromhex(f["args"]["wrk_pub_hex"])).hex() == f["expect"]["hex"]
    with pytest.raises(ValueError):
        wrk.wrk_fp(b"short")
