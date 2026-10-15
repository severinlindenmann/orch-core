"""R2 against the relay's reference: the vendored ``bridge.host_cases`` (suite 2, orch-relay ``ref._host_check``).

Each single-step, non-pairing case runs through :meth:`Bridge.receive` with the vector's state: its epoch keys, its
member list and ``dk_sig_pub``s, its revoked devices, its refusal budget and limits. The vectors carry no full device
certificate, so the certificate check is replaced by one that returns the vector's ``dk_sig_pub`` (the signature
check itself is the real one). Expected:

* ``drop``: nothing posted, nothing recorded;
* ``refuse``: one sealed ``LAST | REFUSAL`` chunk with the vector's code and fields;
* ``accept``: the request passed steps 1 to 10; R2's step 11 then applies its closed op table, so the vector's
  ``http`` op is ``forbidden_scope`` here (proto ops are not served on column R, 11A.4).
"""

from __future__ import annotations

import pytest

from orch import crypto
from orch.relay import bridge as bridge_mod
from orch.relay import sign, wire
from orch.relay.bridge import Bridge, Item
from orch.relay.keys import BridgeKeys, Member, MemberList
from orch.relay.rids import RidStore
from orch.relay.state import StateDir

from . import corpus
from .bridgekit import FakeHost, Poster, open_chunk
from .helpers import Key

CASES = [
    c
    for c in corpus.S2["bridge"]["host_cases"]
    if "steps" not in c and not c["name"].startswith("pair") and not c["state"].get("rids")
]


class _Epochs:
    def __init__(self, st: dict) -> None:
        self.cur, self.keys = st["epoch"], {int(k): bytes.fromhex(v) for k, v in st["wk"].items()}

    def current_epoch(self) -> int:
        return self.cur

    def wk(self, epoch: int) -> bytes | None:
        return self.keys.get(epoch)


class _R6:
    def __init__(self, st: dict) -> None:
        self.st = st

    def member_list(self) -> MemberList:
        members = {
            d: Member(d, "00" * 16, tuple(scopes), {"dk_sig_pub": self.st["certs"][d]["dk_sig_pub"]}, b"")
            for d, scopes in self.st["members"].items()
        }
        return MemberList(self.st["epoch"], "sha256:" + "0" * 64, members)


def test_the_corpus_has_the_cases_r2_runs():
    names = {c["name"] for c in CASES}
    assert {"full_request_accepted", "previous_epoch_refused_stale_epoch", "revoked_device", "old_timestamp"} <= names
    assert len(CASES) >= 20


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_host_case(tmp_path, monkeypatch, case):
    st, now = case["state"], case["now_ms"]
    if st["suite"] != wire.SUITE:
        pytest.skip("another suite")

    def fake_verify_cert(cert, pk_pub, now_ms):
        return {"device_id": d_of(cert), "person_id": "00" * 16, "dk_sig_pub": cert["dk_sig_pub"],
                "scopes_max": ["look", "decide", "operate", "type"]}  # fmt: skip

    def d_of(cert):
        return next(d for d, c in st["certs"].items() if c["dk_sig_pub"] == cert["dk_sig_pub"])

    monkeypatch.setattr(bridge_mod, "verify_cert", fake_verify_cert)
    limits = st.get("limits") or {}
    if "per_device" in limits:
        monkeypatch.setattr(bridge_mod, "PER_DEVICE", limits["per_device"])
    epochs = _Epochs(st)
    host = FakeHost(tmp_path)
    for d in st["revoked"]:
        host.standings[("d_" + d, "p_" + "00" * 16)] = "revoked"
    key = Key()
    ws = bytes.fromhex(st["workspace"])
    keys = BridgeKeys(epochs, ws)
    post = Poster()
    with StateDir(tmp_path / "relay") as sd:
        rids = RidStore(sd)
        try:
            b = Bridge(
                workspace_id=st["workspace"], rids=rids, keys=keys,
                signer=sign.RelaySigner(key, sign.LinkContext(st["workspace"], "https://relay.example"), epochs=keys),
                r6=_R6(st), port=host, job=lambda fn: fn(), post=post, now_ms=lambda: now,
            )  # fmt: skip
            b._budget.extend(st.get("unverified", []))  # noqa: SLF001 - the vector's spent budget
            env = bytes.fromhex(case["envelope"])
            dev = env[24:40].hex() if len(env) >= 40 else ""
            b.receive(Item(1, "bridge", dev, case.get("mailbox_id") or env[40:56].hex(), env))
            want = case["expect"]
            if want["result"] == "drop":
                assert post.posts == [] and rids.dump()["rid"] == []
                return
            (chunk,) = [e for _, _, e in post.posts]
            h, meta, data = open_chunk(chunk, epochs.keys, key.public_key)
            assert h.flags & wire.FLAG_REFUSAL and data == b""
            if want["result"] == "refuse":
                fields = {k: v for k, v in want.items() if k not in ("result", "code")}
                assert meta == {"refusal": want["code"], **fields}
            else:  # accept: passed steps 1 to 10, then R2's closed table
                assert want["result"] == "accept" and meta == {"refusal": "forbidden_scope"}
        finally:
            rids.close()
    assert crypto
