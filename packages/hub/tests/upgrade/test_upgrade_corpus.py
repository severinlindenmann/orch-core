"""Frozen hub data of every released format must open with the code at HEAD."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from orch_hub.access.store import Access
from orch_hub.dataformat import open_data
from orch_hub.keys.store import HubKeys

pytestmark = pytest.mark.slow

CORPUS = Path(__file__).resolve().parent / "corpus"
CASES = [pytest.param(d, id=d.name) for d in sorted(CORPUS.glob("hub-f*")) if d.is_dir()]


def stage(src: Path, dst: Path) -> Path:
    """Restore a private copy, including permissions and empty directories git does not keep."""
    shutil.copytree(src, dst)
    for p in sorted(dst.rglob("*"), reverse=True):
        if p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    for rel in (dst / "empty-dirs.txt").read_text().splitlines():
        (dst / rel).mkdir(parents=True, exist_ok=True)
    for p in [dst, *dst.rglob("*")]:
        if p.is_symlink():
            raise AssertionError(f"a corpus holds no links: {p}")
        os.chmod(p, 0o700 if p.is_dir() else 0o600)
    return dst


@pytest.mark.parametrize("corpus", CASES)
def test_the_corpus_opens_with_head(corpus: Path, tmp_path: Path) -> None:
    path = stage(corpus, tmp_path / "hub")
    # Once a hub migration step exists, run `orch migrate` through core before opening.
    assert json.loads((path / "format.json").read_text())["format"] == 1
    handle = open_data(path, "hub", 1, env={})
    try:
        keys = HubKeys.open(path)
        assert keys is not None
        access = Access(str(path / "access.db"))
        (user,) = (u for u in access.users() if u["name"] == "Corpus User")
        (group,) = (g for g in access.groups() if g["name"] == "corpus-group")
        assert user["user_id"] in group["members"]
    finally:
        handle.close()


@pytest.mark.parametrize("corpus", CASES)
def test_a_newer_format_is_refused(corpus: Path, tmp_path: Path) -> None:
    path = stage(corpus, tmp_path / "hub")
    marker = path / "format.json"
    doc = json.loads(marker.read_text())
    doc["format"] = 2
    marker.write_text(json.dumps(doc) + "\n")

    handle = None
    try:
        with pytest.raises(Exception) as exc:
            handle = open_data(path, "hub", 1, env={})
        assert exc.value.code == "format.newer"
    finally:
        if handle is not None:
            handle.close()
