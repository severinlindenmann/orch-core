"""The v1 fixture (written by v1's own code, see ``make_v1_fixture.py``) and the human-operation workspace."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from orch.ops import human
from tests.importer.helpers import with_repo
from tests.ops.helpers import Cli
from tests.ops.humans import HumanWs, hws, me  # noqa: F401  (fixtures of the human-operation tests)

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


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    """The fixture imported once, for the tests that only read the result (``hws, imported, v1 = shared``); the tests
    that change the workspace or re-run the import keep the per-test ``imported``."""
    tmp = tmp_path_factory.mktemp("imported")
    dst = tmp / "v1-project"
    shutil.copytree(FIXTURE, dst)
    with pytest.MonkeyPatch.context() as mp:
        w = HumanWs(tmp)
        w.bootstrap()
        mp.setattr(human, "open_backend", w.backend)
        mp.setattr(human, "show_secret", w.shown_secrets.append)
        mp.setattr(human, "review_prompt", w.review_prompt)
        w.provider.requests.clear()
        w.provider.shown.clear()
        with_repo(w)
        r = Cli(w, grant=False, session=None)("import", "v1", str(dst))
        assert r.code == 0, r.err + r.out
    yield w, r, dst
    if w.store is not None:
        w.store.close()
