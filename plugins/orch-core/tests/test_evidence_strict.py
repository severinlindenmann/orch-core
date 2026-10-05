"""The strict evidence rules an unattended close relies on (orch.core.evidence.strict_missing), with the review's
probes: negative wording, one line citing every criterion, prose without anything concrete."""
import pytest

from orch.core import evidence, factory_close as fc, factory_report, permits, store
from orch.core.model import parse_ticket
from test_factory_release import (_not_stopping, bin_dir, fa, fh, fws, ready, recipe, remote,  # noqa: F401
                                  switch)

AC2 = "## Acceptance criteria\n\n- [ ] one\n- [ ] two\n"


def _t(verification: str, ac: str = AC2):
    return parse_ticket(f"---\nid: L-0009\ntitle: x\nstatus: testing\n---\n\n{ac}\n## Verification\n\n{verification}\n",
                        "L-0009-x.md")


@pytest.mark.parametrize("line,ok", [
    ("- AC1: ran `make test`, 12 passed", True),
    ("- AC1: out.csv has one row per order", True),
    ("- AC1: see https://ci.example/run/7", True),
    ("- AC1: test_export_rows passes", True),
    ("- AC1: could not verify this, blocked by missing access", False),
    ("- AC1: couldn't run it here", False),
    ("- AC1: not verified yet, but the code looks right", False),
    ("- AC1: skipped because of time, see notes", False),
    ("- AC1: TODO run the export and look at the rows", False),
    ("- AC1: the export works well and looks right to me", False),  # prose, nothing concrete
    ("- AC1: unable to start the server on port 80", False),
    ("- AC1: ok", False),
])
def test_one_line_per_criterion(line, ok):
    missing = dict(evidence.strict_missing(_t(line, "## Acceptance criteria\n\n- [ ] one\n")))
    assert (1 not in missing) is ok, (line, missing)


def test_a_line_citing_several_criteria_counts_for_none():
    t = _t("- AC1, AC2: ran `make test`, 12 passed")
    assert evidence.progress(t) == (2, 2)  # the human-facing rule still counts it
    assert [n for n, _ in evidence.strict_missing(t)] == [1, 2]
    t = _t("- see notes, AC1 AC2 all fine in out.csv")
    assert [n for n, _ in evidence.strict_missing(t)] == [1, 2]
    t = _t("- AC1: ran `make test` 12 passed\n- AC2: out.csv has 3 rows")
    assert evidence.strict_missing(t) == []


def test_a_citation_in_the_text_counts_only_for_the_prefix():
    t = _t("- AC1: ran `make test`, covers AC2 too")
    assert [n for n, _ in evidence.strict_missing(t)] == [2]


def test_negative_evidence_never_closes_the_epic(fws, ready, fa, human):
    eid, (c,), _ = ready(release="none", charter={"close": True})
    fa.set_section(c, "Verification", "- AC1: could not verify this, blocked by missing access")
    epic = store.load(fws, eid)[1]
    assert factory_report.ready(fws, epic) is not None  # the human-facing Ready report still shows it
    assert fc.tick(fws, human) == [] and store.load(fws, eid)[1].status == "open"
    bl = fc.blockers(fws, epic, permits.factory_delegation(fws, epic))
    assert [b["code"] for b in bl] == ["evidence"] and "could not" in bl[0]["text"] and not bl[0]["pending"]
