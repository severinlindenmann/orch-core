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


def test_root_block_is_text_with_every_root_step():
    b = server.root_block("orch-edge")
    for needle in (
        "groupadd --force orch-caddy",
        "usermod -aG orch-caddy orch-edge",
        "install -d -m 2775 -g orch-caddy /etc/caddy/orch.d",
        "import /etc/caddy/orch.d/*.caddy",
        "systemctl reload caddy",
        "loginctl enable-linger orch-edge",
    ):
        assert needle in b


# ------------------------------------------------------------------------------------------------------ dns


def test_dns_check_ok_missing_and_elsewhere():
    table = {"a.example.com": [VPS], "b.example.com": [], "c.example.com": ["1.2.3.4"]}
    res = server.check_dns(
        {"relay": "a.example.com", "publish": "b.example.com", "hub": "c.example.com"},
        lambda n: table[n],
        {VPS, "127.0.0.1"},
    )
    assert [r.status for r in res] == ["ok", "missing", "elsewhere"]
    assert "no DNS record" in res[1].words() and "run this command again" in res[1].words()
    assert "1.2.3.4" in res[2].words() and "not at this computer" in res[2].words()


def test_local_addresses_and_vpn_detection():
    mine = server.local_addresses(lambda argv: IP_ADDR if argv[0] == "ip" else "")
    assert {VPS, VPN, "127.0.0.1"} <= mine
    assert server.detect_vpn_ip(mine) == VPN
    assert server.detect_vpn_ip({"10.0.0.2", VPS}) is None
    assert server.detect_vpn_ip({"100.63.255.255", "100.128.0.1"}) is None  # just outside 100.64/10


def test_vpn_ip_flag_must_be_a_vpn_address_of_this_computer():
    mine = {VPS, VPN}
    assert server.check_vpn_ip(VPN, mine) == VPN
    with pytest.raises(InstallError, match="private network"):
        server.check_vpn_ip(VPS, mine)
    with pytest.raises(InstallError, match="does not have that address"):
        server.check_vpn_ip("100.64.9.9", mine)


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
    return {VPS, VPN, "127.0.0.1"}


def test_edge_role_installs_relay_and_publish_only_and_writes_two_snippets(tmp_path):
    home, caddy = tmp_path / "h", tmp_path / "orch.d"
    home.mkdir(), caddy.mkdir()
    ctx, out, err, calls = stub_ctx(home, SystemdFake())
    plan = server_setup(
        ctx,
        "edge",
        names=server.derive_names("example.com"),
        caddy_dir=str(caddy),
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
    assert "sudo systemctl reload caddy" in text and "sudo groupadd" in text and "enable-linger orch-edge" in text
    assert "not written" in text  # the directory does not exist yet: told to do the root steps and rerun
    everything = [c for c in calls] + spawned
    assert not any("sudo" in str(c) for c in everything)
    assert not any(c[:1] == ["systemctl"] and "caddy" in c for c in calls)
    assert not any("caddy" in str(c[0]) for c in calls)


def test_dns_not_ready_skips_tls_for_that_part_only(tmp_path):
    caddy = tmp_path / "orch.d"
    caddy.mkdir()
    ctx, out, err, calls = stub_ctx(tmp_path, SystemdFake())
    table = {"relay.example.com": [], "publish.example.com": [VPS]}
    plan = server_setup(
        ctx,
        "edge",
        names=server.derive_names("example.com"),
        caddy_dir=str(caddy),
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
    caddy = tmp_path / "orch.d"
    caddy.mkdir()
    ctx, out, err, calls = stub_ctx(tmp_path, SystemdFake())
    server_setup(
        ctx,
        "hub",
        names=server.derive_names("example.com"),
        hub_vpn_only=True,
        caddy_dir=str(caddy),
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
    assert "not remote_ip 100.64.0.0/10" in (caddy / "hub.caddy").read_text()
    assert "https://hub.example.com/#setup=TOKEN" in out.getvalue()
    assert f"resolve hub.example.com to {VPN}" in out.getvalue()
    import re

    for a in card["addresses"]:  # the shape orch_hub.settings validates (web needs a port, link is host:port)
        assert re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]{1,100}:[0-9]{1,5}", a["web"])
        assert a["link"] is None or re.fullmatch(r"[A-Za-z0-9.\-\[\]:]{1,100}:[0-9]{1,5}", a["link"])


def test_hub_without_vpn_only_is_a_public_console(tmp_path):
    caddy = tmp_path / "orch.d"
    caddy.mkdir()
    ctx, *_ = stub_ctx(tmp_path, SystemdFake())
    server_setup(
        ctx,
        "hub",
        names=server.derive_names(None, hub="hub.example.com"),
        caddy_dir=str(caddy),
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
        server_setup(ctx, "hub", names=server.derive_names("example.com"), local=lambda: {VPS}, resolve=lambda n: [VPS])
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
    caddy = tmp_path / "orch.d"
    caddy.mkdir()
    ctx, out, err, calls = stub_ctx(tmp_path, SystemdFake())
    server_setup(
        ctx,
        "hub",
        names=server.derive_names("example.com"),
        caddy_dir=str(caddy),
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
    caddy = tmp_path / "orch.d"
    caddy.mkdir()
    ctx, out, err, calls = stub_ctx(tmp_path, SystemdFake())
    server_setup(
        ctx,
        "edge",
        names=server.derive_names("example.com"),
        caddy_dir=str(caddy),
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
