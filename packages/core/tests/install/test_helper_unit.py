"""The installer makes the hub's service helper (helper.json + a user unit); fake runner, no systemd, no sudo."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from orch.install import cli
from orch.install import helper as H
from orch.install.service import SystemdRunner
from orch.install.state import Layout, load, save

from .test_install_cli import FakeRunner, make

REPO = Path(__file__).resolve().parents[4]
UNIT_RE = re.compile(r"orch-[a-z0-9][a-z0-9-]{0,40}(@[a-z0-9-]{1,40})?\.service")  # the hub's own check


class SysdFake(FakeRunner):
    kind = "systemd"

    def start(self, part, argv, env, log):
        super().start(part, argv, env, log)
        return {"service": f"orch-ab12cd-{part}.service", "pid": 1}

    def info(self, part):
        return {"running": part in self.started, "pid": 1, "service": f"orch-ab12cd-{part}.service"}


@pytest.fixture
def home(tmp_path):
    return tmp_path


def ctx_for(home, runner=None):
    ctx, out, err, calls = make(home, helper_ready=lambda p: True)
    ctx.runner = runner or SysdFake()
    return ctx, out


def helper_json(ctx):
    return ctx.layout.data("hub") / "helper.json"


def test_full_server_writes_helper_json_0600_and_starts_one_helper(home):
    ctx, out = ctx_for(home)
    cli.full_server(ctx, ["publish", "relay", "hub"], address="127.0.0.1")
    p = helper_json(ctx)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    cfg = json.loads(p.read_text())
    assert {n: (v["kind"], v["unit"]) for n, v in cfg["parts"].items()} == {
        "hub": ("systemd-user", "orch-ab12cd-hub.service"),
        "relay": ("systemd-user", "orch-ab12cd-relay.service"),
        "publish": ("systemd-user", "orch-ab12cd-publish.service"),
    }
    argv, env = ctx.runner.started["hub-helper"]
    assert argv == [str(ctx.layout.bin / "orch-hub"), "helper", "--state", str(ctx.layout.data("hub"))]
    assert env == {} and not any(
        "token" in a.lower() or "TOKEN" in a for a in argv
    )  # nothing secret on the command line
    st = load(ctx.layout)
    assert st["helper"]["service"] == "orch-ab12cd-hub-helper.service" and st["helper"]["runner"] == "systemd"
    assert "service buttons: on" in out.getvalue()


def test_config_matches_the_contract_vector_the_hub_parses():
    vec = json.loads((REPO / "contracts/hub/helper_config_vector.json").read_text())
    st = {
        "parts": {
            p: {"runner": "systemd", "service": f"orch-ab12cd-{p}.service", "binary": "/b/orch-hub"}
            for p in ("hub", "relay", "publish")
        }
    }
    assert H.helper_config(st, "vec-host") == vec


def test_unchanged_rerun_does_not_restart_and_a_change_restarts(home):
    ctx, _ = ctx_for(home)
    cli.full_server(ctx, ["hub"], address="127.0.0.1")
    starts = []
    orig = ctx.runner.start
    ctx.runner.start = lambda part, *a: (starts.append(part), orig(part, *a))[1]
    before = helper_json(ctx).read_bytes()
    H.sync_helper(ctx)
    assert starts == [] and helper_json(ctx).read_bytes() == before
    cli.install_part(ctx, "relay", address="127.0.0.1")  # relay joins the list: the file changes, helper restarts
    assert "hub-helper" in starts and "relay" in json.loads(helper_json(ctx).read_text())["parts"]
    assert starts.count("hub-helper") == 1


def test_not_running_helper_is_started_again(home):
    ctx, _ = ctx_for(home)
    cli.full_server(ctx, ["hub"], address="127.0.0.1")
    ctx.runner.started.pop("hub-helper")  # died
    H.sync_helper(ctx)
    assert "hub-helper" in ctx.runner.started


def test_pidfile_runner_gets_no_helper_and_a_plain_note(home):
    ctx, out = ctx_for(home, FakeRunner())  # kind "fake": like pidfile/launchd, not systemd
    cli.full_server(ctx, ["hub"], address="127.0.0.1")
    assert "hub-helper" not in ctx.runner.started and not helper_json(ctx).exists()
    assert "service buttons: off" in out.getvalue() and "systemd" in out.getvalue()
    assert "helper" not in load(ctx.layout)


def test_remove_hub_removes_helper_json_and_record_remove_relay_drops_it(home):
    ctx, _ = ctx_for(home)
    cli.full_server(ctx, ["relay", "hub"], address="127.0.0.1")
    assert cli.remove(ctx, "relay", False) == 0
    assert set(json.loads(helper_json(ctx).read_text())["parts"]) == {"hub"}
    assert cli.remove(ctx, "hub", False) == 0
    assert not helper_json(ctx).exists() and "hub-helper" in ctx.runner.stopped
    assert "helper" not in load(ctx.layout)


def test_a_helper_set_up_by_hand_is_left_alone_on_remove(home):
    ctx, _ = ctx_for(home)
    cli.full_server(ctx, ["hub"], address="127.0.0.1")
    st = load(ctx.layout)
    st.pop("helper")  # as if an owner made helper.json and the unit themselves
    save(ctx.layout, st)
    cli.remove(ctx, "hub", False)
    assert helper_json(ctx).exists() and "hub-helper" not in ctx.runner.stopped


def test_install_leaves_a_hand_written_helper_json_alone_and_starts_no_second_helper(home):
    ctx, out = ctx_for(home)
    data = ctx.layout.data("hub")
    data.mkdir(parents=True)
    mine = data / "helper.json"
    mine.write_text('{"by": "owner"}')
    mine.chmod(0o600)
    cli.full_server(ctx, ["hub"], address="127.0.0.1")
    assert mine.read_text() == '{"by": "owner"}' and "hub-helper" not in ctx.runner.started
    assert "helper" not in load(ctx.layout) and "set up by hand, left alone" in out.getvalue()


def test_no_helper_unless_the_runner_is_systemd_even_if_a_part_record_says_so(home):
    ctx, out = ctx_for(home, FakeRunner())
    cli.full_server(ctx, ["hub"], address="127.0.0.1")
    st = load(ctx.layout)
    st["parts"]["hub"]["runner"] = "systemd"
    st["parts"]["hub"]["service"] = "orch-ab12cd-hub.service"
    save(ctx.layout, st)
    H.sync_helper(ctx)
    assert "hub-helper" not in ctx.runner.started and not helper_json(ctx).exists()


def test_symlink_at_helper_json_is_refused_and_not_followed(home, tmp_path):
    ctx, out = ctx_for(home)
    cli.full_server(ctx, ["hub"], address="127.0.0.1")
    p = helper_json(ctx)
    target = tmp_path / "victim"
    target.write_text("keep")
    p.unlink()
    p.symlink_to(target)
    st = load(ctx.layout)
    st["parts"]["relay"] = {"runner": "systemd", "service": "orch-ab12cd-relay.service"}  # changes the body
    save(ctx.layout, st)
    H.sync_helper(ctx)
    assert target.read_text() == "keep" and p.is_symlink()
    assert "left off" in out.getvalue()


def test_socket_that_never_shows_up_only_warns(home):
    ctx, out = ctx_for(home)
    ctx.helper_ready = lambda p: False
    cli.full_server(ctx, ["hub"], address="127.0.0.1")
    assert "does not answer yet" in out.getvalue() and helper_json(ctx).exists()


def test_host_id_is_sanitized(monkeypatch):
    monkeypatch.setattr(H.socket, "gethostname", lambda: "my laptop.local!")
    assert H.HOST_RE.fullmatch(H._host_id())
    monkeypatch.setattr(H.socket, "gethostname", lambda: "...")
    assert H._host_id() == "this-server"


def test_labels_match_the_hubs_rule():
    assert all(re.fullmatch(r"[A-Za-z][A-Za-z0-9 -]{0,23}", v) for v in H.LABELS.values())


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="systemd unit text: Linux")
def test_systemd_unit_text_for_the_helper(home):
    layout = Layout({"HOME": str(home)})
    calls = []
    r = SystemdRunner(layout, run=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    r.start("hub-helper", ["/b/orch-hub", "helper", "--state", "/d"], {}, layout.logs / "hub-helper.log")
    text = r.path("hub-helper").read_text()
    assert UNIT_RE.fullmatch(r.unit("hub-helper")) and r.unit("hub-helper").endswith("-hub-helper.service")
    assert "ExecStart=/b/orch-hub helper --state /d\n" in text
    assert "PartOf" not in text and "BindsTo" not in text and "Environment=" not in text
    assert os.path.basename(str(r.path("hub-helper"))).startswith("orch-")
