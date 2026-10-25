"""Shared fakes and fixtures of the tier B (spec 13.3) tests: a made-up agent UID, the shared run directory, fake pins
and user database, a fake service manager. Import the fixtures you use (``tree``, ``made``, ``pins_dir``).
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.host.listener import SHARED_MODE, Listener
from orch.host.pins import AgentPins, AgentShare
from tests.host.lifecycle_helpers import short_dir

UID, GID = os.getuid(), os.getgid()
AGENT_UID = UID + 4242
WS = "b" * 32


def share(gid: int = GID, host_uid: int = UID) -> AgentShare:
    return AgentShare(
        host_uid,
        AGENT_UID,
        gid,
        "orch-agent-sev",
        "sev",
        Path("/run/orch/agents/orch-agent-sev"),
        "orch-runner@sev.service",
    )


@pytest.fixture
def tree():
    path = short_dir()
    try:
        yield path
    finally:
        shutil.rmtree(path)


def shared_run(tree: Path, mode: int = SHARED_MODE) -> Path:
    """What root's tmpfiles line makes: the person's directory, group = the agent's, setgid."""
    run = tree / "r"
    run.mkdir(mode=0o700)
    os.chmod(run, mode)
    return run


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def made():
    items: list[Listener] = []

    def make(run, name, **kwargs):
        kwargs.setdefault("host_uid", UID)
        kwargs.setdefault("gid", GID)
        item = Listener(run, name, **kwargs)
        items.append(item)
        return item

    yield make
    for item in items:
        item.close()


def fake_systemctl(out: str, code: int = 0, seen: list | None = None):
    def run(argv):
        if seen is not None:
            seen.append(list(argv))
        return SimpleNamespace(returncode=code, stdout=out, stderr="")

    return run


class FixedPins:
    def __init__(self, found):
        self.found = found

    def share_for(self, host_uid):
        if isinstance(self.found, Exception):
            raise self.found
        return self.found


def show(pid="4242", start="5000000"):
    return f"MainPID={pid}\nExecMainStartTimestampMonotonic={start}\n"


class Names:
    """A user database with one person (``sev``) and one agent user."""

    def __init__(self, agent_members=(), agent_name="orch-agent-sev", other_in_group=False):
        self.agent_members, self.agent_name, self.other_in_group = tuple(agent_members), agent_name, other_in_group

    def getpwuid(self, uid):
        users = {
            UID: SimpleNamespace(pw_name="sev", pw_uid=UID, pw_gid=GID),
            AGENT_UID: SimpleNamespace(pw_name=self.agent_name, pw_uid=AGENT_UID, pw_gid=AGENT_UID),
        }
        if uid not in users:
            raise KeyError(uid)
        return users[uid]

    def getgrgid(self, gid):
        if gid != AGENT_UID:
            return SimpleNamespace(gr_name="staff", gr_mem=[])
        return SimpleNamespace(gr_name="orch-agent-sev", gr_mem=list(self.agent_members))

    def getpwall(self):
        users = [self.getpwuid(UID), self.getpwuid(AGENT_UID)]
        if self.other_in_group:
            users.append(SimpleNamespace(pw_name="mallory", pw_uid=999999, pw_gid=AGENT_UID))
        return users

    def share(self, pins):
        return pins.share_for(UID, getpwuid=self.getpwuid, getgrgid=self.getgrgid, getpwall=self.getpwall)


def write_pin(directory: Path, uid: int, doc: dict, mode: int = 0o644) -> Path:
    import json

    path = directory / f"{uid}.json"
    path.write_text(json.dumps(doc))
    os.chmod(path, mode)
    return path


@pytest.fixture
def pins_dir(tree):
    path = tree / "pins"
    path.mkdir(mode=0o755)
    return path


AGENT_PIN = {"class": "agent", "host_uid": UID, "workspaces": "*"}


class SharedPins(AgentPins):
    def __init__(self, directory, found):
        super().__init__(directory, owner=UID)
        self.found = found

    def share_for(self, host_uid, **kwargs):
        if isinstance(self.found, Exception):
            raise self.found
        return self.found
