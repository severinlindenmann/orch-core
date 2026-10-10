"""F1 canon/hash vectors (ticket-format §11.5): committed fixtures, the independent oracle, and orch.canon agree."""

import json
from pathlib import Path

import pytest

from orch import canon
from orch.canon import jcs

from . import oracle_f1 as oracle

DIR = Path(__file__).parent.parent / "vectors" / "f1"
PROTOCOL_LABELS = json.loads((DIR.parent / "vectors_v2.json").read_text(encoding="utf-8"))["labels"]


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


# --- canonical json -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("v", load("canon.json")["depth"], ids=lambda v: v["name"])
def test_canon_depth_vectors(v):
    if v["ok"]:
        assert jcs.dumps(jcs.loads_strict(v["text"])).decode() == v["canonical"]
    else:
        with pytest.raises(jcs.JcsError):
            jcs.loads_strict(v["text"])


# --- text -----------------------------------------------------------------------------------------------------


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


# --- hashes ---------------------------------------------------------------------------------------------------

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


@pytest.mark.parametrize("v", H["question_hash_refused"], ids=range(len(H["question_hash_refused"])))
def test_question_hash_refused(v):
    with pytest.raises(canon.HashError):
        canon.question_hash("a" * 32, oracle.UID, v["text"], v["options"])


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


@pytest.mark.parametrize("v", H["people_hash"], ids=range(len(H["people_hash"])))
def test_people_hash(v):
    assert canon.people_hash(v["people"]) == v["hash"]


@pytest.mark.parametrize("v", H["question_id"], ids=lambda v: v["question"])
def test_question_id(v):
    assert canon.question_id(v["workspace_id"], v["ticket"], v["question"]) == v["qid"]


@pytest.mark.parametrize("v", H["question_hash"], ids=range(len(H["question_hash"])))
def test_question_hash(v):
    assert canon.question_hash(v["qid"], v["ticket"], v["text"], v["options"]) == v["hash"]


@pytest.mark.parametrize("v", H["grant_secret_hash"], ids=range(len(H["grant_secret_hash"])))
def test_grant_secret_hash(v):
    assert canon.grant_secret_hash(bytes.fromhex(v["secret_hex"])) == v["hash"]


# --- gate hash ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("v", load("gate_hash.json")["gate_hash"], ids=lambda v: v["name"])
def test_gate_hash_vectors(v):
    assert canon.gate_hash(v["G"]) == v["hash"]
    assert jcs.dumps(v["G"]).hex() == v["cj_hex"]
    assert len(v["G"]) == 15


# --- chain and signed bytes -----------------------------------------------------------------------------------

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


# --- repo identity --------------------------------------------------------------------------------------------

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
    g = oracle.gate_inputs()["verify"]
    g["source_sha"] = [dict(g["source_sha"][0], repo=a) for a in R["same"][0]]
    with pytest.raises(canon.HashError):
        canon.gate_hash(g)
