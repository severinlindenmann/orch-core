"""Every artifact entry says who added it (`by`, the actor string), and the ticket document carries it for the phone;
a hand-edited value that is not an actor string never reaches the document."""
from orch.core import store
from orch.core.artifacts import doc_items


def _png(tmp_path, name="shot.png"):
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return p


def test_a_file_records_the_agent(ws, aops, working, tmp_path):
    aops.artifact_add(working, _png(tmp_path))
    [e] = store.load(ws, working)[1].meta["artifacts"]
    assert e["by"] == "agent:claude-code:7f3c9a21"
    assert doc_items(store.load(ws, working)[1])[0]["by"] == "agent:claude-code:7f3c9a21"


def test_a_link_records_the_human(ws, hops, working):
    hops.artifact_link(working, "https://ci.example.com/run/1")
    assert doc_items(store.load(ws, working)[1])[-1]["by"] == "human:you"


def test_a_scanned_file_records_the_scanner(ws, aops, working):
    folder = ws.artifacts_dir / working
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "notes.txt").write_text("found later")
    aops.artifact_scan(working)
    [e] = store.load(ws, working)[1].meta["artifacts"]
    assert e["by"].startswith("agent:claude-code")


def test_a_forged_by_never_reaches_the_document(ws, aops, working, tmp_path):
    aops.artifact_add(working, _png(tmp_path))
    path, t = store.load(ws, working)
    t.meta["artifacts"][0]["by"] = "<script>alert(1)</script>"
    assert "by" not in doc_items(t)[0]


def test_older_entries_without_by_stay_valid(ws, aops, working, tmp_path):
    aops.artifact_add(working, _png(tmp_path))
    path, t = store.load(ws, working)
    del t.meta["artifacts"][0]["by"]
    assert "by" not in doc_items(t)[0]
