"""P14: on a stock Ubuntu account (umask 002: ~/.config, ~/.local, ~/.local/share, ~/.local/state are 0775) setup makes
orch's folders private ONCE when it starts, before the first host call, for a new workspace and for Finish setup."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from orch import layout as layout_mod
from orch import privdir
from orch.setup.flow import SetupFlow

from .conftest import get, make_env
from .fakes import WID, FakePorts


def _loose_tree(home: Path) -> list[Path]:
    dirs = [home / ".config", home / ".local", home / ".local" / "share", home / ".local" / "state"]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o775)
    return dirs


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def _privatizer(home: Path, state: Path):
    def run(root: Path) -> None:
        privdir.privatize(privdir.setup_paths(root, layout_mod.store_of(root), state, home), os.getuid())

    return run


def test_new_workspace_folders_are_private_before_anything_else(tmp):
    home = tmp.home
    state = home / ".local" / "state" / "orch"
    loose = _loose_tree(home)
    state.mkdir()
    os.chmod(state, 0o775)
    f = SetupFlow(make_env(tmp, state_dir=state, ports=lambda: FakePorts(), privatize=_privatizer(home, state)))
    f.launch(str(tmp.work))
    assert all(_mode(d) == 0o755 for d in [*loose, state])


def test_finish_setup_makes_them_private_before_the_facts_are_read(tmp):
    home = tmp.home
    state = home / ".local" / "state" / "orch"
    loose = _loose_tree(home)
    state.mkdir()
    (tmp.work / "config.json").write_text("{}")
    entry = {"workspace_id": WID, "prefix": "ANN", "root": str(tmp.work)}
    seen: list[list[int]] = []

    def read_facts(_entry):
        seen.append([_mode(d) for d in loose])
        raise RuntimeError("stop here")

    env = make_env(
        tmp,
        state_dir=state,
        ports=lambda: FakePorts(),
        registered=lambda root: entry if root == str(tmp.work) else None,
        read_facts=read_facts,
        privatize=_privatizer(home, state),
    )
    try:
        SetupFlow(env).launch(str(tmp.work))
    except RuntimeError:
        pass
    assert seen == [[0o755] * 4]


def test_a_failing_privatize_never_stops_setup_from_starting(tmp):
    def boom(_root):
        raise OSError("read-only")

    f = SetupFlow(make_env(tmp, ports=lambda: FakePorts(), privatize=boom))
    f.launch(str(tmp.work))
    assert get(f, "state")[0] == 200


def test_finish_review_names_the_private_folders_in_the_same_words(tmp):
    from .conftest import PROFILE
    from .test_finish import finish_flow, mkfacts

    f = finish_flow(tmp, FakePorts(), [mkfacts()])
    rv = f.dispatch("POST", "profile", {}, PROFILE)
    assert rv[0] == 200
    rv = f.dispatch("POST", "review", {}, {})[1]
    perms = next(x for x in rv["flags"] if x["id"] == "perms")
    assert perms["text"].startswith("Folders made private")
    assert "~/.local/state" in perms["detail"] and "~/.local/share" in perms["detail"]
