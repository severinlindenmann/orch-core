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



# -- round 2 --------------------------------------------------------------------------------------------------------

def _missing(verification, ac=AC2):
    return [n for n, _ in evidence.strict_missing(_t(verification, ac))]


def test_a_doubt_line_blocks_its_criterion_whatever_another_line_says():
    assert _missing("- AC1: could not verify on prod, skipped\n- AC1: ran `make test`, 12 passed\n"
                    "- AC2: out.csv has 3 rows") == [1]
    assert _missing("- AC1, AC2: blocked, could not run anything\n- AC1: ran `make a`\n- AC2: ran `make b`") == [1, 2]
    assert _missing("- notes: AC2 is not implemented yet\n- AC1: ran `make a`\n- AC2: ran `make b`") == [2]


@pytest.mark.parametrize("line", ["- AC1: Verified and/or confirmed manually by reading it",
                                  "- AC1: Works as expected on day 1 of use",
                                  "- AC1: Looks right, checked it 2 times"])
def test_prose_with_a_slash_or_a_bare_number_is_not_concrete(line):
    assert _missing(line, "## Acceptance criteria\n\n- [ ] one\n") == [1]


@pytest.mark.parametrize("line", ["- AC1: Not implemented yet in src/app.py", "- AC1: Doesn't work in src/app.py",
                                  "- AC1: isn't wired in src/app.py", "- AC1: won't load src/app.py",
                                  "- AC1: `make test` fails", "- AC1: src/app.py partially done",
                                  "- AC1: pending review of src/app.py", "- AC1: assuming src/app.py is used",
                                  "- AC1: not applicable to src/app.py", "- AC1: unable to open src/app.py",
                                  "- AC1: cannot open src/app.py"])
def test_the_extended_doubt_list(line):
    assert _missing(line, "## Acceptance criteria\n\n- [ ] one\n") == [1], line


@pytest.mark.parametrize("line", ["- AC1: `grep -rn TODO src` prints nothing",
                                  "- AC1: `orch check` reports no unverified-verdict finding",
                                  "- AC1: pytest tests/test_x.py::test_rows passes",
                                  "- AC1: `make test` 12 passed in 40 ms"])
def test_doubt_words_inside_backticks_or_identifiers_do_not_count(line):
    assert _missing(line, "## Acceptance criteria\n\n- [ ] one\n") == [], line


def test_a_blocked_in_plain_prose_still_blocks_as_documented():
    line = "- AC1: `pytest tests/test_x.py` asserts the route returns 403 when access is blocked"
    assert _missing(line, "## Acceptance criteria\n\n- [ ] one\n") == [1]


def test_no_criteria_is_missing_and_out_of_range_citations_count_for_nothing():
    assert evidence.strict_missing(_t("- AC1: ran `make test`", "## Acceptance criteria\n\n")) == [
        (0, "the ticket has no acceptance criteria")]
    assert _missing("- AC0: ran `make a` on src/a.py\n- AC99: ran `make b` on src/b.py") == [1, 2]
