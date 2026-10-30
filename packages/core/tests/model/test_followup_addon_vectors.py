"""The add-on event vectors of follow-ups (#718, ``contracts/core/followup_addon_event_vectors.json``) replayed through
``orch.model``. T0: ``uv run pytest tests/model/test_followup_addon_vectors.py -x -q``"""

import json
from datetime import timedelta
from pathlib import Path

import pytest

from orch import canon
from orch.model import Code
from tests.model.world import World, digest, refused, stamp

V = json.loads((Path(__file__).resolve().parents[4] / "contracts/core/followup_addon_event_vectors.json").read_text())


def code_of(result):
    code = getattr(result, "code", None)
    return None if code is None else str(code)


def run(sc):
    w = World().bootstrap({"tom": "member"})
    w.wev(
        "addon.granted", "sev", name="schedules", version="1.0.0", package_sha256=digest("schedules"),
        capabilities=[], fields={}, sections=[], artifact_kinds=[],
    )  # fmt: skip
    uid = w.ticket(owner="tom")
    agent = w.claim(uid, "tom")
    sets = []
    for i, st in enumerate(sc["steps"]):
        where = f"{sc['name']} step {i}"
        if st["op"] == "grant":
            p = {**st["payload"], "package_sha256": digest(st["payload"]["name"])}
            got = refused(w, "workspace", "addon.granted", "sev", **p)
            assert (None if got is None else str(got)) == st["expect"], where
            if st["expect"] is None:
                w.wev("addon.granted", "sev", **p)
        elif st["op"] == "set":
            e, r = w.try_(
                uid, "followup.set", agent, followup="fu_" + w.nid(), trigger=st["trigger"], on=st["on"],
                deadline=stamp(w.clock + timedelta(seconds=st["deadline_after_s"])), brief="x",
            )  # fmt: skip
            assert code_of(r) == st["expect"], f"{where}: {r}"
            if st["expect"] is None:
                sets.append(e)
        else:
            w.clock += timedelta(seconds=st["after_s"])
            extra = {}
            if "event" in st:
                ev = dict(st["event"])
                off = ev.pop("at_offset_s")
                extra["event"] = {**ev, "at": stamp(w.clock + timedelta(seconds=off))}
            if "fallback" in st:
                extra["fallback"] = st["fallback"]
            _, r = w.try_(
                uid, "followup.fired", w.HOST, followup=sets[st["set"]]["followup"], grant="gr_" + w.nid(),
                trigger=st["trigger"], secret_hash=canon.grant_secret_hash(b"s" * 32), run="r_" + w.nid(), **extra,
            )  # fmt: skip
            assert code_of(r) == st["expect"], f"{where}: {r}"


@pytest.mark.parametrize("sc", V["scenarios"], ids=lambda s: s["name"])
def test_vector(sc):
    run(sc)


def test_the_vectors_cover_the_refusals_that_matter():
    codes = {st.get("expect") for sc in V["scenarios"] for st in sc["steps"]}
    assert {None, str(Code.FOLLOWUP_DENIED), "addon.value_invalid"} <= codes
    assert len(V["scenarios"]) >= 10
