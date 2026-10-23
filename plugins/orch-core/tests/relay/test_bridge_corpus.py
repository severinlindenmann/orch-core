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
from orch.relay.bridge import Bridge, Item
from orch.relay.keys import BridgeKeys, Member, MemberList, SealState
from orch.relay.rids import RidStore
from orch.wsproto import sign, wire
from orch.wsproto.state import StateDir

from . import corpus
from .bridgekit import FakeHost, Poster, SealLockFixture, open_chunk
from .helpers import Key

CASES = [c for c in corpus.S2["bridge"]["host_cases"] if "steps" not in c and not c["name"].startswith("pair")]
#: the multi-step non-pairing cases (round 2: every non-pairing host case runs against core). Core's mapping of a
#: relay-ref "accept" is R2's closed op table (``forbidden_scope``, recorded): so the ref's ``already_done`` for an
#: identical resend of an accepted (still running) rid is, in core, the stored answer re-posted byte for byte.
STEPPED = [c for c in corpus.S2["bridge"]["host_cases"] if "steps" in c and not c["name"].startswith("pair")]


class _Epochs(SealLockFixture):
    def __init__(self, st: dict) -> None:
        self.cur, self.keys = st["epoch"], {int(k): bytes.fromhex(v) for k, v in st["wk"].items()}

    def current_epoch(self) -> int:
        return self.cur

    def wk(self, epoch: int) -> bytes | None:
        return self.keys.get(epoch) if epoch <= self.cur else None

    def refusal_key(self, epoch: int) -> bytes | None:
        return None

    def is_burned(self, epoch: int) -> bool:
        return False

    def seal_state(self) -> SealState:
        return SealState(self.cur, False, self.cur + 1)


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
    assert "quota_busy" in names and len(CASES) >= 21
    assert {c["name"] for c in STEPPED} == {
        "replayed_request", "stale_epoch_consumes_its_seq_then_a_new_request_runs", "repeated_sequence",
    }  # fmt: skip
    every = corpus.S2["bridge"]["host_cases"]
    assert len(CASES) + len(STEPPED) == len([c for c in every if not c["name"].startswith("pair")])


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
            for r, rec in (st.get("rids") or {}).items():  # the vector's live records (quota cases)
                rids.record_refusal(
                    device=rec["device"], rid=r, first_ms=rec["until"] - bridge_mod.RID_RETENTION_MS,
                    req_digest="sha256:" + rec["digest"], epoch=st["epoch"], seq=0, kind="refusal", chunk=b"",
                    window=None,
                )  # fmt: skip
            env = bytes.fromhex(case["envelope"])
            dev = env[24:40].hex() if len(env) >= 40 else ""
            b.receive(Item(1, "bridge", dev, case.get("mailbox_id") or env[40:56].hex(), env))
            want = case["expect"]
            if want["result"] == "drop":
                assert post.posts == [] and len(rids.dump()["rid"]) == len(st.get("rids") or {})
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


@pytest.mark.parametrize("case", STEPPED, ids=[c["name"] for c in STEPPED])
def test_host_case_steps(tmp_path, monkeypatch, case):
    st = case["state"]
    clock = [0]

    def fake_verify_cert(cert, pk_pub, now_ms):
        d = next(d for d, c in st["certs"].items() if c["dk_sig_pub"] == cert["dk_sig_pub"])
        return {"device_id": d, "person_id": "00" * 16, "dk_sig_pub": cert["dk_sig_pub"],
                "scopes_max": ["look", "decide", "operate", "type"]}  # fmt: skip

    monkeypatch.setattr(bridge_mod, "verify_cert", fake_verify_cert)
    epochs, host, key, post = _Epochs(st), FakeHost(tmp_path), Key(), Poster()
    keys = BridgeKeys(epochs, bytes.fromhex(st["workspace"]))
    with StateDir(tmp_path / "relay") as sd:
        rids = RidStore(sd)
        try:
            b = Bridge(
                workspace_id=st["workspace"], rids=rids, keys=keys,
                signer=sign.RelaySigner(key, sign.LinkContext(st["workspace"], "https://relay.example"), epochs=keys),
                r6=_R6(st), port=host, job=lambda fn: fn(), post=post, now_ms=lambda: clock[0],
            )  # fmt: skip
            stored: dict[str, bytes] = {}
            for i, step in enumerate(case["steps"]):
                clock[0] = step["now_ms"]
                env = bytes.fromhex(step["envelope"])
                n = len(post.posts)
                b.receive(Item(i + 1, "bridge", env[24:40].hex(), step["mailbox_id"], env))
                got = [e for _, _, e in post.posts[n:]]
                want = step["expect"]
                if want["result"] == "refuse" and want["code"] == "already_done" and step["mailbox_id"] in stored:
                    assert got == [stored[step["mailbox_id"]]]  # core: the stored answer again, byte for byte
                    continue
                (chunk,) = got
                stored[step["mailbox_id"]] = chunk
                _h, meta, _ = open_chunk(chunk, epochs.keys, key.public_key)
                if want["result"] == "refuse":
                    fields = {k: v for k, v in want.items() if k not in ("result", "code")}
                    assert meta == {"refusal": want["code"], **fields}
                else:
                    assert want["result"] == "accept" and meta == {"refusal": "forbidden_scope"}
        finally:
            rids.close()
