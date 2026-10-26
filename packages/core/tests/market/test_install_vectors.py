"""V3 G3 (#921): the product's install checks (``index.evaluate``, ``install.check_entry``, ``install.check_package``)
give the reference answer (``tests/addons/market_ref.install``) on every ``install`` vector. The hub policy has its own
file (``test_policy_vectors.py``: the shared ``evaluate`` vectors). The lockfile rows (G4) run here too."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch.addons.package import PackageError, from_files
from orch.market import MarketError, constants, index
from orch.market import install as mi
from tests.addons import market_ref as ref

CASES = [
    c
    for c in json.loads((Path(ref.__file__).parent / "vectors_market.json").read_text())["cases"]
    if c["check"] == "install"
]
G4 = {c["name"] for c in CASES if "lock" in json.dumps(c["expect"]) or "runtime" in json.dumps(c["args"]["files"])}
assert G4  # the lockfile cases are in this file's scope since V3 G4


def product(a: dict, monkeypatch) -> dict:
    ctx = a["ctx"]
    try:
        if ctx["actor"] != "human":
            raise MarketError("market.actor_refused")
        url = ctx["market_repo"]
        official = url == ctx["official"]["repo"]
        monkeypatch.setattr(constants, "OFFICIAL_KEYS", (ctx["official"]["key"],))
        monkeypatch.setattr(constants, "OFFICIAL_URL", ctx["official"]["repo"])
        from orch.market.url import canonical_market_url

        canonical_market_url(url)
        v = index.evaluate(json.dumps(a["index"]).encode(), url, official=official, now=0)
        if ctx["last_version"] is not None and v.doc["version"] < ctx["last_version"]:
            raise MarketError("market.rollback")
        if not official and not ctx["isolation"]:
            raise MarketError("market.community_unisolated")
        mctx = mi.Ctx(
            url=url, cls="official" if official else "community", keys=(ctx["official"]["key"],) if official else (),
            core_version=ctx["core_version"], git_hosts=frozenset(ctx["git_hosts"]), installed=ctx["installed"],
            known_keys=ctx["known_keys"], seen=ctx["seen"], builtin=frozenset(ctx["builtin"]),
            official_ids=frozenset(ctx["official_ids"]), moved=frozenset(ctx["moved"]),
        )  # fmt: skip
        facts = mi.check_entry(mctx, v.doc, a["id"])
        try:
            pkg = from_files({p: t.encode() for p, t in a["files"].items()})
        except PackageError:
            raise MarketError("market.package_invalid") from None
        mi.check_package(facts, pkg, ctx["core_version"])
        return {"ok": {"id": facts.entry["id"], "tofu": facts.tofu, "package_sha256": pkg.digest}}
    except MarketError as exc:
        return {"refuse": exc.code}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_product_gives_the_reference_answer(case, monkeypatch):
    want = ref.run_case(case)
    got = product(case["args"], monkeypatch)
    if "ok" in want:
        assert got["ok"] == {k: want["ok"][k] for k in ("id", "tofu", "package_sha256")}
    else:
        assert got == want
