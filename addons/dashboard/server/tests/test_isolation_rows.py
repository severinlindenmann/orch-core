"""L13: the dashboard passes on only checked {what, fix} rows of the host's isolation problems."""

from orch_dashboard import adapters


def test_rows_are_rebuilt_checked_and_capped():
    rows = [{"what": "w" * 500, "fix": "f", "extra": "x"}, {"what": 1, "fix": "f"}, "x", {"what": "a"}]
    got = adapters.isolation_rows({"isolation": rows})
    assert got == [{"what": "w" * 400, "fix": "f"}]
    assert len(adapters.isolation_rows({"isolation": [{"what": "a", "fix": "b"}] * 50})) == 20


def test_a_host_without_isolation_gives_an_empty_list():
    for data in ({}, {"isolation": "x"}, None, []):
        assert adapters.isolation_rows(data) == []
