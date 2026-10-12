"""H7: host mode on by default for a registered workspace (phase 1, tier A).

* the per-user service: the unit per platform (LaunchAgent, systemd user unit, none on Windows), the service-manager
  commands, ``orch host enable``;
* ``orch host register`` turns host mode on: it starts the service when no host answers and maps this UID to the
  person whose device key is on this machine;
* ``orch host status``: the host, the service, the workspaces (state, socket, person, click-to-sign) and the mode of
  the current directory, with the problems and their fixes;
* end to end from a fresh workspace: ``orch init`` → ``orch host register`` → the CLI (person and agent commands,
  ``wait``) and the dashboard's host link both work, and with the host gone the CLI answers ``host.unavailable``,
  never a direct write.

The service manager is always a fake (``cli.SERVICE_MANAGER``): no test runs launchctl or systemctl, and every unit
file lands under a temporary ``HOME``."""

from __future__ import annotations

import io
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from orch.host import cli as host_cli
from orch.host import enable as svc
from orch.host import units
from orch.host.pins import AgentPins
from tests.host.lifecycle_helpers import UID, HostThread, remove_tree, short_dir
from tests.instructions.conftest import FakeTerminal, Passphrases

REPO = Path(__file__).resolve().parents[4]
DASHBOARD = REPO / "addons" / "dashboard" / "server"
SESSION = "s_01J9ZP0000000000000000000S"


def done(code: int = 0, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, "")


class FakeLaunchd:
    """The person's service manager, faked: records every command, and "starts" the host (a :class:`HostThread` on
    the unit's state and run directories) when the unit is bootstrapped, kickstarted or (re)started."""

    def __init__(self, tmp: Path) -> None:
        self.ran: list[list[str]] = []
        self.loaded = False
        self.host: HostThread | None = None
        self.pins = AgentPins(tmp / "pins", owner=UID)
        (tmp / "pins").mkdir(exist_ok=True)
        self.plan: svc.Plan | None = None

    def manager(self, unit: svc.Plan) -> svc.Manager:
        self.plan = unit
        return svc.Manager(unit, run=self.run)

    def start_host(self) -> None:
        assert self.plan is not None
        if self.host is None:
            self.host = HostThread(self.plan.state, self.plan.run, pins=self.pins)
            self.host.__enter__()

    def stop_host(self) -> None:
        if self.host is not None:
            self.host.stop()
            self.host = None

    def run(self, argv: Any) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.ran.append(argv)
        verb = argv[1] if argv[0] == "launchctl" else argv[2]
        if verb == "print":
            return done(0, "state = running\n") if self.loaded else done(113)
        if verb in ("is-enabled", "is-active"):
            return done(0, "active\n" if self.host else "inactive\n") if self.loaded else done(1)
        if verb in ("bootstrap", "enable"):
            self.loaded = True
        if verb in ("bootout",):
            self.loaded = False
            self.stop_host()
        if verb in ("bootstrap", "kickstart", "start", "restart"):
            self.start_host()
        return done()


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = FakeLaunchd(tmp_path)
    monkeypatch.setattr(host_cli, "SERVICE_MANAGER", f.manager)
    yield f
    f.stop_host()


def host(argv: list[str], env: dict[str, str]) -> tuple[int, str]:
    from orch.cli.main import main

    out = io.StringIO()
    code = main(["host", *argv], env=env, stdout=out, stderr=io.StringIO())
    return code, out.getvalue()


# --- the unit per platform ------------------------------------------------------------------------------------------


def test_the_unit_per_platform_runs_the_release_on_the_tier_a_run_directory(tmp_path):
    state, run, release = tmp_path / "state", tmp_path / "state" / "run", tmp_path / "rel" / "current"
    env = {"HOME": str(tmp_path / "home")}
    mac = svc.plan(env, state, run, release, platform="darwin", python=Path("/py"), uid=501)
    assert mac.kind == "launchd" and mac.unit_path == tmp_path / "home/Library/LaunchAgents/io.orch.host.plist"
    doc = plistlib.loads(mac.content)
    assert doc["ProgramArguments"] == [
        "/py", "-I", str(release / "bin" / "orch-host"), "run", "--state", str(state), "--run", str(run)
    ]  # fmt: skip
    assert doc["RunAtLoad"] is True and doc["KeepAlive"] is True and doc["Umask"] == 0o077
    assert doc["StandardErrorPath"] == str(state / "host" / "host.log")
    # the user manager searches ~/.config/systemd/user: the CLI's XDG_CONFIG_HOME is not where it looks
    linux = svc.plan({**env, "XDG_CONFIG_HOME": str(tmp_path / "xdg")}, state, run, release, platform="linux")
    assert linux.kind == "systemd" and linux.unit_path == tmp_path / "home/.config/systemd/user/orch-host.service"
    text = linux.content.decode()
    assert f'"--run" "{run}"' in text and "WantedBy=default.target" in text and "User=" not in text
    assert svc.plan(env, state, run, release, platform="linux").unit_path == (
        tmp_path / "home/.config/systemd/user/orch-host.service"
    )
    for other in ("win32", "cygwin", "freebsd14"):
        with pytest.raises(svc.Unsupported):
            svc.plan(env, state, run, release, platform=other)
    with pytest.raises(svc.Unsupported):
        svc.plan({}, state, run, release, platform="darwin")  # no HOME: no per-user unit


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_the_service_manager_commands(tmp_path, platform):
    env = {"HOME": str(tmp_path / "home")}
    unit = svc.plan(env, tmp_path / "s", tmp_path / "s/run", tmp_path / "r", platform=platform, uid=501)
    ran: list[list[str]] = []
    loaded = [False]

    def run(argv):
        ran.append(list(argv))
        if argv[1] in ("print",) or argv[2:3] == ["is-enabled"]:
            return done(0 if loaded[0] else 1)
        return done()

    m = svc.Manager(unit, run=run)
    assert m.install() is True and m.installed() and m.install() is False
    assert unit.unit_path.stat().st_mode & 0o777 == 0o644
    m.start(changed=True)
    if platform == "darwin":
        assert ran[-1] == ["launchctl", "bootstrap", "gui/501", str(unit.unit_path)]
        loaded[0] = True
        ran.clear()
        m.start(changed=False)
        assert ran[-1] == ["launchctl", "kickstart", "gui/501/io.orch.host"]
        ran.clear()
        m.start(changed=True)  # launchd reads a plist at bootstrap only
        assert ran[1:] == [
            ["launchctl", "bootout", "gui/501/io.orch.host"],
            ["launchctl", "bootstrap", "gui/501", str(unit.unit_path)],
        ]
    else:
        assert ran == [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", units.SYSTEMD_NAME],
            ["systemctl", "--user", "restart", units.SYSTEMD_NAME],
        ]
        ran.clear()
        m.start(changed=False)
        assert ran[-1] == ["systemctl", "--user", "start", units.SYSTEMD_NAME]

    def failing(argv):
        return done(5) if argv[1] in ("bootstrap", "enable") or argv[2:3] == ["enable"] else done(1)

    with pytest.raises(OSError, match="failed"):
        svc.Manager(unit, run=failing).start(changed=True)


def test_enable_on_a_platform_without_a_user_service_stays_direct_and_installs_nothing(tmp_path, monkeypatch, fake):
    monkeypatch.setattr(sys, "platform", "win32")
    env = {"HOME": str(tmp_path / "home"), "ORCH_STATE_DIR": str(tmp_path / "state")}
    code, out = host(["enable"], env)
    doc = json.loads(out)
    assert code == 0 and doc["result"]["data"]["mode"] == "direct" and doc["result"]["data"]["service"] is None
    assert fake.ran == [] and not (tmp_path / "home").exists()


# --- end to end -----------------------------------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path, monkeypatch, fake):
    """A fresh directory, a state dir, a temporary HOME, a short run directory, the scripted terminal and the
    cheap-scrypt passphrase backend for both ``init`` and the signer."""
    from orch.custody import KdfParams, PassphraseBackend
    from orch.ops import human
    from orch.ops import workspace_init as wi

    base = Path(os.path.realpath(tmp_path))
    root = base / "work"
    root.mkdir()
    short = short_dir()
    term = FakeTerminal()
    monkeypatch.setattr(wi, "TERMINAL", term)
    p = Passphrases(term)

    def backend(directory):
        return PassphraseBackend(directory, _passphrase_provider=p, _kdf=KdfParams(2**15, 8, 1))

    monkeypatch.setattr(wi, "open_backend", backend)
    monkeypatch.setattr(human, "open_backend", backend)
    reviews: list[str] = []
    monkeypatch.setattr(human, "review_prompt", lambda text, word: reviews.append(text) or True)
    secrets: list[str] = []
    monkeypatch.setattr(human, "show_secret", secrets.append)
    monkeypatch.chdir(root)
    env = {
        "ORCH_STATE_DIR": str(base / "state"),
        "HOME": str(base / "home"),
        "USER": "severin",
        "ORCH_HOST_RUN_DIR": str(short / "r"),
    }
    yield {"root": root, "env": env, "run": short / "r", "secrets": secrets, "reviews": reviews, "fake": fake}
    fake.stop_host()
    remove_tree(short)


def cli(env: dict[str, str], *argv: str) -> tuple[int, dict[str, Any]]:
    from orch.cli.main import main

    out = io.StringIO()
    code = main([*argv, "--json"], env=env, stdout=out, stderr=io.StringIO())
    return code, json.loads(out.getvalue())


@pytest.mark.slow
def test_a_fresh_workspace_init_register_then_the_cli_and_the_dashboard_work(world):
    env, root, fake = world["env"], world["root"], world["fake"]
    code, doc = cli(env, "init", "--prefix", "DEMO", "--name", "Severin")
    assert code == 0, doc
    wid = doc["data"]["workspace_id"]

    # before: direct mode, nothing registered, no host
    code, out = host(["status", "--json"], env)
    rep = json.loads(out)
    assert rep["here"] == {"root": str(root), "mode": "direct"} and rep["workspaces"] == []
    assert rep["host"]["answers"] is False and rep["ok"] is True

    # register: no host answers, so the service is installed and started; the UID maps to the init's person
    code, out = host(["register", str(root), "--dev"], env)
    res = json.loads(out)
    assert code == 0 and res["ok"] is True, res
    assert res["result"]["data"]["workspace_id"] == wid
    assert res["service"]["started"] is True and res["service"]["mode"] == "host"
    assert fake.plan is not None and fake.plan.unit_path.is_file() and fake.plan.run == world["run"]
    assert str(fake.plan.unit_path).startswith(env["HOME"])  # never the real user's LaunchAgents or systemd
    assert (Path(env["HOME"]) / ".local/share/orch/releases/current/bin/orch-host").is_file()
    person = json.loads((Path(env["ORCH_STATE_DIR"]) / "host" / "workspaces.json").read_text())["workspaces"][0]
    owner = person["peers"][str(UID)]["person"]
    assert owner.startswith("p_") and person["root"] == str(root)

    # status: everything answers, host mode here, click-to-sign off
    code, out = host(["status", "--json"], env)
    rep = json.loads(out)
    assert code == 0 and rep["ok"] is True, rep["problems"]
    assert rep["here"]["mode"] == "host" and rep["here"]["workspace_id"] == wid
    [row] = rep["workspaces"]
    assert (row["state"], row["socket_answers"], row["person"], row["click_to_sign"]) == ("ok", True, owner, False)
    assert rep["service"]["unit_present"] and rep["service"]["unit_current"] and rep["service"]["loaded"]
    code, text = host(["doctor"], env)
    assert code == 0 and "here: " + str(root) + ": host mode" in text and "click-to-sign=off" in text

    # a person command through the host and the terminal signer: a grant for the agent
    code, doc = cli(env, "grant", "--label", "agent")
    assert code == 0, doc
    assert world["reviews"] and len(world["secrets"]) == 1
    token = world["secrets"][0].split("ORCH_GRANT=")[1].split()[0]
    agent = {**env, "ORCH_GRANT": token, "ORCH_SESSION": SESSION}

    # agent commands through the host, wait included
    code, doc = cli(agent, "new", "From host mode")
    assert code == 0 and doc["key"] == "DEMO-0001", doc
    assert cli(agent, "claim", "1")[0] == 0
    code, doc = cli(agent, "wait", "--timeout", "1")
    assert code == 0 and doc["ok"] is True and doc["data"]["kind"] == "timeout", doc
    code, doc = cli(agent, "show", "1")
    assert code == 0 and doc["key"] == "DEMO-0001"

    # the dashboard's host link reads the same registry and run directory
    rows = dashboard_query(env, wid)
    if rows is not None:
        assert [r["key"] for r in rows] == ["DEMO-0001"]

    # with the host gone the CLI never falls back to a direct write
    fake.stop_host()
    code, doc = cli(agent, "log", "-m", "while the host is down")
    assert (code, doc["error"]["code"]) == (9, "host.unavailable")
    code, out = host(["status", "--json"], env)
    rep = json.loads(out)
    assert rep["ok"] is False and any(p["fix"] == "orch host enable" for p in rep["problems"])

    # enable brings it back (the unit is current: a kickstart, no rewrite)
    fake.ran.clear()
    code, out = host(["enable", "--dev"], env)
    assert code == 0 and json.loads(out)["result"]["data"]["changed"] is False, out
    assert ["launchctl", "kickstart", f"gui/{UID}/io.orch.host"] in fake.ran or [
        "systemctl", "--user", "start", units.SYSTEMD_NAME
    ] in fake.ran  # fmt: skip
    assert cli(agent, "show", "1")[0] == 0


def dashboard_query(env: dict[str, str], wid: str) -> list[dict[str, Any]] | None:
    """``ticket.query`` through the dashboard server's own gateway and host link, in its own environment (it needs
    FastAPI, which orch-core does not). ``None`` when that environment is not installed (orch-core's CI job)."""
    python = DASHBOARD / ".venv" / "bin" / "python"
    if not python.exists():
        assert os.environ.get("ORCH_TEST_DASHBOARD") != "1", f"no dashboard environment at {python} (uv sync there)"
        return None
    script = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from orch_dashboard.gateway import Gateway\n"
        "link = Gateway(Path(sys.argv[1])).link(sys.argv[2])\n"
        "print(json.dumps(link.call('ticket.query', {})))\n"
    )
    child_env = {**os.environ, "ORCH_STATE_DIR": env["ORCH_STATE_DIR"], "ORCH_RUN_DIR": env["ORCH_HOST_RUN_DIR"]}
    res = subprocess.run(
        [str(python), "-c", script, env["ORCH_STATE_DIR"], wid],
        capture_output=True, text=True, timeout=60, env=child_env, cwd=DASHBOARD,
    )  # fmt: skip
    assert res.returncode == 0, res.stderr[-2000:]
    doc = json.loads(res.stdout.strip().splitlines()[-1])
    data = doc.get("data", doc)
    return data["rows"]


def test_register_without_a_device_key_here_asks_for_the_mapping(world, tmp_path):
    from tests.ops.helpers import Ws

    other = Ws(Path(os.path.realpath(tmp_path)) / "other")
    other.bootstrap()
    other.store.close()
    env = {**world["env"], "ORCH_STATE_DIR": str(other.host_state)}
    code, out = host(["register", str(other.root), "--no-service"], env)
    doc = json.loads(out)
    assert code != 0 and doc["error"]["code"] == "invalid.input" and "--peer" in doc["error"]["message"]
    assert world["fake"].ran == []  # --no-service: the service manager was not asked


def test_status_names_the_fix_for_a_registered_workspace_without_a_host(world):
    env, root = world["env"], world["root"]
    assert cli(env, "init", "--prefix", "DEMO", "--name", "Severin")[0] == 0
    assert host(["register", str(root), "--dev"], env)[0] == 0
    world["fake"].stop_host()
    code, text = host(["status"], env)
    assert code == 1
    assert "host: does not answer" in text
    assert "problem: workspaces are registered but no host answers" in text and "fix: orch host enable" in text
    assert text.rstrip().endswith("1 problem(s)")


def test_a_running_host_is_used_as_it_is_and_no_service_is_touched(world):
    env, root, fake = world["env"], world["root"], world["fake"]
    assert cli(env, "init", "--prefix", "DEMO", "--name", "Severin")[0] == 0
    state = Path(env["ORCH_STATE_DIR"])
    with HostThread(state, world["run"], pins=fake.pins):  # a dev host (orch host run --foreground)
        code, out = host(["register", str(root), "--dev"], env)
        assert code == 0 and "service" not in json.loads(out)
    assert fake.ran == []


# --- the review round (#422) -----------------------------------------------------------------------------------------


def test_systemd_exec_start_uses_systemd_quoting_and_refuses_a_newline(tmp_path):
    """Opus 3: a newline in a path would end the ExecStart line and add a directive of its own; ``%`` and ``$``
    expand in systemd even inside quotes."""
    with pytest.raises(ValueError, match="control character"):
        units.systemd_unit(
            Path("/py"), Path("/rel"), Path("/s/x\nExecStartPre=/bin/touch /tmp/pwn"), run_dir=Path("/r")
        )
    for bad in ("\r", "\x00", "\x1b", "\x7f"):
        with pytest.raises(ValueError):
            units.systemd_unit(Path("/py"), Path("/rel"), Path(f"/s{bad}"), run_dir=Path("/r"))
    text = units.systemd_unit(Path("/p y"), Path('/home/a%h/"q"'), Path("/s/$HOME\\x"), run_dir=Path("/r"))
    [line] = [x for x in text.splitlines() if x.startswith("ExecStart=")]
    assert line == (
        'ExecStart="/p y" "-I" "/home/a%%h/\\"q\\"/bin/orch-host" "run" "--state" "/s/$$HOME\\\\x" "--run" "/r"'
    )
    assert sum(x.startswith("Exec") for x in text.splitlines()) == 1
    assert svc.unit_fields("systemd", text.encode()) == {"python": "/p y", "state": "/s/$HOME\\x", "run": "/r"}


def test_enable_refuses_a_control_character_cleanly(tmp_path, monkeypatch, fake):
    monkeypatch.setattr(sys, "platform", "darwin")
    env = {"HOME": str(tmp_path / "home"), "ORCH_HOST_RUN_DIR": str(tmp_path / "r\nx")}
    code, out = host(["enable", "--dev", "--state", str(tmp_path / "s")], env)
    doc = json.loads(out)
    assert (code, doc["error"]["code"]) == (5, "invalid.input") and "control character" in doc["error"]["message"]
    assert fake.ran == [] and not (tmp_path / "home").exists()


def test_enable_refuses_a_run_directory_too_long_for_a_socket(tmp_path, fake):
    env = {"HOME": str(tmp_path / "home"), "ORCH_HOST_RUN_DIR": "/" + "r" * 80}
    code, out = host(["enable", "--dev", "--state", str(tmp_path / "s")], env)
    doc = json.loads(out)
    assert (code, doc["error"]["code"]) == (5, "invalid.input") and "too long" in doc["error"]["message"]
    assert fake.ran == [] and not (tmp_path / "home").exists()


def test_enable_refuses_development_code_as_the_service_unless_dev(tmp_path, fake):
    """Opus 2: these tests run from a git checkout, which is exactly what must not become the long-running host."""
    env = {"HOME": str(tmp_path / "home"), "ORCH_HOST_RUN_DIR": "/r"}
    code, out = host(["enable", "--state", str(tmp_path / "s")], env)
    doc = json.loads(out)
    assert (code, doc["error"]["code"]) == (5, "invalid.input") and "--dev" in doc["error"]["message"]
    assert "checkout" in doc["error"]["message"] and fake.ran == [] and not (tmp_path / "home").exists()
    venv = tmp_path / "proj" / ".venv" / "bin"
    venv.mkdir(parents=True)
    (tmp_path / "proj" / ".git").mkdir()
    assert "checkout" in svc.dev_source(venv / "python", tmp_path / "pkg")
    assert "workspace" in svc.dev_source(tmp_path / "ws" / "py", tmp_path / "pkg", [str(tmp_path / "ws")])
    assert svc.dev_source(tmp_path / "usr" / "bin" / "python", tmp_path / "lib" / "orch") is None


def test_a_service_for_another_setup_is_never_replaced_silently(world):
    """Opus 1: one label per UID. A unit installed for state A is not re-targeted by enable or register from state B;
    ``enable --replace`` does it on purpose, and status names the fields that differ."""
    env, root, fake = world["env"], world["root"], world["fake"]
    assert cli(env, "init", "--prefix", "DEMO", "--name", "Severin")[0] == 0
    assert host(["register", str(root), "--dev"], env)[0] == 0  # the service now serves state A
    unit = fake.plan
    assert unit is not None
    before = unit.unit_path.read_bytes()
    fake.stop_host()
    other = {**env, "ORCH_STATE_DIR": env["ORCH_STATE_DIR"] + "-b"}
    fake.ran.clear()
    code, out = host(["enable", "--dev"], other)
    doc = json.loads(out)
    assert (code, doc["error"]["code"]) == (4, "host.busy") and doc["differs"] == ["state"], doc
    assert "--replace" in doc["error"]["message"]
    assert unit.unit_path.read_bytes() == before and not any(a[1] in ("bootout", "bootstrap") for a in fake.ran)
    code, out = host(["register", str(root), "--dev", "--peer", f"{UID}=person:p_" + "0" * 32], other)
    assert json.loads(out)["error"]["code"] == "host.busy" and unit.unit_path.read_bytes() == before
    code, out = host(["status", "--json"], other)
    rep = json.loads(out)
    assert rep["service"]["differs"] == ["state"] and any("another setup" in p["what"] for p in rep["problems"])
    assert not any(p["fix"].startswith("orch host enable") for p in rep["problems"])
    code, out = host(["enable", "--dev", "--replace"], other)
    assert code == 0, out
    assert svc.unit_fields(unit.kind, unit.unit_path.read_bytes())["state"] == other["ORCH_STATE_DIR"]


def test_the_peer_default_ignores_a_dead_device_key(world, monkeypatch):
    """Opus 6: the key on this machine is the evidence for the default; a removed, revoked or expired one is none."""
    env, root = world["env"], world["root"]
    assert cli(env, "init", "--prefix", "DEMO", "--name", "Severin")[0] == 0
    state = Path(env["ORCH_STATE_DIR"])
    assert list(host_cli.default_peers(state, root, env)) == [str(UID)]
    monkeypatch.setattr(host_cli, "_dead_device", lambda device, person, ws: "revoked")
    with pytest.raises(host_cli.HostError) as e:
        host_cli.default_peers(state, root, env)
    assert "revoked" in e.value.message and "--peer" in e.value.message


def test_dead_device_reasons():
    class D:
        removed = revoked = False
        expires_ms = None
        scopes = ("look", "decide")

    class Ws:
        members = {"p_a": {}}

    def dead(**kw):
        d = D()
        for k, v in kw.items():
            setattr(d, k, v)
        return host_cli._dead_device(d, kw.pop("person", "p_a"), Ws())

    assert dead() is None
    assert dead(removed=True) == "removed" and dead(revoked=True) == "revoked"
    assert dead(expires_ms=1) == "expired" and dead(scopes=("look",)) == "without the decide scope"
    assert dead(person="p_gone") == "of a person who is no longer a member"


def test_status_prints_no_control_character(world, monkeypatch):
    from orch.host import status

    rep = {
        "state_dir": "/s\x1b[2J",
        "service": None,
        "host": {"answers": False, "control": "/c"},
        "workspaces": [
            {"prefix": "D\x1b]0;x\x07", "workspace_id": "w", "state": "ok", "socket_answers": True, "person": None,
             "click_to_sign": False, "root": "/a\nproblem: forged"}
        ],
        "here": None,
        "problems": [],
        "ok": True,
    }  # fmt: skip
    text = status.render(rep)
    assert "\x1b" not in text and "\x07" not in text and "\nproblem: forged" not in text
