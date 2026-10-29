"""M4 (#1357, S0): ``contracts/hub/vectors_published.json`` is what the generator builds, and EVERY section is replayed
here with the hub's reference rules (``orch_hub.contract.published``). orch-publish and orch-core replay the same file
with their own copies, so the three cannot drift apart."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from orch_hub.contract import hub as H
from orch_hub.contract import published as P
from orch_hub.contract.canon import unb64u
from orch_hub.contract.labels import HUB_LABELS

VEC_DIR = Path(__file__).resolve().parents[3] / "contracts" / "hub"
VEC = json.loads((VEC_DIR / "vectors_published.json").read_text())
SECTIONS = [k for k in VEC if k.endswith("_cases")]
REPLAYED = set(SECTIONS)  # the hub replays every section


def _gen():
    spec = importlib.util.spec_from_file_location("make_vectors_published", VEC_DIR / "make_vectors_published.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ids(section: str) -> list[str]:
    return [c["name"] for c in VEC[section]]


def test_file_is_generated() -> None:
    assert json.loads(json.dumps(_gen().build())) == VEC


def test_every_section_is_replayed_here() -> None:
    assert set(SECTIONS) == REPLAYED
    assert len(SECTIONS) == 16


def test_labels_are_the_hub_labels() -> None:
    assert VEC["labels"] == {k: HUB_LABELS[k] for k in VEC["labels"]}
    assert set(VEC["labels"]) == {
        "sig_hub_release_narrow", "sig_hub_app_pass", "h_publish_request_direct", "h_app_meta", "h_app_reach",
    }  # fmt: skip


def test_required_cases_exist_and_hold_their_error() -> None:
    names = {sec: {c["name"] for c in VEC[sec + "_cases"]} for sec in VEC["required"]}
    for sec, req in VEC["required"].items():
        assert set(req) <= names[sec], sec


@pytest.mark.parametrize("c", VEC["slug_cases"], ids=ids("slug_cases"))
def test_slug(c) -> None:
    assert P.slug_problem(c["slug"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["origin_cases"], ids=ids("origin_cases"))
def test_origin(c) -> None:
    assert P.origin_problem(c["origin"], c["forbidden"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["reach_cases"], ids=ids("reach_cases"))
def test_reach(c) -> None:
    assert P.reach_problem(c["reach"], c["forbidden"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["csp_cases"], ids=ids("csp_cases"))
def test_csp(c) -> None:
    assert P.csp_outcome(c["base"], c["reach"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["password_record_cases"], ids=ids("password_record_cases"))
def test_password_record(c) -> None:
    assert P.password_record_problem(c["record"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["password_verify_cases"], ids=ids("password_verify_cases"))
def test_password_verify(c) -> None:
    assert P.password_ok(c["record"], c["password"]) is c["expect"]


@pytest.mark.parametrize("c", VEC["meta_cases"], ids=ids("meta_cases"))
def test_meta(c) -> None:
    assert P.meta_problem(c["meta"], c["forbidden"]) == c["expect"]


def test_meta_hash() -> None:
    seen = set()
    for c in VEC["meta_hash_cases"]:
        assert P.meta_hash(c["meta"]) == c["hash"], c["name"]
        assert P.reach_hash(c["meta"]["reach"]) == c["reach_hash"], c["name"]
        seen.add(c["hash"])
    assert len(seen) == len(VEC["meta_hash_cases"])  # every field is covered: no two variants share a hash


@pytest.mark.parametrize("c", VEC["commit_cases"], ids=ids("commit_cases"))
def test_commit(c) -> None:
    files = None if c["stored_files"] is None else {p: tuple(v) for p, v in c["stored_files"].items()}
    assert P.check_release(c["body"], c["meta"], files, c["forbidden"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["route_cases"], ids=ids("route_cases"))
def test_route(c) -> None:
    assert P.app_route(c["method"], c["path"]) == c["expect"]


@pytest.mark.parametrize("c", VEC["extend_cases"], ids=ids("extend_cases"))
def test_extend(c) -> None:
    got = P.extend_problem(c["body"])
    if "error" in c["expect"]:
        assert got == c["expect"]["error"]
    else:
        assert got is None and P.extend_expiry(c["now_ms"], c["body"]) == c["expect"]["ok"]


@pytest.mark.parametrize("c", VEC["row_cases"], ids=ids("row_cases"))
def test_row(c) -> None:
    try:
        P.check_published_row(c["row"])
        got = None
    except H.Refused as e:
        got = e.code
    assert got == c["expect"]


@pytest.mark.parametrize("c", VEC["row_scope_cases"], ids=ids("row_scope_cases"))
def test_row_scope(c) -> None:
    try:
        got = {"ok": len(P.check_published_rows(c["rows"], c["scope"]))}
    except H.Refused as e:
        got = {"error": e.code}
    assert got == c["expect"]


def _outcome(fn):
    try:
        return {"ok": fn()}
    except H.Refused as e:
        return {"error": e.code}


@pytest.mark.parametrize("c", VEC["approval_cases"], ids=ids("approval_cases"))
def test_approval(c) -> None:
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
        got = {"ok": {k: got["ok"][k] for k in ("op", "app_id", "access", "audience", "approved_by_person")}}
    assert got == c["expect"]


class Nonces:
    def __init__(self, seen) -> None:
        self.seen = set(seen)

    def record(self, hub: str, nonce: str, now_ms: int) -> bool:
        if nonce in self.seen:
            return False
        self.seen.add(nonce)
        return True


def _state(a):
    held = H.verify_card(a["hub_card"], now_ms=a["now_ms"], dev_mode=a["dev_mode"])
    bindings = {}
    for ws, steps in a["bindings"].items():
        state = None
        wsk = unb64u(a["binding_wsk"][ws], 65)
        for st in steps:
            if "binding" in st:
                state = H.apply_binding(
                    state,
                    H.verify_binding(st["binding"], workspace_id=ws, wsk_pub=wsk, now_ms=a["now_ms"], dev_mode=True),
                )
            else:
                state = H.apply_unbound(
                    state, H.verify_unbound(st["unbound"], workspace_id=ws, wsk_pub=wsk, dev_mode=True)
                )
        bindings[ws] = state
    return held, a["cards"], bindings


@pytest.mark.parametrize("c", VEC["narrow_cases"], ids=ids("narrow_cases"))
def test_narrow(c) -> None:
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
def test_pass(c) -> None:
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


def test_protocol_doc_names_every_code_of_the_file() -> None:
    doc = (VEC_DIR / "protocol.md").read_text()
    codes = set()
    for sec in SECTIONS:
        for c in VEC[sec]:
            e = c.get("expect")
            if isinstance(e, dict) and "error" in e:
                codes.add(e["error"])
            elif isinstance(e, str):
                codes.add(e)
    for code in sorted(codes):
        assert f"`{code}`" in doc, code


def test_the_hub_signer_signs_the_new_kinds_and_the_checks_accept_them() -> None:
    from orch_hub.contract import keystore as KS
    from orch_hub.contract import suite as S
    from orch_hub.contract import vectorgen as G

    op = KS.OpSigner(G.K.priv["hk_op"])
    for case in (VEC["narrow_cases"][0], VEC["pass_cases"][0]):
        o = case["args"]["signed"]["o"]
        out = op.sign_object(o)
        assert S.verify(op.public, unb64u(out["sig"], 64), H.signing_bytes(H.L[H.LABEL_OF_KIND[o["kind"]]], o))
    wsk = KS.WskSigner(G.K.priv["wsk_a"])
    with pytest.raises(KS.KeyUseError):
        wsk.sign_object(VEC["narrow_cases"][0]["args"]["signed"]["o"])
