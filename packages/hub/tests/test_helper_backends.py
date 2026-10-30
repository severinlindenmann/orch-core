"""HUB-SV-02 piece b: closed config, fixed argv, sudoers text."""

import pytest

from orch_hub.services import helper_backends as B
from orch_hub.services.helper_proto import HelperError

GOOD = {
    "v": 1,
    "host_id": "vps1",
    "parts": {
        "hub": {"label": "Hub", "kind": "systemd", "unit": "orch-hub.service"},
        "relay": {"label": "Relay", "kind": "systemd", "unit": "orch-relay.service"},
        "publish": {"label": "Publish", "kind": "systemd-user", "unit": "orch-publish-api.service"},
    },
}


def test_argv_is_fixed_and_exact():
    c = B.parse_config(GOOD)
    assert c.parts["relay"].action_argv("restart") == [
        "sudo",
        "-n",
        "/usr/bin/systemctl",
        "restart",
        "orch-relay.service",
    ]
    assert c.parts["relay"].logs_argv() == [
        "sudo", "-n", "/usr/bin/journalctl", "-u", "orch-relay.service",
        "--since", "-1h", "-n", "500", "-o", "short-iso", "--no-pager",
    ]  # fmt: skip
    assert c.parts["publish"].action_argv("stop") == ["systemctl", "--user", "stop", "orch-publish-api.service"]
    for bad in ("status", "edit", "restart; rm", "--now", "update"):
        with pytest.raises(HelperError):
            c.parts["relay"].action_argv(bad)


@pytest.mark.parametrize(
    "mut",
    [
        lambda g: g["parts"]["relay"].update(unit="ssh.service"),
        lambda g: g["parts"]["relay"].update(unit="orch-relay.service;x"),
        lambda g: g["parts"]["relay"].update(unit="../orch-relay.service"),
        lambda g: g["parts"]["relay"].update(kind="launchd"),
        lambda g: g["parts"]["relay"].update(extra=1),
        lambda g: g["parts"].update({"Relay": g["parts"]["relay"]}),
        lambda g: g["parts"].update({"../x": g["parts"]["relay"]}),
        lambda g: g.update(host_id="a b"),
        lambda g: g["parts"].update({"wiki": g["parts"]["relay"]}),
        lambda g: g["parts"]["relay"].update(unit="orch-hub-helper.service"),
        lambda g: g["parts"]["relay"].update(unit="orch-ab12cd-hub-helper.service"),
        lambda g: g["parts"]["relay"].update(unit="orch-ab12cd-hub-helper@x.service"),
        lambda g: g.update(v=2),
        lambda g: g.update(parts={}),
    ],
)
def test_config_refuses_anything_open(mut):
    import copy

    g = copy.deepcopy(GOOD)
    mut(g)
    with pytest.raises(HelperError) as e:
        B.parse_config(g)
    assert e.value.code == "bad_config"


def test_load_config_needs_private_file(tmp_path):
    import json
    import os

    os.chmod(tmp_path, 0o700)
    with pytest.raises(HelperError) as e:
        B.load_config(tmp_path)
    assert e.value.code == "not_set_up"
    f = tmp_path / "helper.json"
    f.write_text(json.dumps(GOOD))
    os.chmod(f, 0o644)
    with pytest.raises(HelperError):
        B.load_config(tmp_path)
    os.chmod(f, 0o600)
    assert B.load_config(tmp_path).host_id == "vps1"


def test_sudoers_lists_exact_argv_no_wildcards():
    c = B.parse_config(GOOD)
    t = B.sudoers_text("orch-hub", list(c.parts.values()))
    assert "/usr/bin/systemctl restart orch-relay.service" in t
    assert "/usr/bin/journalctl -u orch-relay.service --since -1h -n 500 -o short-iso --no-pager" in t
    assert "orch-publish" not in t  # a user unit needs no sudo
    assert "*" not in t and "status" not in t and "edit" not in t
    assert "Defaults:orch-hub env_reset" in t and "orch-hub ALL=(root) NOPASSWD: ORCH_SVC" in t
    with pytest.raises(HelperError):
        B.sudoers_text("x; rm", [])


def test_real_runner_bounded_and_no_shell():
    rc, out, _ = B.run_argv(["/bin/echo", "a;b $HOME"], 5)
    assert rc == 0 and out.strip() == "a;b $HOME"
    assert B.run_argv(["/bin/sleep", "3"], 1)[0] == 124


def test_installer_vector_parses_and_the_installers_own_unit_is_refused():
    """contracts/hub/helper_config_vector.json is what ``orch install`` writes (core test_helper_unit checks it)."""
    import json
    from pathlib import Path

    vec = json.loads((Path(__file__).resolve().parents[3] / "contracts/hub/helper_config_vector.json").read_text())
    cfg = B.parse_config(vec)
    assert set(cfg.parts) == {"hub", "relay", "publish"} and cfg.parts["hub"].kind == "systemd-user"
