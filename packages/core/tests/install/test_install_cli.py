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
        detect=lambda: [("127.0.0.1", "this computer only"), ("100.64.1.2", "private network")],
        port_free=lambda p, h: True,
        token=lambda: "TOKEN",
        dashboard_entry=lambda: ["/bin/orch-dashboard"],
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
    assert "http://100.64.1.2:8100/#setup=TOKEN" in text and "100.64.1.2:8105" in text
    assert (ctx.layout.data("hub") / "setup-token").read_text() == "TOKEN\n"
    install = next(c for c in calls if c[:3] == ["uv", "tool", "install"])
    assert (
        install[-1] == "git+https://github.com/severinlindenmann/orch-core@" + "a" * 40 + "#subdirectory=packages/hub"
    )
    argv, _ = ctx.runner.started["hub"]
    assert "--link-listen" in argv and "100.64.1.2:8105" in argv
    assert argv[argv.index("--web-listen") + 1] == "100.64.1.2:8100"
    assert "--dev-shared-machine" not in argv  # only with --dev
    assert any(c[1] == "keys" and "init" in c and "--real" in c for c in calls)  # no --dev: a real hub
    assert "--bind-all" in argv and "127.0.0.1:8100/#setup=TOKEN" in text  # every interface; loopback line printed
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
    assert cli.add_workspace(ctx, "/w/two") == 8151  # one dashboard serves every workspace of this person
    assert cli.add_workspace(ctx, "/w/one") == 8151


def test_role_question(home):
    ctx, *_ = make(home)
    assert cli.choose_role(ctx, None) == ("workspace", [])  # not a terminal: nothing is asked
    assert cli.choose_role(ctx, "full") == ("full", ["publish", "relay", "hub"])
    ctx.interactive, answers = True, iter(["3", "y", "n", "y"])
    ctx.ask = lambda prompt, default: next(answers)
    assert cli.choose_role(ctx, None) == ("pick", ["publish", "hub"])


def test_detected_addresses_offer_the_netbird_ip():
    ifc = (
        "lo0: flags=8049\n\tinet 127.0.0.1 netmask 0xff000000\n"
        "utun4: flags=8051\n\tinet 100.64.9.9 --> 100.64.9.9 netmask 0xffffffff\n"
        "en0: flags=8863\n\tinet 192.168.1.10 netmask 0xffffff00\n"
    )
    got = detect_addresses(lambda argv: ifc if argv == ["ifconfig"] else "")
    assert (
        ("100.64.9.9", "private network") in got
        and ("192.168.1.10", "local network") in got
        and got[0][0] == "127.0.0.1"
    )


def test_an_agent_is_refused(home):
    out, err = io.StringIO(), io.StringIO()
    assert cli.main(["status"], env={"ORCH_SESSION": "x", "HOME": str(home)}, out=out, err=err) == 3


def test_full_server_on_an_overlay_ip_listens_everywhere_advertises_it_and_the_hub_dials_it(home):
    ctx, out, err, _ = make(home)
    cli.full_server(ctx, ["publish", "relay", "hub"], address="100.64.1.2")
    pub = ctx.runner.started["publish"][0]
    # this computer cannot dial its own overlay IP: every interface listens, the chosen address is only advertised
    assert pub[pub.index("--listen") + 1] == "0.0.0.0:8102" and "http://100.64.1.2:8102" in pub
    relay = ctx.runner.started["relay"][0]
    assert relay[relay.index("--host") + 1] == "0.0.0.0"
    hub = ctx.runner.started["hub"][0]
    assert hub[hub.index("--publish-url") + 1] == "http://100.64.1.2:8102"
    assert hub[hub.index("--relay-url") + 1] == "http://100.64.1.2:8101"


def test_hub_with_dev_gets_the_shared_machine_flag_on_a_network_address(home):
    ctx, out, err, _ = make(home)
    assert cli.main(["hub", "--address", "100.64.1.2", "--dev"], env=ctx.env, out=out, err=err, ctx=ctx) == 0
    assert "--dev-shared-machine" in ctx.runner.started["hub"][0]


def test_a_workspace_dashboard_runs_on_the_chosen_address_and_status_shows_it(home):
    ctx, out, err, _ = make(home)
    cli.full_server(ctx, ["hub"], address="100.64.1.2")
    assert cli.add_workspace(ctx, "/w/one") == 8151
    argv, env = ctx.runner.started["dashboard"]
    assert argv == ["/bin/orch-dashboard", "serve", "--port", "8151", "--listen", "100.64.1.2"]
    assert env["ORCH_STATE_DIR"] == str(ctx.layout.root.parent)
    out.truncate(0), out.seek(0)
    cli.main(["status", "--json"], env=ctx.env, out=out, err=err, ctx=ctx)
    ws = [r for r in json.loads(out.getvalue())["parts"] if r["part"] == "workspace"]
    assert ws[0]["url"] == "http://100.64.1.2:8151/" and ws[0]["state"] == "running"
    out.truncate(0), out.seek(0)
    cli.main(["status"], env=ctx.env, out=out, err=err, ctx=ctx)
    assert "orch dashboard open --print-url --network" in out.getvalue()
    assert cli.main(["remove", "dashboard"], env=ctx.env, out=out, err=err, ctx=ctx) == 0
    assert "dashboard" not in ctx.runner.started


def test_a_loopback_dashboard_has_no_listen_flag(home):
    ctx, *_ = make(home)
    cli.full_server(ctx, ["hub"], address="127.0.0.1")
    cli.add_workspace(ctx, "/w/one")
    assert "--listen" not in ctx.runner.started["dashboard"][0]


def test_workspace_only_asks_for_no_address(home):
    ctx, out, err, _ = make(home)
    ctx.interactive = True
    ctx.ask = lambda *a: (_ for _ in ()).throw(AssertionError("asked"))
    assert cli.full_server(ctx, []) == {}


def test_default_ref_is_develop_override_works_and_status_shows_sha(home):
    ctx, out, err, _ = make(home)
    seen = []
    ctx.resolve_ref = lambda url, ref: seen.append(ref) or "b" * 40
    cli.main(["relay"], env=ctx.env, out=out, err=err, ctx=ctx)
    cli.main(["hub", "--ref", "v1"], env=ctx.env, out=out, err=err, ctx=ctx)
    assert seen == ["develop", "v1"]
    out.truncate(0), out.seek(0)
    cli.main(["status"], env=ctx.env, out=out, err=err, ctx=ctx)
    assert "b" * 12 in out.getvalue()


def _phone_ctx(home, address="100.64.1.2", users=None, qr=None):
    users = [{"user_id": "ab" * 16, "name": "Alice", "groups": ["hub-admin"]}] if users is None else users
    ctx, out, err, calls = make(home, qr=lambda link: qr)
    assert cli.main(["hub", "--address", address], env=ctx.env, out=out, err=err, ctx=ctx) == 0
    out.truncate(0), out.seek(0)
    base = ctx.run

    def run(argv, env=None):
        calls.append(list(argv))
        if "users" in argv:
            return 0, json.dumps(users)
        if "pair-code" in argv:
            return 0, "note\n" + json.dumps({"code": "c0de", "link": "orch://hub-pair/x#h1.h.u.c0de"})
        return base(argv, env)

    ctx.run = run
    return ctx, out, err, calls


def test_phone_prints_link_with_advertised_origin_and_the_single_admin(home):
    ctx, out, err, calls = _phone_ctx(home, qr="[QR]")
    assert cli.main(["phone"], env=ctx.env, out=out, err=err, ctx=ctx) == 0, err.getvalue()
    assert "orch://hub-pair/x#h1.h.u.c0de" in out.getvalue() and "[QR]" in out.getvalue()
    pc = next(c for c in calls if "pair-code" in c)
    assert pc[pc.index("--user") + 1] == "ab" * 16
    assert pc[pc.index("--api-origin") + 1] == "http://100.64.1.2:8100"
    files = [p for p in home.rglob("*") if p.is_file() and p.stat().st_size < 10**6]
    assert not any("orch://hub-pair" in p.read_text(errors="replace") for p in files)


def test_phone_without_qrencode_hints_brew(home):
    ctx, out, err, _ = _phone_ctx(home)
    assert cli.main(["phone", "--user", "dd"], env=ctx.env, out=out, err=err, ctx=ctx) == 0
    assert "brew install qrencode" in out.getvalue()


def test_phone_refuses_loopback_none_installed_and_many_admins(home):
    ctx, out, err, _ = _phone_ctx(home, address="127.0.0.1")
    assert cli.main(["phone"], env=ctx.env, out=out, err=err, ctx=ctx) == 1
    assert "cannot reach" in err.getvalue()
    ctx2, out2, err2, _ = make(home / "none")
    assert cli.main(["phone"], env=ctx2.env, out=out2, err=err2, ctx=ctx2) == 1
    assert "no hub is installed" in err2.getvalue()
    two = [{"user_id": u, "name": u, "groups": ["hub-admin"]} for u in ("aa", "bb")]
    ctx3, out3, err3, _ = _phone_ctx(home / "two", users=two)
    assert cli.main(["phone"], env=ctx3.env, out=out3, err=err3, ctx=ctx3) == 1
    assert "--user" in err3.getvalue()


def test_status_shows_version_and_commit_of_the_workspace_dashboard(home, monkeypatch):
    # run-11 owner item 9: the workspace row said "-" for version and commit
    ctx, out, err, _ = make(home)
    monkeypatch.setattr(cli, "installed_version", lambda ctx, dist: ("1.2.3", "c" * 40))
    cli.full_server(ctx, ["hub"], address="127.0.0.1")
    cli.add_workspace(ctx, "/w/one")
    out.truncate(0), out.seek(0)
    cli.main(["status", "--json"], env=ctx.env, out=out, err=err, ctx=ctx)
    ws = [r for r in json.loads(out.getvalue())["parts"] if r["part"] == "workspace"][0]
    assert (ws["version"], ws["ref"]) == ("1.2.3", "c" * 40)
    out.truncate(0), out.seek(0)
    cli.main(["status"], env=ctx.env, out=out, err=err, ctx=ctx)
    line = next(ln for ln in out.getvalue().splitlines() if ln.startswith("workspace"))
    assert "1.2.3" in line and "c" * 12 in line


def test_mac_is_told_it_cannot_open_its_own_netbird_address(home, monkeypatch):
    ctx, out, err, _ = make(home)
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    cli.choose_address(ctx, None, {})
    assert out.getvalue().count("open it at 127.0.0.1") == 1
    out.truncate(0), out.seek(0)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    cli.choose_address(ctx, None, {})
    assert "open it at 127.0.0.1" not in out.getvalue()
    out.truncate(0), out.seek(0)
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    ctx.detect = lambda: [("127.0.0.1", "this computer only")]
    cli.choose_address(ctx, None, {})
    assert "127.0.0.1 (a Mac" not in out.getvalue()
