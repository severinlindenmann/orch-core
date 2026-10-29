"""M4 (#1357, S0): the shared ``contracts/hub/vectors_published.json`` against the host's own copy of the rules
(``orch.hubclient.published``). Every section is accounted for: replayed here, or skipped on purpose because the host
never verifies it (orch-publish does, and the hub replays the same file as the reference).

The host BUILDS the owner's approval (S4), it never verifies one, so ``approval_cases`` are replayed for what the host
computes: the request pre-check and, for an accepted case, the exact ``request_hash`` the approval names."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch.crypto.encoding import unb64u
from orch.hubclient import published as P

VEC = json.loads((Path(__file__).resolve().parents[4] / "contracts/hub/vectors_published.json").read_text())
SECTIONS = {k for k in VEC if k.endswith("_cases")}
SKIPPED = {
    "row_cases",  # rows are made by orch-publish and read by the hub console
    "row_scope_cases",
    "narrow_cases",  # hub.release_narrow and hub.app_pass are verified by orch-publish
    "pass_cases",
}
REPLAYED = SECTIONS - SKIPPED


def ids(section: str) -> list[str]:
    return [c["name"] for c in VEC[section]]


def test_every_section_is_replayed_or_skipped_on_purpose() -> None:
    assert len(SECTIONS) == 16
    assert SECTIONS == REPLAYED | SKIPPED and not REPLAYED & SKIPPED


def test_labels_are_the_hosts_labels() -> None:
    assert {k: v.encode() for k, v in VEC["labels"].items()} == {k: P.LABELS[k] for k in VEC["labels"]}


def test_required_cases_exist() -> None:
    for sec, req in VEC["required"].items():
        assert set(req) <= {c["name"] for c in VEC[sec + "_cases"]}, sec


@pytest.mark.parametrize("c", VEC["slug_cases"], ids=ids("slug_cases"))
def test_slug(c: dict) -> None:
    assert P.slug_problem(c["slug"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["origin_cases"], ids=ids("origin_cases"))
def test_origin(c: dict) -> None:
    assert P.origin_problem(c["origin"], c["forbidden"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["reach_cases"], ids=ids("reach_cases"))
def test_reach(c: dict) -> None:
    assert P.reach_problem(c["reach"], c["forbidden"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["csp_cases"], ids=ids("csp_cases"))
def test_csp(c: dict) -> None:
    assert P.csp_outcome(c["base"], c["reach"], c["forbidden"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["password_record_cases"], ids=ids("password_record_cases"))
def test_password_record(c: dict) -> None:
    assert P.password_record_problem(c["record"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["password_verify_cases"], ids=ids("password_verify_cases"))
def test_password_verify(c: dict) -> None:
    assert P.password_ok(c["record"], c["password"]) is c["expect"]


def test_the_host_makes_the_record_the_vectors_pin() -> None:
    ok = next(c for c in VEC["password_verify_cases"] if c["name"] == "the right password")
    salt = unb64u(ok["record"]["salt"], 16)
    assert P.make_password_record(ok["password"], salt) == ok["record"]


@pytest.mark.parametrize("c", VEC["meta_cases"], ids=ids("meta_cases"))
def test_meta(c: dict) -> None:
    assert P.meta_problem(c["meta"], c["forbidden"]) == c["expect"]


def test_meta_hash() -> None:
    seen = set()
    for c in VEC["meta_hash_cases"]:
        assert P.meta_hash(c["meta"]) == c["hash"], c["name"]
        assert P.reach_hash(c["meta"]["reach"]) == c["reach_hash"], c["name"]
        seen.add(c["hash"])
    assert len(seen) == len(VEC["meta_hash_cases"])


@pytest.mark.parametrize("c", VEC["commit_cases"], ids=ids("commit_cases"))
def test_commit(c: dict) -> None:
    files = None if c["stored_files"] is None else {p: tuple(v) for p, v in c["stored_files"].items()}
    assert P.check_release(c["body"], c["meta"], files, c["forbidden"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["route_cases"], ids=ids("route_cases"))
def test_route(c: dict) -> None:
    assert P.app_route(c["method"], c["path"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["extend_cases"], ids=ids("extend_cases"))
def test_extend(c: dict) -> None:
    got = P.extend_problem(c["body"])
    if "error" in c["expect"]:
        assert got == c["expect"]["error"]
    else:
        assert got is None and P.extend_expiry(c["now_ms"], c["body"]) == c["expect"]["ok"]


@pytest.mark.parametrize("c", VEC["approval_cases"], ids=ids("approval_cases"))
def test_approval_request(c: dict) -> None:
    a = c["args"]
    r = a["request"]
    req = {**r, "body": bytes.fromhex(r["body_hex"])}
    err = c["expect"].get("error")
    problem = P.approval_request_problem(req)
    if err == "malformed":
        assert problem == "malformed"
        return
    assert problem is None  # a request the host could not have sent is never an approval case about crypto
    want = P.app_request_hash(
        bytes.fromhex(r["ws"]), r["ts_ms"], bytes.fromhex(r["nonce_hex"]), r["method"], r["host"], r["target"],
        req["body"], r["hub"],
    )  # fmt: skip
    named = unb64u(a["approval"]["o"]["request_hash"], 32) if a["approval"] else None
    if "ok" in c["expect"]:
        assert named == want  # what the host computes is what the owner's approval names, byte for byte
        assert a["approval"]["o"]["hub_id"] == r["hub"]  # null on the direct path
        assert a["approval"]["o"]["access"] == c["expect"]["ok"]["access"]
