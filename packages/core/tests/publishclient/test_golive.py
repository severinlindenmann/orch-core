"""#1455 go-live: every check of the second signature refuses one tampered field each, and the approval it builds
verifies like the vectors' (``contracts/hub/vectors_published.json``): core is a producer of that signed shape."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from orch import crypto
from orch.canon.jcs import dumps as cj
from orch.crypto import suite as S
from orch.crypto.encoding import b64u, unb64u
from orch.crypto.labels import L
from orch.hubclient import published as pub
from orch.hubclient.errors import HubRefused
from orch.publishclient import approval, golive

from .test_app_release import SERVER, SHARE, make_meta

WS = "ab" * 16
HUB = "cd" * 16
APP = "ef" * 16
DEVICE = "12" * 16
HOST = "pub.example.org"
NOW = 1_790_000_000_000
NONCE = bytes(range(16))
STAGED = "pa_" + "c" * 32


def facts(op="commit", version=2, meta=None, previous=None):
    meta = meta or make_meta()
    return {
        "op": op, "app_id": APP, "version": version, "meta": meta, "previous": previous, "staged_id": STAGED,
        "ticket_key": "TST-0001", "origin": "agent", "server": SERVER, "share": SHARE,
    }  # fmt: skip


_NOTE_KEYS = ("staged_id", "ticket_key", "origin", "op", "app_id", "version", "meta", "previous", "server", "share")


def note(f):
    return approval.app_release_text(**{k: f[k] for k in _NOTE_KEYS})


def request(f, *, hub=HUB, ts=NOW, nonce=NONCE, target=None, body=None, device=DEVICE, **over):
    body = body if body is not None else golive.commit_body(f["meta"], f["op"], f["app_id"], f["version"])
    target = target or golive.commit_target(WS, f["app_id"], f["op"])
    rh = golive.request_hash_b64(WS, hub, ts, nonce, HOST, target, body)
    o = golive.approval_object(WS, hub, device, rh, f["meta"]["access"])
    return {"target": target, "body": body, "ts_ms": ts, "nonce": nonce, "o": o} | over


_SAME = object()


def check(f, req, text=_SAME, **kw):
    args = dict(workspace_id=WS, host=HOST, hub_id=HUB, device_id=DEVICE, now_ms=NOW) | kw
    return golive.check_commit_request(req, f, note(f) if text is _SAME else text, **args)


def test_a_request_that_matches_the_note_is_accepted_and_returns_the_object_to_sign():
    f = facts()
    req = request(f)
    assert check(f, req) == req["o"]


@pytest.mark.parametrize("op, version, previous", [
    ("commit", 3, {"version": 2, "meta": make_meta()}), ("rollback", 1, {"version": 2, "meta": make_meta()}),
    ("start", 2, {"version": 2, "meta": make_meta()}),
])  # fmt: skip
def test_every_op_is_accepted_with_its_own_words_and_body(op, version, previous):
    f = facts(op, version, previous=previous)
    check(f, request(f))


def refused(f, req, **kw):
    with pytest.raises(HubRefused) as caught:
        check(f, req, **kw)
    assert caught.value.code == "malformed"


def test_each_field_of_the_request_is_checked_alone():
    f = facts()
    good = request(f)
    body = json.loads(good["body"])
    for key, value in (
        ("meta_hash", pub.meta_hash(make_meta(name="Other"))), ("reach_hash", pub.reach_hash(["https://x.example.com"])),
        ("access", "company"), ("audience", "company"), ("version", 9), ("app_id", "00" * 16), ("op", "start"),
    ):  # fmt: skip
        tampered = cj({**body, key: value})
        refused(f, request(f, body=tampered))  # the body is not the one rebuilt from the release
    refused(f, request(f, target=golive.commit_target(WS, f["app_id"], "start")))  # another op's route
    refused(f, request(f, target=golive.commit_target("00" * 16, f["app_id"], "commit")))  # another workspace
    refused(f, request(f, target=golive.commit_target(WS, "00" * 16, "commit")))  # another app
    refused(f, good, hub_id="00" * 16)  # another hub than the workspace's
    refused(f, good, host="evil.example.org")  # another host than the signed setting
    refused(f, good, device_id="00" * 16)  # another device
    refused(f, good, workspace_id="00" * 16)
    refused(f, good, now_ms=NOW + golive.SKEW_MS + 1)  # skew, both ways
    refused(f, good, now_ms=NOW - golive.SKEW_MS - 1)
    check(f, good, now_ms=NOW + golive.SKEW_MS)  # at the edge it is fine
    refused(f, request(f, ts=NOW + 1, o=good["o"]))  # an object for another ts
    refused(f, request(f, nonce=bytes(16), o=good["o"]))  # ... another nonce
    other = copy.deepcopy(good)
    other["o"]["access"] = "company"
    refused(f, other)
    other = copy.deepcopy(good)
    other["o"]["request_hash"] = b64u(bytes(32))
    refused(f, other)
    other = copy.deepcopy(good)
    other["o"]["extra"] = 1
    refused(f, other)
    other = copy.deepcopy(good)
    other["o"]["kind"] = "hub.card"
    refused(f, other)


def test_the_note_must_be_the_one_the_client_signed_to_the_byte():
    f = facts()
    text = note(f)
    refused(f, request(f), text=text + " ")
    refused(f, request(f), text=text.replace("Version: 2", "Version: 3"))
    refused(f, request(f), text="")
    refused(f, request(f), text=None)
    # facts that differ from the signed note (a host that swapped the release) have other words
    swapped = facts(meta=make_meta(reach=["https://api.example.com"]))
    refused(swapped, request(swapped), text=text)


def test_garbage_facts_or_requests_are_malformed_never_a_crash():
    f = facts()
    for bad in ({}, {"op": "commit"}, {**f, "meta": None}, {**f, "version": "2"}, {**f, "op": "stop"}):
        with pytest.raises(HubRefused):
            golive.check_commit_request(
                request(f), bad, note(f), workspace_id=WS, host=HOST, hub_id=HUB, device_id=DEVICE, now_ms=NOW
            )
    for key in ("target", "body", "ts_ms", "nonce", "o"):
        broken = {k: v for k, v in request(f).items() if k != key}
        refused(f, broken)
    good = request(f)
    refused(f, {**good, "nonce": b"short"})
    refused(f, {**good, "ts_ms": "1"})


# -- the approval it builds is the contract's ----------------------------------------------------------------------

VEC = json.loads((Path(__file__).resolve().parents[4] / "contracts/hub/vectors_published.json").read_text())


def _cases():
    return [c for c in VEC["approval_cases"] if "ok" in c["expect"]]


@pytest.mark.parametrize("case", _cases(), ids=[c["name"] for c in _cases()])
def test_core_rebuilds_the_vectors_approval_object_and_its_request_hash(case):
    """Core as a PRODUCER: from each accepted vector's request, the object ``o`` and the ``request_hash`` core builds
    are byte-equal to the vector's, and the vector's signature verifies under the label core signs with."""
    a = case["args"]
    req, ap = a["request"], a["approval"]
    body = bytes.fromhex(req["body_hex"])
    hub = req["hub"]
    got = golive.request_hash_b64(req["ws"], hub, req["ts_ms"], bytes.fromhex(req["nonce_hex"]), req["host"],
                                  req["target"], body)  # fmt: skip
    assert got == ap["o"]["request_hash"]
    built = golive.approval_object(req["ws"], hub, ap["o"]["device_id"], got, json.loads(body)["access"])
    assert built == ap["o"]
    assert cj(built) == cj(ap["o"])
    cert = ap["device_cert"]["o"]
    assert S.verify(unb64u(cert["dk_sig_pub"], 65), unb64u(ap["sig"], 64), L["sig_hub_publish_approval"] + cj(ap["o"]))


def test_a_built_approval_verifies_with_the_device_key_and_only_under_the_approval_label():
    key = S.generate_private_key()
    pub_key = crypto.public_bytes(key)
    f = facts()
    o = check(f, request(f))
    cert = {"o": {"device_id": DEVICE, "dk_sig_pub": b64u(pub_key)}, "sig": "x"}
    ap = golive.build_approval(o, lambda payload: crypto.sign(key, payload), cert, "pk")
    assert set(ap) == {"o", "sig", "device_cert", "pk_pub"} and ap["o"] == o
    assert S.verify(pub_key, unb64u(ap["sig"], 64), L["sig_hub_publish_approval"] + cj(o))
    assert not S.verify(pub_key, unb64u(ap["sig"], 64), L["sig_publish_hub"] + cj(o))
