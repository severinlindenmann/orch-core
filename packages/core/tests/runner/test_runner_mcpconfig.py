"""The MCP config file of a Claude Code agent in a ticket work folder: content, place, what is refused."""

from __future__ import annotations

import json
import logging
import os

import pytest

from orch.host.mcpshared import new_capability
from orch.runner import mcpconfig
from orch.runner.grants import GrantStore, RedactFilter
from orch.runner.supervise import SpawnError

RUN = "r_01J0000000000000000000000F"
ARGV = [
    "claude",
    "--strict-mcp-config",
    mcpconfig.FLAG,
    mcpconfig.PLACEHOLDER,
    "--append-system-prompt-file",
    "x",
    "go",
]
CAP = new_capability()
GOOD = {"port": 4711, "capability": CAP, "servers": ["wiki", "tickets-2"]}


def env(tmp_path):
    return {"ORCH_WORKSPACE": str(tmp_path)}


def test_the_file_lists_only_the_local_proxy_and_is_private(tmp_path):
    base = tmp_path / "state"
    argv = mcpconfig.prepare(base, RUN, ARGV, env(tmp_path), GOOD)
    path = argv[3]
    assert argv[:3] == ARGV[:3] and argv[4:] == ARGV[4:] and path == str(base / "agent-mcp" / f"{RUN}.json")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600" and oct(os.stat(base / "agent-mcp").st_mode & 0o777) == "0o700"
    raw = open(path).read()
    want = {"type": "http", "headers": {"Authorization": f"Bearer {CAP}"}}
    assert json.loads(raw) == {
        "mcpServers": {n: {**want, "url": f"http://127.0.0.1:4711/m/{n}"} for n in GOOD["servers"]}
    }
    assert "https://" not in raw and "secret" not in raw.lower()
    mcpconfig.remove(base, RUN)
    assert not os.path.exists(path)
    mcpconfig.remove(base, RUN)  # again: nothing to do


def test_without_org_mcp_the_file_has_no_servers(tmp_path):
    argv = mcpconfig.prepare(tmp_path / "s", RUN, ARGV, env(tmp_path), None)
    assert json.load(open(argv[3])) == {"mcpServers": {}}


def test_an_argv_without_the_placeholder_is_untouched(tmp_path):
    plain = ["claude", "go"]
    assert mcpconfig.prepare(tmp_path / "s", RUN, plain, env(tmp_path), None) is plain
    assert not (tmp_path / "s").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["claude", mcpconfig.PLACEHOLDER],
        ["claude", "--other", mcpconfig.PLACEHOLDER],
        [mcpconfig.PLACEHOLDER, "x"],
        ["claude", mcpconfig.FLAG, mcpconfig.PLACEHOLDER, mcpconfig.FLAG, mcpconfig.PLACEHOLDER],
        ["claude", mcpconfig.FLAG, mcpconfig.PLACEHOLDER, mcpconfig.PLACEHOLDER],
    ],
)
def test_a_misplaced_or_repeated_placeholder_is_refused(tmp_path, argv):
    with pytest.raises(SpawnError) as e:
        mcpconfig.prepare(tmp_path / "s", RUN, argv, env(tmp_path), None)
    assert e.value.code == "invalid.input"


@pytest.mark.parametrize("workspace", [None, "", "rel/ws"])
def test_the_workspace_must_be_absolute(tmp_path, workspace):
    with pytest.raises(SpawnError) as e:
        mcpconfig.prepare(tmp_path / "s", RUN, ARGV, {"ORCH_WORKSPACE": workspace} if workspace is not None else {})
    assert e.value.code == "invalid.input"


def test_org_mcp_without_the_placeholder_is_refused(tmp_path):
    with pytest.raises(SpawnError) as e:
        mcpconfig.prepare(tmp_path / "s", RUN, ["claude", "go"], env(tmp_path), GOOD)
    assert e.value.code == "invalid.input"


@pytest.mark.parametrize(
    "bad",
    [
        {**GOOD, "port": True},
        {**GOOD, "port": 0},
        {**GOOD, "port": 65536},
        {**GOOD, "port": "80"},
        {**GOOD, "capability": "ocap_short"},
        {**GOOD, "capability": CAP + "x"},
        {**GOOD, "servers": []},
        {**GOOD, "servers": ["orch-x"]},
        {**GOOD, "servers": ["a", "a"]},
        {**GOOD, "servers": ["Up"]},
        {**GOOD, "servers": "wiki"},
        {**GOOD, "servers": [f"s{i}" for i in range(21)]},
        {**GOOD, "extra": 1},
        {"port": 1, "capability": CAP},
        ["port"],
        "x",
    ],
)
def test_a_bad_org_mcp_is_refused(tmp_path, bad):
    assert mcpconfig.problem(bad) is not None
    with pytest.raises(SpawnError) as e:
        mcpconfig.prepare(tmp_path / "s", RUN, ARGV, env(tmp_path), bad)
    assert e.value.code == "invalid.input"
    assert not (tmp_path / "s" / "agent-mcp" / f"{RUN}.json").exists()


def test_twenty_servers_are_fine():
    assert mcpconfig.problem({**GOOD, "servers": [f"s{i}" for i in range(20)]}) is None
    assert mcpconfig.problem(None) is None


def test_the_log_filter_redacts_a_capability():
    rec = logging.LogRecord("orch.runner", logging.INFO, __file__, 1, "auth %s ok", (CAP,), None)
    assert RedactFilter(GrantStore()).filter(rec)
    assert rec.getMessage() == "auth [redacted] ok"
