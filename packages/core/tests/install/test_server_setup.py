"""The pilot server setup: roles, names, DNS check, Caddy snippets (golden), the printed sudo block, the ops units.
No DNS, no Caddy, no systemd, no sudo: every seam is a stub."""

from __future__ import annotations

import datetime
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
    assert "import /etc/caddy/orch.d/orch-edge/*.caddy" in b and "/*/*" not in b and "systemctl reload caddy" in b
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
    assert argv[argv.index("--host") + 1] == "192.168.1.20"  # the given address, never wider


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
    assert sorted(p.name for p in dest.glob("hub-*")) == [f"hub-abcd1234-2026010{i}T030000Z.orchbak" for i in (2, 3, 4)]
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
    assert f'rrsync -ro {drop}"' in doc and "--exclude '.*' orch-backup@100.x.y.z: " in doc
    assert "sudo chmod 700 /home/orch-backup" in doc
    assert f"install -d -m 3770 -o orch-backup -g orch-backup {drop}" in doc
    assert "usermod -aG orch-backup orch-hub" in doc and f"--backup-drop {drop}" in doc
    assert "command -v rrsync" in doc


def test_shared_drop_folder_is_not_re_moded_and_files_are_group_readable(tmp_path):
    src, drop = tmp_path / "b", tmp_path / "drop"
    src.mkdir()
    drop.mkdir()
    drop.chmod(0o3770)
    (src / "hub-abcd1234-20260101T030000Z.orchbak").write_bytes(b"x")
    copied, problems = ops.backup_copy(src, drop, shared=True, record=tmp_path / "rec.json")
    assert len(copied) == 1 and problems == []
    assert oct(drop.stat().st_mode & 0o7777) == "0o3770"  # untouched
    assert oct((drop / copied[0]).stat().st_mode & 0o777) == "0o640"
    assert ops.backup_copy(src, tmp_path / "nope", shared=True, record=tmp_path / "rec.json")[1][0].startswith(
        "the backup drop folder"
    )
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


def test_allow_non_cgnat_warns_for_a_lan_v4_and_every_ipv6_is_refused(tmp_path):
    lan = [
        server.Addr("10.1.1.5", 24, "eth1"),
        server.Addr(VPN, 16, "wt0"),
        server.Addr(VPS, 24, "eth0"),
        server.Addr("fd00::5", 8, "eth2"),
        server.Addr("fd12:3456:789a::5", 64, "eth3"),
    ]
    ctx, out, *_ = stub_ctx(tmp_path)
    server_setup(ctx, "hub", names=server.Names(), local=lambda: lan, vpn_ip="10.1.1.5/24", allow_non_cgnat=True)
    text = out.getvalue()
    assert "WARNING: 10.1.1.5 is not in the usual private-network" in text and "10.1.1.0/24" in text
    assert "hub link will listen on that address" in text
    argv, _ = ctx.runner.started["hub"]
    assert argv[argv.index("--link-listen") + 1] == "10.1.1.5:8105"
    for bad in ("fd00::5", "fd00::5/8", "fd12:3456:789a::5/64"):
        for flag in (False, True):
            with pytest.raises(InstallError, match="uses IPv4 addresses"):
                server.check_vpn_ip(bad, lan, allow_non_cgnat=flag)
    with pytest.raises(InstallError, match="--allow-non-cgnat"):  # without the flag a LAN address is still refused
        server.check_vpn_ip("10.1.1.5", lan)


def test_health_warns_about_files_in_the_drop_this_hub_did_not_copy(tmp_path):
    src, drop = _shared(tmp_path)
    rec = tmp_path / "rec.json"
    ops.backup_copy(src, drop, shared=True, record=rec)
    cfg = {"parts": [], "backup_drop": str(drop), "backup_record": str(rec)}
    kw = dict(ntp=lambda: True, mem_mb=lambda: 900.0, disk_free_gb=lambda p: 50.0)
    assert ops.run_health(cfg, **kw)["ok"]
    (drop / "hub-abcd1234-99999999T999999Z.orchbak").write_bytes(b"planted")
    rep = ops.run_health(cfg, **kw)
    assert not rep["ok"] and "unexpected or changed" in rep["problems"][0] and "99999999" in rep["problems"][0]


def _shared(tmp_path):
    src, drop = tmp_path / "b", tmp_path / "drop"
    src.mkdir()
    drop.mkdir()
    drop.chmod(0o3770)
    (src / "hub-abcd1234-20260101T030000Z.orchbak").write_bytes(b"real")
    return src, drop


def test_planted_links_in_the_shared_folder_are_never_followed(tmp_path):
    src, drop = _shared(tmp_path)
    canary = tmp_path / "canary.txt"
    canary.write_text("keep")
    canary.chmod(0o600)
    name = "hub-abcd1234-20260101T030000Z.orchbak"
    (drop / f".{name}.part").symlink_to(canary)  # the old predictable temp name
    (drop / name).symlink_to(tmp_path / "does-not-exist")  # dangling link at the target name
    copied, problems = ops.backup_copy(src, drop, shared=True, record=tmp_path / "rec.json")
    assert copied == [] and any("left alone" in p for p in problems)
    assert canary.read_text() == "keep" and oct(canary.stat().st_mode & 0o777) == "0o600"
    assert (drop / name).is_symlink() and not (tmp_path / "does-not-exist").exists()
    (drop / name).unlink()  # link removed: now it copies, and still not through the planted .part link
    copied, problems = ops.backup_copy(src, drop, shared=True, record=tmp_path / "rec.json")
    assert copied == [name] and problems == []
    assert canary.read_text() == "keep" and oct(canary.stat().st_mode & 0o777) == "0o600"
    assert (drop / name).read_bytes() == b"real" and not (drop / name).is_symlink()
    assert oct((drop / name).stat().st_mode & 0o777) == "0o640"


def test_a_symlinked_or_wrong_mode_or_foreign_drop_folder_copies_nothing(tmp_path):
    import getpass

    src, drop = _shared(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(drop)
    copied, problems = ops.backup_copy(src, link, shared=True, record=tmp_path / "rec.json")
    assert copied == [] and "link or not a folder" in problems[0] and list(drop.iterdir()) == []
    drop.chmod(0o770)
    copied, problems = ops.backup_copy(src, drop, shared=True, record=tmp_path / "rec.json")
    assert copied == [] and "mode 3770" in problems[0]
    drop.chmod(0o3770)
    copied, problems = ops.backup_copy(src, drop, shared=True, record=tmp_path / "rec.json", owner="no-such-user-xyz")
    assert copied == [] and "does not exist" in problems[0]
    assert ops.backup_copy(src, drop, shared=True, record=tmp_path / "rec.json", owner=getpass.getuser())[
        0
    ]  # the right owner passes
    plain_link = tmp_path / "plain-link"
    plain_link.symlink_to(tmp_path / "out")
    (tmp_path / "out").mkdir()
    assert ops.backup_copy(src, plain_link)[1] and list((tmp_path / "out").iterdir()) == []  # also not shared


def test_names_planted_in_the_shared_folder_change_neither_copy_nor_prune(tmp_path):
    src, drop = tmp_path / "b", tmp_path / "drop"
    src.mkdir()
    drop.mkdir()
    drop.chmod(0o3770)
    rec = tmp_path / "state" / "rec.json"
    canary = tmp_path / "canary.txt"
    canary.write_text("keep")
    real = [f"hub-abcd1234-2026010{i}T030000Z.orchbak" for i in (1, 2, 3)]
    for n in real[:2]:
        (src / n).write_bytes(b"r" + n[-14:-13].encode())
    fake_future = "hub-abcd1234-99999999T999999Z.orchbak"
    (drop / fake_future).write_bytes(b"fake")
    link = "hub-abcd1234-20251231T030000Z.orchbak"
    (drop / link).symlink_to(canary)
    foreign = "hub-ffffffff-20260102T030000Z.orchbak"
    (drop / foreign).write_bytes(b"other hub")
    copied, problems = ops.backup_copy(src, drop, keep=2, shared=True, record=rec)
    assert copied == real[:2] and problems == []  # the planted future name did not block the real backups
    (src / real[2]).write_bytes(b"r3")
    copied, _ = ops.backup_copy(src, drop, keep=2, shared=True, record=rec)
    assert copied == [real[2]]
    assert not (drop / real[0]).exists()  # our oldest copy is pruned (keep=2) ...
    assert (drop / real[1]).exists() and (drop / real[2]).exists()
    assert (drop / fake_future).read_bytes() == b"fake" and (drop / foreign).read_bytes() == b"other hub"
    assert (drop / link).is_symlink() and canary.read_text() == "keep"  # ... and nothing planted was touched
    assert oct(rec.stat().st_mode & 0o777) == "0o600"
    # a source file stamped far in the future (or with a wrong shape) is not copied
    (src / "hub-abcd1234-20990101T000000Z.orchbak").write_bytes(b"x")
    (src / "hub-abcd1234-bad.orchbak").write_bytes(b"x")
    assert ops.backup_copy(src, drop, keep=2, shared=True, record=rec)[0] == []
    # a record entry that is now a symlink or a directory is never deleted
    victim = tmp_path / "victim.txt"
    victim.write_text("v")
    (drop / real[1]).unlink()
    (drop / real[1]).symlink_to(victim)
    (src / "hub-abcd1234-20260104T030000Z.orchbak").write_bytes(b"r4")
    ops.backup_copy(src, drop, keep=2, shared=True, record=rec)
    assert victim.read_text() == "v" and (drop / real[1]).is_symlink()


def test_backup_drop_needs_root_owned_parents_that_nobody_else_can_write(tmp_path):
    import os

    def fake(uid, mode):
        return lambda p: os.stat_result((0o40000 | mode, 1, 1, 1, uid, 0, 0, 0, 0, 0))

    server.check_drop_path("/var/lib/orch-backup-drop", fake(0, 0o755))  # root-owned, not writable by others: fine
    for bad in ("relative/dir", "/var/../etc/x"):
        with pytest.raises(InstallError, match="full path"):
            server.check_drop_path(bad, fake(0, 0o755))
    with pytest.raises(InstallError, match="must belong to root"):  # a user-owned parent
        server.check_drop_path("/var/lib/orch-backup-drop", fake(1000, 0o755))
    with pytest.raises(InstallError, match="must belong to root"):  # group- or world-writable parent
        server.check_drop_path("/var/lib/orch-backup-drop", fake(0, 0o775))
    with pytest.raises(InstallError, match="must belong to root"):
        server.check_drop_path("/var/lib/orch-backup-drop", fake(0, 0o757))
    # the real filesystem: a temp folder of this user is refused through setup too
    ctx, *_ = stub_ctx(tmp_path)
    (tmp_path / "drop").mkdir()
    with pytest.raises(InstallError, match="must belong to root"):
        server_setup(ctx, "hub", names=server.Names(), local=local, backup_drop=str(tmp_path / "drop" / "x"))
    assert not ctx.runner.started


def test_a_drop_without_the_sticky_bit_is_refused(tmp_path):
    src, drop = _shared(tmp_path)
    drop.chmod(0o2770)
    copied, problems = ops.backup_copy(src, drop, shared=True, record=tmp_path / "rec.json")
    assert copied == [] and "3770" in problems[0] and list(drop.iterdir()) == []


def test_health_never_shows_a_raw_foreign_name_and_notices_a_replaced_file(tmp_path):
    src, drop = _shared(tmp_path)
    rec = tmp_path / "rec.json"
    copied, _ = ops.backup_copy(src, drop, shared=True, record=rec)
    cfg = {"parts": [], "backup_drop": str(drop), "backup_record": str(rec)}
    kw = dict(ntp=lambda: True, mem_mb=lambda: 900.0, disk_free_gb=lambda p: 50.0)
    assert ops.run_health(cfg, **kw)["ok"]
    evil = "x\x1b[31mRED\nFAKE: all fine\r.orchbak"
    (drop / evil).write_bytes(b"p")
    rep = ops.run_health(cfg, **kw)
    text = "\n".join(rep["problems"])
    assert not rep["ok"] and "1 unexpected or changed" in text
    assert "\x1b" not in text and "FAKE" not in text and "\n" not in rep["problems"][0]
    (drop / evil).unlink()
    # a member replaces the recorded file with their own file of the same name (same size, other content)
    name = copied[0]
    (drop / name).unlink()
    (drop / name).write_bytes(b"fake")  # "real" was 4 bytes too
    rep = ops.run_health(cfg, **kw)
    assert not rep["ok"] and ascii(name) in rep["problems"][0]
    # replaced by a symlink: also reported, never followed
    (drop / name).unlink()
    (drop / name).symlink_to(tmp_path / "canary")
    assert not ops.run_health(cfg, **kw)["ok"]


def _record(tmp_path, issued):
    f = tmp_path / "install.json"
    f.write_text(json.dumps({"revocations_issued_at": issued}))
    return str(f)


def _health_at(tmp_path, left_days, issued_as="iso"):
    now = 1_800_000_000.0
    issued = now + left_days * 86400 - 30 * 86400
    v = datetime.datetime.fromtimestamp(issued, datetime.UTC).isoformat().replace("+00:00", "Z")
    rec = _record(tmp_path, v if issued_as == "iso" else issued)
    return ops.run_health(
        {"parts": [], "update_record": rec},
        ntp=lambda: True,
        mem_mb=lambda: 900.0,
        disk_free_gb=lambda p: 50.0,
        now=lambda: now,
    ), now


def test_health_warns_ten_days_before_the_update_signature_list_expires(tmp_path):
    rep, _ = _health_at(tmp_path, 11)
    assert rep["ok"]  # 11 days left: quiet
    rep, now = _health_at(tmp_path, 9)
    date = datetime.datetime.fromtimestamp(now + 9 * 86400, datetime.UTC).strftime("%Y-%m-%d")
    assert not rep["ok"] and rep["problems"] == [
        f"the update signature list expires on {date}: ask your admin to renew it"
    ]
    rep, _ = _health_at(tmp_path, 9, issued_as="epoch")
    assert "expires on" in rep["problems"][0]


def test_health_warns_when_the_update_signature_list_has_expired(tmp_path):
    rep, _ = _health_at(tmp_path, -2)
    assert not rep["ok"] and "expired on" in rep["problems"][0] and "ask your admin to renew it" in rep["problems"][0]


def test_health_skips_the_signature_list_check_quietly_without_a_record(tmp_path):
    kw = dict(ntp=lambda: True, mem_mb=lambda: 900.0, disk_free_gb=lambda p: 50.0)
    assert ops.run_health({"parts": [], "update_record": str(tmp_path / "missing.json")}, **kw)["ok"]
    (tmp_path / "bad.json").write_text("not json")
    assert ops.run_health({"parts": [], "update_record": str(tmp_path / "bad.json")}, **kw)["ok"]
    assert ops.run_health({"parts": []}, **kw)["ok"]
    assert ops.run_health({"parts": [], "update_record": _record(tmp_path, "garbage")}, **kw)["ok"]


def test_expired_boundary_matches_install_sh_exactly_30_days(tmp_path):
    now = 1_800_000_000.0
    kw = dict(ntp=lambda: True, mem_mb=lambda: 900.0, disk_free_gb=lambda p: 50.0, now=lambda: now)

    def verdict(elapsed):
        iso = datetime.datetime.fromtimestamp(now - elapsed, datetime.UTC).isoformat().replace("+00:00", "Z")
        rep = ops.run_health({"parts": [], "update_record": _record(tmp_path, iso)}, **kw)
        return rep["problems"][0].split(" list ")[1].split(" on ")[0]

    assert verdict(30 * 86400) == "expired"  # (now - issued).days >= 30
    assert verdict(30 * 86400 - 1) == "expires"
    assert verdict(30 * 86400 + 5) == "expired"


# ------------------------------------------------------------------------ readiness probes the bound address


def test_readiness_probes_the_vpn_address_the_hub_binds_not_loopback(tmp_path, monkeypatch):
    probed = []
    monkeypatch.setattr(cli, "dial_host", lambda h: "127.0.0.1")  # as on the VPS: own addresses redirect to loopback
    ctx, out, *_ = stub_ctx(tmp_path)
    ctx.ready_deadline = 3.0
    t = {"now": 0.0}
    ctx.clock = lambda: t["now"]
    ctx.sleep = lambda s: t.__setitem__("now", t["now"] + s)

    def health(host, port, *a):
        probed.append((host, port))
        return (host, port) == (VPN, 8100)  # the hub answers ONLY on the VPN address (no --bind-all)

    ctx.health = health
    ctx.runner.info = lambda part: {"running": False, "pid": None, "service": "x"}
    server_setup(ctx, "hub", names=server.Names(), local=local)  # no domain: VPN-only bind
    assert (VPN, 8100) in probed and ("127.0.0.1", 8100) not in probed
    assert "The hub listens on" in out.getvalue()  # the setup went on to the end


def test_a_hub_that_answers_nowhere_gives_plain_words_naming_the_probed_address(tmp_path):
    ctx, *_ = stub_ctx(tmp_path)
    ctx.ready_deadline = 1.0
    t = {"now": 0.0}
    ctx.clock = lambda: t["now"]
    ctx.sleep = lambda s: t.__setitem__("now", t["now"] + s)
    ctx.health = lambda h, p, *a: False
    ctx.runner.info = lambda part: {"running": False, "pid": None, "service": "x"}
    with pytest.raises(InstallError, match=f"did not answer on {VPN}:8100"):
        server_setup(ctx, "hub", names=server.Names(), local=local)


def test_the_edge_binds_exactly_the_given_address_not_every_interface(tmp_path):
    ctx, out, *_ = stub_ctx(tmp_path)
    server_setup(ctx, "edge", names=server.Names(), address=VPS, local=local)
    relay = ctx.runner.started["relay"][0]
    pub = ctx.runner.started["publish"][0]
    assert relay[relay.index("--host") + 1] == VPS
    assert pub[pub.index("--listen") + 1] == f"{VPS}:8102"
    assert "0.0.0.0" not in " ".join(relay + pub)
    # unchanged for the plain `orch install relay --address X` (no server role)
    (tmp_path / "y").mkdir()
    ctx2, out2, err2, _ = stub_ctx(tmp_path / "y")
    cli.main(["relay", "--address", VPS], env=ctx2.env, out=out2, err=err2, ctx=ctx2)
    r2 = ctx2.runner.started["relay"][0]
    assert r2[r2.index("--host") + 1] == "0.0.0.0"


def test_command_for_exact_bind_keeps_loopback_loopback():
    from pathlib import Path

    from orch.install import parts as P

    kw = dict(binary=Path("/b"), data=Path("/d"), port=8101, publish_url=None, relay_url=None, dev=False)
    assert "127.0.0.1" in P.command_for("relay", address="127.0.0.1", exact_bind=True, **kw)
    assert "0.0.0.0" not in P.command_for("relay", address=VPS, exact_bind=True, **kw)


# ---------------------------------------------------------------------------------------- sudo -iu: user bus


def test_user_bus_is_found_when_linger_is_on_and_set_for_systemctl():
    env: dict[str, str] = {}
    server.ensure_user_bus(env, 1001, "orch-edge", exists=lambda p: p == "/run/user/1001/bus")
    assert env == {"XDG_RUNTIME_DIR": "/run/user/1001", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1001/bus"}
    full = {"XDG_RUNTIME_DIR": "/run/user/5", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"}
    server.ensure_user_bus(full, 1, "u", exists=lambda p: False)  # both set: untouched, no check
    assert full["XDG_RUNTIME_DIR"] == "/run/user/5"


def test_no_user_bus_stops_before_installing_in_plain_words(tmp_path, monkeypatch):
    with pytest.raises(InstallError, match=r"loginctl enable-linger orch-edge.*machinectl shell orch-edge@"):
        server.ensure_user_bus({}, 1001, "orch-edge", exists=lambda p: False)
    ctx, *_ = stub_ctx(tmp_path, SystemdFake())
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(server, "ensure_user_bus", lambda *a, **k: (_ for _ in ()).throw(InstallError("no session")))
    with pytest.raises(InstallError, match="no session"):
        server_setup(ctx, "edge", names=server.Names(), address="127.0.0.1", local=local, user="orch-edge")
    assert not ctx.runner.started and not ctx.layout.root.exists()  # nothing installed


def test_a_failing_system_command_is_plain_words_not_a_traceback(tmp_path):
    import subprocess

    ctx, out, err, _ = stub_ctx(tmp_path)

    def boom(*a, **k):
        raise subprocess.CalledProcessError(
            1, ["systemctl", "--user", "daemon-reload"], stderr=b"Failed to connect to bus\n"
        )

    ctx.runner.start = boom
    rc = cli.main(["relay", "--address", "127.0.0.1"], env=ctx.env, out=out, err=err, ctx=ctx)
    assert rc == 1 and "Traceback" not in err.getvalue()
    text = err.getvalue()
    assert "systemctl --user daemon-reload" in text and "Failed to connect to bus" in text
    assert "machinectl shell" in text and "linger" in text


def test_publish_without_a_name_says_plainly_that_it_needs_a_name_and_https(tmp_path):
    ctx, out, *_ = stub_ctx(tmp_path)
    server_setup(ctx, "edge", names=server.Names(), address="192.168.1.20", local=local)
    text = out.getvalue()
    assert "publish needs a name and https" in text and "once DNS is in" in text
    assert "invalid.input" not in text and "not set yet" not in text


def test_the_server_user_is_the_real_os_user_not_logname(monkeypatch):
    import os
    import pwd
    from types import SimpleNamespace

    from orch.install import serverflow

    monkeypatch.setenv("LOGNAME", "evil")
    monkeypatch.setenv("USER", "evil")
    monkeypatch.setattr(os, "geteuid", lambda: 4242)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="orch-edge") if uid == 4242 else None)
    assert serverflow.real_user() == "orch-edge"


def test_a_bad_user_name_stops_before_any_root_block_is_printed(tmp_path):
    for bad in ("evil; rm -rf /", "../x", "Evil", "", "a b"):
        with pytest.raises(InstallError, match="not a usable user name"):
            server.root_block(bad)
    ctx, out, *_ = stub_ctx(tmp_path, SystemdFake())
    (tmp_path / "orch.d").mkdir()
    with pytest.raises(InstallError, match="not a usable user name"):
        server_setup(
            ctx,
            "edge",
            names=server.derive_names("example.com"),
            caddy_dir=str(tmp_path / "orch.d"),
            resolve=lambda n: [VPS],
            local=local,
            user="evil; id",
        )
    assert "sudo" not in out.getvalue() and not ctx.runner.started


def test_every_printed_import_line_has_exactly_one_wildcard_and_names_its_user():
    import re
    import shutil
    import subprocess

    for user in ("orch-hub", "orch-edge"):
        block = server.root_block(user)
        lines = re.findall(r"import (\S+?)'", block)
        assert set(lines) == {f"/etc/caddy/orch.d/{user}/*.caddy"} and all(x.count("*") == 1 for x in lines)
        assert f"steps for {user}" in block
    docs = (Path(__file__).resolve().parents[4] / "docs" / "pilot-server.md").read_text()
    assert "orch.d/*/*" not in docs and "/etc/caddy/orch.d/orch-hub/*.caddy" in docs
    if shutil.which("caddy") is None:
        pytest.skip("caddy is not installed: the validate step is skipped")
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        for user, part in (("orch-hub", "hub"), ("orch-edge", "relay")):
            (Path(d) / user).mkdir()
            (Path(d) / user / f"{part}.caddy").write_text(server.caddy_snippet(part, f"{part}.example.com", 8100))
        cf = Path(d) / "Caddyfile"
        cf.write_text("".join(f"import {d}/{u}/*.caddy\n" for u in ("orch-hub", "orch-edge")))
        r = subprocess.run(
            ["caddy", "validate", "--config", str(cf), "--adapter", "caddyfile"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert r.returncode == 0, r.stderr


# ----------------------------------------------------------------------------- #1796: behind the proxy, ops, rerun


def test_behind_proxy_binds_the_vpn_ip_only_writes_no_snippet_and_trusts_the_vpn_network(tmp_path):
    ctx, out, err, calls = stub_ctx(tmp_path, SystemdFake())
    plan = server_setup(
        ctx,
        "edge",
        names=server.derive_names(None, relay="core.relay.example.com", publish="publish.example.com"),
        behind_proxy=True,
        local=local,
        user="orch-edge",
        caddy_dir=str(tmp_path / "caddy"),
    )
    relay_argv, relay_env = ctx.runner.started["relay"]
    assert relay_argv[relay_argv.index("--host") + 1] == VPN
    assert relay_env["ORCH_RELAY_TRUSTED_PROXY"] == "100.64.0.0/16"
    pub_argv, pub_env = ctx.runner.started["publish"]
    assert pub_argv[pub_argv.index("--listen") + 1] == f"{VPN}:8102"
    assert pub_argv[pub_argv.index("--public-origin") + 1] == "https://publish.example.com"
    assert pub_env["ORCH_PUBLISH_TRUSTED_PROXY"] == "100.64.0.0/16"
    assert plan.snippets == {} and not (tmp_path / "caddy").exists()
    assert not any(c[:1] == ["systemctl"] and "caddy" in c for c in calls)
    assert load(ctx.layout)["server"]["behind_proxy"] is True
    # no listener on loopback or on every interface
    for argv in (relay_argv, pub_argv):
        assert "0.0.0.0" not in " ".join(argv) and "127.0.0.1" not in " ".join(argv)


def test_behind_proxy_needs_a_vpn_address_the_edge_role_and_no_address(tmp_path):
    ctx, *_ = stub_ctx(tmp_path)
    with pytest.raises(InstallError, match="no private-network"):
        server_setup(ctx, "edge", names=server.Names(), behind_proxy=True, local=lambda: [server.Addr(VPS)])
    with pytest.raises(InstallError, match="role edge"):
        server_setup(ctx, "hub", names=server.Names(), behind_proxy=True, local=local)
    with pytest.raises(InstallError, match="address only"):
        server_setup(ctx, "edge", names=server.Names(), behind_proxy=True, address="1.2.3.4", local=local)


def test_the_proxy_networks_include_the_interfaces_ipv6_subnet():
    text = (
        IP_ADDR + "3: wt0    inet6 fd0f:80b5:c7cd:bbad::7/64 scope global wt0\n4: wt0    inet6 fe80::1/64 scope link\n"
    )
    addrs = server.local_interfaces(lambda argv: text if argv[0] == "ip" else "")
    chosen = server.detect_vpn_ip(addrs)
    assert server.proxy_networks(chosen, None, addrs) == ["100.64.0.0/16", "fd0f:80b5:c7cd:bbad::/64"]


def test_the_ops_timers_are_installed_in_the_no_domain_fallback_and_the_proxy_mode(tmp_path):
    for tag, kw in (("f", {"address": "127.0.0.1"}), ("p", {"behind_proxy": True})):
        ctx, out, err, calls = stub_ctx(tmp_path / tag, SystemdFake())
        server_setup(ctx, "edge", names=server.Names(), local=local, user="orch-edge", **kw)
        assert ["systemctl", "--user", "enable", "--now", "orch-health.timer"] in calls, tag
    ctx, out, err, calls = stub_ctx(tmp_path / "hub", SystemdFake())
    server_setup(ctx, "hub", names=server.Names(), local=local, user="orch-hub")
    assert ["systemctl", "--user", "enable", "--now", "orch-health.timer"] in calls
    assert ["systemctl", "--user", "enable", "--now", "orch-hub-backup.timer"] in calls
    cfg = json.loads((ctx.layout.root / "ops" / "health.json").read_text())
    assert cfg["parts"][0]["url"].startswith(f"http://{VPN}:")  # the part answers on the address it listens on


def test_a_rerun_keeps_the_port_of_the_part_this_user_already_runs(tmp_path):
    ctx, out, err, calls = stub_ctx(tmp_path, SystemdFake())
    ctx.runner.started["hub"] = (["orch-hub"], {})  # an earlier run died after starting the hub, before recording it
    held = {8100}
    ctx.port_free = lambda p, h: p not in held or ("hub" not in ctx.runner.started)
    server_setup(ctx, "hub", names=server.Names(), local=local, user="orch-hub")
    assert load(ctx.layout)["parts"]["hub"]["port"] == 8100  # not 8103
    held.add(8101)  # a port held by something else is still refused or skipped, never taken over
    assert ctx.port_free(8101, "x") is False


# ------------------------------------------------------------------------------------------------------ --hub-name

NAME = "core.hub.example.com"


def _name_setup(ctx, name=NAME, resolve=None, role="hub", **kw):
    return server_setup(
        ctx,
        role,
        names=kw.pop("names", server.Names()),
        local=local,
        hub_name=name,
        resolve=resolve or (lambda n: [VPN]),
        **kw,
    )


def test_hub_name_writes_one_private_http_entry_and_the_link_keeps_the_ip(tmp_path):
    ctx, out, err, _ = stub_ctx(tmp_path, SystemdFake())
    _name_setup(ctx, "Core.Hub.Example.com")  # upper case is normalised
    card = json.loads((ctx.layout.data("hub") / "hub-settings.json").read_text())
    assert card["addresses"] == [{"kind": "private", "web": f"http://{NAME}:8100", "link": f"{VPN}:8105"}]
    argv, _ = ctx.runner.started["hub"]
    assert argv[argv.index("--link-listen") + 1] == f"{VPN}:8105"
    assert load(ctx.layout)["hub_name"] == NAME
    assert f"http://{NAME}:8100" in out.getvalue() and "the name only" in out.getvalue()
    assert "caddy" not in out.getvalue().lower()


def test_a_rerun_without_the_flag_keeps_the_name(tmp_path):
    ctx, *_ = stub_ctx(tmp_path, SystemdFake())
    _name_setup(ctx)
    ctx2, *_ = stub_ctx(tmp_path, SystemdFake())
    server_setup(ctx2, "hub", names=server.Names(), local=local, resolve=lambda n: [VPN])
    card = json.loads((ctx2.layout.data("hub") / "hub-settings.json").read_text())
    assert card["addresses"][0]["web"] == f"http://{NAME}:8100"
    assert load(ctx2.layout)["hub_name"] == NAME


@pytest.mark.parametrize(
    "bad",
    [
        "core",  # one label
        "*.hub.example.com",
        "-a.example.com",
        "a-.example.com",
        "a..example.com",
        "core.hub.example.com.",
        "core.hub.example.com:8100",
        "100.64.7.9",
        "[::1]",
        "core_x.example.com",
        "x" * 64 + ".example.com",
        ".".join(["abcdefghi"] * 30),
        "",
    ],
)
def test_a_bad_hub_name_is_refused_before_anything_is_installed(tmp_path, bad):
    ctx, *_ = stub_ctx(tmp_path, SystemdFake())
    with pytest.raises(InstallError, match="--hub-name"):
        _name_setup(ctx, bad)
    assert ctx.runner.started == {} and not load(ctx.layout)["parts"]


def test_a_name_that_resolves_elsewhere_or_not_at_all_is_refused(tmp_path):
    for answers, words in (([VPS], VPS), ([VPN, VPS], VPS), ([], "does not resolve"), (["100.64.7.10"], "100.64.7.10")):
        ctx, *_ = stub_ctx(tmp_path, SystemdFake())
        with pytest.raises(InstallError, match=words):
            _name_setup(ctx, resolve=lambda n, a=answers: a)
        assert ctx.runner.started == {} and not load(ctx.layout)["parts"] and "hub_name" not in load(ctx.layout)


@pytest.mark.parametrize(
    "kw",
    [
        {"names": server.derive_names("example.com")},
        {"names": server.derive_names(None, hub="hub.example.com")},
        {"hub_vpn_only": True},
        {"behind_proxy": True},
        {"address": "127.0.0.1"},
    ],
)
def test_hub_name_conflicts_are_refused(tmp_path, kw):
    ctx, *_ = stub_ctx(tmp_path, SystemdFake())
    with pytest.raises(InstallError):
        _name_setup(ctx, **kw)
    assert ctx.runner.started == {} and not load(ctx.layout)["parts"]


def test_hub_name_is_refused_for_the_edge_role(tmp_path):
    ctx, *_ = stub_ctx(tmp_path, SystemdFake())
    with pytest.raises(InstallError, match="belongs to --role hub"):
        _name_setup(ctx, role="edge")
    assert ctx.runner.started == {}


def test_hub_name_reaches_the_install_flag(tmp_path):
    ctx, out, err, _ = stub_ctx(tmp_path, SystemdFake())
    rc = cli.main(["server", "--role", "hub", "--hub-name", "NOT A NAME"], env=ctx.env, out=out, err=err, ctx=ctx)
    assert rc == 1 and "--hub-name" in err.getvalue()
