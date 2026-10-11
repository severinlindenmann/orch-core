"""The F1 checkpoint vectors (ticket-format §5.10) against ``orch.store.checkpoints``: the §2.4 shape and the signed
bytes, forged variants, a lower seq (or n) and the same seq with another head refused (``chain.diverged``), a log
that disagrees with a checkpoint is diverged, and a workspace restore re-appends the PK-signed revocations."""

import json
from pathlib import Path

import pytest

from orch import canon, crypto, schema
from orch.store import checkpoints as cps
from orch.store.logs import WORKSPACE, LogInfo
from tests.model.f1_runner import run_scenario
from tests.store.helpers import Env

DIR = Path(__file__).parent.parent / "vectors" / "f1"
C = json.loads((DIR / "checkpoint.json").read_text(encoding="utf-8"))
W = C["workspace_id"]
WSK = crypto.unb64u(C["wsk_pub"], crypto.PUB_LEN)
UID = C["ticket_log"]["uid"]
KEYS = json.loads((DIR / "signatures.json").read_text(encoding="utf-8"))["keys"]
SIGNER = crypto.private_key_from_scalar(int(KEYS["wsk"]["scalar_hex"], 16))


def sign(msg: bytes) -> bytes:
    return crypto.sign(SIGNER, msg)


def test_the_published_wsk_is_the_vector_key():
    assert crypto.public_bytes(SIGNER) == WSK


@pytest.mark.parametrize("which", ["ticket_checkpoint", "workspace_checkpoint"])
def test_signed_bytes_shape_and_signature(which):
    cp = C[which]["signed"]
    schema.validate("checkpoint", cp)
    assert (cps.LABEL + canon.cj_checked(cp["o"])).hex() == C[which]["signing_hex"]
    assert cps.verify_object(WSK, cp)
    assert cp["o"]["kind"] == which and cp["o"]["v"] == 2 and cp["o"]["suite"] == 2
    assert crypto.unb64u(cp["sig"], crypto.SIG_LEN)


def test_the_checkpoint_names_the_log_heads_and_the_genesis():
    t, w = C["ticket_checkpoint"]["signed"]["o"], C["workspace_checkpoint"]["signed"]["o"]
    assert t["head"] == C["ticket_log"]["heads"][t["seq"] - 1]
    assert w["workspace_log"]["head"] == C["workspace_log"]["heads"][w["workspace_log"]["seq"] - 1]
    assert w["tickets"] == {UID: {"seq": t["seq"], "head": t["head"]}} and w["genesis"] == C["genesis"] and w["n"] == 1


@pytest.mark.parametrize("c", C["forged"], ids=lambda c: c["name"])
def test_forged_checkpoints_do_not_verify(c):
    assert not cps.verify_object(WSK, c["checkpoint"])


def _store(tmp_path, ticket=True, workspace=True):
    cp = cps.Checkpoints(tmp_path, W)
    if ticket:
        cp._write(cp._ticket_path(UID), C["ticket_checkpoint"]["signed"])
    if workspace:
        cp._write(cp._workspace_path, C["workspace_checkpoint"]["signed"])
    return cp


@pytest.mark.parametrize("offer", C["ticket_offers"], ids=lambda o: o["name"])
def test_ticket_checkpoint_offers(tmp_path, offer):
    cp = _store(tmp_path)
    r = cp.write_ticket(sign, UID, offer["seq"], offer["head"], "2026-10-10T10:05:00Z")
    stored = cp.ticket(UID)
    assert cps.verify_object(WSK, stored)
    if offer["expect"] == "ok":
        assert r is None and (stored["o"]["seq"], stored["o"]["head"]) == (offer["seq"], offer["head"])
    else:
        assert offer["expect"] == "chain.diverged" and r == "diverged"
        assert stored == C["ticket_checkpoint"]["signed"]  # nothing was written


def _logs(heads_ws, heads_t=None):
    out = {WORKSPACE: LogInfo(WORKSPACE, Path("w"), seq=len(heads_ws), heads=list(heads_ws))}
    if heads_t is not None:
        out[UID] = LogInfo(UID, Path("t"), seq=len(heads_t), heads=list(heads_t))
    return out


@pytest.mark.parametrize("offer", C["workspace_offers"], ids=lambda o: o["name"])
def test_workspace_checkpoint_offers(tmp_path, offer):
    cp = _store(tmp_path)
    heads = [*C["workspace_log"]["heads"][: offer["log_seq"] - 1], offer["head"]]
    r = cp.write_workspace(sign, C["genesis"], "2026-10-10T10:06:00Z", _logs(heads))
    stored = cp.workspace()
    assert cps.verify_object(WSK, stored)
    if offer["expect"] == "ok":
        assert r is None and stored["o"]["n"] == offer["n"] == C["workspace_checkpoint"]["signed"]["o"]["n"] + 1
    else:
        assert r == "diverged" and stored == C["workspace_checkpoint"]["signed"]


@pytest.mark.parametrize("o", C["workspace_n_offers"], ids=lambda o: o["name"])
def test_n_offers_are_signed_and_judged_by_a_receiver(o):
    """Receivers (the host, the relay, devices) judge a workspace checkpoint against the highest one they hold (§5.10):
    a higher n without a lower seq is accepted, a lower n without a higher seq is ignored, the rest is diverged."""
    assert cps.verify_object(WSK, o["checkpoint"]) and cps.verify_object(WSK, C["workspace_n_held"])
    got = cps.judge_offer(
        C["workspace_n_held"]["o"], o["checkpoint"]["o"], pinned_genesis=C["genesis"], restore=o.get("restore")
    )
    assert got == o["expect"]


def test_a_receiver_accepts_the_first_checkpoint_only_under_the_pinned_genesis():
    o = C["workspace_n_held"]["o"]
    assert cps.judge_offer(None, o, pinned_genesis=C["genesis"]) == "ok"
    assert cps.judge_offer(None, o, pinned_genesis="sha256:" + "00" * 32) == "trust.genesis_mismatch"


def test_the_p1_host_never_writes_a_lower_n(tmp_path):
    held = C["workspace_n_held"]["o"]["n"]
    cp = cps.Checkpoints(tmp_path, W)
    cp._write(cp._workspace_path, C["workspace_n_held"])
    r = cp.write_workspace(sign, C["genesis"], "2026-10-10T10:06:00Z", _logs(C["workspace_log"]["heads"]))
    assert r is None and cp.workspace()["o"]["n"] == held + 1


@pytest.mark.parametrize("d", C["divergence"], ids=lambda d: d["name"])
def test_a_log_that_disagrees_with_a_checkpoint_is_diverged(tmp_path, d):
    cp = _store(tmp_path)
    logs = _logs(C["workspace_log"]["heads"], d["ticket_heads"])
    found = cps.find_divergence(cp, WSK, C["genesis"], logs, exists={UID})
    assert [x.code for x in found if x.log == UID] == d["expect"]
    assert not [x for x in found if x.log == WORKSPACE]


def test_a_checkpoint_of_another_genesis_is_refused(tmp_path):
    cp = _store(tmp_path)
    found = cps.find_divergence(cp, WSK, "sha256:" + "00" * 32, _logs(C["workspace_log"]["heads"]), exists={UID})
    assert [x.code for x in found] == ["trust.genesis_mismatch"]


# --- restore re-appends every PK-signed revocation (§5.10) --------------------------------------------------

R = json.loads((DIR / "restore.json").read_text(encoding="utf-8"))["scenarios"]


@pytest.mark.parametrize("sc", R, ids=lambda s: s["name"])
def test_restore_vectors_through_the_model(sc):
    # ``reader_cannot_see_a_dropped_revocation`` is accepted by a log-only reader (the model) but must be refused by
    # the host (``host_expect``); the host side is test_the_store_re_appends_the_revocation_as_the_vector_says.
    ws, _ = run_scenario(sc)
    assert len(ws) == (6 if sc["reappends"] else 5)
    assert ws[4]["type"] == "restore" and ws[4]["prev"] == canon.event_head(ws[3])
    assert ws[4]["from_seq"] == 4 and ws[4]["head"] == canon.event_head(ws[3])
    if sc["reappends"]:
        assert ws[5]["type"] == "device.revoked" and ws[5]["actor"] == {
            "kind": "host"
        }  # allowed by the embedded revocation, never by a person's say-so


def test_the_store_re_appends_the_revocation_as_the_vector_says(tmp_path):
    import shutil

    from tests.store.test_checkpoints import rollback

    env = Env(tmp_path)
    s = env.bootstrap()
    d2, _ = env.add_device()
    s.close()
    backup = tmp_path / "backup"
    shutil.copytree(env.root, backup)
    s = env.open()
    env.revoke_device(d2, reason="lost")
    person_signed = [e for e in env.read_events("workspace") if e["type"] == "device.revoked"][0]
    s.close()
    rollback(env, backup)
    s = env.open()
    facts = s.restore_facts("workspace")
    s.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="rolled back"), log="workspace")
    evs = env.read_events("workspace")
    vector_host = next(st["event"] for st in R[0]["steps"] if st["event"]["type"] == "device.revoked")
    assert [e["type"] for e in evs[-2:]] == ["restore", "device.revoked"]
    host = evs[-1]
    assert host["actor"] == vector_host["actor"] == {"kind": "host"}
    assert set(host) == set(vector_host)
    assert host["revocation"] == person_signed["revocation"] and host["reason"] == person_signed["reason"] == "lost"
    s.close()
    env.store = None
