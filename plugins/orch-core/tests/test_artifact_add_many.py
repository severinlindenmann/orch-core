"""#215: `orch artifact add <id> a.png b.png --ac 2` attaches several files all-or-nothing, with one Log line."""
import pytest

from orch.cli import run
from orch.core import store
from orch.core.events import read_events
from orch.errors import UsageError, ValidationError


def _f(tmp_path, name, data=b"x"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _t(ws, tid):
    return store.load(ws, tid)[1]


def _folder(ws, tid):
    d = ws.artifacts_dir / tid
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def test_several_files_in_one_call(ws, aops, working, tmp_path):
    files = [_f(tmp_path, "a.png"), _f(tmp_path, "b.png"), _f(tmp_path, "c.log")]
    dests = aops.artifact_add_many(working, files, ac=1, label="shots")
    assert [d.name for d in dests] == ["a.png", "b.png", "c.log"]
    t = _t(ws, working)
    assert [e["name"] for e in t.meta["artifacts"]] == ["a.png", "b.png", "c.log"]
    assert all(e["ac"] == 1 and e["label"] == "shots" and e["sha256"] for e in t.meta["artifacts"])
    added = [l for l in t.section("Log").splitlines() if "artifact" in l]
    assert len(added) == 1 and "a.png" in added[0] and "c.log" in added[0]
    ev = read_events(ws, working)[-1]
    assert ev.kind == "artifact.added" and ev.data["names"] == ["a.png", "b.png", "c.log"]


def test_a_missing_file_changes_nothing(ws, aops, working, tmp_path):
    with pytest.raises(UsageError):
        aops.artifact_add_many(working, [_f(tmp_path, "a.png"), tmp_path / "nope.png"])
    assert _folder(ws, working) == [] and not _t(ws, working).meta.get("artifacts")


def test_a_bad_ac_changes_nothing(ws, aops, working, tmp_path):
    with pytest.raises(ValidationError):
        aops.artifact_add_many(working, [_f(tmp_path, "a.png"), _f(tmp_path, "b.png")], ac=9)
    assert _folder(ws, working) == [] and not _t(ws, working).meta.get("artifacts")


def test_an_existing_file_in_the_batch_changes_nothing(ws, aops, working, tmp_path):
    aops.artifact_add(working, _f(tmp_path, "b.png", b"old"))
    other = tmp_path / "sub"
    other.mkdir()
    before = _t(ws, working).section("Log")
    with pytest.raises(ValidationError, match="already exists"):
        aops.artifact_add_many(working, [_f(tmp_path, "a.png"), _f(other, "b.png", b"new")])
    assert _folder(ws, working) == ["b.png"]
    assert (ws.artifacts_dir / working / "b.png").read_bytes() == b"old"
    assert _t(ws, working).section("Log") == before


def test_replace_applies_to_every_file(ws, aops, working, tmp_path):
    aops.artifact_add(working, _f(tmp_path, "a.png", b"old"))
    other = tmp_path / "sub"
    other.mkdir()
    aops.artifact_add_many(working, [_f(other, "a.png", b"new"), _f(other, "b.png")], replace=True)
    assert (ws.artifacts_dir / working / "a.png").read_bytes() == b"new"
    assert [e["name"] for e in _t(ws, working).meta["artifacts"]] == ["a.png", "b.png"]


def test_duplicate_names_inside_the_batch_are_refused(ws, aops, working, tmp_path):
    other = tmp_path / "sub"
    other.mkdir()
    with pytest.raises(UsageError, match="twice"):
        aops.artifact_add_many(working, [_f(tmp_path, "a.png"), _f(other, "a.png")])
    assert _folder(ws, working) == []


def test_too_large_file_changes_nothing(ws, aops, working, tmp_path, monkeypatch):
    from orch.core import artifacts as art
    monkeypatch.setattr(art, "max_bytes", lambda ws: 5)
    with pytest.raises(ValidationError, match="larger"):
        aops.artifact_add_many(working, [_f(tmp_path, "a.png"), _f(tmp_path, "b.png", b"123456789")])
    assert _folder(ws, working) == []


def test_a_failing_copy_rolls_back(ws, aops, working, tmp_path, monkeypatch):
    from orch.core import ops as ops_mod
    real = ops_mod._copy_capped
    calls = []

    def flaky(src, stream, dest, limit):
        calls.append(dest.name)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(src, stream, dest, limit)

    monkeypatch.setattr(ops_mod, "_copy_capped", flaky)
    with pytest.raises(OSError):
        aops.artifact_add_many(working, [_f(tmp_path, "a.png"), _f(tmp_path, "b.png")])
    assert _folder(ws, working) == [] and not _t(ws, working).meta.get("artifacts")


def test_name_and_inline_need_one_file(ws, aops, working, tmp_path):
    two = [_f(tmp_path, "a.png"), _f(tmp_path, "b.png")]
    with pytest.raises(UsageError):
        aops.artifact_add_many(working, two, name="x.png")
    with pytest.raises(UsageError):
        aops.artifact_add_many(working, two, ac=1, inline=True)
    assert _folder(ws, working) == []


def test_cli_takes_several_files(ws_root, ws, aops, working, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    monkeypatch.setenv("ORCH_SESSION", "7f3c9a21-0000")
    a, b = _f(tmp_path, "a.png"), _f(tmp_path, "b.png")
    assert run(["artifact", "add", working, str(a), str(b), "--ac", "2"]) == 0
    out = capsys.readouterr().out
    assert "a.png" in out and "b.png" in out
    assert _folder(ws, working) == ["a.png", "b.png"]
    assert run(["artifact", "add", working, str(a), str(b), "--name", "x.png"]) != 0
    assert run(["artifact", "add", working, str(a), "--url", "https://example.com/x"]) != 0
