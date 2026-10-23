"""F1 canon/hash vectors (ticket-format §11.5): committed fixtures, the independent oracle, and orch.canon agree."""

import json
from pathlib import Path

import pytest

from orch import canon
from orch.canon import jcs

from ..wsproto import corpus
from . import oracle_f1 as oracle
from . import oracle_f1_gate as oracle_gate

DIR = Path(__file__).parent.parent / "vectors" / "f1"
PROTOCOL_LABELS = corpus.VECTORS["labels"]  # the one vendored copy (tests/wsproto/vectors, xz-compressed)


def load(name: str):
    return json.loads((DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(oracle.all_vectors()))
def test_fixture_files_are_what_the_oracle_produces(name):
    assert open(DIR / name, encoding="utf-8", newline="").read() == oracle.render(oracle.all_vectors()[name])


def test_labels_are_prefix_free_with_the_protocol_labels():
    ours = load("labels.json")["labels"]
    assert ours == canon.LABELS
    theirs = set(PROTOCOL_LABELS.values())
    assert "orch/v2/question|" in theirs  # shared with protocol §13 on purpose, same string
    assert all(v.endswith("|") for v in ours.values())
    for a in ours.values():
        for b in theirs | set(ours.values()):
            assert a == b or not (a.startswith(b) or b.startswith(a)), (a, b)


def test_label_literals():
    # the §5.6 table, spelled out
    assert canon.LABELS["gate"] == "orch/v2/gate|"
    assert canon.LABELS["section"] == "orch/v2/section|"
    assert canon.LABELS["question_id"] == "orch/v2/question-id|"
    assert canon.LABELS["sig_host_event"] == "orch/v2/sig/host-event|"


# --- canonical json -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("v", load("canon.json")["depth"], ids=lambda v: v["name"])
def test_canon_depth_vectors(v):
    if v["ok"]:
        assert jcs.dumps(jcs.loads_strict(v["text"])).decode() == v["canonical"]
    else:
        with pytest.raises(jcs.JcsError):
            jcs.loads_strict(v["text"])


# --- text ---------------------------------------------------------------------------------------------------


T = load("text.json")


def test_vector_unicode_version():
    assert T["unicode_version"] == canon.UNICODE_VERSION == "16.0.0"


@pytest.mark.parametrize("v", T["normalize"], ids=lambda v: v["name"])
def test_text_normalize(v):
    assert canon.normalize_text(v["input"]) == v["output"]
    assert canon.check_text(v["output"]) == v["output"]


@pytest.mark.parametrize("v", T["refused"], ids=lambda v: v["name"])
def test_text_refused(v):
    with pytest.raises(canon.TextError):
        canon.normalize_text(v["input"])
    with pytest.raises(canon.TextError):
        canon.check_text(v["input"])
    with pytest.raises(canon.HashError):
        canon.section_hash(v["input"])


@pytest.mark.parametrize("v", T["refused_not_normalised"], ids=lambda v: v["name"])
def test_check_text_refuses_what_normalize_accepts(v):
    assert canon.normalize_text(v["input"])
    with pytest.raises(canon.TextError):
        canon.check_text(v["input"])


@pytest.mark.parametrize("v", T["kept_invisible"], ids=lambda v: v["name"])
def test_invisible_characters_are_kept(v):
    assert canon.normalize_text(v["input"]) == v["input"]


# --- hashes -------------------------------------------------------------------------------------------------

H = load("hashes.json")


@pytest.mark.parametrize("v", H["artifact_digest"], ids=lambda v: v["bytes_hex"] or "empty")
def test_artifact_digest(v):
    assert canon.artifact_digest(bytes.fromhex(v["bytes_hex"])) == v["hash"]


@pytest.mark.parametrize("v", H["section_hash"], ids=range(len(H["section_hash"])))
def test_section_hash(v):
    assert canon.section_hash(v["text"]) == v["hash"]


@pytest.mark.parametrize("text", H["section_hash_refused"])
def test_section_hash_refuses(text):
    with pytest.raises(canon.HashError):
        canon.section_hash(text)


@pytest.mark.parametrize("v", H["value_hash_refused"], ids=range(len(H["value_hash_refused"])))
def test_value_hash_refused(v):
    with pytest.raises(canon.HashError):
        canon.value_hash(v)


@pytest.mark.parametrize("v", H["question_hash_refused"], ids=lambda v: v["name"])
def test_question_hash_refused(v):
    with pytest.raises(canon.HashError):
        canon.question_hash("a" * 32, oracle.UID, v["question"])


@pytest.mark.parametrize("secret_hex", H["grant_secret_hash_refused"])
def test_grant_secret_hash_refused(secret_hex):
    with pytest.raises(canon.HashError):
        canon.grant_secret_hash(bytes.fromhex(secret_hex))


@pytest.mark.parametrize("v", H["value_hash"], ids=range(len(H["value_hash"])))
def test_value_hash(v):
    assert canon.value_hash(v["value"]) == v["hash"]


@pytest.mark.parametrize("v", H["policy_hash"], ids=lambda v: v["gate"])
def test_policy_hash(v):
    assert canon.policy_hash(v["gate"], v["policy"]) == v["hash"]


@pytest.mark.parametrize("v", H["policy_hash_refused"], ids=lambda v: v["why"])
def test_policy_hash_refuses_a_policy_without_canonical_form(v):
    with pytest.raises(canon.HashError):
        canon.policy_hash(v["gate"], v["policy"])


@pytest.mark.parametrize("v", H["people_hash"], ids=range(len(H["people_hash"])))
def test_people_hash(v):
    assert canon.people_hash(v["people"]) == v["hash"]


@pytest.mark.parametrize("v", H["question_id"], ids=lambda v: v["question"])
def test_question_id(v):
    assert canon.question_id(v["workspace_id"], v["ticket"], v["question"]) == v["qid"]


@pytest.mark.parametrize("v", H["question_hash"], ids=lambda v: v["name"])
def test_question_hash(v):
    assert canon.question_hash(v["qid"], v["ticket"], v["question"]) == v["hash"]


@pytest.mark.parametrize("v", H["grant_secret_hash"], ids=range(len(H["grant_secret_hash"])))
def test_grant_secret_hash(v):
    assert canon.grant_secret_hash(bytes.fromhex(v["secret_hex"])) == v["hash"]


# --- gate hash ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("v", load("gate_hash.json")["gate_hash"], ids=lambda v: v["name"])
def test_gate_hash_vectors(v):
    assert canon.gate_hash(v["G"]) == v["hash"]
    assert jcs.dumps(v["G"]).hex() == v["cj_hex"]
    assert len(v["G"]) == 15


# --- chain and signed bytes ---------------------------------------------------------------------------------

C = load("chain.json")


def test_chain_heads_and_prev():
    evs = C["events"]
    assert [canon.event_head(e) for e in evs] == C["heads"]
    assert canon.check_chain(evs) == C["heads"]
    assert canon.log_head(evs) == C["log_head"] == C["heads"][-1]
    assert canon.log_head([]) is None
    assert evs[0]["prev"] is None and evs[1]["prev"] == C["heads"][0] and evs[2]["prev"] == C["heads"][1]


def test_chain_lines():
    for e, line_hex in zip(C["events"], C["lines_hex"], strict=True):
        line = bytes.fromhex(line_hex)
        assert canon.event_line(e) == line
        assert canon.parse_event_line(line) == e


def test_tampered_event_changes_head_and_breaks_the_chain():
    evs = [dict(e) for e in C["events"]]
    evs[1]["gate"] = "plan"
    assert canon.event_head(evs[1]) == C["tampered_event_head"] != C["heads"][1]
    with pytest.raises(canon.ChainError) as ei:
        canon.check_chain(evs)
    assert ei.value.seq == 3


@pytest.mark.parametrize("line_hex", C["non_cj_lines"], ids=range(len(C["non_cj_lines"])))
def test_non_cj_lines_are_refused(line_hex):
    with pytest.raises(canon.HashError):
        canon.parse_event_line(bytes.fromhex(line_hex))


def test_host_signing_bytes():
    for e, expected in zip(C["events"], C["host_signing_hex"], strict=True):
        assert canon.host_signing_bytes(C["workspace_id"], C["log"], e).hex() == expected
        # host_sig is not covered; everything else is, so the sig field is
        assert canon.host_signing_bytes(C["workspace_id"], C["log"], {**e, "host_sig": "z"}).hex() == expected


def test_person_signing_bytes_ticket_and_workspace():
    e = C["events"][1]
    assert canon.person_signing_bytes(C["workspace_id"], C["log"], e).hex() == C["ticket_event_signing_hex"]
    ws = C["ws_event"]
    assert canon.person_signing_bytes(C["workspace_id"], "workspace", ws).hex() == C["ws_event_signing_hex"]
    full_ws = {**ws, "seq": 1, "at": "2026-10-09T09:00:00Z", "prev": None, "ws_seq": 1}
    assert canon.host_signing_bytes(C["workspace_id"], "workspace", full_ws).hex() == C["ws_log_host_signing_hex"]


def test_signing_bytes_do_not_replay():
    e = C["events"][1]
    a = canon.person_signing_bytes(C["workspace_id"], C["log"], e)
    assert a != canon.person_signing_bytes(C["workspace_id"], oracle.UID2, e)
    assert a != canon.person_signing_bytes("f" * 32, C["log"], e)
    assert a != canon.host_signing_bytes(C["workspace_id"], C["log"], {**e, "host_sig": "x"})
    # seq, at, prev, ws_seq, sig and host_sig are not covered by the person's signature
    changed = {**e, "seq": 9, "at": "2030-01-01T00:00:00Z", "prev": None, "ws_seq": 7, "sig": "Z", "host_sig": "Y"}
    assert canon.person_signing_bytes(C["workspace_id"], C["log"], changed) == a
    # but the payload, actor and gate_gen are
    for k, v in (("gate_gen", 1), ("auth", "webauthn"), ("hash", "sha256:" + "00" * 32), ("roster_v", 2)):
        assert canon.person_signing_bytes(C["workspace_id"], C["log"], {**e, k: v}) != a


# --- repo identity ------------------------------------------------------------------------------------------

R = load("repo_identity.json")


@pytest.mark.parametrize("v", R["ok"])
def test_repo_identity_ok(v):
    assert canon.check_repo_identity(v) == v


@pytest.mark.parametrize("v", R["refused"], ids=range(len(R["refused"])))
def test_repo_identity_refused(v):
    with pytest.raises(canon.HashError):
        canon.check_repo_identity(v)


def test_same_repo_identity_ignores_ascii_case_only():
    for a, b in R["same"]:
        assert canon.same_repo_identity(a, b)
    assert not canon.same_repo_identity("https://github.com/a/x", "https://github.com/a/y")
    # the hashed value keeps the raw form: two case variants hash differently and the source list refuses both
    t = oracle_gate.sample_ticket()
    g = oracle_gate.derive_G(t, "verify")
    g["source_sha"] = [dict(g["source_sha"][0], repo=a) for a in R["same"][0]]
    with pytest.raises(canon.HashError):
        canon.gate_hash(g)


def test_repo_userinfo_decision_defers_to_the_operative_rule():
    spec = DIR.parents[4] / "docs" / "architecture" / "orch-v2-ticket-format.md"
    row = next(line for line in spec.read_text(encoding="utf-8").splitlines() if line.startswith("| 116 |"))
    assert "Only ssh/scp `git@` userinfo is allowed and removed" in row
    assert "all https userinfo" in row and "`local:<name>`" in row and "§5.7" in row


@pytest.mark.parametrize("v", load("repo_paths.json")["cases"], ids=lambda v: v["name"])
def test_repo_paths_after_input_normalization(v):
    from orch.schema import SchemaError, validate_settings

    paths = [canon.normalize_text(path) for path in v["input"]]
    assert paths == v["normalized"]
    settings = {"repos": {f"repo{i}": {"path": path} for i, path in enumerate(paths)}}
    if v["collision"]:
        with pytest.raises(SchemaError, match="two repos resolve to the same path"):
            validate_settings(settings)
    else:
        validate_settings(settings)


# --- phone_events.json (orch-v2-host §11A.14, H12e) ---------------------------------------------------------------

PH = load("phone_events.json")


@pytest.mark.parametrize("v", PH["accepted"], ids=lambda v: v["name"])
def test_phone_event_signing_bytes_digest_sig_and_head(v):
    import hashlib

    from orch import crypto
    from orch.host.admission import signed_digest

    sub, ev = v["submitted"], v["event"]
    assert not {"seq", "at", "prev", "ws_seq", "host_sig"} & set(sub) and "sig" in sub
    assert jcs.dumps(sub).decode() == v["submitted_cj"]
    assert {k: ev[k] for k in sub} == sub  # what lands is what the phone signed, plus the host's stamp
    signing = canon.person_signing_bytes(PH["workspace_id"], v["log"], sub)
    assert signing.hex() == v["person_signing_hex"]
    assert signing.startswith(b"orch/v2/sig/ticket-event|")
    assert (
        v["digest"]
        == "sha256:" + hashlib.sha256(signing).hexdigest()
        == signed_digest(PH["workspace_id"], v["log"], ev)
    )
    pub = crypto.unb64u(PH["keys"]["dk_sevp1"]["pub_b64u"])
    assert crypto.verify(pub, crypto.unb64u(v["sig"], 64), signing)
    assert canon.event_head(ev) == v["head"]
    assert sub["actor"] == {"kind": "person", "id": PH["phone"]["person"], "device": PH["phone"]["device"]}
    assert sub["auth"] in PH["phone"]["column_r_auth"]
    if sub["type"] == "ticket.created":
        assert sub["based_on"] is None and ev["seq"] == 1
    if "question" in v:
        q = v["question"]
        h = q["hashed"]
        assert canon.question_id(PH["workspace_id"], h["ticket"], sub["question"]) == q["qid"] == h["question_id"]
        assert canon.question_hash(q["qid"], h["ticket"], q["raw"]) == q["hash"] == sub["hash"]
        assert canon.question_spec(q["raw"]) == h["spec"] and h["options"] == q["raw"].get("options", [])


def test_phone_events_cover_the_vector_list_of_11a14():
    names = {v["name"] for v in PH["accepted"]}
    assert {"answer_non_ascii_option_with_cost", "answer_text_to_options_absent_with_tab", "creation",
            "answer_passphrase", "answer_high_s", "delayed_answer_after_a_b_a"} <= names  # fmt: skip
    codes = {v["code"] for v in PH["refused"]}
    assert codes == {"question.stale", "members.stale", "event.bad_base", "sig.invalid", "device.scope",
                     "event.duplicate_id", "ticket.exists"}  # fmt: skip
    auths = {v["submitted"]["auth"] for v in PH["accepted"]}
    assert auths == {"secure-enclave-unlocked", "passphrase"}
    high = next(v for v in PH["accepted"] if v["name"] == "answer_high_s")
    s = int.from_bytes(__import__("base64").urlsafe_b64decode(high["sig"] + "==")[32:], "big")
    assert s > 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551 // 2
    costed = next(v for v in PH["accepted"] if v["name"] == "answer_non_ascii_option_with_cost")
    assert any("cost" in o for o in costed["question"]["hashed"]["options"])
    assert any(ord(c) > 127 for c in costed["question"]["hashed"]["text"])
    bare = next(v for v in PH["accepted"] if v["name"] == "answer_text_to_options_absent_with_tab")
    assert bare["question"]["hashed"]["options"] == [] and "\t" in bare["submitted"]["text"]
