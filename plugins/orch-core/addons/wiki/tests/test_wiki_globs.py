import pytest

from orch_wiki.globs import glob_match, split_repo


@pytest.mark.parametrize("pattern, path, repo, ok", [
    ("src/acme/ingest/**", "src/acme/ingest/loader.py", None, True),
    ("src/acme/ingest/**", "src/acme/ingest/sub/x.py", None, True),
    ("src/acme/ingest/**", "src/acme/quality/checks.py", None, False),
    ("src/*.py", "src/a.py", None, True),
    ("src/*.py", "src/a/b.py", None, False),
    ("**/*.sql", "sql/a.sql", None, True),
    ("**/*.sql", "a.sql", None, True),
    ("docs/", "docs/x/y.md", None, True),
    ("./src/a.py", "src/a.py", None, True),
    ("/src/a.py", "./src/a.py", None, True),
    ("src/a?.py", "src/ab.py", None, True),
    ("src/[x].py", "src/[x].py", None, True),
    ("src/[x].py", "src/x.py", None, False),
    ("acme-energy-data:sql/*.sql", "sql/a.sql", "acme-energy-data", True),
    ("acme-energy-data:sql/*.sql", "sql/a.sql", "other", False),
    ("acme-energy-data:sql/*.sql", "sql/a.sql", None, True),
])
def test_glob_match(pattern, path, repo, ok):
    assert glob_match(pattern, path, repo) is ok


def test_split_repo():
    assert split_repo("ingest:src/**") == ("ingest", "src/**")
    assert split_repo("src/a:b.py") == (None, "src/a:b.py") and split_repo("ingest:") == (None, "ingest:")
