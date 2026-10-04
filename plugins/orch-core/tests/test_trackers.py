import subprocess

import pytest

from orch.core import trackers

GH = {"prefix": "GH", "pattern": "GH-(?P<id>\\d+)", "url": "https://github.com/acme/ticket-orch-demo/issues/{id}"}
ABC = {"prefix": "ABC", "pattern": "ABC-(?P<id>\\d+)", "url": "https://jira.example/browse/ABC-{id}"}
JIRA = {"prefix": "ABC", "pattern": "ABC-\\d+", "url": "https://jira.example/browse/{key}"}
LEGACY = {"prefix": "GH", "pattern": "\\d+", "url": "https://github.com/acme/ticket-orch-demo/issues/{key}"}
BODY = "\n\nWhat: add it\nWhy:  needed\nRisk: low\n"


def test_plain_turns_named_groups_into_plain_groups():
    assert trackers.plain("GH-(?P<id>\\d+)") == "GH-(?:\\d+)"
    assert trackers.plain("ABC-\\d+") == "ABC-\\d+"


def test_url_for_uses_the_id_group():
    assert trackers.url_for(GH, "GH-12") == "https://github.com/acme/ticket-orch-demo/issues/12"
    assert trackers.url_for(ABC, "ABC-7") == "https://jira.example/browse/ABC-7"
    assert trackers.url_for(JIRA, "ABC-123") == "https://jira.example/browse/ABC-123"
    assert trackers.url_for(LEGACY, "12") == "https://github.com/acme/ticket-orch-demo/issues/12"
    assert trackers.url_for(GH, "ABC-1") is None


def test_id_is_the_whole_key_without_a_group():
    assert trackers.url_for({"prefix": "X", "pattern": "X-\\d+", "url": "https://x.example/{id}"}, "X-3") == "https://x.example/X-3"


def test_external_ref_upper_cases_and_maps():
    assert trackers.external_ref([JIRA, GH], " gh-12 ") == {"key": "GH-12", "url": "https://github.com/acme/ticket-orch-demo/issues/12"}
    assert trackers.external_ref([GH], "other-1") == {"key": "other-1", "url": None}


def test_a_broken_pattern_never_raises():
    assert trackers.external_ref([{"prefix": "B", "pattern": "B-(", "url": "u/{key}"}], "B-1") == {"key": "B-1", "url": None}
    assert trackers.matches("B-(", "B-1") is False


def test_bare_number_patterns_are_detected():
    assert trackers.matches_bare_number("\\d+") and not trackers.matches_bare_number(GH["pattern"])


@pytest.mark.parametrize("tracker, needle", [
    (GH, None), (JIRA, None), (LEGACY, None),
    ({"prefix": "GH", "pattern": "GH-\\d+", "url": "https://x/{id}"}, "named group (?P<id>"),
    ({"prefix": "GH", "pattern": "GH-\\d+", "url": "https://x/"}, "{key} or {id}"),
    ({"prefix": "GH", "pattern": "GH-(", "url": "https://x/{key}"}, "not a valid regex"),
])
def test_tracker_problem(tracker, needle):
    problem = trackers.tracker_problem(tracker)
    assert (problem is None) if needle is None else (needle in problem)


def test_ops_use_id_urls(configure, agent):
    from orch.core.ops import Ops
    ws = configure(external_trackers=[GH])
    t = Ops(ws, agent).new("x", external="gh-12")
    assert t.meta["external"] == [{"key": "GH-12", "url": "https://github.com/acme/ticket-orch-demo/issues/12"}]


def test_two_id_trackers_work_in_commit_check_and_orch_check(configure, put, ws_root):
    from orch.core.check import run_checks
    from orch.hooks.commit_msg import check_message

    def git(*args):
        subprocess.run(["git", "-C", str(ws_root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    git("init", "-q")
    git("commit", "--allow-empty", "-q", "-m", "ABC-5 Fix thing")
    ws = configure(external_trackers=[GH, ABC], git={"repos": {"hub": {"path": "."}}})
    put("in-progress", size="xs", external=[{"key": "GH-12", "url": None}])
    assert check_message(ws, f"GH-12 Add thing{BODY}") == []
    found = {(f.code, f.level) for f in run_checks(ws)}
    assert ("commit-unlinked-key", "warning") in found and not any(code == "tracker-config" for code, _ in found)


def test_orch_check_reports_a_bad_tracker(configure):
    from orch.core.check import run_checks
    ws = configure(external_trackers=[{"prefix": "GH", "pattern": "GH-\\d+", "url": "https://x/{id}"}])
    found = [f for f in run_checks(ws) if f.code == "tracker-config"]
    assert found and found[0].level == "error" and "named group" in found[0].message


def test_init_refuses_an_id_url_without_the_group():
    from orch.config.answers import parse_tracker
    from orch.errors import ValidationError
    assert parse_tracker("GH=GH-(?P<id>\\d+)=https://github.com/a/b/issues/{id}")["url"].endswith("{id}")
    with pytest.raises(ValidationError, match="named group"):
        parse_tracker("GH=GH-\\d+=https://github.com/a/b/issues/{id}")


@pytest.mark.parametrize("pattern", ["(?P<id>\\d+)-\\1", "GH-(?P<id>\\d+)(?P=id)", "(a)\\1"])
def test_backreferences_are_refused(pattern):
    problem = trackers.tracker_problem({"prefix": "GH", "pattern": pattern, "url": "https://x.example/{key}"})
    assert problem is not None and "backreference" in problem


def test_an_escaped_backslash_is_not_a_backreference():
    assert trackers.tracker_problem({"prefix": "W", "pattern": "W\\\\1-\\d+", "url": "https://x.example/{key}"}) is None


def test_the_pattern_must_compile_in_the_key_group_form():
    # compiles alone, but not once OR-ed as (?P<key>(?:…)) the way the commit check does
    problem = trackers.tracker_problem({"prefix": "K", "pattern": "(?i)K-\\d+", "url": "https://x.example/{key}"})
    assert problem is not None and "not a valid regex" in problem


@pytest.mark.parametrize("url, ok", [
    ("https://github.com/a/b/issues/{id}", True), ("http://jira.local/browse/{key}", True),
    ("https://jira.example/browse/ABC-{id}?x=1", True),
    ("u/{key}", False), ("javascript:alert('{key}')", False), ("//evil.example/{key}", False),
    ("https://{key}.example/x", False), ("https://x.example{id}/y", False), ("https:///{key}", False),
])
def test_url_templates_need_http_and_placeholders_after_the_host(url, ok):
    problem = trackers.tracker_problem({"prefix": "GH", "pattern": "GH-(?P<id>\\d+)", "url": url})
    assert (problem is None) == ok


def test_url_for_encodes_values_in_one_pass():
    t = {"prefix": "X", "pattern": "X-(?P<id>[^ ]+)", "url": "https://x.example/{key}/{id}"}
    assert trackers.url_for(t, "X-{key}") == "https://x.example/X-%7Bkey%7D/%7Bkey%7D"
    assert trackers.url_for(t, "X-a/b?c#d") == "https://x.example/X-a%2Fb%3Fc%23d/a%2Fb%3Fc%23d"
    assert trackers.url_for({"prefix": "X", "pattern": "X-\\d+", "url": "u/{key}"}, "X-1") is None


@pytest.mark.parametrize("pattern", ["\\d+", "\\d{2}", "\\d{1,4}", "[0-9]{4}", "\\d"])
def test_bare_number_probes_several_lengths(pattern):
    assert trackers.matches_bare_number(pattern)


def test_link_texts_are_capped(ws_root, configure, put):
    from orch.core.links import MAX_TEXT, LinkIndex
    ws = configure(external_trackers=[GH])
    assert MAX_TEXT == 300
    links = LinkIndex(ws)
    assert links.keys_in("x" * 299 + " GH-12") == []
    assert links.keys_in("x" * 200 + " GH-12") == ["GH-12"]
