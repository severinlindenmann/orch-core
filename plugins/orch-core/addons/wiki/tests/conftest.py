import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # the addon folder, so tests import orch_wiki

from orch.testing.pytest_plugin import orch_user_dir, orch_workspace  # noqa: E402,F401

PAGES = Path(__file__).with_name("fixtures") / "pages"


@pytest.fixture(autouse=True)
def _user_dir(orch_user_dir):
    return orch_user_dir


@pytest.fixture
def pages_dir():
    return PAGES


@pytest.fixture
def clone_with_pages():
    """Put the fixture pages into the addon's clone folder, as if git had already cloned the wiki."""
    def make(state_dir, owner="acme", name="ticket-orch-demo"):
        dest = Path(state_dir) / "git" / owner / f"{name}.wiki"
        (dest / ".git").mkdir(parents=True, exist_ok=True)
        for page in PAGES.glob("*.md"):
            shutil.copy(page, dest / page.name)
        return dest
    return make
