"""M4 (#1357, S0): the shared ``contracts/hub/vectors_published.json`` against orch-publish's own copy of the rules
(``pubsrv.published``). EVERY section is replayed; the hub (reference) and orch-core replay the same file."""

import json
import pathlib

import pytest

from pubsrv import hub_path as H
from pubsrv import published as P
from pubsrv import relay_proto as proto

VEC = json.loads(
    (
        pathlib.Path(__file__).resolve().parents[3] / "contracts" / "hub" / "vectors_published.json"
    ).read_text()
)
SECTIONS = [k for k in VEC if k.endswith("_cases")]
REPLAYED = set(SECTIONS)  # publish replays every section


def ids(section):
    return [c["name"] for c in VEC[section]]


def test_every_section_is_replayed_here():
    assert set(SECTIONS) == REPLAYED
    assert len(SECTIONS) == 17


def test_labels_are_publishs_labels():
    assert {k: v.encode() for k, v in VEC["labels"].items()} == {
        k: H.LABELS[k] for k in VEC["labels"]
    }


def test_required_cases_exist():
    for sec, req in VEC["required"].items():
        assert set(req) <= {c["name"] for c in VEC[sec + "_cases"]}, sec


@pytest.mark.parametrize("c", VEC["slug_cases"], ids=ids("slug_cases"))
def test_slug(c):
    assert P.slug_problem(c["slug"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["origin_cases"], ids=ids("origin_cases"))
def test_origin(c):
    assert P.origin_problem(c["origin"], c["forbidden"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["reach_cases"], ids=ids("reach_cases"))
def test_reach(c):
    assert P.reach_problem(c["reach"], c["forbidden"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["csp_cases"], ids=ids("csp_cases"))
def test_csp(c):
    assert P.csp_outcome(c["base"], c["reach"], c["forbidden"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["password_record_cases"], ids=ids("password_record_cases"))
def test_password_record(c):
    assert P.password_record_problem(c["record"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["password_verify_cases"], ids=ids("password_verify_cases"))
def test_password_verify(c):
    assert P.password_ok(c["record"], c["password"]) is c["expect"]


@pytest.mark.parametrize("c", VEC["meta_cases"], ids=ids("meta_cases"))
def test_meta(c):
    assert P.meta_problem(c["meta"], c["forbidden"]) == c["expect"]


def test_meta_hash():
    seen = set()
    for c in VEC["meta_hash_cases"]:
        assert P.meta_hash(c["meta"]) == c["hash"], c["name"]
        assert P.reach_hash(c["meta"]["reach"]) == c["reach_hash"], c["name"]
        seen.add(c["hash"])
    assert len(seen) == len(VEC["meta_hash_cases"])


@pytest.mark.parametrize("c", VEC["commit_cases"], ids=ids("commit_cases"))
def test_commit(c):
    files = (
        None if c["stored_files"] is None else {p: tuple(v) for p, v in c["stored_files"].items()}
    )
    assert P.check_release(c["body"], c["meta"], files, c["forbidden"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["route_cases"], ids=ids("route_cases"))
def test_route(c):
    assert P.app_route(c["method"], c["path"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["extend_cases"], ids=ids("extend_cases"))
def test_extend(c):
    got = P.extend_problem(c["body"])
    if "error" in c["expect"]:
        assert got == c["expect"]["error"]
    else:
        assert got is None and P.extend_expiry(c["now_ms"], c["body"]) == c["expect"]["ok"]


def _outcome(fn):
    try:
        return {"ok": fn()}
    except H.Refused as e:
        return {"error": e.code}


@pytest.mark.parametrize("c", VEC["row_cases"], ids=ids("row_cases"))
def test_row(c):
    got = _outcome(lambda: P.check_published_row(c["row"]))
    assert got == ({"error": c["expect"]} if c["expect"] else {"ok": c["row"]})


@pytest.mark.parametrize("c", VEC["row_scope_cases"], ids=ids("row_scope_cases"))
def test_row_scope(c):
    got = _outcome(lambda: len(P.check_published_rows(c["rows"], c["scope"])))
    assert got == c["expect"]


@pytest.mark.parametrize("c", VEC["approval_cases"], ids=ids("approval_cases"))
def test_approval(c):
    a = c["args"]
    r = a["request"]
    req = {**r, "body": bytes.fromhex(r["body_hex"]), "nonce": bytes.fromhex(r["nonce_hex"])}
    got = _outcome(
        lambda: P.check_app_approval(
            req,
            a["approval"],
            owner_person_id=a["owner_person_id"],
            now_ms=a["now_ms"],
            revoked_devices=frozenset(a["revoked_devices"]),
        )
    )
    if "ok" in got:
        keys = ("op", "app_id", "access", "audience", "approved_by_person")
        got = {"ok": {k: got["ok"][k] for k in keys}}
    assert got == c["expect"]


class Nonces:
    def __init__(self, seen):
        self.seen = set(seen)

    def record(self, hub, nonce, now_ms):
        if nonce in self.seen:
            return False
        self.seen.add(nonce)
        return True


def _state(a):
    held = H.verify_card(a["hub_card"], a["now_ms"], a["dev_mode"])
    bindings = {}
    for ws, steps in a["bindings"].items():
        state = None
        wsk = proto.unb64u(a["binding_wsk"][ws], 65)
        for st in steps:
            if "binding" in st:
                state = H.apply_binding(
                    state, H.verify_binding(st["binding"], ws, wsk, a["now_ms"], True)
                )
            else:
                state = H.apply_unbound(state, H.verify_unbound(st["unbound"], ws, wsk, True))
        bindings[ws] = state
    return held, a["cards"], bindings


@pytest.mark.parametrize("c", VEC["allow_cases"], ids=ids("allow_cases"))
def test_allow(c):
    a = c["args"]
    held = H.verify_card(a["hub_card"], a["now_ms"], a["dev_mode"])
    got = _outcome(
        lambda: P.check_allow(
            a["signed"],
            path_ws=a["path_ws"],
            trusted=a["trusted"],
            held=held,
            nonces=Nonces(a["seen_nonces"]),
            now_ms=a["now_ms"],
        )
    )
    assert got == c["expect"]


@pytest.mark.parametrize("c", VEC["narrow_cases"], ids=ids("narrow_cases"))
def test_narrow(c):
    a = c["args"]
    held, cards, bindings = _state(a)
    got = _outcome(
        lambda: P.check_release_narrow(
            a["signed"],
            held=held,
            cards=cards,
            bindings=bindings,
            owners=a["owners"],
            nonces=Nonces(a["seen_nonces"]),
            now_ms=a["now_ms"],
        )
    )
    assert got == c["expect"]


@pytest.mark.parametrize("c", VEC["pass_cases"], ids=ids("pass_cases"))
def test_pass(c):
    a = c["args"]
    held, cards, bindings = _state(a)
    got = _outcome(
        lambda: P.check_app_pass(
            a["signed"],
            held=held,
            cards=cards,
            bindings=bindings,
            app=a["app"],
            issued=a["issued"],
            spent=frozenset(a["spent"]),
            now_ms=a["now_ms"],
        )
    )
    assert got == c["expect"]
