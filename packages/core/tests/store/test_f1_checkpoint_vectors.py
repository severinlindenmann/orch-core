"""The F1 checkpoint vectors (ticket-format §5.10) against ``orch.store.checkpoints``: the §2.4 shape and the signed
bytes, forged variants, a lower seq (or n) and the same seq with another head refused (``chain.diverged``), a log
that disagrees with a checkpoint is diverged, and a workspace restore atomically embeds the PK-signed revocations."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from orch import canon, crypto, schema
from orch.store import checkpoints as cps
from orch.store.logs import WORKSPACE, LogInfo
from tests.model.f1_runner import run_scenario
from tests.store.helpers import Env

DIR = Path(__file__).resolve().parents[4] / "contracts" / "core" / "f1"
C = json.loads((DIR / "checkpoint.json").read_text(encoding="utf-8"))
W = C["workspace_id"]
WSK = crypto.unb64u(C["wsk_pub"], crypto.PUB_LEN)
UID = C["ticket_log"]["uid"]
KEYS = json.loads((DIR / "signatures.json").read_text(encoding="utf-8"))["keys"]
GENESIS_DELEGATION = C["transition_cases"][0]["pinned_delegation"]
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
    assert (
        w["tickets"] == {UID: {"seq": t["seq"], "head": t["head"], "acc": t["acc"]}}
        and w["genesis"] == C["genesis"]
        and w["n"] == 1
    )


@pytest.mark.parametrize("c", C["forged"], ids=lambda c: c["name"])
def test_forged_checkpoints_do_not_verify(c):
    assert not cps.verify_object(WSK, c["checkpoint"])


def view(held, now):
    """A receiver view for plain offers: it knows no devices or revocations (explicitly)."""
    return cps.ReceiverView(
        workspace_id=W,
        pinned_genesis=C["genesis"],
        wsk_pub=WSK,
        pinned_delegation=GENESIS_DELEGATION,
        now=now,
        held=held["o"] if held else None,
        known_devices={},
        known_revocations=(),
        local_revocations=(),
        learned_later=(),
    )


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
    r = cp.write_ticket(sign, UID, offer["seq"], offer["head"], "2026-10-10T10:05:00Z", offer["acc"])
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
    cp = o["checkpoint"]  # signed by the WSK even where the content is invalid: the defect is not a signature
    assert crypto.verify(WSK, crypto.unb64u(cp["sig"], crypto.SIG_LEN), cps.LABEL + canon.cj_checked(cp["o"]))
    assert cps.verify_object(WSK, C["workspace_n_held"])
    held = o["held"] if "held" in o else C["workspace_n_held"]
    got = cps.judge_offer(view(held, o["now"]), o["checkpoint"]["o"], proofs=o.get("proofs"))
    assert got == cps.Verdict(o["expect"])


def test_a_receiver_accepts_the_first_checkpoint_only_under_the_pinned_genesis():
    o = C["workspace_n_held"]["o"]
    v = view(None, "2026-10-10T12:00:00Z")
    assert cps.judge_offer(v, o) == cps.Verdict("ok")
    assert cps.judge_offer(replace(v, pinned_genesis="sha256:" + "00" * 32), o).code == "trust.genesis_mismatch"
    assert cps.judge_offer(replace(v, workspace_id="f" * 32), o).code == "chain.diverged"


@pytest.mark.parametrize("missing", ["known_devices", "known_revocations", "local_revocations", "learned_later"])
def test_the_receiver_view_has_no_defaults(missing):
    """Every view field is required (Opus #377 B1): forgetting known_devices or the out-of-band revocations must fail
    loudly, not open."""
    fields = {"known_devices": {}, "known_revocations": (), "local_revocations": (), "learned_later": ()}
    del fields[missing]
    with pytest.raises(TypeError):
        cps.ReceiverView(  # type: ignore[call-arg]
            workspace_id=W, pinned_genesis=C["genesis"], wsk_pub=WSK, pinned_delegation={}, now="2026-10-10T12:00:00Z",
            held=None, **fields,
        )  # fmt: skip


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


# --- restore atomically embeds every PK-signed revocation (§5.10) --------------------------------------------------

R = json.loads((DIR / "restore.json").read_text(encoding="utf-8"))["scenarios"]


@pytest.mark.parametrize("sc", R, ids=lambda s: s["name"])
def test_restore_vectors_through_the_model(sc):
    run_scenario(sc)


@pytest.mark.parametrize("case", C["transition_cases"], ids=lambda c: c["name"])
def test_source_and_accumulator_transitions(case):
    if "host_scenario" in case:
        run_scenario(case["host_scenario"])
    assert cps.verify_object(WSK, case["offered"])
    got = cps.judge_offer(case_view(case), case["offered"]["o"], restore=bundle(case))
    expected_alarms = (case["restore"]["event"]["actor"]["device"],) if case.get("alarm") else ()
    evidence = tuple(case["local_revocations"][i] for i in case["unpublished_evidence"])
    pairs = tuple(tuple(x) for x in case["unpublished_pairs"])
    want = cps.Verdict(case["expect"], case["recorded"], expected_alarms, tuple(case["unpublished"]), pairs, evidence)
    assert got == want
    if case["learned_later_in_walk"]:  # only the receiver's order separates this case from a refusal
        for field in ("known_revocations", "local_revocations"):  # held when judging, in either collection
            earlier = replace(case_view(case), **{field: case["learned_later_in_walk"]}, learned_later=())
            got_earlier = cps.judge_offer(earlier, case["offered"]["o"], restore=bundle(case))
            if case["name"] in OTHER_PERSON_LATER:  # another person's revocation of the signer's device id
                # never refuses as a signer revocation; chain-learned, it is a workspace restore's completeness duty
                duty = field == "known_revocations" and case["restore"]["log"] == WORKSPACE
                assert got_earlier.code == ("chain.diverged" if duty else "ok")
            else:
                assert got_earlier == cps.Verdict("chain.diverged")


OTHER_PERSON_LATER = {
    "cross_person_compromised_learned_later_no_alarm",
    "known_signer_device_other_person_compromised_learned_later_no_alarm",
}


def case_view(case):
    return cps.ReceiverView(
        workspace_id=W,
        pinned_genesis=C["genesis"],
        wsk_pub=WSK,
        pinned_delegation=case["pinned_delegation"],
        now=case["now"],
        held=case["held"]["o"] if case["held"] else None,
        known_devices=case["known_devices"],
        known_revocations=case["known_revocations"],
        local_revocations=case["local_revocations"],
        learned_later=case["learned_later_in_walk"],
    )


def bundle(case):
    return cps.Restore(transition=case["restore"], proofs=case["proofs"], applied=case["applied"])


# Cases whose one defect is a signature; every other refusal must keep both event signatures valid, so that the
# defect it pins is not masked by a broken signature (a receiver that checks only signatures must fail them).
SIGNATURE_DEFECTS = {
    "full_restore_signature_required": ("person", "host"),
    "source_signature_required": ("source",),
    "restore_shape_hash_version": ("person", "host"),  # signed by the oracle; orch builds no bytes for hash_v 2
    "owner_policy_person_sig_by_another_device": ("person",),
    "owner_policy_host_sig_by_another_key": ("host",),
    "owner_policy_actor_device_not_the_cert": ("person",),  # actor.device names no key of the certificate
    "restore_shape_signature_encoding_malformed": ("person",),
}


@pytest.mark.parametrize(
    "case",
    [c for c in C["transition_cases"] if c["expect"] != "ok" and "event" in c["restore"]],
    ids=lambda c: c["name"],
)
def test_receiver_refusals_carry_valid_signatures(case):
    from orch.identity import CryptoVerifier
    from orch.model.verifier import SigContext

    tr, v = case["restore"], CryptoVerifier()
    ev, log = tr["event"], tr["log"]
    broken = SIGNATURE_DEFECTS.get(case["name"], ())
    assert cps.verify_object(WSK, tr["T"])
    assert tr["C"] is None or cps.verify_object(WSK, tr["C"]) == ("source" not in broken)
    assert v.verify_host(ev, log=log, wsk_pub=WSK, workspace_id=W) == ("host" not in broken)
    assert v.verify_person(ev, SigContext(W, log, tr["device_cert"])) == ("person" not in broken)


def test_the_store_carries_the_revocation_as_the_vector_says(tmp_path):
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
    s.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="rollback"), log="workspace")
    evs = env.read_events("workspace")
    assert evs[-1]["type"] == "restore"
    assert evs[-1]["revocations"][0]["revocation"] == person_signed["revocation"]
    assert s.state.workspace.devices[d2].revoked == "lost"
    s.close()
    env.store = None


def test_the_convenience_cases_for_the_relay_auth_kinds():
    """H12d (orch-v2-host §0.8.6 item 9.8): checkpoint.json carries a restore line with auth "convenience" (accepted)
    and one with an unknown auth (refused); the relay vendors them for AUTH_KINDS. Both are judged above."""
    by_name = {c["name"]: c for c in C["transition_cases"]}
    ok, bad = by_name["restore_auth_convenience_accepted"], by_name["restore_auth_unknown_refused"]
    assert (ok["restore"]["event"]["auth"], ok["expect"]) == ("convenience", "ok")
    assert (bad["restore"]["event"]["auth"], bad["expect"]) == ("bogus", "chain.diverged")
