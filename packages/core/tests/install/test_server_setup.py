"""The pilot server setup: roles, names, DNS check, Caddy snippets (golden), the printed sudo block, the ops units.
No DNS, no Caddy, no systemd, no sudo: every seam is a stub."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from orch.install import cli, ops, server
from orch.install.parts import InstallError
from orch.install.serverflow import server_setup
from orch.install.state import load

from .test_install_cli import FakeRunner, make

GOLDEN = Path(__file__).parent / "golden"
VPS = "179.237.125.155"
VPN = "100.64.7.9"
IP_ADDR = (
    "1: lo    inet 127.0.0.1/8 scope host lo\n"
    f"2: eth0    inet {VPS}/24 scope global eth0\n"
    f"3: wt0    inet {VPN}/16 scope global wt0\n"
)


def golden(name: str, text: str) -> None:
    f = GOLDEN / name
    if not f.exists():  # first run writes the golden; review it in the diff
        f.write_text(text)
    assert text == f.read_text()


# --------------------------------------------------------------------------------------------------------- names


def test_domain_derives_three_names_and_overrides_win():
    n = server.derive_names("Example.com.", relay="rly.other.org")
    assert (n.hub, n.relay, n.publish) == ("hub.example.com", "rly.other.org", "publish.example.com")
    assert n.url("hub") == "https://hub.example.com"
    assert server.derive_names(None) == server.Names()


@pytest.mark.parametrize("bad", ["not a domain", "a..b", "-x.com", "localhost", "x.c"])
def test_a_bad_name_is_refused_in_plain_words(bad):
    with pytest.raises(InstallError, match="not a domain name"):
        server.derive_names(bad)


# ------------------------------------------------------------------------------------------------------ caddy


def test_caddy_goldens():
    golden("relay.caddy", server.caddy_snippet("relay", "relay.example.com", 8101))
    golden("publish.caddy", server.caddy_snippet("publish", "publish.example.com", 8102))
    golden("hub-public.caddy", server.caddy_snippet("hub", "hub.example.com", 8100))
    golden("hub-vpn-only.caddy", server.caddy_snippet("hub", "hub.example.com", 8100, vpn_only=True))


def test_vpn_matcher_only_with_vpn_only_and_only_for_the_hub():
    assert "100.64.0.0/10" not in server.caddy_snippet("hub", "hub.example.com", 8100)
    assert "not remote_ip 100.64.0.0/10" in server.caddy_snippet("hub", "hub.example.com", 8100, vpn_only=True)
    assert "remote_ip" not in server.caddy_snippet("relay", "relay.example.com", 8101, vpn_only=True)


def test_publish_share_paths_are_not_logged_and_limits_differ():
    pub = server.caddy_snippet("publish", "p.example.com", 8102)
    assert "log_skip @shares" in pub and "path /s/*" in pub and "max_size 64MB" in pub
    assert "max_size 8MB" in server.caddy_snippet("relay", "r.example.com", 8101)


def test_root_block_golden_has_no_shared_writable_folder():
    b = server.root_block("orch-edge")
    golden("root-block-edge.sh", b)
    assert "groupadd" not in b and "orch-caddy" not in b and "2775" not in b
    assert "install -d -m 0755 -o orch-edge -g orch-edge /etc/caddy/orch.d/orch-edge" in b
    assert "import /etc/caddy/orch.d/*/*.caddy" in b and "systemctl reload caddy" in b
    assert "loginctl enable-linger orch-edge" in b


def test_snippets_never_follow_a_planted_link(tmp_path):
    d = tmp_path / "orch-edge"
    d.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    # a link at the final name and at the old fixed temp names: none of them is written through
    (d / "relay.caddy").symlink_to(victim)
    (d / ".relay.caddy.tmp").symlink_to(victim)
    server.write_snippets(d, {"relay": "site {}\n"})
    assert victim.read_text() == "keep"
    assert not (d / "relay.caddy").is_symlink() and (d / "relay.caddy").read_text() == "site {}\n"
    assert oct((d / "relay.caddy").stat().st_mode & 0o777) == "0o644"
    assert sorted(p.name for p in d.iterdir()) == [".relay.caddy.tmp", "relay.caddy"]  # no temp left behind


def test_a_linked_or_foreign_snippet_folder_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "orch-edge"
    link.symlink_to(real)
    with pytest.raises(InstallError, match="not a plain folder"):
        server.write_snippets(link, {"relay": "x"})
    assert list(real.iterdir()) == []
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o777)  # writable by others: another user could swap files
    with pytest.raises(InstallError, match="not writable by others"):
        server.write_snippets(loose, {"relay": "x"})
    with pytest.raises(OSError):
        server.write_snippets(tmp_path / "missing", {"relay": "x"})
    with pytest.raises(InstallError, match="usable user name"):
        server.user_dir("/etc/caddy/orch.d", "../evil")


# ------------------------------------------------------------------------------------------------------ dns


def test_dns_check_ok_missing_elsewhere_and_mixed():
    table = {
        "a.example.com": [VPS],
        "b.example.com": [],
        "c.example.com": ["1.2.3.4"],
        "d.example.com": [VPS, "1.2.3.4"],  # L1: one answer elsewhere is not "ours"
        "e.example.com": ["10.0.0.5"],
    }
    res = server.check_dns(
        {
            "relay": "a.example.com",
            "publish": "b.example.com",
            "hub": "c.example.com",
            "x": "d.example.com",
            "y": "e.example.com",
        },
        lambda n: table[n],
        {VPS, "127.0.0.1", "10.0.0.5"},
    )
    assert [r.status for r in res] == ["ok", "missing", "elsewhere", "elsewhere", "private"]
    assert "no DNS record" in res[1].words() and "run this command again" in res[1].words()
    assert "1.2.3.4" in res[2].words() and "not (only) this computer" in res[2].words()
    assert "private address" in res[4].words()


def ifaces():
    return server.local_interfaces(lambda argv: IP_ADDR if argv[0] == "ip" else "")


def test_local_addresses_and_vpn_detection():
    mine = ifaces()
    assert {VPS, VPN, "127.0.0.1"} <= {a.ip for a in mine}
    got = server.detect_vpn_ip(mine)
    assert got.ip == VPN and got.prefix == 16 and got.iface == "wt0"
    assert server.detect_vpn_ip([server.Addr("10.0.0.2"), server.Addr(VPS)]) is None
    assert server.detect_vpn_ip([server.Addr("100.63.255.255"), server.Addr("100.128.0.1")]) is None


def test_two_cgnat_addresses_are_refused_unless_one_is_on_a_vpn_interface():
    two = [server.Addr("100.64.1.1", 10, "eth1"), server.Addr("100.70.2.2", 16, "eth2")]
    with pytest.raises(InstallError, match="more than one private-network"):
        server.detect_vpn_ip(two)
    assert server.detect_vpn_ip([*two, server.Addr("100.90.3.3", 16, "wt0")]).ip == "100.90.3.3"


def test_vpn_ip_flag_accepts_only_cgnat_unless_overridden():
    mine = [server.Addr(VPS, 24, "eth0"), server.Addr(VPN, 16, "wt0"), server.Addr("10.1.1.5", 24, "eth1")]
    assert server.check_vpn_ip(VPN, mine)[0].ip == VPN
    with pytest.raises(InstallError, match="100.64"):
        server.check_vpn_ip("10.1.1.5", mine)
    assert server.check_vpn_ip("10.1.1.5", mine, allow_non_cgnat=True)[0].ip == "10.1.1.5"
    with pytest.raises(InstallError, match="100.64"):
        server.check_vpn_ip(VPS, mine, allow_non_cgnat=True)  # public stays refused
    with pytest.raises(InstallError, match="does not have that address"):
        server.check_vpn_ip("100.64.9.9", mine)
    assert server.check_vpn_ip("100.64.7.9/16", mine)[1] == 16


def test_vpn_gate_uses_the_real_subnet_golden(tmp_path):
    golden(
        "hub-vpn-only-16.caddy",
        server.caddy_snippet("hub", "hub.example.com", 8100, vpn_only=True, vpn_net="100.92.0.0/16"),
    )
    assert str(server.vpn_network(server.Addr("100.92.1.2", 16, "wt0"))) == "100.92.0.0/16"
    assert str(server.vpn_network(server.Addr("100.92.1.2"), 16)) == "100.92.0.0/16"  # --vpn-ip 100.92.1.2/16
    with pytest.raises(InstallError, match="cannot tell the network"):
        server.vpn_network(server.Addr("100.92.1.2", 32, "wt0"))


# ------------------------------------------------------------------------------------------------ the flow


class SystemdFake(FakeRunner):
    kind = "systemd"


def stub_ctx(home, runner=None):
    ctx, out, err, calls = make(home)
    ctx.helper_ready = lambda p: True  # no real helper socket to wait for
    if runner:
        ctx.runner = runner
    return ctx, out, err, calls


def local():
    return ifaces()


def test_edge_role_installs_relay_and_publish_only_and_writes_two_snippets(tmp_path):
    home, root = tmp_path / "h", tmp_path / "orch.d"
    caddy = root / "orch-edge"
    home.mkdir(), caddy.mkdir(parents=True)
    ctx, out, err, calls = stub_ctx(home, SystemdFake())
    plan = server_setup(
        ctx,
        "edge",
        names=server.derive_names("example.com"),
        caddy_dir=str(root),
        resolve=lambda n: [VPS],
        local=local,
        user="orch-edge",
    )
    assert set(load(ctx.layout)["parts"]) == {"relay", "publish"}  # no hub on the edge user
    assert sorted(p.name for p in caddy.iterdir()) == ["publish.caddy", "relay.caddy"]
    relay_argv, _ = ctx.runner.started["relay"]
    assert relay_argv[relay_argv.index("--host") + 1] == "127.0.0.1"  # behind Caddy: loopback only
    pub_argv, _ = ctx.runner.started["publish"]
    assert pub_argv[pub_argv.index("--public-origin") + 1] == "https://publish.example.com"
    assert pub_argv[pub_argv.index("--listen") + 1] == "127.0.0.1:8102"
    text = out.getvalue()
    assert "orch publish setup --server https://publish.example.com --share https://publish.example.com" in text
    assert plan.tls_parts == ["relay", "publish"]
    assert (caddy / "relay.caddy").read_text() == server.caddy_snippet("relay", "relay.example.com", 8101)


def test_sudo_block_is_printed_and_never_run(tmp_path, monkeypatch):
    spawned = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: spawned.append(argv) or real(["true"], *a, **k))
    monkeypatch.setattr(subprocess, "Popen", lambda argv, *a, **k: spawned.append(argv) or pytest.fail("spawned"))
    ctx, out, err, calls = stub_ctx(tmp_path, SystemdFake())
    server_setup(
        ctx,
        "edge",
        names=server.derive_names("example.com"),
        caddy_dir=str(tmp_path / "missing" / "orch.d"),
        resolve=lambda n: [VPS],
        local=local,
        user="orch-edge",
    )
    text = out.getvalue()
    assert "sudo systemctl reload caddy" in text and "sudo install -d" in text and "enable-linger orch-edge" in text
    assert "not written" in text  # the directory does not exist yet: told to do the root steps and rerun
    everything = [c for c in calls] + spawned
    assert not any("sudo" in str(c) for c in everything)
    assert not any(c[:1] == ["systemctl"] and "caddy" in c for c in calls)
    assert not any("caddy" in str(c[0]) for c in calls)


def test_dns_not_ready_skips_tls_for_that_part_only(tmp_path):
    root = tmp_path / "orch.d"
    caddy = root / "orch-edge"
    caddy.mkdir(parents=True)
    ctx, out, err, calls = stub_ctx(tmp_path, SystemdFake())
    table = {"relay.example.com": [], "publish.example.com": [VPS]}
    plan = server_setup(
        ctx,
        "edge",
        names=server.derive_names("example.com"),
        caddy_dir=str(root),
        resolve=lambda n: table[n],
        local=local,
        user="orch-edge",
    )
    assert [p.name for p in caddy.iterdir()] == ["publish.caddy"]
    text = out.getvalue()
    assert "relay.example.com: no DNS record" in text and "run this same command again" in text
    assert set(load(ctx.layout)["parts"]) == {"relay", "publish"}  # still installed, on loopback
    assert plan.tls_parts == ["publish"]


def test_hub_role_binds_the_vpn_ip_and_the_card_advertises_it(tmp_path):
    root = tmp_path / "orch.d"
    caddy = root / "orch-hub"
    caddy.mkdir(parents=True)
    ctx, out, err, calls = stub_ctx(tmp_path, SystemdFake())
    server_setup(
        ctx,
        "hub",
        names=server.derive_names("example.com"),
        hub_vpn_only=True,
        caddy_dir=str(root),
        resolve=lambda n: [VPS],
        local=local,
        user="orch-hub",
    )
    assert set(load(ctx.layout)["parts"]) == {"hub"}
    argv, _ = ctx.runner.started["hub"]
    assert argv[argv.index("--link-listen") + 1] == f"{VPN}:8105"
    assert argv[argv.index("--web-listen") + 1] == "127.0.0.1:8100"
    assert "--bind-all" not in argv
    assert argv[argv.index("--publish-url") + 1] == "https://publish.example.com"
    assert argv[argv.index("--relay-url") + 1] == "https://relay.example.com"
    card = json.loads((ctx.layout.data("hub") / "hub-settings.json").read_text())
    assert card["addresses"][0] == {"kind": "private", "web": "https://hub.example.com:443", "link": f"{VPN}:8105"}
    assert "not remote_ip 100.64.0.0/16" in (caddy / "hub.caddy").read_text()
    assert "https://hub.example.com/#setup=TOKEN" in out.getvalue()
    assert f"resolve hub.example.com to {VPN}" in out.getvalue()
    import re

    for a in card["addresses"]:  # the shape orch_hub.settings validates (web needs a port, link is host:port)
        assert re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]{1,100}:[0-9]{1,5}", a["web"])
        assert a["link"] is None or re.fullmatch(r"[A-Za-z0-9.\-\[\]:]{1,100}:[0-9]{1,5}", a["link"])


def test_hub_without_vpn_only_is_a_public_console(tmp_path):
    root = tmp_path / "orch.d"
    caddy = root / "orch-hub"
    caddy.mkdir(parents=True)
    ctx, *_ = stub_ctx(tmp_path, SystemdFake())
    server_setup(
        ctx,
        "hub",
        names=server.derive_names(None, hub="hub.example.com"),
        caddy_dir=str(root),
        resolve=lambda n: [VPS],
        local=local,
        user="orch-hub",
    )
    assert "remote_ip" not in (caddy / "hub.caddy").read_text()
    card = json.loads((ctx.layout.data("hub") / "hub-settings.json").read_text())
    assert card["addresses"][0]["kind"] == "public"


def test_hub_vpn_only_needs_a_name_and_a_vpn_address(tmp_path):
    ctx, *_ = stub_ctx(tmp_path)
    with pytest.raises(InstallError, match="needs a name"):
        server_setup(ctx, "hub", names=server.Names(), hub_vpn_only=True, local=local)
    with pytest.raises(InstallError, match="no private-network"):
        server_setup(
            ctx,
            "hub",
            names=server.derive_names("example.com"),
            local=lambda: [server.Addr(VPS)],
            resolve=lambda n: [VPS],
        )
    with pytest.raises(InstallError, match="belongs to --role hub"):
        server_setup(ctx, "edge", names=server.derive_names("example.com"), hub_vpn_only=True, local=local)


def test_no_names_is_the_ip_port_fallback(tmp_path):
    ctx, out, err, calls = stub_ctx(tmp_path)
    plan = server_setup(ctx, "edge", names=server.Names(), address="192.168.1.20", local=local)
    assert plan.snippets == {} and plan.dns == []
    assert set(load(ctx.layout)["parts"]) == {"relay", "publish"}
    text = out.getvalue()
    assert "No domain name given" in text and "http://192.168.1.20:8101" in text
    assert "http://192.168.1.20:8102" in text and "sudo" not in text and "caddy" not in text.lower()
    argv, _ = ctx.runner.started["relay"]
    assert argv[argv.index("--host") + 1] == "0.0.0.0"  # today's behaviour


def test_hub_fallback_takes_the_vpn_address(tmp_path):
    ctx, out, *_ = stub_ctx(tmp_path)
    server_setup(ctx, "hub", names=server.Names(), local=local)
    argv, _ = ctx.runner.started["hub"]
    assert f"{VPN}:8105" in argv and f"http://{VPN}:8100" in out.getvalue()


def test_cli_server_command_and_usage(tmp_path):
    ctx, out, err, _ = stub_ctx(tmp_path)
    assert cli.main(["server"], env=ctx.env, out=out, err=err, ctx=ctx) == 2  # --role is required
    assert cli.main(["server", "--role", "other"], env=ctx.env, out=out, err=err, ctx=ctx) == 2
    assert cli.main(["server", "--role", "hub", "--hub-vpn-only"], env=ctx.env, out=out, err=err, ctx=ctx) == 1
    assert "needs a name" in err.getvalue()


# ------------------------------------------------------------------------------------------------------- ops


def test_ops_units_are_written_and_enabled_for_the_hub(tmp_path):
    root = tmp_path / "orch.d"
    caddy = root / "orch-hub"
    caddy.mkdir(parents=True)
    ctx, out, err, calls = stub_ctx(tmp_path, SystemdFake())
    server_setup(
        ctx,
        "hub",
        names=server.derive_names("example.com"),
        caddy_dir=str(root),
        resolve=lambda n: [VPS],
        local=local,
        user="orch-hub",
    )
    d = tmp_path / ".config" / "systemd" / "user"
    assert {"orch-health.service", "orch-health.timer", "orch-hub-backup.service", "orch-hub-backup.timer"} <= {
        p.name for p in d.iterdir()
    }
    assert ["systemctl", "--user", "enable", "--now", "orch-health.timer"] in calls
    assert ["systemctl", "--user", "enable", "--now", "orch-hub-backup.timer"] in calls
    cfg = json.loads((ctx.layout.root / "ops" / "health.json").read_text())
    assert cfg["parts"] == [{"name": "hub", "url": "http://127.0.0.1:8100/"}]
    assert cfg["cert_names"] == ["hub.example.com"]


def test_edge_has_the_health_timer_but_no_backup_timer(tmp_path):
    root = tmp_path / "orch.d"
    caddy = root / "orch-edge"
    caddy.mkdir(parents=True)
    ctx, out, err, calls = stub_ctx(tmp_path, SystemdFake())
    server_setup(
        ctx,
        "edge",
        names=server.derive_names("example.com"),
        caddy_dir=str(root),
        resolve=lambda n: [VPS],
        local=local,
        user="orch-edge",
    )
    names = {p.name for p in (tmp_path / ".config" / "systemd" / "user").iterdir()}
    assert "orch-health.timer" in names and "orch-hub-backup.timer" not in names
    cfg = json.loads((ctx.layout.root / "ops" / "health.json").read_text())
    assert [p["url"] for p in cfg["parts"]] == ["http://127.0.0.1:8101/healthz", "http://127.0.0.1:8102/healthz"]


def test_unit_files_golden():
    f = ops.health_unit_files("/usr/bin/python3", Path("/home/u/h.json"))
    golden("orch-health.service", f["orch-health.service"])
    golden("orch-health.timer", f["orch-health.timer"])
    b = ops.backup_unit_files("/usr/bin/python3", Path("/home/u/data/backups"), Path("/home/u/orch-backup-out"))
    golden("orch-hub-backup.service", b["orch-hub-backup.service"])
    golden("orch-hub-backup.timer", b["orch-hub-backup.timer"])


def test_health_reports_every_problem_in_plain_words():
    cfg = {
        "parts": [{"name": "hub", "url": "u1"}, {"name": "relay", "url": "u2"}],
        "cert_names": ["a.example.com", "b.example.com"],
        "disk_path": "/",
    }
    rep = ops.run_health(
        cfg,
        http=lambda u: None if u == "u1" else "no answer",
        ntp=lambda: False,
        cert_days=lambda n: 3.0 if n.startswith("a") else None,
        mem_mb=lambda: 120.0,
        disk_free_gb=lambda p: 0.5,
        now=lambda: 1000,
    )
    assert rep["ok"] is False and rep["ts"] == 1000
    text = "\n".join(rep["problems"])
    for needle in (
        "relay is not healthy",
        "0.5 GB of disk",
        "NTP",
        "a.example.com expires in 3 days",
        "cannot read the certificate of b.example.com",
        "120 MB of memory",
    ):
        assert needle in text


def test_health_ok_when_all_is_well():
    rep = ops.run_health(
        {"parts": [{"name": "hub", "url": "u"}], "cert_names": ["a.example.com"]},
        http=lambda u: None,
        ntp=lambda: True,
        cert_days=lambda n: 60.0,
        mem_mb=lambda: 900.0,
        disk_free_gb=lambda p: 20.0,
    )
    assert rep["ok"] and rep["problems"] == []


def test_health_main_writes_status_file_and_exits_1_on_trouble(tmp_path, monkeypatch):
    cfg = tmp_path / "h.json"
    status = tmp_path / "s" / "status.json"
    cfg.write_text(json.dumps({"parts": [], "status_file": str(status), "min_disk_gb": 10**9}))
    assert ops.main(["health", "--config", str(cfg)]) == 1
    assert json.loads(status.read_text())["ok"] is False
    assert oct(status.stat().st_mode & 0o777) == "0o600"


def test_backup_copy_is_incremental_and_prunes(tmp_path):
    src, dest = tmp_path / "backups", tmp_path / "out"
    src.mkdir()
    for i in range(1, 5):
        (src / f"hub-abcd1234-2026010{i}T030000Z.orchbak").write_bytes(b"sealed%d" % i)
    copied, problems = ops.backup_copy(src, dest, keep=3)
    assert len(copied) == 4 and problems == []
    assert sorted(p.name for p in dest.iterdir()) == [f"hub-abcd1234-2026010{i}T030000Z.orchbak" for i in (2, 3, 4)]
    copied2, _ = ops.backup_copy(src, dest, keep=3)
    assert copied2 == []  # nothing new, and the pruned one is not copied back
    (src / "hub-abcd1234-20260105T030000Z.orchbak").write_bytes(b"new")
    assert ops.backup_copy(src, dest, keep=3)[0] == ["hub-abcd1234-20260105T030000Z.orchbak"]
    assert oct(dest.stat().st_mode & 0o777) == "0o700"


def test_backup_copy_warns_when_there_is_no_backup_yet(tmp_path):
    copied, problems = ops.backup_copy(tmp_path / "none", tmp_path / "out")
    assert copied == [] and "no hub backup yet" in problems[0]


def test_setup_server_flag_goes_to_install_server_without_a_workspace(tmp_path):
    from orch.setup import flow

    ctx, out, err, calls = stub_ctx(tmp_path)
    rc = flow.main(
        ["--server", "--role", "edge", "--address", "192.168.1.20"], env=ctx.env, out=out, err=err, install_ctx=ctx
    )
    assert rc == 0, err.getvalue()
    assert set(load(ctx.layout)["parts"]) == {"relay", "publish"} and not load(ctx.layout)["workspaces"]
    err2 = io.StringIO()
    assert flow.main(["--server", "--role", "edge"], env={"ORCH_GRANT": "x"}, out=out, err=err2) == 3


def test_a_part_that_answers_after_five_seconds_passes_and_a_dead_one_fails_in_plain_words(tmp_path):
    t = {"now": 0.0}
    ctx, out, err, _ = stub_ctx(tmp_path)
    ctx.clock = lambda: t["now"]
    ctx.sleep = lambda s: t.__setitem__("now", t["now"] + s)
    ctx.health = lambda h, p: t["now"] >= 5.0  # the stub part comes up after 5 s
    ctx.port_free = lambda p, h: True
    ctx.runner.info = lambda part: {"running": False, "pid": None, "service": "x"}
    assert cli.main(["publish", "--address", "127.0.0.1"], env=ctx.env, out=out, err=err, ctx=ctx) == 0
    assert 5.0 <= t["now"] < 7.0
    ctx.health = lambda h, p: False
    assert cli.main(["relay", "--address", "127.0.0.1"], env=ctx.env, out=out, err=err, ctx=ctx) == 1
    assert "within 60 seconds" in err.getvalue()
    assert t["now"] >= 65.0


# ------------------------------------------------------------------------------------------------ --port-base


def test_port_base_gives_one_block_and_the_card_says_the_chosen_link_port(tmp_path):
    ctx, out, err, _ = stub_ctx(tmp_path)
    ctx.port_base = 8200
    cli.full_server(ctx, ["publish", "relay", "hub"], address="100.64.1.2")
    st = load(ctx.layout)
    assert {p: st["parts"][p]["port"] for p in st["parts"]} == {"hub": 8200, "relay": 8201, "publish": 8202}
    assert st["parts"]["hub"]["link_port"] == 8205 and st["port_base"] == 8200
    card = json.loads((ctx.layout.data("hub") / "hub-settings.json").read_text())
    assert card["addresses"][0]["link"] == "100.64.1.2:8205" and card["addresses"][0]["web"].endswith(":8200")
    argv, _ = ctx.runner.started["hub"]
    assert "100.64.1.2:8205" in argv and "http://100.64.1.2:8202" in argv  # link and the publish default
    # what a workspace stores comes from this card: hub_where carries the link address
    from orch.hubclient.where import check_hub_where

    where = {
        "name": "Hub",
        "addresses": [{"kind": a["kind"], "link": a["link"]} for a in card["addresses"] if a["link"]],
    }
    assert check_hub_where(where)["addresses"][0]["link"] == "100.64.1.2:8205"
    assert not any(str(p) in out.getvalue() for p in (8100, 8101, 8102, 8105))


def test_port_base_default_is_unchanged(tmp_path):
    ctx, *_ = stub_ctx(tmp_path)
    cli.full_server(ctx, ["publish", "relay", "hub"], address="100.64.1.2")
    st = load(ctx.layout)
    assert [st["parts"][p]["port"] for p in ("hub", "relay", "publish")] == [8100, 8101, 8102]
    assert st["parts"]["hub"]["link_port"] == 8105 and "port_base" not in st


def test_a_busy_port_is_refused_before_anything_is_written_and_nothing_is_killed(tmp_path):
    ctx, out, err, calls = stub_ctx(tmp_path)
    ctx.port_free = lambda p, h: p != 8202
    rc = cli.main(["publish", "--port-base", "8200", "--address", "127.0.0.1"], env=ctx.env, out=out, err=err, ctx=ctx)
    assert rc == 1
    assert "port 8202" in err.getvalue() and "already used by another program on this computer" in err.getvalue()
    assert "pick another --port-base" in err.getvalue()
    assert not ctx.layout.root.exists() and not calls  # nothing installed, nothing run (no kill either)
    with pytest.raises(InstallError, match="port 8205"):  # the hub link counts
        ctx.port_free = lambda p, h: p != 8205
        server_setup(ctx, "hub", names=server.Names(), local=local)
    assert not ctx.layout.root.exists()


def test_a_rerun_may_use_its_own_ports_and_the_block_must_fit(tmp_path):
    ctx, out, err, _ = stub_ctx(tmp_path)
    ctx.port_base = 8200
    cli.full_server(ctx, ["relay"], address="127.0.0.1")
    ctx.port_free = lambda p, h: p != 8201  # held by our own relay
    cli.full_server(ctx, ["relay"], address="127.0.0.1")
    for bad in (80, 65531):
        assert cli.main(["relay", "--port-base", str(bad)], env=ctx.env, out=out, err=err, ctx=ctx) == 1
    assert "must fit in 1024 to 65535" in err.getvalue()


def test_caddy_proxies_to_the_chosen_ports(tmp_path):
    root = tmp_path / "orch.d"
    caddy = root / "orch-edge"
    caddy.mkdir(parents=True)
    ctx, *_ = stub_ctx(tmp_path, SystemdFake())
    ctx.port_base = 8200
    server_setup(
        ctx,
        "edge",
        names=server.derive_names("example.com"),
        caddy_dir=str(root),
        resolve=lambda n: [VPS],
        local=local,
        user="orch-edge",
    )
    assert "reverse_proxy 127.0.0.1:8201" in (caddy / "relay.caddy").read_text()
    assert "reverse_proxy 127.0.0.1:8202" in (caddy / "publish.caddy").read_text()


def test_setup_passes_port_base_to_the_full_server(tmp_path):
    from orch.setup import flow

    assert "--port-base" in flow.USAGE


# ------------------------------------------------------------------------------------- H2: behind the edge


def test_command_for_behind_edge_has_both_hub_flags_and_the_relay_gets_its_proxy_env():
    from pathlib import Path

    from orch.install import parts as P

    kw = dict(
        binary=Path("/b/orch-hub"),
        data=Path("/d"),
        address="127.0.0.1",
        port=8100,
        publish_url=None,
        relay_url=None,
        dev=False,
    )
    plain = P.command_for("hub", **kw)
    edge = P.command_for("hub", behind_edge=True, link_address=VPN, **kw)
    assert "--behind-edge" not in plain and "--trusted-peer" not in plain
    assert edge[edge.index("--trusted-peer") + 1] == "127.0.0.1" and "--behind-edge" in edge
    assert P.env_for("relay", Path("/d"), behind_edge=True)["ORCH_RELAY_TRUSTED_PROXY"] == "127.0.0.1"
    assert "ORCH_RELAY_TRUSTED_PROXY" not in P.env_for("relay", Path("/d"))


def test_server_role_with_tls_starts_hub_and_relay_behind_the_edge_and_the_card_has_one_https_entry(tmp_path):
    (tmp_path / "e").mkdir()
    ctx, *_ = stub_ctx(tmp_path, SystemdFake())
    (tmp_path / "orch.d" / "orch-hub").mkdir(parents=True)
    server_setup(
        ctx,
        "hub",
        names=server.derive_names("example.com"),
        hub_vpn_only=True,
        caddy_dir=str(tmp_path / "orch.d"),
        resolve=lambda n: [VPS],
        local=local,
        user="orch-hub",
    )
    argv, _ = ctx.runner.started["hub"]
    assert "--behind-edge" in argv and argv[argv.index("--trusted-peer") + 1] == "127.0.0.1"
    card = json.loads((ctx.layout.data("hub") / "hub-settings.json").read_text())
    assert [a["web"] for a in card["addresses"]] == [
        "https://hub.example.com:443"
    ]  # no http entry: behind_edge refuses it
    ctx2, *_ = stub_ctx(tmp_path / "e", SystemdFake())
    (tmp_path / "o2" / "orch-edge").mkdir(parents=True)
    server_setup(
        ctx2,
        "edge",
        names=server.derive_names("example.com"),
        caddy_dir=str(tmp_path / "o2"),
        resolve=lambda n: [VPS],
        local=local,
        user="orch-edge",
    )
    assert ctx2.runner.started["relay"][1]["ORCH_RELAY_TRUSTED_PROXY"] == "127.0.0.1"


def test_no_tls_means_no_edge_mode(tmp_path):
    ctx, *_ = stub_ctx(tmp_path, SystemdFake())
    server_setup(
        ctx,
        "hub",
        names=server.derive_names("example.com"),
        caddy_dir=str(tmp_path / "x"),
        resolve=lambda n: [],
        local=local,
        user="orch-hub",
    )  # DNS missing: no TLS, no edge flags
    assert "--behind-edge" not in ctx.runner.started["hub"][0]


# ----------------------------------------------------------------------------------------- H3: never --bind-all


def test_the_hub_of_a_server_never_listens_on_every_interface(tmp_path):
    for names, kw in ((server.Names(), {}), (server.derive_names("example.com"), {"resolve": lambda n: [VPS]})):
        home = tmp_path / ("a" if not names.hub else "b")
        home.mkdir()
        (home / "orch.d" / "orch-hub").mkdir(parents=True)
        ctx, *_ = stub_ctx(home, SystemdFake())
        server_setup(ctx, "hub", names=names, local=local, caddy_dir=str(home / "orch.d"), user="orch-hub", **kw)
        argv, _ = ctx.runner.started["hub"]
        assert "--bind-all" not in argv and "0.0.0.0" not in " ".join(argv)
        assert argv[argv.index("--link-listen") + 1] == f"{VPN}:8105"
        web = argv[argv.index("--web-listen") + 1]
        assert web in ("127.0.0.1:8100", f"{VPN}:8100")


def test_no_vpn_address_refuses_the_hub_in_plain_words_with_or_without_names(tmp_path):
    ctx, *_ = stub_ctx(tmp_path)
    for names in (server.Names(), server.derive_names("example.com")):
        with pytest.raises(InstallError, match="no private-network .VPN. address found"):
            server_setup(
                ctx,
                "hub",
                names=names,
                local=lambda: [server.Addr(VPS), server.Addr("127.0.0.1")],
                resolve=lambda n: [VPS],
            )
    assert not ctx.runner.started


def test_a_server_hub_refuses_another_address(tmp_path):
    ctx, *_ = stub_ctx(tmp_path)
    with pytest.raises(InstallError, match="private-network address 100.64.7.9"):
        server_setup(ctx, "hub", names=server.Names(), address="192.168.1.20", local=local)


# ----------------------------------------------------------------------- the hub bind is never wider than asked


def test_explicit_loopback_is_honoured_and_prints_the_binds(tmp_path):
    ctx, out, *_ = stub_ctx(tmp_path)
    server_setup(ctx, "hub", names=server.Names(), address="127.0.0.1", local=local)
    argv, _ = ctx.runner.started["hub"]
    assert argv[argv.index("--listen") + 1] == "127.0.0.1:8105" and "--link-listen" not in argv
    assert argv[argv.index("--web-listen") + 1] == "127.0.0.1:8100"
    assert "--bind-all" not in argv and VPN not in " ".join(argv)
    assert "The hub listens on: console 127.0.0.1:8100, link 127.0.0.1:8105 (nothing else)." in out.getvalue()


def test_no_address_means_the_vpn_ip_and_the_binds_are_printed(tmp_path):
    ctx, out, *_ = stub_ctx(tmp_path)
    server_setup(ctx, "hub", names=server.Names(), local=local)
    argv, _ = ctx.runner.started["hub"]
    assert argv[argv.index("--link-listen") + 1] == f"{VPN}:8105"
    assert f"The hub listens on: console {VPN}:8100, link {VPN}:8105 (nothing else)." in out.getvalue()


def test_a_public_address_is_refused_without_anything_started(tmp_path):
    ctx, *_ = stub_ctx(tmp_path)
    for bad in (VPS, "0.0.0.0", "192.168.1.20"):
        with pytest.raises(InstallError, match="127.0.0.1 only"):
            server_setup(ctx, "hub", names=server.Names(), address=bad, local=local)
    assert not ctx.runner.started


def test_loopback_with_names_or_vpn_only_is_refused(tmp_path):
    ctx, *_ = stub_ctx(tmp_path)
    with pytest.raises(InstallError, match="cannot be combined with domain names"):
        server_setup(ctx, "hub", names=server.derive_names("example.com"), address="127.0.0.1", local=local)


def test_runbook_backup_pull_uses_a_restricted_key_and_no_shell():
    doc = (Path(__file__).resolve().parents[4] / "docs" / "pilot-server.md").read_text()
    assert "restrict," in doc and "rrsync -ro" in doc and 'from="100.64.0.0/10"' in doc
    assert "**Do not give `orch-hub` a login" in doc
    assert "first hop from the internet" in doc and "orch-hub restore --state" in doc
    # one design, one user name, one path, used the same way in every command
    assert " backup@" not in doc and "orch-backup-out" not in doc
    drop = "/var/lib/orch-backup-drop"
    assert doc.count(drop) >= 3
    assert f'rrsync -ro {drop}"' in doc and "orch-backup@100.x.y.z:/ " in doc
    assert f"install -d -m 2770 -o orch-backup -g orch-backup {drop}" in doc
    assert "usermod -aG orch-backup orch-hub" in doc and f"--backup-drop {drop}" in doc
    assert "command -v rrsync" in doc


def test_shared_drop_folder_is_not_re_moded_and_files_are_group_readable(tmp_path):
    src, drop = tmp_path / "b", tmp_path / "drop"
    src.mkdir()
    drop.mkdir(mode=0o2770)
    drop.chmod(0o2770)
    (src / "hub-abcd1234-20260101T030000Z.orchbak").write_bytes(b"x")
    copied, problems = ops.backup_copy(src, drop, shared=True)
    assert len(copied) == 1 and problems == []
    assert oct(drop.stat().st_mode & 0o7777) == "0o2770"  # untouched
    assert oct((drop / copied[0]).stat().st_mode & 0o777) == "0o640"
    assert ops.backup_copy(src, tmp_path / "nope", shared=True)[1][0].startswith("the backup drop folder")
    assert "--shared" in ops.backup_unit_files("/p", src, drop, True)["orch-hub-backup.service"]
    assert "--shared" not in ops.backup_unit_files("/p", src, drop)["orch-hub-backup.service"]


def test_backup_drop_flag_reaches_the_unit(tmp_path):
    (tmp_path / "orch.d" / "orch-hub").mkdir(parents=True)
    ctx, *_ = stub_ctx(tmp_path, SystemdFake())
    server_setup(
        ctx,
        "hub",
        names=server.derive_names("example.com"),
        caddy_dir=str(tmp_path / "orch.d"),
        resolve=lambda n: [VPS],
        local=local,
        user="orch-hub",
        backup_drop="/var/lib/orch-backup-drop",
    )
    unit = (tmp_path / ".config/systemd/user/orch-hub-backup.service").read_text()
    assert "--dest /var/lib/orch-backup-drop" in unit and "--shared" in unit


def test_backup_out_folder_is_made_0700_on_every_run(tmp_path):
    src, dest = tmp_path / "b", tmp_path / "out"
    src.mkdir()
    dest.mkdir(mode=0o755)
    ops.backup_copy(src, dest)
    assert oct(dest.stat().st_mode & 0o777) == "0o700"
