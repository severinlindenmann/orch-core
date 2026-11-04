"""``orch host webhooks on|off|show``: the receiver setting lives in the host state dir, so a service host (whose unit
carries no flags) serves webhooks after a restart (run 12, 6.5)."""

import io
import json
import os

from orch.host import cli, hooks


def run(state, *argv, env=None):
    out = io.StringIO()
    rc = cli.main(["webhooks", *argv, "--state", str(state)], env=env or {}, out=out)
    return rc, out.getvalue()


def test_on_keeps_a_free_loopback_port_and_off_clears_it(tmp_path):
    rc, text = run(tmp_path, "on")
    assert rc == 0 and "orch host restart" in text
    saved = hooks.read_settings(tmp_path, os.getuid())
    addr, _, port = saved["listen"].rpartition(":")
    assert addr == "127.0.0.1" and int(port) > 0 and saved["url"] == f"http://127.0.0.1:{port}"
    assert json.loads(run(tmp_path, "show")[1]) == saved
    assert run(tmp_path, "off")[0] == 0
    assert hooks.read_settings(tmp_path, os.getuid()) is None


def test_url_and_address_are_taken_and_a_wildcard_needs_a_url(tmp_path):
    run(tmp_path, "on", "--address", "127.0.0.1", "--port", "8191", "--url", "https://hooks.example.test")
    saved = hooks.read_settings(tmp_path, os.getuid())
    assert saved == {"listen": "127.0.0.1:8191", "url": "https://hooks.example.test"}


def test_an_agent_cannot_change_it(tmp_path):
    rc, _ = run(tmp_path, "on", env={"ORCH_GRANT": "x"})
    assert rc != 0 and hooks.read_settings(tmp_path, os.getuid()) is None


def test_on_works_before_the_state_dir_exists(tmp_path):
    assert run(tmp_path / "fresh" / "state", "on")[0] == 0
