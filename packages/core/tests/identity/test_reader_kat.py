"""H12d-1: core's KAT runner (orch-v2-host §0.8.6 item 9.2) over ``tests/vectors/f1/reader_kat_1.json``.

The vector file is written by the independent oracle (``tests/canon/oracle_f1_signed.py`` ``reader_kat_vector``;
``tests/canon/test_f1_vectors.py`` regenerates it byte for byte). Here core's PRODUCTION parsers must reproduce the
oracle's expected results and digests: the log variant through ``orch.store.logs.parse_line`` (the line parser every
log read uses) and ``orch.model.replay`` with ``CryptoVerifier``; the view variant through
``orch.identity.reader.parse_signer``.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from orch import canon
from orch.identity import reader

KAT = json.loads(
    (Path(__file__).resolve().parents[2] / "src" / "orch" / "identity" / "reader_kat_1.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("surface", reader.SURFACES)
def test_core_reproduces_the_expected_digests(surface):
    results, digest = reader.run_kat(KAT, surface)
    assert results == KAT[surface]["results"] == [c["expect"] for c in KAT[surface]["cases"]]
    assert digest == KAT[surface]["digest"] == KAT["expected"]["1"][surface]
    assert digest == reader.KAT_DIGESTS["1"][surface]


def test_the_digest_construction_is_sha256_of_cj_of_the_results_in_corpus_order():
    for surface in reader.SURFACES:
        results = KAT[surface]["results"]
        assert reader.kat_digest(results) == "sha256:" + hashlib.sha256(canon.cj_checked(results)).hexdigest()
        assert reader.kat_digest(list(reversed(results))) != KAT[surface]["digest"]


def test_the_expected_results_are_accepted_accepted_refused_with_auth_null():
    assert [(r["accepted"], r["auth"]) for r in KAT["log"]["results"]] == [
        (True, "convenience"),
        (True, "passphrase"),
        (False, None),
    ]
    assert [r["view"] for r in KAT["view"]["results"]] == [v for v in reader.READER_VIEWS_1 for _ in range(3)]
    assert KAT["view"]["views"] == list(reader.READER_VIEWS_1)


def test_each_view_case_differs_only_in_answer_signer_auth():
    objs = [copy.deepcopy(c["object"]) for c in KAT["view"]["cases"]]
    assert [o["answer"]["signer"]["auth"] for o in objs] == ["convenience", "passphrase", "bogus"]
    for o in objs:
        del o["answer"]["signer"]["auth"]
    assert objs[0] == objs[1] == objs[2]
    assert objs[0]["state"] == "answered"


def test_each_log_case_differs_only_in_auth_and_its_signatures():
    events = [c["event"] for c in KAT["log"]["cases"]]
    strip = [{k: v for k, v in e.items() if k not in ("auth", "sig", "host_sig")} for e in events]
    assert strip[0] == strip[1] == strip[2]
    assert [e["auth"] for e in events] == ["convenience", "passphrase", "bogus"]
    assert len({e["sig"] for e in events}) == 3


def test_the_bogus_case_fails_only_on_auth():
    """Re-labelled ``passphrase`` (the same bytes otherwise), the bogus event's own signatures would not verify, but
    its signatures are its own: a reader that maps an unknown auth to a known one and then verifies over the mapped
    value would still refuse; so the test pins the parser, not the verifier, via the schema (the only difference)."""
    from orch import schema

    bogus = KAT["log"]["cases"][2]["event"]
    with pytest.raises(schema.SchemaError):
        schema.validate("event", bogus, log="workspace")
    ok = copy.deepcopy(bogus)
    ok["auth"] = "passphrase"
    schema.validate("event", ok, log="workspace")  # the shape is otherwise valid


def test_a_broken_signature_is_refused_by_the_log_runner():
    kat = copy.deepcopy(KAT)
    e = kat["log"]["cases"][0]["event"]
    e["sig"] = KAT["log"]["cases"][1]["event"]["sig"]  # the passphrase case's signature over other bytes
    results, _ = reader.run_kat(kat, "log")
    assert results[0]["accepted"] is False and results[0]["auth"] is None
    assert results[1:] == KAT["log"]["results"][1:]


def test_a_broken_host_signature_is_refused_by_the_log_runner():
    kat = copy.deepcopy(KAT)
    kat["log"]["cases"][1]["event"]["host_sig"] = KAT["log"]["cases"][0]["event"]["host_sig"]
    results, _ = reader.run_kat(kat, "log")
    assert results[1]["accepted"] is False


def test_another_pinned_workspace_or_genesis_refuses_every_case_or_the_fixture():
    for key, value in (("workspace_id", "f" * 32), ("genesis", "sha256:" + "0e" * 32)):
        kat = copy.deepcopy(KAT)
        kat["log"]["context"][key] = value
        with pytest.raises(ValueError):
            reader.run_kat(kat, "log")  # the fixture itself must replay cleanly


@pytest.mark.parametrize(
    ("signer", "accepted"),
    [
        ({"device": "d_" + "ab" * 16, "auth": "convenience"}, True),
        ({"device": "d_" + "ab" * 16, "auth": "secure-enclave"}, True),
        ({"device": "d_" + "ab" * 16, "auth": "Convenience"}, False),
        ({"device": "d_" + "ab" * 16, "auth": None}, False),
        ({"device": "d_" + "ab" * 16}, False),
        ({"device": "d_" + "ab" * 16, "auth": "convenience", "presence": True}, False),
        ({"device": "ab" * 16, "auth": "convenience"}, False),
        ({"device": "d_" + "AB" * 16, "auth": "convenience"}, False),
        ("convenience", False),
        (None, False),
    ],
)
def test_parse_signer(signer, accepted):
    view = {"id": "Q1", "state": "answered", "answer": {"by": "p_" + "ab" * 16, "signer": signer}}
    if accepted:
        assert reader.parse_signer(VIEW, view) == signer["auth"]
    else:
        with pytest.raises(ValueError):
            reader.parse_signer(VIEW, view)


VIEW = "ticket.get.questions.answer"


def test_reader_views_1_is_the_question_answer_only():
    """Owner decision with orch-f1 (#463 round 1): the answer of a question in ticket.get is the only place the phone
    shows a signer."""
    assert reader.READER_VIEWS_1 == (VIEW,)


def test_parse_signer_needs_an_answer_with_a_signer():
    for bad in ({"id": "Q1"}, {"id": "Q1", "answer": None}, {"id": "Q1", "answer": {"by": "x"}}, [], None):
        with pytest.raises(ValueError):
            reader.parse_signer(VIEW, bad)
    ok = {"id": "Q1", "answer": {"signer": {"device": "d_" + "ab" * 16, "auth": "passphrase"}}}
    with pytest.raises(ValueError):
        reader.parse_signer("activity.list.items", ok)  # not a reader view


def test_the_view_auth_values_are_the_event_schema_values():
    from orch.custody.base import AUTH_VALUES
    from orch.schema import load

    # "hub" (C3b, 15.1) is custody by the hub's signatures, not a backend a key can sign with: not in AUTH_VALUES
    enum = tuple(load("common")["$defs"]["auth"]["enum"])
    assert enum == (*AUTH_VALUES, "hub") == reader.auth_values()


def test_a_corpus_without_every_reader_view_is_refused():
    kat = copy.deepcopy(KAT)
    kat["view"]["cases"] = kat["view"]["cases"][:2]
    with pytest.raises(ValueError):
        reader.run_kat(kat, "view")
    with pytest.raises(ValueError):
        reader.run_kat(KAT, "screen")


def test_the_default_expectation_of_verify_decl_is_cores_own_kat_digests():
    from .test_reader_decl import CTX, _o, _signed

    ctx = {k: v for k, v in CTX.items() if k != "expected"}
    d = _signed(_o(kat={"1": dict(reader.KAT_DIGESTS["1"])}))
    assert reader.verify_decl(d, **{**ctx, "surfaces": ("log", "view")}).level == 1
    with pytest.raises(reader.DeclInvalid) as exc:
        reader.verify_decl(_signed(), **ctx)  # the test digest is not core's
    assert exc.value.reason == "kat_mismatch"
