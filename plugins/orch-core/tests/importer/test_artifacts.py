"""v1 files become v2 artifacts: names, digests, references, and what is refused (symlinks, big files)."""

from __future__ import annotations

import os

from orch import canon
from orch.importer.v1 import MAX_ARTIFACT_BYTES, read_artifact, read_v1
from tests.importer.helpers import tree, with_repo
from tests.ops.humans import hws, me  # noqa: F401


def arts(ws, ref):
    return {a.name: a for a in ws.view(ref).artifacts}


def test_files_come_in_with_their_digest_and_never_as_evidence(imported, hws):
    a = arts(hws, "DEMO-0003")
    assert sorted(a) == ["after.png", "sub_out.log", "v1-import.json"]  # sub/out.log is flattened, gone.csv is missing
    uid = hws.uid("DEMO-0003")
    for name, art in a.items():
        data = (hws.root / "tickets" / uid / "artifacts" / name).read_bytes()
        assert art.digest == canon.artifact_digest(data)
        assert art.ac is None and art.task is None  # v1's ac/task links are not carried
    assert all(not ac.evidence for ac in hws.view("DEMO-0003").acceptance)  # so no criterion has evidence
    assert a["after.png"].kind == "screenshot" and a["sub_out.log"].kind == "log"  # a v1 receipt is a plain log


def test_labels_keep_what_v1_said(imported, hws):
    ev = [e for e in hws.events("DEMO-0003") if e["type"] == "artifact.added"]
    labels = {e["name"]: e["label"] for e in ev}
    assert labels["after.png"] == "v1 screenshot | ac 1 | After"
    assert labels["sub_out.log"] == "v1 receipt | task T2"


def test_references_follow_renames_and_dangling_ones_are_neutralised(imported, hws):
    body = (hws.root / "tickets" / hws.uid("DEMO-0003") / "body.md").read_text()
    assert "(artifact:after.png)" in body
    assert "(missing-artifact:gone.csv)" in body and "(artifact:gone.csv)" not in body
    assert "```text\n## not a heading\n```" in body  # a heading inside a fence is text


def test_missing_and_changed_files_are_reported_not_imported(hws, me, v1):  # noqa: F811
    with_repo(hws)
    (v1 / "orchestrator" / "artifacts" / "DEMO-0003" / "after.png").write_bytes(b"swapped")
    r = me("import", "v1", str(v1))
    assert r.code == 0
    a = arts(hws, "DEMO-0003")
    assert "after.png" not in a and "sub_out.log" in a
    import json

    m = json.loads((hws.root / "tickets" / hws.uid("DEMO-0003") / "artifacts" / "v1-import.json").read_bytes())
    why = {x["name"]: x["why"] for x in m["artifacts"] if not x["imported"]}
    assert why == {"after.png": "sha256 differs from v1's record", "gone.csv": "missing or unsafe"}


def _mkdirs(v1):
    return v1 / "orchestrator" / "artifacts" / "DEMO-0003"


def test_a_symlinked_middle_directory_is_not_followed(v1, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    os.symlink(outside, _mkdirs(v1) / "linked")
    home = v1 / "orchestrator"
    try:
        read_artifact(home, "DEMO-0003", "linked/secret.txt")
    except OSError:
        pass
    else:
        raise AssertionError("followed a symlinked directory")


def test_a_symlinked_file_is_not_followed(v1, tmp_path):
    (tmp_path / "secret.txt").write_text("secret")
    os.symlink(tmp_path / "secret.txt", _mkdirs(v1) / "link.txt")
    try:
        read_artifact(v1 / "orchestrator", "DEMO-0003", "link.txt")
    except OSError:
        pass
    else:
        raise AssertionError("followed a symlinked file")


def test_a_symlinked_artifact_folder_is_not_followed(v1, tmp_path):
    outside = tmp_path / "o"
    outside.mkdir()
    (outside / "x.txt").write_text("x")
    os.symlink(outside, v1 / "orchestrator" / "artifacts" / "DEMO-0009")
    try:
        read_artifact(v1 / "orchestrator", "DEMO-0009", "x.txt")
    except OSError:
        pass
    else:
        raise AssertionError("followed a symlinked folder")


def test_a_symlinked_ticket_folder_or_file_is_reported(v1, tmp_path):
    home = v1 / "orchestrator"
    real = next((home / "tickets" / "backlog").glob("DEMO-0001*"))
    outside = tmp_path / "t"
    outside.mkdir()
    (outside / "DEMO-0010-x.md").write_text(real.read_text().replace("DEMO-0001", "DEMO-0010"))
    os.symlink(outside / "DEMO-0010-x.md", home / "tickets" / "open" / "DEMO-0010-x.md")
    os.rename(home / "tickets" / "testing", tmp_path / "moved")
    os.symlink(tmp_path / "moved", home / "tickets" / "testing")
    ws = read_v1(home)
    keys = {t.key for t in ws.tickets}
    assert "DEMO-0010" not in keys and "DEMO-0004" not in keys  # neither the file nor the folder was followed
    why = {p.where: p.why for p in ws.problems}
    assert "tickets/open/DEMO-0010-x.md" in why and "tickets/testing" in why


def test_a_symlinked_history_is_not_read(v1, tmp_path):
    ev = v1 / "orchestrator" / ".state" / "events.jsonl"
    os.rename(ev, tmp_path / "events.jsonl")
    os.symlink(tmp_path / "events.jsonl", ev)
    ws = read_v1(v1 / "orchestrator")
    assert all(t.events == [] for t in ws.tickets)
    assert any(p.where == ".state/events.jsonl" for p in ws.problems)


def test_a_huge_artifact_is_refused_without_reading_it(v1):
    p = _mkdirs(v1) / "huge.bin"
    with open(p, "wb") as f:
        f.truncate(MAX_ARTIFACT_BYTES + 1)  # sparse: no disk is used
    try:
        read_artifact(v1 / "orchestrator", "DEMO-0003", "huge.bin")
    except OSError as e:
        assert "larger" in str(e)
    else:
        raise AssertionError("read a file over the cap")


def test_a_hard_link_cannot_be_told_from_a_file(v1, tmp_path):
    """Documented limit: a hard link to a file outside is a regular file to the kernel. The importer cannot tell; the
    review says what is imported and v1 data is untrusted, so the person sees the names and sizes."""
    (tmp_path / "outside.txt").write_text("outside")
    os.link(tmp_path / "outside.txt", _mkdirs(v1) / "hard.txt")
    assert read_artifact(v1 / "orchestrator", "DEMO-0003", "hard.txt") == b"outside"


def test_reading_leaves_v1_untouched(v1):
    before = tree(v1)
    read_v1(v1 / "orchestrator")
    assert tree(v1) == before
