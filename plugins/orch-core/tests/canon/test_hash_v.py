"""Hash forms, labels, the gate hash and its validation, policy/people/question hashes (ticket-format §5.6, §5.7).

The fixtures in tests/vectors/f1 are checked in test_f1_vectors.py; this file pins literal digests and probes
the refusals.
"""

import copy
import hashlib

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from orch import canon
from orch.canon import HashError

from . import oracle_f1 as oracle

ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
GOOD = "sha256:" + "ab" * 32


# --- forms and literal known answers ------------------------------------------------------------------------


def test_literal_known_answers():
    # computed independently with hashlib, pinned as literals
    assert canon.artifact_digest(b"abc") == "sha256:" + ABC
    assert canon.section_hash("") == "sha256:513b24f990af0ba5f96cae1a0d7f0956a1c3e505c83d8ad833c41472a7a0369a"
    assert canon.section_hash("caf\u00e9\nline  ") == oracle.h("section", "caf\u00e9\nline  ".encode())
    assert (
        canon.grant_secret_hash(bytes(32)) == "sha256:8380c7dc19efbb04e8d07e16da1850dda54145b814fdbaa21024dfd8d07d3c0c"
    )
    assert canon.question_id(oracle.W, oracle.UID, "Q1") == "d60478ec5ff8f6ff213cded9c0f8f243"
    g = {x["name"]: x for x in oracle.gate_vectors()["gate_hash"]}
    assert canon.gate_hash(g["requirements"]["G"]) == (
        "sha256:f9edf87ff4a72af07aafb01ae69179cbf2a57f0490749d639a9981ec0cae21c5"
    )
    assert canon.gate_hash(g["code"]["G"]) == "sha256:16be57bdbf53706b3d3a0b274274a5b66b0362588de645afb749c07fc7e4bcf4"


def test_section_text_with_json_sensitive_characters_hashes_raw_utf8():
    # U+2028, an astral character and a quote: the section hash is over UTF-8, not over any JSON escaping
    t = 'x\u2028y \U0001f600 "q" \\'
    assert canon.section_hash(t) == "sha256:" + hashlib.sha256(b"orch/v2/section|" + t.encode()).hexdigest()


def test_format_and_parse_hash():
    assert canon.format_hash(ABC) == "sha256:" + ABC
    assert canon.parse_hash("sha256:" + ABC) == bytes.fromhex(ABC)
    for bad in [
        "",
        ABC,
        "sha256:" + ABC.upper(),
        "sha256:" + ABC[:-1],
        "sha256:" + ABC + "0",
        "SHA256:" + ABC,
        "sha256:" + ABC + "\n",
        " sha256:" + ABC,
        None,
        1,
        b"sha256:" + ABC.encode(),
    ]:
        with pytest.raises(HashError):
            canon.parse_hash(bad)  # type: ignore[arg-type]
    for bad in ["", ABC.upper(), ABC[:-1], "sha256:" + ABC, None]:
        with pytest.raises(HashError):
            canon.format_hash(bad)  # type: ignore[arg-type]


def test_check_hash_v():
    assert canon.check_hash_v(1) == 1
    for bad in (0, 2, True, 1.0, "1", None, -1):
        with pytest.raises(HashError):
            canon.check_hash_v(bad)


def test_domain_labels_separate_every_kind():
    # the same payload under different labels gives different hashes; the artifact digest is unlabelled
    assert canon.value_hash("x") != canon.section_hash("x")
    assert canon.section_hash('"x"') != canon.value_hash("x")
    sec = canon.section_hash("")
    assert sec != canon.artifact_digest(b"")
    # a section whose text is the cj of a gate payload no longer equals the gate hash (security review 2)
    g = oracle.gate_inputs()["code"]
    assert canon.section_hash(canon.dumps(g).decode()) != canon.gate_hash(g)


def test_hash_helpers_reject_wrong_types():
    for fn, arg in [
        (canon.artifact_digest, "text"),
        (canon.grant_secret_hash, b""),
        (canon.grant_secret_hash, "abc"),
        (canon.section_hash, b"x"),
        (canon.section_hash, None),
    ]:
        with pytest.raises(HashError):
            fn(arg)  # type: ignore[arg-type]


def test_refuses_non_normalised_everywhere():
    nfd = "cafe\u0301"
    with pytest.raises(HashError):
        canon.section_hash(nfd)
    with pytest.raises(HashError):
        canon.value_hash({"k": [nfd]})
    with pytest.raises(HashError):
        canon.value_hash("a\r\nb")
    with pytest.raises(HashError):
        canon.question_hash("a" * 32, oracle.UID, nfd, [])
    g = oracle.gate_inputs()["requirements"]
    g["fields"]["acceptance"][0]["text"] = nfd
    with pytest.raises(HashError):
        canon.gate_hash(g)
    ev = copy.deepcopy(oracle.chain_events()[2])
    ev["text"] = nfd
    for fn in (canon.event_head, canon.event_line):
        with pytest.raises(HashError):
            fn(ev)
    with pytest.raises(HashError):
        canon.host_signing_bytes(oracle.W, oracle.UID, ev)


def test_cj_errors_are_hash_errors():
    for bad in (
        {"a": 1.5},
        {"a": "\ud800"},
        {"": 1},
        {"\u00e4": 1},
        [[[[[[[[[[[[[[[[[1]]]]]]]]]]]]]]]]],
        2**53,
        object(),
    ):
        with pytest.raises(HashError):
            canon.value_hash(bad)
    a: list = []
    a.append(a)
    with pytest.raises(HashError):
        canon.value_hash(a)


# --- policy, people, question -------------------------------------------------------------------------------

POLICY = {"approvers": ["owner", "maintainer"], "count": 1, "not": [], "applies": "all", "independent": False}


def test_policy_hash_canonical_form():
    h = canon.policy_hash("requirements", POLICY)
    assert canon.policy_hash("requirements", {**POLICY, "approvers": ["maintainer", "owner", "owner"]}) == h
    assert canon.policy_hash("plan", POLICY) != h  # the gate name is hashed
    assert canon.policy_hash("code", {**POLICY, "applies": ["bug", "feature", "bug"]}) == canon.policy_hash(
        "code", {**POLICY, "applies": ["feature", "bug"]}
    )


@pytest.mark.parametrize(
    "mutate",
    [
        {"approvers": []},
        {"approvers": ["root"]},
        {"approvers": "owner"},
        {"count": 0},
        {"count": True},
        {"count": 1.0},
        {"not": ["nobody"]},
        {"applies": "some"},
        {"applies": []},
        {"applies": ["feature", "story"]},
        {"independent": 1},
        {"independent": None},
    ],
    ids=lambda m: str(m),
)
def test_policy_hash_refuses(mutate):
    with pytest.raises(HashError):
        canon.policy_hash("verify", {**POLICY, **mutate})


def test_policy_hash_refuses_shape():
    for bad in (None, [], {k: v for k, v in POLICY.items() if k != "not"}, {**POLICY, "extra": 1}):
        with pytest.raises(HashError):
            canon.policy_hash("verify", bad)  # type: ignore[arg-type]
    with pytest.raises(HashError):
        canon.policy_hash("deploy", POLICY)


def test_people_hash():
    sev, mara = oracle.P_SEV, oracle.P_MARA
    h = canon.people_hash({"ticket_owner": sev, "assignees": [sev, mara]})
    assert canon.people_hash({"assignees": [mara, sev, mara], "ticket_owner": sev}) == h
    assert canon.people_hash({"ticket_owner": None}) != h
    for bad in (
        {"owner": sev},
        {"ticket_owner": "p_sev"},
        {"ticket_owner": [sev]},
        {"assignees": sev},
        {"assignees": ["x"]},
        {"viewers": []},
        [],
        None,
    ):
        with pytest.raises(HashError):
            canon.people_hash(bad)  # type: ignore[arg-type]


def test_question_forms():
    qid = canon.question_id(oracle.W, oracle.UID, "Q1")
    assert len(qid) == 32 and qid == qid.lower()
    assert canon.question_id(oracle.W, oracle.UID, "Q2") != qid
    assert canon.question_id(oracle.W, oracle.UID2, "Q1") != qid
    for args in [
        ("x", oracle.UID, "Q1"),
        (oracle.W, "uid", "Q1"),
        (oracle.W, oracle.UID, "q1"),
        (oracle.W, oracle.UID, "Q0"),
    ]:
        with pytest.raises(HashError):
            canon.question_id(*args)
    assert canon.question_hash(qid, oracle.UID, "t", None) == canon.question_hash(qid, oracle.UID, "t", [])
    for opts in ([{"key": "a"}], [{"key": "a", "label": "b", "extra": "c"}], [{"key": "a", "label": 1}], [1], "x"):
        with pytest.raises(HashError):
            canon.question_hash(qid, oracle.UID, "t", opts)  # type: ignore[arg-type]
    with pytest.raises(HashError):
        canon.question_hash("abc", oracle.UID, "t", [])


# --- gate hash validation -----------------------------------------------------------------------------------


def _g(gate="requirements"):
    return copy.deepcopy(oracle.gate_inputs()[gate])


def test_gate_keys_are_the_fifteen():
    assert len(canon.GATE_KEYS) == 15 and len(set(canon.GATE_KEYS)) == 15
    assert set(_g()) == set(canon.GATE_KEYS)
    for gate in canon.GATES:
        assert set(_g(gate)) == set(canon.GATE_KEYS)


def test_gate_hash_needs_every_key_and_no_extra():
    for k in canon.GATE_KEYS:
        g = _g()
        del g[k]
        with pytest.raises(HashError):
            canon.gate_hash(g)
    g = _g()
    g["extra"] = 1
    with pytest.raises(HashError):
        canon.gate_hash(g)
    for bad in (None, [], "x"):
        with pytest.raises(HashError):
            canon.gate_hash(bad)  # type: ignore[arg-type]


def test_gate_hash_depends_on_every_input():
    for gate in canon.GATES:
        base = canon.gate_hash(_g(gate))
        g = _g(gate)
        g["hash_v"] = 1
        g["policy_hash"] = GOOD
        assert canon.gate_hash(g) != base
        g = _g(gate)
        g["people_hash"] = GOOD
        assert canon.gate_hash(g) != base
        g = _g(gate)
        g["prior"] = {"requirements": {"gen": 3, "approvals": []}} if gate != "requirements" else {}
        if gate != "requirements":
            assert canon.gate_hash(g) != base
    g = _g("plan")
    g["tasks"][0]["text"] = "changed"
    assert canon.gate_hash(g) != canon.gate_hash(_g("plan"))
    g = _g("requirements")
    g["artifacts"]["mock.png"]["digest"] = GOOD
    assert canon.gate_hash(g) != canon.gate_hash(_g("requirements"))


def _set(path, value):
    def apply(g):
        node = g
        for p in path[:-1]:
            node = node[p]
        node[path[-1]] = value

    return apply


BAD_GATE_INPUTS = {
    "workspace_id upper": ("requirements", _set(["workspace_id"], "A" * 32)),
    "workspace_id short": ("requirements", _set(["workspace_id"], "ab")),
    "uid": ("requirements", _set(["uid"], "uid")),
    "gate unknown": ("requirements", _set(["gate"], "deploy")),
    "schema int": ("requirements", _set(["schema"], 2)),
    "schema str": ("requirements", _set(["schema"], "orch.ticket/1")),
    "hash_v 2": ("requirements", _set(["hash_v"], 2)),
    "hash_v true": ("requirements", _set(["hash_v"], True)),
    "section other gate": ("requirements", _set(["sections", "plan"], GOOD)),
    "section ascii only": ("requirements", _set(["sections", "L\u00f6sung.x"], GOOD)),
    "section non-ascii core": ("requirements", _set(["sections", "r\u00e9sum\u00e9"], GOOD)),
    "section hash bare hex": ("requirements", _set(["sections", "summary"], "ab" * 32)),
    "section hash not hash": ("requirements", _set(["sections", "summary"], "x")),
    "code with sections": ("code", _set(["sections", "plan"], GOOD)),
    "ticket_type": ("requirements", _set(["fields", "ticket_type"], "story")),
    "size": ("requirements", _set(["fields", "size"], "XL")),
    "ac id": ("requirements", _set(["fields", "acceptance", 0, "id"], "ac1")),
    "ac extra": ("requirements", _set(["fields", "acceptance", 0, "x"], 1)),
    "ac duplicate": ("requirements", _set(["fields", "acceptance", 1, "id"], "AC1")),
    "links on requirements": ("requirements", _set(["fields", "links"], {})),
    "links null on verify": ("verify", _set(["fields", "links"], None)),
    "links key missing": ("verify", lambda g: g["fields"]["links"].pop("prs")),
    "fields extra": ("requirements", _set(["fields", "type"], "feature")),
    "addons nested": ("requirements", _set(["fields", "addons"], {"Est": {"a": 1}})),
    "addon float": ("requirements", _set(["fields", "addons"], {"est": {"a": 1.5}})),
    "addon_packages bare": ("requirements", _set(["addon_packages", "est"], "ab" * 32)),
    "tasks on verify": ("verify", _set(["tasks"], [{"id": "T1", "text": "x", "verify": None, "proves": []}])),
    "task id": ("plan", _set(["tasks", 0, "id"], "T0")),
    "task dup": ("plan", _set(["tasks", 1, "id"], "T1")),
    "task extra": ("plan", _set(["tasks", 0, "assignee"], oracle.P_SEV)),
    "task verify shape": ("plan", _set(["tasks", 0, "verify"], "ls")),
    "task proves": ("plan", _set(["tasks", 1, "proves"], ["ac1"])),
    "artifact kind": ("requirements", _set(["artifacts", "mock.png", "kind"], "image")),
    "artifact digest bare": ("requirements", _set(["artifacts", "mock.png", "digest"], "ab" * 32)),
    "artifact ac": ("requirements", _set(["artifacts", "mock.png", "ac"], "AC0")),
    "artifact missing key": ("requirements", lambda g: g["artifacts"]["mock.png"].pop("task")),
    "artifacts on code": ("code", _set(["artifacts", "a"], {"kind": "log", "digest": GOOD, "ac": None, "task": None})),
    "receipts on code": ("code", _set(["receipts", "T1"], {"event": "x", "repo": "r", "commit": "a" * 40, "exit": 0})),
    "receipt commit short": ("verify", _set(["receipts", "T2", "commit"], "b7e1f02")),
    "receipt exit bool": ("verify", _set(["receipts", "T2", "exit"], False)),
    "receipt task id": ("verify", lambda g: g["receipts"].update({"t2": g["receipts"].pop("T2")})),
    "source_sha on plan": ("plan", _set(["source_sha"], [{"repo": "r", "ref": "refs/heads/a", "sha": "a" * 40}])),
    "source ref": ("verify", _set(["source_sha", 0, "ref"], "main")),
    "source ref empty branch": ("verify", _set(["source_sha", 0, "ref"], "refs/heads/")),
    "source sha upper": ("verify", _set(["source_sha", 0, "sha"], "A" * 40)),
    "source unsorted": (
        "verify",
        _set(
            ["source_sha"],
            [
                {"repo": "b", "ref": "refs/heads/x", "sha": "a" * 40},
                {"repo": "a", "ref": "refs/heads/x", "sha": "a" * 40},
            ],
        ),
    ),
    "source duplicate": (
        "verify",
        _set(["source_sha"], [{"repo": "a", "ref": "refs/heads/x", "sha": "a" * 40}] * 2),
    ),
    "prior later gate": ("plan", _set(["prior", "verify"], {"gen": 0, "approvals": []})),
    "prior self": ("plan", _set(["prior", "plan"], {"gen": 0, "approvals": []})),
    "prior on requirements": ("requirements", _set(["prior", "requirements"], {"gen": 0, "approvals": []})),
    "prior gen negative": ("plan", _set(["prior", "requirements", "gen"], -1)),
    "prior approvals unsorted": (
        "plan",
        _set(["prior", "requirements", "approvals"], ["01J9ZP0000000000000000000B", "01J9ZP0000000000000000000A"]),
    ),
    "prior approval id": ("plan", _set(["prior", "requirements", "approvals"], ["x"])),
    "policy_hash empty": ("requirements", _set(["policy_hash"], "")),
    "policy_hash upper": ("requirements", _set(["policy_hash"], "sha256:" + "AB" * 32)),
    "people_hash x": ("requirements", _set(["people_hash"], "x")),
    "people_hash none": ("requirements", _set(["people_hash"], None)),
    "nfd text": ("requirements", _set(["fields", "acceptance", 0, "text"], "cafe\u0301")),
    "bidi text": ("plan", _set(["tasks", 0, "text"], "a\u202eb")),
    "control text": ("plan", _set(["tasks", 0, "text"], "a\x00b")),
    "surrogate text": ("plan", _set(["tasks", 0, "text"], "a\ud800")),
    "empty ac text": ("requirements", _set(["fields", "acceptance", 0, "text"], "")),
}


@pytest.mark.parametrize("name", sorted(BAD_GATE_INPUTS))
def test_gate_hash_refuses(name):
    gate, mutate = BAD_GATE_INPUTS[name]
    g = _g(gate)
    mutate(g)
    with pytest.raises(HashError):
        canon.gate_hash(g)


def test_gate_hash_accepts_empty_and_addon_sections():
    g = _g("plan")
    g["sections"]["estimate.notes"] = GOOD
    g["tasks"] = []
    assert canon.gate_hash(g).startswith("sha256:")
    g = _g("code")
    g["fields"]["size"] = None
    g["prior"] = {}
    assert canon.gate_hash(g).startswith("sha256:")


def test_gate_hash_does_not_mutate_input():
    g = _g("verify")
    before = copy.deepcopy(g)
    canon.gate_hash(g)
    assert g == before


@settings(max_examples=60, deadline=None)
@given(st.text(st.characters(codec="utf-8"), max_size=30))
def test_gate_hash_text_is_exact_or_refused(t):
    g = _g("requirements")
    g["fields"]["acceptance"][0]["text"] = t or "x"
    try:
        canon.normalize_text(t or "x")
        ok = canon.is_clean_text(t or "x")
    except canon.TextError:
        ok = False
    if ok:
        canon.gate_hash(g)
    else:
        with pytest.raises(HashError):
            canon.gate_hash(g)


# --- events: chain, line form, signing contexts -------------------------------------------------------------


def test_chain_rules():
    evs = oracle.chain_events()
    canon.check_chain(evs)
    assert canon.check_chain([]) == []
    bad = copy.deepcopy(evs)
    bad[0]["prev"] = "sha256:" + "00" * 32
    with pytest.raises(canon.ChainError) as e:
        canon.check_chain(bad)
    assert e.value.seq == 1
    bad = copy.deepcopy(evs)
    bad[2]["seq"] = 4
    with pytest.raises(canon.ChainError):
        canon.check_chain(bad)
    with pytest.raises(canon.ChainError):
        canon.check_chain(evs[1:])  # seq must start at 1
    bad = copy.deepcopy(evs)
    bad[1]["seq"] = True
    with pytest.raises(canon.ChainError):
        canon.check_chain(bad)


def test_head_covers_host_sig_and_needs_hash_v():
    e = oracle.chain_events()[0]
    assert canon.event_head({**e, "host_sig": "B" * 86}) != canon.event_head(e)
    for v in (2, None, True):
        with pytest.raises(HashError):
            canon.event_head({**e, "hash_v": v})
    with pytest.raises(HashError):
        canon.event_head({k: v for k, v in e.items() if k != "hash_v"})
    with pytest.raises(HashError):
        canon.event_head([e])  # type: ignore[arg-type]


def test_parse_event_line_refusals():
    e = oracle.chain_events()[0]
    line = canon.event_line(e)
    assert line.endswith(b"\n") and canon.parse_event_line(line) == e
    for bad in (
        line[:-1],  # no LF
        line + b"\n",
        line[:-1] + b"\r\n",
        b" " + line,
        b"[1]\n",
        b'{"a":1,"a":2}\n',
        b'{"a":1.5}\n',
        b'{"hash_v":1,"a":"\xff"}\n',
        b'{"a":"caf\xc3\xa9"}\n'.replace(b"\xc3\xa9", b"e\xcc\x81"),  # NFD text
        "plain str",
        None,
    ):
        with pytest.raises(HashError):
            canon.parse_event_line(bad)  # type: ignore[arg-type]


def test_signed_context_refusals():
    e = oracle.chain_events()[1]
    ok = dict(workspace_id=oracle.W, event=e)
    canon.signed_context("sig_ticket_event", log=oracle.UID, **ok)
    canon.signed_context("sig_ws_event", log="workspace", **ok)
    canon.signed_context("sig_host_event", log="workspace", **ok)
    canon.signed_context("sig_host_event", log=oracle.UID, **ok)
    for args in [
        ("sig_ticket_event", "workspace"),
        ("sig_ws_event", oracle.UID),
        ("sig_ticket_event", "uid"),
        ("sig_decision", oracle.UID),
        ("sig_checkpoint", oracle.UID),
    ]:
        with pytest.raises(HashError):
            canon.signed_context(args[0], oracle.W, args[1], e)
    for kw in ({"contract": 2}, {"contract": 0}, {"contract": True}, {"suite": 1}, {"suite": "2"}):
        with pytest.raises(HashError):
            canon.signed_context("sig_ticket_event", oracle.W, oracle.UID, e, **kw)
    with pytest.raises(HashError):
        canon.signed_context("sig_ticket_event", "A" * 32, oracle.UID, e)


def test_signed_context_exact_bytes():
    # label || cj({contract, suite, workspace_id, log, event}); keys sorted by cj
    e = {"hash_v": 1, "id": "x"}
    expected = (
        b"orch/v2/sig/ticket-event|"
        + b'{"contract":1,"event":{"hash_v":1,"id":"x"},"log":"'
        + oracle.UID.encode()
        + b'","suite":2,"workspace_id":"'
        + oracle.W.encode()
        + b'"}'
    )
    assert canon.signed_context("sig_ticket_event", oracle.W, oracle.UID, e) == expected


def test_host_signing_bytes_needs_the_full_event():
    e = {k: v for k, v in oracle.chain_events()[0].items() if k != "seq"}
    with pytest.raises(HashError):
        canon.host_signing_bytes(oracle.W, oracle.UID, e)


# --- section key sets (ticket-format §4/§5.7) ---------------------------------------------------------------

_EXPECTED_SECTIONS = {
    ("requirements", "feature"): {"summary", "context", "requirements", "out_of_scope"},
    ("requirements", "bug"): {"summary", "context", "requirements", "out_of_scope"},
    ("requirements", "chore"): {"summary", "context", "requirements"},
    ("requirements", "spike"): {"summary", "context", "requirements"},
    ("requirements", "epic"): {"summary", "context", "requirements", "out_of_scope"},
    ("plan", "feature"): {"plan", "decisions"},
    ("plan", "epic"): {"decisions"},
    ("verify", "feature"): {"verification"},
    ("verify", "bug"): {"verification"},
    ("verify", "spike"): {"findings"},
    ("verify", "chore"): set(),
    ("verify", "epic"): set(),
    ("code", "feature"): set(),
}


@pytest.mark.parametrize(("gate", "ttype"), sorted(_EXPECTED_SECTIONS))
def test_exact_core_section_set_per_gate_and_type(gate, ttype):
    want = _EXPECTED_SECTIONS[gate, ttype]
    g = _g(gate)
    g["fields"]["ticket_type"] = ttype
    if gate == "verify" and ttype in ("chore", "epic", "spike"):
        g["receipts"] = {}
    g["sections"] = {k: oracle.h("section", b"") for k in want}  # missing sections are present as H("")
    assert canon.gate_hash(g).startswith("sha256:")
    for k in want:  # leaving one out is a second encoding of the same approval: refused
        h = dict(g["sections"])
        del h[k]
        with pytest.raises(HashError):
            canon.gate_hash({**g, "sections": h})
    for extra in {
        "summary",
        "context",
        "requirements",
        "out_of_scope",
        "plan",
        "decisions",
        "verification",
        "findings",
    } - want:
        with pytest.raises(HashError):
            canon.gate_hash({**g, "sections": {**g["sections"], extra: GOOD}})
    canon.gate_hash({**g, "sections": {**g["sections"], "estimate.notes": GOOD}})  # addon sections are allowed


def test_section_hash_text_shape():
    for bad in ("\nabc", "abc\n", "\n", "a" * 65537, "\u00e9" * 32769):
        with pytest.raises(HashError):
            canon.section_hash(bad)
    canon.section_hash("a" * 65536)
    canon.section_hash("a\n\nb  \t")  # interior blank lines, trailing spaces and tabs are kept


def test_receipts_may_have_null_repo_and_commit():
    g = _g("verify")
    g["receipts"]["T2"].update(repo=None, commit=None)
    assert canon.gate_hash(g).startswith("sha256:")
    g["receipts"]["T2"]["repo"] = "bad repo"
    with pytest.raises(HashError):
        canon.gate_hash(g)


@pytest.mark.parametrize(
    "repo",
    [
        "https://user:token@github.com/acme/x",
        "https://token@github.com/acme/x",
        "https://GitHub.com/acme/x",
        "https://github.com/acme/x.git",
        "https://github.com/acme/x/",
        "https://github.com:443/acme/x",
        "http://github.com/acme/x",
        "ssh://git@github.com/acme/x",
        "git@github.com:acme/x",
        "file:///tmp/x",
        "local:",
        "local:a b",
        "",
        "https://github.com",
    ],
)
def test_source_repo_identity_must_be_canonical(repo):
    g = _g("code")
    g["source_sha"][0]["repo"] = repo
    with pytest.raises(HashError):
        canon.gate_hash(g)


@pytest.mark.parametrize("repo", ["https://github.com/acme/x", "https://git.example.com:8443/a/b", "local:my-repo"])
def test_source_repo_identity_accepts_canonical(repo):
    g = _g("code")
    g["source_sha"][0]["repo"] = repo
    canon.gate_hash(g)


@pytest.mark.parametrize("name", ["../x", "a/b", ".hidden", "-x", "", "a b", "x" * 129, "caf\u00e9.png"])
def test_artifact_names_follow_section_6(name):
    g = _g("requirements")
    g["artifacts"] = {name: {"kind": "log", "digest": GOOD, "ac": None, "task": None}}
    with pytest.raises(HashError):
        canon.gate_hash(g)
    g["artifacts"] = {"x" * 128: {"kind": "log", "digest": GOOD, "ac": None, "task": None}}
    canon.gate_hash(g)


def test_chain_inputs_are_typed():
    ev = oracle.chain_events()
    for bad in (None, (ev[0],), "x", {"a": 1}):
        with pytest.raises(HashError):
            canon.check_chain(bad)  # type: ignore[arg-type]
    for bad in ([1], [None], [[]], ["x"]):
        with pytest.raises(canon.ChainError):
            canon.check_chain(bad)  # type: ignore[arg-type]
    no_prev = {k: v for k, v in ev[0].items() if k != "prev"}
    with pytest.raises(canon.ChainError):
        canon.check_chain([no_prev])
    for bad in (None, 1, "x", [1]):
        with pytest.raises(HashError):
            canon.log_head(bad)  # type: ignore[arg-type]


def test_escaped_characters_in_hashed_strings():
    v = 'a\nb\t"q" \\ \u2028'
    assert canon.value_hash(v) == oracle.h("value", oracle.cj(v))
    assert oracle.cj(v) == b'"a\\nb\\t\\"q\\" \\\\ \xe2\x80\xa8"'  # \n \t \" \\ escaped, U+2028 raw
