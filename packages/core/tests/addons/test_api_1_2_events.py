"""addon-api 1.2 (#718, C9): the add-on `events`. T0: `uv run pytest tests/addons/test_api_1_2_events.py -x -q`"""

from __future__ import annotations

import copy
import json

import pytest

from orch.addons.manifest import ManifestError, declarations, load_manifest
from orch.model import followup_events as fe
from tests.addons.helpers import manifest

EVENT = {
    "kind": "run.finished",
    "title": "Run finished",
    "statuses": ["succeeded", "failed"],
    "ref": {"format": "int"},
    "poll": {"interval_s": 300},
}
V4 = {"schema": "orch.addon/3", "api": ">=1.2,<2", "capabilities": ["network"]}


def load(**over):
    return load_manifest(json.dumps(manifest(**{**V4, **over})).encode())


def bad(**over):
    with pytest.raises(ManifestError):
        load(**over)


def ev(**over):
    e = copy.deepcopy(EVENT)
    e.update(over)
    return e


def test_events_load_and_are_declared_sorted_and_flat():
    m = load(events=[ev(kind="zed.done"), EVENT])
    d = declarations(m)["events"]
    assert [e["kind"] for e in d] == ["run.finished", "zed.done"]
    assert d[0] == {
        "kind": "run.finished",
        "title": "Run finished",
        "statuses": ["failed", "succeeded"],
        "ref": "int",
        "interval_s": 300,
    }


def test_no_events_leaves_the_declarations_as_they_were():
    assert "events" not in declarations(load())


@pytest.mark.parametrize(
    "over",
    [
        {"api": ">=1.1,<2"},
        {"api": ">=1.0,<2"},
        {"capabilities": ["spawn_agent"]},
        {"events": [EVENT, EVENT]},
        {"events": [ev(kind="finished")]},  # needs <word>.<word>
        {"events": [ev(kind="Run.Finished")]},
        {"events": [ev(statuses=[])]},
        {"events": [ev(statuses=["a", "a"])]},
        {"events": [ev(statuses=["a", "b", "c", "d", "e"])]},
        {"events": [ev(statuses=["Has Space"])]},
        {"events": [ev(ref={"format": "regex"})]},
        {"events": [ev(ref={"format": "int", "pattern": ".*"})]},
        {"events": [ev(poll={"interval_s": 59})]},
        {"events": [ev(poll={"interval_s": 3601})]},
        {"events": [ev(poll={"interval_s": 300, "command": ["x"]})]},
        {"events": [ev(title="x‮y")]},
        {"events": [ev(title="")]},
        {"events": [ev(title=" padded")]},
        {"events": [ev(extra=1)]},
        {"events": [ev(kind=f"k{i}.done") for i in range(9)]},
    ],
)
def test_events_are_strict(over):
    base = {"events": [EVENT]}
    bad(**{**base, **over})


@pytest.mark.parametrize(
    ("fmt", "good", "wrong"),
    [
        ("int", ["8812", "9" * 19], ["", "x", "1" * 20, "-1", "1.5"]),
        ("slug", ["a", "abc_1.2:x-y"], ["", ".a", "a..b", "-a", "a" * 65, "a b", "a/b"]),
        (
            "owner_repo_number",
            ["acme:api:412", "a-b:r.e_p-o:1"],
            [
                "acme:api",
                "acme:api:0",
                "-a:r:1",
                "a-:r:1",
                "a--b:r:1",
                "acme:..:1",
                "acme:.:1",
                "acme:api:1x",
                "a:b:1234567890",
            ],
        ),
        ("hex40", ["a" * 40], ["a" * 39, "A" * 40, "g" * 40]),
    ],
)
def test_ref_formats_are_closed(fmt, good, wrong):
    assert all(fe.ref_fits(fmt, r) for r in good)
    assert not any(fe.ref_fits(fmt, r) for r in wrong)
    assert not fe.ref_fits("nope", "1") and not fe.ref_fits(fmt, None) and not fe.ref_fits(fmt, 5)


def test_core_is_api_1_2_and_an_older_core_range_still_fits():
    from orch.addons.manifest import incompatibility

    assert incompatibility(load(events=[EVENT]), "2.0.0.dev0") is None


def test_the_grant_event_carries_the_declaration_and_every_review_uses_the_same_words():
    from orch.addons import event_words
    from orch.addons.install import grant_event
    from orch.host import signcontent

    m = load(events=[EVENT])
    ev = grant_event(m, _Pkg())
    assert ev["events"] == declarations(m)["events"]
    (words,) = signcontent._events_words(ev["events"], ev["capabilities"])  # noqa: SLF001
    assert "can reach the internet (orch does not limit where)" in words and "Run finished" in words
    assert "stored key" not in words
    # the same helper makes the line of `orch addon grant` and the hub summary; "your stored key" only with a credential
    (keyed,) = event_words.event_lines(ev["events"], ["network", "credential"])
    assert keyed == words + "; it uses your stored key"
    with pytest.raises(signcontent.PlainUnavailable):
        signcontent._events_words([{"kind": "x"}], [])  # noqa: SLF001


class _Pkg:
    digest = "sha256:" + "0" * 64
    files: dict = {}


def test_orch_addon_grant_reviews_each_event_in_the_shared_words():
    from orch.addons import event_words
    from orch.addons.install import grant_event, review_lines

    m = load(events=[EVENT])
    ev = grant_event(m, _Pkg())
    lines = review_lines(m, _Pkg(), ev, None)
    (line,) = [x for x in lines if x.startswith("event ")]
    assert event_words.event_lines(ev["events"], ev["capabilities"])[0] in line
    assert "can reach the internet (orch does not limit where)" in line and "reports" in line
