"""``orch install``: parts as background services, with a fake runner and fake commands (no network, no uv)."""

from __future__ import annotations

import io
import json

import pytest

from orch.install import cli
from orch.install.parts import detect_addresses
from orch.install.state import load


class FakeRunner:
    kind = "fake"

    def __init__(self):
        self.started, self.stopped = {}, []

    def start(self, part, argv, env, log):
        self.started[part] = (list(argv), dict(env))
        return {"service": f"fake.{part}", "pid": 4000 + len(self.started)}

    def stop(self, part):
        self.stopped.append(part)
        self.started.pop(part, None)

    def info(self, part):
        return {"running": part in self.started, "pid": 4242, "service": f"fake.{part}"}


@pytest.fixture
def home(tmp_path):
    return tmp_path


def make(home, **kw):
    out, err = io.StringIO(), io.StringIO()
    calls = []

    def run(argv, env=None):
        calls.append(list(argv))
        if argv[:3] == ["uv", "tool", "list"]:
            return 0, "orch-hub v0.1.0\norch-relay v0.2.0\norch-publish v0.3.0\n"
        return 0, ""

    ctx = cli.Ctx(
        env={"HOME": str(home)},
        out=out,
        err=err,
        runner=FakeRunner(),
        run=run,
        resolve_ref=lambda url, ref: "a" * 40,
        health=lambda h, p: True,
        detect=lambda: [("127.0.0.1", "this computer only"), ("100.64.1.2", "NetBird")],
        port_free=lambda p, h: True,
        token=lambda: "TOKEN",
        **kw,
    )
    return ctx, out, err, calls


def test_hub_install_pins_a_ref_starts_it_and_prints_console_and_setup_link(home):
    ctx, out, err, calls = make(home)
    rc = cli.main(
        ["hub", "--address", "100.64.1.2", "--publish-url", "http://100.64.1.2:8102"],
        env=ctx.env,
        out=out,
        err=err,
        ctx=ctx,
    )
    assert rc == 0, err.getvalue()
    text = out.getvalue()
    assert "http://127.0.0.1:8100/#setup=TOKEN" in text and "100.64.1.2:8105" in text
    install = next(c for c in calls if c[:3] == ["uv", "tool", "install"])
    assert install[-1] == "git+https://github.com/severinlindenmann/orch-hub@" + "a" * 40
    argv, _ = ctx.runner.started["hub"]
    assert "--link-listen" in argv and "100.64.1.2:8105" in argv and "--web-listen" in argv
    assert any(c[1:3] == ["keys", "init"] for c in calls)
    st = load(ctx.layout)
    assert st["parts"]["hub"]["port"] == 8100 and st["parts"]["hub"]["version"] == "0.1.0"
    assert str(home) in str(ctx.layout.root)  # state under the temp HOME, never the real config


def test_each_part_gets_its_suggested_port_and_can_be_installed_alone(home):
    ctx, out, err, _ = make(home)
    for part, port in (("publish", 8102), ("relay", 8101), ("hub", 8100)):
        assert cli.main([part, "--address", "127.0.0.1"], env=ctx.env, out=out, err=err, ctx=ctx) == 0
        assert load(ctx.layout)["parts"][part]["port"] == port
    argv, _ = ctx.runner.started["publish"]
    assert argv[1] == "run" and "127.0.0.1:8102" in argv and "http://127.0.0.1:8102" in argv


def test_port_below_1024_and_a_clash_are_refused(home):
    ctx, out, err, _ = make(home)
    assert cli.main(["relay", "--port", "1001"], env=ctx.env, out=out, err=err, ctx=ctx) == 1
    assert "1024" in err.getvalue()
    assert cli.main(["relay", "--port", "9000"], env=ctx.env, out=out, err=err, ctx=ctx) == 0
    assert cli.main(["publish", "--port", "9000"], env=ctx.env, out=out, err=err, ctx=ctx) == 1


def test_publish_install_sets_server_equal_share(home):
    seen = []
    ctx, out, err, _ = make(home, publish_setup=lambda url: seen.append(url))
    cli.main(["publish", "--address", "127.0.0.1"], env=ctx.env, out=out, err=err, ctx=ctx)
    assert seen == ["http://127.0.0.1:8102"] and "server = share" in out.getvalue()


def test_a_domain_is_only_noted(home):
    ctx, out, err, _ = make(home)
    cli.main(["relay", "--domain", "relay.example.com"], env=ctx.env, out=out, err=err, ctx=ctx)
    assert load(ctx.layout)["parts"]["relay"]["domain"] == "relay.example.com"
    assert "127.0.0.1" in ctx.runner.started["relay"][0] and "orch does not do that" in out.getvalue()


def test_status_and_remove(home):
    ctx, out, err, _ = make(home)
    cli.main(["relay"], env=ctx.env, out=out, err=err, ctx=ctx)
    out.truncate(0), out.seek(0)
    cli.main(["status", "--json"], env=ctx.env, out=out, err=err, ctx=ctx)
    rows = {r["part"]: r for r in json.loads(out.getvalue())["parts"]}
    assert rows["relay"]["state"] == "running" and rows["hub"]["state"] == "not installed"
    assert cli.main(["remove", "relay"], env=ctx.env, out=out, err=err, ctx=ctx) == 0
    assert ctx.runner.stopped == ["relay", "relay"] or "relay" in ctx.runner.stopped
    assert "relay" not in load(ctx.layout)["parts"]


def test_full_server_and_more_workspaces_take_the_next_ports(home):
    ctx, out, err, _ = make(home)
    cli.full_server(ctx, ["publish", "relay", "hub"], address="100.64.1.2")
    assert set(load(ctx.layout)["parts"]) == {"hub", "relay", "publish"}
    assert cli.add_workspace(ctx, "/w/one") == 8151
    assert cli.add_workspace(ctx, "/w/two") == 8152
    assert cli.add_workspace(ctx, "/w/one") == 8151


def test_role_question(home):
    ctx, *_ = make(home)
    assert cli.choose_role(ctx, None) == ("workspace", [])  # not a terminal: nothing is asked
    assert cli.choose_role(ctx, "full") == ("full", ["publish", "relay", "hub"])
    ctx.interactive, answers = True, iter(["3", "y", "n", "y"])
    ctx.ask = lambda prompt, default: next(answers)
    assert cli.choose_role(ctx, None) == ("pick", ["publish", "hub"])


def test_detected_addresses_offer_the_netbird_ip():
    ifc = "lo0: flags=8049\n\tinet 127.0.0.1 netmask 0xff000000\nutun4: flags=8051\n\tinet 100.64.9.9 --> 100.64.9.9 netmask 0xffffffff\nen0: flags=8863\n\tinet 192.168.1.10 netmask 0xffffff00\n"
    got = detect_addresses(lambda argv: ifc if argv == ["ifconfig"] else "")
    assert ("100.64.9.9", "NetBird") in got and ("192.168.1.10", "local network") in got and got[0][0] == "127.0.0.1"


def test_an_agent_is_refused(home):
    out, err = io.StringIO(), io.StringIO()
    assert cli.main(["status"], env={"ORCH_SESSION": "x", "HOME": str(home)}, out=out, err=err) == 3
