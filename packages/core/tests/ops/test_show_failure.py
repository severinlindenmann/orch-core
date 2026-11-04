# ruff: noqa: F811 (shared pytest fixtures are also function arguments)

"""W30b: the hosted show result reaches both CLI renderers."""

import pytest

from tests.host.test_hostclient import hcli
from tests.host.test_reads_ticket import w  # noqa: F401
from tests.host.test_show_failure import FAILURE, LINE, failed


@pytest.mark.parametrize("mode", [[], ["--full"], ["--log"], ["--diff"], ["--section", "plan"]])
def test_show_failed_start_text_and_json(w, mode):
    _, key = w.ticket()
    failed(w, key)
    cli = hcli(w.lab)
    text = cli("show", key, *mode)
    assert text.code == 0, text.err
    assert LINE in text.out.splitlines()
    result = cli.j("show", key, *mode)
    assert result.code == 0, result.err
    assert result.data["ticket"]["last_agent_start_failure"] == FAILURE
    assert "SECRET RAW" not in text.out + result.out


def test_show_direct_has_no_failed_start_summary(ws, cli):
    cli("new", "Direct ticket")
    result = cli.j("show", "1")
    assert result.code == 0, result.err
    assert "last_agent_start_failure" not in result.data["ticket"]
    assert "Last agent start failed:" not in cli("show", "1").out
