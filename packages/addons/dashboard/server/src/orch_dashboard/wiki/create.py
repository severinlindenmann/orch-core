"""Create the wiki folder: ``wiki/readme.md`` with a one-line title, committed alone (prototype of run-9 feedback 3.3).

Only for the owner or a maintainer, only while the Wiki add-on is on and the project has no wiki folder at HEAD. The
file is committed with the identity ``orch <orch@localhost>`` under the isolated git environment (the same pattern as
setup's first commit); ``git commit --only -- wiki/readme.md`` leaves everything else the person has staged alone.
Like the records tool, the dashboard authorizes the person through the host and then runs git as the person itself."""

import os
import subprocess
from pathlib import Path

from orch import gitsafe

from ..errors import fail
from . import reader, scope

TITLE = "# Wiki\n"
MESSAGE = "Start the wiki (created by orch)"
IDENTITY = {
    "GIT_AUTHOR_NAME": "orch",
    "GIT_AUTHOR_EMAIL": "orch@localhost",
    "GIT_COMMITTER_NAME": "orch",
    "GIT_COMMITTER_EMAIL": "orch@localhost",
}


def _git(folder: str, env: dict, *args: str) -> None:
    res = subprocess.run(  # noqa: S603 - fixed argv under the isolated environment
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
        cwd=folder, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False,
    )  # fmt: skip
    if res.returncode != 0:
        raise fail("internal", "Git could not create the wiki folder.", exit=1)


def create(gateway, ws: str) -> dict:
    sc = scope.open_wiki(gateway, ws)
    if sc.role not in ("owner", "maintainer"):
        raise fail("role.denied", "Only an owner or a maintainer can create the wiki folder.", exit=3)
    if sc.repo is None:
        raise fail("invalid.input", "This workspace has no Git repository.", exit=5)
    state = reader.list_pages(sc.repo)["state"]
    if state == "no_commits":
        raise fail("invalid.input", "This repository has no commits yet.", exit=5)
    if state != "no_folder":
        raise fail("conflict", "This project already has a wiki folder.", exit=8)
    folder = sc.repo.folder
    target = Path(folder, "wiki")
    if os.path.lexists(target):
        raise fail("conflict", "There is already a file or folder called wiki here.", exit=8)
    try:
        own = {**os.environ}
        entries = gitsafe.local_config(folder, own)
        if gitsafe.exec_risk(entries):
            raise fail("invalid.input", "This repository's git settings are not safe to use here.", exit=5)
        env = {**gitsafe.isolated_env(own), **IDENTITY}
    except gitsafe.Unsafe:
        raise fail("invalid.input", "This repository's git settings are not safe to use here.", exit=5) from None
    os.mkdir(target, 0o755)
    fd = os.open(target / "readme.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as out:
        out.write(TITLE)
    _git(folder, env, "add", "--", "wiki/readme.md")
    _git(folder, env, "commit", "--quiet", "--no-verify", "--only", "-m", MESSAGE, "--", "wiki/readme.md")
    return {"created": True, "path": "wiki/readme.md"}
