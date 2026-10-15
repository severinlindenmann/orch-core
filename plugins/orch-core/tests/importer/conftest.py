"""The v1 fixture (written by v1's own code, see ``make_v1_fixture.py``) and the human-operation workspace."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.importer.helpers import with_repo
from tests.ops.humans import hws, me  # noqa: F401  (fixtures of the human-operation tests)

FIXTURE = Path(__file__).parent / "fixtures" / "v1"


@pytest.fixture
def v1(tmp_path) -> Path:
    """A copy of the v1 fixture workspace (the project folder: it holds ``orchestrator/``)."""
    dst = tmp_path / "v1-project"
    shutil.copytree(FIXTURE, dst)
    return dst


@pytest.fixture
def imported(hws, me, v1):  # noqa: F811
    """The fixture imported into a workspace that has the ``pipelines`` repository configured."""
    with_repo(hws)
    r = me("import", "v1", str(v1))
    assert r.code == 0, r.err + r.out
    return r
