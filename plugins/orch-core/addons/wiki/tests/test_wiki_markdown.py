from orch_wiki.markdown import front_matter, parse_page
from orch_wiki.pages import IndexReader, index_path, page_item, write_index


def parse(pages_dir, stem):
    return parse_page(stem, (pages_dir / f"{stem}.md").read_text(encoding="utf-8"),
                      local_prefix="DEMO", pad=4, tracker_prefixes=("GH",))


def test_front_matter_title_documents_keys_and_links(pages_dir):
    p = parse(pages_dir, "Architecture")
    assert p.title == "Architecture overview"
    assert p.documents == ("src/acme/ingest/**", "acme-energy-data:sql/*.sql")
    assert p.keys == ("DEMO-0003", "DEMO-0007")
    assert p.file_links == ("src/acme/ingest/loader.py",)
    assert p.excerpt.startswith("The ingest service loads meter readings") and p.problems == ()
    assert "architecture overview" not in p.text and "Architecture overview" in p.text  # index lowercases later


def test_heading_title_tracker_keys_and_link_text(pages_dir):
    p = parse(pages_dir, "Runbook")
    assert p.title == "Runbook: nightly load" and p.keys == ("GH-12",)
    assert p.file_links == ("src/acme/quality/checks.py",)
    assert p.excerpt.startswith("When the nightly load fails, check the quality checks first.")


def test_bad_front_matter_is_reported_not_fatal(pages_dir):
    p = parse(pages_dir, "Data-model")
    assert p.problems == ("front matter is not valid YAML",) and p.title == "Data model" and p.documents == ()


def test_title_falls_back_to_the_file_name(pages_dir):
    p = parse(pages_dir, "Home")
    assert p.title == "Home" and p.excerpt == "Welcome to the ticket-orch-demo wiki!" and p.keys == ()
    assert parse_page("Getting-started", "plain", local_prefix="DEMO", pad=4).title == "Getting started"


def test_documents_as_a_string_and_junk():
    p = parse_page("x", "---\ndocuments: src/a.py\n---\nbody", local_prefix="L", pad=4)
    assert p.documents == ("src/a.py",)
    p = parse_page("x", "---\ndocuments: [1, {a: b}, './src/b.py']\n---\nbody", local_prefix="L", pad=4)
    assert p.documents == ("src/b.py",)
    assert front_matter("no front matter") == ({}, "no front matter", [])


def test_page_item_and_index(tmp_path):
    item = page_item(provider="github-wiki", space="acme/ticket-orch-demo", id="Home", title="T" * 300,
                     url="https://github.com/acme/ticket-orch-demo/wiki/Home", path="Home.md",
                     updated_at="2026-10-02T12:17:45+00:00", links=("DEMO-0002", "DEMO-0002"), author="Severin")
    assert len(item["title"]) == 200 and item["links"] == ["DEMO-0002"] and item["author"] == "Severin"
    write_index(tmp_path, "github-wiki", "acme/ticket-orch-demo", {"Home": "Hello World " + "x" * 60000})
    reader = IndexReader(tmp_path)
    texts = reader.texts("github-wiki", "acme/ticket-orch-demo")
    assert texts["Home"].startswith("hello world") and len(texts["Home"]) == 50000
    assert reader.texts("github-wiki", "acme/ticket-orch-demo") is texts  # cached until the file changes
    assert "/" not in index_path(tmp_path, "github-wiki", "acme/ticket-orch-demo").name
    assert reader.texts("github-wiki", "other/space") == {}


def test_index_holds_mentions_computed_at_fetch_time(tmp_path):
    write_index(tmp_path, "github-wiki", "A/b", {"Home": "See DEMO-0003 and the Ingest job; ingest daily. ux"},
                titles={"Home": "Home of DEMO-3"})
    found = IndexReader(tmp_path).mentions("github-wiki", "A/b")["Home"]
    assert found["demo-0003"] == 1 and found["demo-3"] == 1 and found["ingest"] == 2 and found["home"] == 1
    assert "ux" not in found and "of" not in found  # words under three characters are never looked up
    assert IndexReader(tmp_path).mentions("github-wiki", "other/space") == {}
