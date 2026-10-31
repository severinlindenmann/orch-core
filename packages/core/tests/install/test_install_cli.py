"""``orch install``: parts as background services, with a fake runner and fake commands (no network, no uv)."""

from __future__ import annotations

import io
import json

import pytest

from orch import crypto
from orch.install import cli
from orch.install.parts import detect_addresses
from orch.install.state import load


def _root_pub() -> bytes:
    from orch.crypto import suite

    return suite.public_bytes(suite.generate_private_key())


def real_card(hub_id=None, *, root_pub=None, **o_over):
    """A card in the real shape ({"o", "op_cert", "sig"}); its id is derived from a real root key (wire.hub_id)."""
    from orch.hubclient import wire

    root_pub = root_pub or _root_pub()
    o = {
        "v": 2, "suite": 2, "kind": "hub.card", "hv": 0, "hub_id": hub_id or wire.hub_id(root_pub),
        "hk_root_pub": crypto.b64u(root_pub), "hxk_pub": crypto.b64u(_root_pub()), "relay_url": None,
        "tls_spki_sha256": None, "card_seq": 1, "reader": 1, "issued_ms": 1, "dev": False,
    }  # fmt: skip
    o.update(o_over)
    return {"o": o, "op_cert": {"o": {"kind": "hub.op_cert"}, "sig": "x"}, "sig": "x"}


_ROOT = _root_pub()
HUB_ID = real_card(root_pub=_ROOT)["o"]["hub_id"]


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
    ids = [_ROOT]  # the root key of the hub the fake reports (append another to change its id)
    card_out = []  # a test may put the exact stdout of `keys card` here

    def run(argv, env=None):
        calls.append(list(argv))
        if argv[:3] == ["uv", "tool", "list"]:
            return 0, "orch-hub v0.1.0\norch-relay v0.2.0\norch-publish v0.3.0\n"
        return 0, ""

    def run_stdout(argv, env=None):
        calls.append(list(argv))
        assert argv[1] == "keys" and argv[-1] == "card"  # the only stdout-only call
        return 0, card_out[-1] if card_out else json.dumps(real_card(root_pub=ids[-1]))

    ctx = cli.Ctx(
        env={"HOME": str(home)},
        out=out,
        err=err,
        runner=FakeRunner(),
        run=run,
        run_stdout=run_stdout,
        resolve_ref=lambda url, ref: "a" * 40,
        core_commit=lambda: None,
        health=lambda h, p: True,
        detect=lambda: [("127.0.0.1", "this computer only"), ("100.64.1.2", "private network")],
        port_free=lambda p, h: True,
        token=lambda: "TOKEN",
        dashboard_entry=lambda: ["/bin/orch-dashboard"],
        **kw,
    )
    ctx.ids, ctx.card_out = ids, card_out
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


def test_parts_default_to_the_commit_of_the_installed_core_not_develop(home):
    ctx, out, err, calls = make(home)
    seen = []
    ctx.resolve_ref = lambda url, ref: seen.append(ref) or "b" * 40
    ctx.core_commit = lambda: "c" * 40
    cli.main(["relay"], env=ctx.env, out=out, err=err, ctx=ctx)
    assert seen == []  # no lookup of develop
    assert any("c" * 40 in " ".join(c) for c in calls if "install" in c)
    cli.main(["hub", "--ref", "v1"], env=ctx.env, out=out, err=err, ctx=ctx)
    assert seen == ["v1"]  # --ref still overrides
    ctx.core_commit = lambda: None
    out.truncate(0), out.seek(0)
    cli.main(["publish"], env=ctx.env, out=out, err=err, ctx=ctx)
    assert seen[-1] == "develop" and "no known commit" in out.getvalue()


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


def test_hub_install_seeds_hub_settings_with_the_chosen_address_and_keeps_edits_on_a_rerun(home):
    ctx, out, err, _ = make(home)
    f = ctx.layout.data("hub") / "hub-settings.json"
    assert cli.main(["hub", "--address", "100.64.1.2"], env=ctx.env, out=out, err=err, ctx=ctx) == 0
    assert (f.stat().st_mode & 0o777) == 0o600
    assert json.loads(f.read_text()) == {
        "v": 1,
        "name": "Hub",
        "company": "",
        "addresses": [
            {"kind": "private", "web": "http://100.64.1.2:8100", "link": "100.64.1.2:8105"},
            {"kind": "server", "web": "http://127.0.0.1:8100", "link": "127.0.0.1:8105"},
        ],
    }
    # an admin's name and company survive a re-run; only the addresses follow the new choice
    body = json.loads(f.read_text())
    f.write_text(json.dumps({**body, "name": "Acme", "company": "Acme AG"}))
    assert cli.main(["hub", "--address", "127.0.0.1"], env=ctx.env, out=out, err=err, ctx=ctx) == 0
    again = json.loads(f.read_text())
    assert (again["name"], again["company"]) == ("Acme", "Acme AG")
    assert again["addresses"] == [{"kind": "server", "web": "http://127.0.0.1:8100", "link": "127.0.0.1:8105"}]
    assert cli.hub_address_kind("192.168.1.5") == "office" and cli.hub_address_kind("8.8.8.8") == "public"


def test_hub_settings_ignores_a_linked_old_file_and_brackets_ipv6(home, tmp_path):
    ctx, out, err, _ = make(home)
    f = ctx.layout.data("hub") / "hub-settings.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"name": "Evil", "company": "X"}))
    f.symlink_to(other)
    cli.write_hub_settings(f.parent, address="fd00::5", domain=None, port=8100)
    body = json.loads(f.read_text())
    assert (body["name"], body["company"]) == ("Hub", "")
    assert body["addresses"][0]["web"] == "http://[fd00::5]:8100" and body["addresses"][0]["link"] == "[fd00::5]:8105"
    assert json.loads(other.read_text())["name"] == "Evil"  # the link target is untouched


def _trusts(calls):
    return [c for c in calls if c[-3:-1] == ["hub", "trust"]]


def _install(ctx, out, err, *args):
    assert cli.main(list(args), env=ctx.env, out=out, err=err, ctx=ctx) == 0, err.getvalue()


def test_hub_then_publish_pins_the_hub_at_its_own_publish(home):
    ctx, out, err, calls = make(home)
    _install(ctx, out, err, "hub", "--address", "127.0.0.1")
    assert _trusts(calls) == []  # no publish in this install yet
    _install(ctx, out, err, "publish", "--address", "127.0.0.1")
    (t,) = _trusts(calls)
    assert t == [
        str(ctx.layout.bin / "orch-publish"),
        "admin",
        "--data",
        str(ctx.layout.data("publish")),
        "hub",
        "trust",
        HUB_ID,
    ]


def test_publish_then_hub_pins_the_hub_at_its_own_publish(home):
    ctx, out, err, calls = make(home)
    _install(ctx, out, err, "publish", "--address", "127.0.0.1")
    assert _trusts(calls) == []
    _install(ctx, out, err, "hub", "--address", "127.0.0.1")
    (t,) = _trusts(calls)
    assert t[-1] == HUB_ID and t[3] == str(ctx.layout.data("publish"))


def test_a_reinstall_pins_again(home):
    ctx, out, err, calls = make(home)
    _install(ctx, out, err, "hub", "--address", "127.0.0.1")
    _install(ctx, out, err, "publish", "--address", "127.0.0.1")
    _install(ctx, out, err, "publish", "--address", "127.0.0.1")
    _install(ctx, out, err, "hub", "--address", "127.0.0.1")
    assert len(_trusts(calls)) == 3


def test_publish_alone_or_hub_with_a_remote_publish_pins_nothing(home):
    ctx, out, err, calls = make(home)
    _install(ctx, out, err, "publish", "--address", "127.0.0.1")
    assert _trusts(calls) == []
    ctx2, out2, err2, calls2 = make(home / "two")
    _install(ctx2, out2, err2, "hub", "--address", "127.0.0.1", "--publish-url", "https://share.example.com")
    _install(ctx2, out2, err2, "publish", "--address", "127.0.0.1")  # a publish beside a hub that dials another one
    assert _trusts(calls2) == []


def test_a_failed_pin_is_one_plain_line_and_the_install_goes_on(home):
    ctx, out, err, calls = make(home)
    inner = ctx.run

    def run(argv, env=None):
        if "trust" in argv:
            return 1, "boom"
        return inner(argv, env)

    ctx.run = run
    _install(ctx, out, err, "hub", "--address", "127.0.0.1")
    _install(ctx, out, err, "publish", "--address", "127.0.0.1")
    lines = [ln for ln in out.getvalue().splitlines() if "could not be allowed to share" in ln]
    assert len(lines) == 1 and "publish" in load(ctx.layout)["parts"]


def test_a_missing_publish_binary_is_a_note_not_a_failed_install(home):
    ctx, out, err, calls = make(home)
    inner = ctx.run

    def run(argv, env=None):
        if "trust" in argv:
            raise FileNotFoundError(argv[0])
        return inner(argv, env)

    ctx.run = run
    _install(ctx, out, err, "hub", "--address", "127.0.0.1")
    _install(ctx, out, err, "publish", "--address", "127.0.0.1")
    assert "could not be allowed to share" in out.getvalue() and "publish" in load(ctx.layout)["parts"]


def test_the_pin_is_recorded_and_said_once(home):
    ctx, out, err, calls = make(home)
    _install(ctx, out, err, "hub", "--address", "127.0.0.1")
    _install(ctx, out, err, "publish", "--address", "127.0.0.1")
    assert out.getvalue().count(f"pinned hub {HUB_ID} at publish") == 1
    assert load(ctx.layout)["parts"]["publish"]["pinned_hub_id"] == HUB_ID


def test_a_changed_hub_id_untrusts_the_old_one(home):
    ctx, out, err, calls = make(home)
    _install(ctx, out, err, "hub", "--address", "127.0.0.1")
    _install(ctx, out, err, "publish", "--address", "127.0.0.1")
    root2 = _root_pub()
    new = real_card(root_pub=root2)["o"]["hub_id"]
    ctx.ids.append(root2)
    _install(ctx, out, err, "hub", "--address", "127.0.0.1")
    untrusts = [c for c in calls if c[-3:-1] == ["hub", "untrust"]]
    assert [c[-1] for c in untrusts] == [HUB_ID]
    assert calls.index(untrusts[0]) < max(i for i, c in enumerate(calls) if c[-2:] == ["trust", new])
    assert load(ctx.layout)["parts"]["publish"]["pinned_hub_id"] == new


def test_removing_the_hub_untrusts_the_pinned_id(home):
    ctx, out, err, calls = make(home)
    _install(ctx, out, err, "hub", "--address", "127.0.0.1")
    _install(ctx, out, err, "publish", "--address", "127.0.0.1")
    assert cli.main(["remove", "hub"], env=ctx.env, out=out, err=err, ctx=ctx) == 0
    assert [c[-1] for c in calls if c[-3:-1] == ["hub", "untrust"]] == [HUB_ID]
    assert "pinned_hub_id" not in load(ctx.layout)["parts"]["publish"]


def _card(**o):
    return real_card(root_pub=_ROOT, **o)


def _without(key):
    c = _card()
    del c[key]
    return c


@pytest.mark.parametrize(
    "stdout",
    [
        json.dumps({"hub_id": HUB_ID}),  # no envelope
        json.dumps({"o": _card()["o"], "sig": "x"}),  # the old two-key shape: op_cert missing
        json.dumps(_without("op_cert")),
        json.dumps(_without("sig")),
        json.dumps(_card(kind="hub.link")),  # wrong kind
        json.dumps(_card(hub_id="cd" * 16)),  # a well-formed id that is not the one of hk_root_pub
        json.dumps(_card(hk_root_pub="AAAA")),  # not a key
        json.dumps(_card()) + "\n" + json.dumps(_card()),  # a second object
        json.dumps(_card()) + "\ntrailing text",
        "note: " + json.dumps(_card()),  # leading text
        json.dumps({**_card(), "extra": 1}),  # an extra key
        json.dumps(_card(hub_id="AB" * 16)),  # not lower-case hex
        json.dumps(_card(hub_id="ab" * 15)),  # too short
        json.dumps([_card()]),
        "",
    ],
    ids=[
        "no-envelope",
        "two-keys",
        "no-op-cert",
        "no-sig",
        "wrong-kind",
        "id-not-of-root",
        "bad-root",
        "two-objects",
        "trailing-text",
        "leading-text",
        "extra-key",
        "upper-hex",
        "short-id",
        "list",
        "empty",
    ],  # fixed ids: the cards hold random signatures, and xdist workers must collect the same names
)
def test_a_card_that_is_not_exactly_the_real_card_is_not_pinned(home, stdout):
    ctx, out, err, calls = make(home)
    ctx.card_out.append(stdout)
    _install(ctx, out, err, "hub", "--address", "127.0.0.1")
    _install(ctx, out, err, "publish", "--address", "127.0.0.1")
    assert _trusts(calls) == []
    assert len([ln for ln in out.getvalue().splitlines() if "could not be allowed to share" in ln]) == 1
    assert "publish" in load(ctx.layout)["parts"]


def test_stderr_noise_next_to_a_valid_card_still_pins(home):
    """The card is read from stdout only; the real runner drops stderr."""
    ctx, out, err, calls = make(home)
    ctx.card_out.append(json.dumps(_card()) + "\n")
    _install(ctx, out, err, "hub", "--address", "127.0.0.1")
    _install(ctx, out, err, "publish", "--address", "127.0.0.1")
    assert [c[-1] for c in _trusts(calls)] == [HUB_ID]


def test_the_real_orch_hub_card_is_accepted(tmp_path):
    """Integration: the real `orch-hub keys init` and `keys card` (skipped when orch-hub is not importable here)."""
    import subprocess
    import sys

    pytest.importorskip("orch_hub")
    state = tmp_path / "hub"
    base = [sys.executable, "-m", "orch_hub", "keys", "--state", str(state)]
    for argv in (["init"], ["card"]):
        r = subprocess.run([*base, *argv], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    got = cli._card_hub_id(r.stdout)
    assert got is not None and len(got) == 32


def test_run_stdout_drops_stderr():
    rc, out = cli._run_stdout(["sh", "-c", "echo noise >&2; echo '{}'"])
    assert (rc, out) == (0, "{}\n")
