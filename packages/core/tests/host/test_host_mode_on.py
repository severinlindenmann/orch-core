"""H7: host mode on by default for a registered workspace (phase 1, tier A).

* the per-user service: the unit per platform (LaunchAgent, systemd user unit, none on Windows), the service-manager
  commands, ``orch host enable``;
* ``orch host register`` turns host mode on: it starts the service when no host answers and maps this UID to the
  person whose device key is on this machine;
* ``orch host status``: the host, the service, the workspaces (state, socket, person, click-to-sign) and the mode of
  the current directory, with the problems and their fixes;
* end to end from a fresh workspace: its genesis → ``orch host register`` → the CLI (person and agent commands,
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
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from orch.host import cli as host_cli
from orch.host import enable as svc
from orch.host import units
from orch.host.pins import AgentPins
from tests.host.lifecycle_helpers import UID, HostThread, remove_tree, short_dir
from tests.instructions.conftest import FakeTerminal, Passphrases

REPO = Path(__file__).resolve().parents[4]
DASHBOARD = REPO / "packages" / "addons" / "dashboard" / "server"
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
        #: systemd only: enabled and active are separate answers; ``None`` follows ``loaded`` / the host
        self.enabled: bool | None = None
        self.active: bool | None = None

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
        if verb == "is-enabled":
            on = self.loaded if self.enabled is None else self.enabled
            return done(0, "enabled\n") if on else done(1, "disabled\n")
        if verb == "is-active":
            on = bool(self.host) if self.active is None else self.active
            return done(0, "active\n") if on else done(3, "inactive\n")
        if verb in ("bootstrap", "enable"):
            self.loaded = True
        if verb in ("bootout", "disable"):
            self.loaded = False
            if verb == "bootout" or "--now" in argv:
                self.enabled = self.active = None
                self.stop_host()
            else:
                self.enabled = False
        if verb == "stop":
            self.active = False
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
    # N18: enable, disable and restart print plain text; the tests read the JSON document
    machine = ["--json"] if argv and argv[0] in ("enable", "disable", "restart") else []
    code = main(["host", *argv, *machine], env=env, stdout=out, stderr=io.StringIO())
    return code, out.getvalue()


# --- the unit per platform ------------------------------------------------------------------------------------------


def test_the_unit_per_platform_runs_the_release_on_the_tier_a_run_directory(tmp_path):
    state, run, release = tmp_path / "state", tmp_path / "state" / "run", tmp_path / "rel" / "current"
    env = {"HOME": str(tmp_path / "home")}
    mac = svc.plan(env, state, run, release, platform="darwin", python=Path("/py"), uid=501)
    assert mac.kind == "launchd" and mac.unit_path == tmp_path / "home/Library/LaunchAgents/io.orch.host.plist"
    doc = plistlib.loads(mac.content)
    assert doc["ProgramArguments"] == [
        str(release / "bin" / "orch-host"),
        "run",
        "--state",
        str(state),
        "--run",
        str(run),
    ]
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
        if argv[1] == "bootout":
            loaded[0] = False  # unloaded at once (the wait for a slow unload is in test_bootout_wait)
        return done()

    m = svc.Manager(unit, run=run, sleep=lambda _s: None)
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
        assert [a for a in ran[1:] if a[1] != "print"] == [
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
        svc.Manager(unit, run=failing, sleep=lambda _s: None).start(changed=True)


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
    cheap-scrypt passphrase backend for both the genesis and the signer."""
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
    monkeypatch.setattr(human, "review_prompt", lambda text: reviews.append(text) or True)
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


def init_ws(world: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """A workspace on disk with its keys, NOT registered with any host: it is made through a throwaway in-process host
    (V3-D38: ``orch setup`` is the only product path, and it registers), which is then stopped and whose registry
    is dropped, so the tests of ``host register``/``enable`` start from "a workspace this host does not serve yet"."""
    from orch.ops.base import Context
    from orch.ops.caps import Files
    from orch.ops.runtime import Workspace
    from tests.host.hosted_create import create_hosted

    env, root, fake = world["env"], world["root"], world["fake"]
    ctx = Context(
        env=env,
        grant=None,
        human_presence=True,
        now=time.time,
        workspace=Workspace(env, time.time, cwd=str(root), files=Files.direct()),
    )
    state = Path(env["ORCH_STATE_DIR"])
    with (
        mock.patch.dict(os.environ, {"ORCH_HOST_RUN_DIR": env["ORCH_HOST_RUN_DIR"]}),  # where the library looks (#1193)
        HostThread(state, world["run"], pins=fake.pins),
    ):
        res = create_hosted(ctx, {"prefix": "DEMO", "name": "Severin"})
    (state / "host" / "workspaces.json").unlink()
    return 0, {"ok": True, "data": {"workspace_id": res.data["workspace_id"], "prefix": "DEMO"}}


def cli(env: dict[str, str], *argv: str) -> tuple[int, dict[str, Any]]:
    from orch.cli.main import main

    out = io.StringIO()
    code = main([*argv, "--json"], env=env, stdout=out, stderr=io.StringIO())
    return code, json.loads(out.getvalue())


@pytest.mark.slow
def test_a_fresh_workspace_register_then_the_cli_and_the_dashboard_work(world):
    env, root, fake = world["env"], world["root"], world["fake"]
    code, doc = init_ws(world)
    assert code == 0, doc
    wid = doc["data"]["workspace_id"]

    # before: direct mode, nothing registered, no host
    code, out = host(["status", "--json"], env)
    rep = json.loads(out)
    assert rep["here"] == {"root": str(root), "mode": "direct"} and rep["workspaces"] == []
    assert rep["host"]["answers"] is False and rep["ok"] is True

    # register: no host answers, so the service is installed and started; the UID maps to the owner person
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
    run = env["ORCH_HOST_RUN_DIR"]  # the canonical name; ORCH_RUN_DIR too while the dashboard still reads that one
    child_env = {**os.environ, "ORCH_STATE_DIR": env["ORCH_STATE_DIR"], "ORCH_HOST_RUN_DIR": run, "ORCH_RUN_DIR": run}
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
    assert init_ws(world)[0] == 0
    assert host(["register", str(root), "--dev"], env)[0] == 0
    world["fake"].stop_host()
    code, text = host(["status"], env)
    assert code == 1
    assert "host: does not answer" in text
    assert "problem: workspaces are registered but no host answers" in text and "fix: orch host enable" in text
    assert text.rstrip().endswith("1 problem(s)")


def test_a_running_host_is_used_as_it_is_and_no_service_is_touched(world):
    env, root, fake = world["env"], world["root"], world["fake"]
    assert init_ws(world)[0] == 0
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
    (tmp_path / "proj" / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert "checkout" in svc.dev_source(venv / "python", tmp_path / "pkg")
    assert "checkout" in svc.dev_source(tmp_path / "usr" / "python", tmp_path / "proj" / "src" / "orch")
    assert "workspace" in svc.dev_source(tmp_path / "ws" / "py", tmp_path / "pkg", [str(tmp_path / "ws")])
    assert svc.dev_source(tmp_path / "usr" / "bin" / "python", tmp_path / "lib" / "orch") is None


def test_a_homebrew_or_pipx_install_is_not_a_checkout(tmp_path, monkeypatch):
    """T3 L-3 (#446): Homebrew's prefix is a git work tree and a home directory may be one too; neither makes an
    installed interpreter or an installed orch development code."""
    brew = tmp_path / "opt" / "homebrew"
    (brew / ".git").mkdir(parents=True)
    python = brew / "Cellar" / "python@3.13" / "3.13.1" / "bin" / "python3.13"
    python.parent.mkdir(parents=True)
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    (home / "pyproject.toml").write_text("[project]\nname = 'dotfiles'\n")
    pipx = home / ".local" / "pipx" / "venvs" / "orch"
    package = pipx / "lib" / "python3.13" / "site-packages" / "orch"
    package.mkdir(parents=True)
    assert svc.dev_source(python, brew / "lib" / "python3.13" / "site-packages" / "orch") is None
    tool = pipx / "bin" / "python"  # a tool venv's interpreter, whose environment lies under a git home
    tool.parent.mkdir(parents=True)
    tool.symlink_to(python)
    (pipx / "pyvenv.cfg").write_text("home = x\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert svc.dev_source(tool, package) is None
    project = home / "code" / "orch"  # but a checkout under the home directory is still one
    (project / ".git").mkdir(parents=True)
    (project / "setup.py").write_text("")
    (project / ".venv" / "bin").mkdir(parents=True)
    assert "checkout" in svc.dev_source(project / ".venv" / "bin" / "python", package)
    linked = tmp_path / "linkhome"  # a symlinked home directory still ends the walk (Codex, #448)
    linked.symlink_to(home)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: linked))
    assert svc.dev_source(linked / ".local" / "pipx" / "venvs" / "orch" / "bin" / "python", package) is None
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    repo = home / "code" / "other"  # a venv at a checkout's root, orch installed into it without -e (Opus L-1)
    (repo / ".git").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "pyvenv.cfg").write_text("home = x\n")
    assert "checkout" in svc.dev_source(repo / ".venv" / "bin" / "python", package)


def test_a_relative_run_directory_becomes_absolute(tmp_path, monkeypatch):
    """T3 L-4 (#446): a unit file never carries a path relative to the directory enable ran in."""
    monkeypatch.chdir(tmp_path)
    assert svc.run_dir_for(tmp_path / "s", Path("rel")) == tmp_path / "rel"
    assert svc.run_dir_for(tmp_path / "s", None, {"ORCH_HOST_RUN_DIR": "r2"}) == tmp_path / "r2"
    assert svc.run_dir_for(Path("s"), None, {}) == tmp_path / "s" / "run"


def test_a_service_for_another_setup_is_never_replaced_silently(world):
    """Opus 1: one label per UID. A unit installed for state A is not re-targeted by enable or register from state B;
    ``enable --replace`` does it on purpose, and status names the fields that differ."""
    env, root, fake = world["env"], world["root"], world["fake"]
    assert init_ws(world)[0] == 0
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
    assert init_ws(world)[0] == 0
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


def test_a_label_loaded_from_another_file_is_never_replaced_silently(tmp_path, fake):
    """Opus 1 (last item): no unit file here, but the service manager has the label loaded from elsewhere."""
    fake.loaded = True
    env = {"HOME": str(tmp_path / "home"), "ORCH_HOST_RUN_DIR": "/r"}
    code, out = host(["enable", "--dev", "--state", str(tmp_path / "s")], env)
    doc = json.loads(out)
    assert (code, doc["error"]["code"]) == (4, "host.busy") and doc["differs"] == ["unit"], doc
    assert "loaded from another file" in doc["error"]["message"] and "--replace" in doc["error"]["message"]
    assert not any(a[1] in ("bootout", "bootstrap", "kickstart") for a in fake.ran if a[0] == "launchctl")
    assert not (tmp_path / "home" / "Library").exists() and not (tmp_path / "home" / ".config").exists()


def test_a_subprocess_orch_host_enable_in_a_test_env_never_reaches_the_service_manager(tmp_path):
    """A child ``orch host enable`` gets ``ORCH_NO_SERVICE_MANAGER=1`` from ``child_env`` (and from the session env), so
    its own default runner refuses launchctl and systemctl before running either."""
    from tests.conftest import child_env

    env = child_env(tmp_path, ORCH_STATE_DIR=str(tmp_path / "s"), ORCH_HOST_RUN_DIR="/r")
    assert env["ORCH_NO_SERVICE_MANAGER"] == "1"
    res = subprocess.run(
        [sys.executable, "-m", "orch", "host", "enable", "--dev", "--json"],
        capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path,
    )  # fmt: skip
    doc = json.loads(res.stdout.strip().splitlines()[-1])
    if sys.platform not in ("darwin",) and not sys.platform.startswith("linux"):
        assert doc["result"]["data"]["mode"] == "direct"
        return
    assert (res.returncode, doc["error"]["code"]) == (9, "host.unavailable"), res
    assert "the service manager is off" in doc["error"]["message"]


# --- enable restarts a host that runs an older release --------------------------------------------------------------


@pytest.fixture
def stale_env(tmp_path, monkeypatch, fake):
    """``orch host enable --dev`` against the fake service manager; the host's reported release and the restart are
    seams, so nothing reaches launchctl, systemctl or a real process."""
    short = short_dir()
    restarts: list[str] = []
    monkeypatch.setattr(host_cli, "_restart", lambda manager, control, mine: restarts.append(mine) or "drained")
    env = {
        "HOME": str(tmp_path / "home"),
        "ORCH_STATE_DIR": str(tmp_path / "state"),
        "ORCH_HOST_RUN_DIR": str(short / "r"),
    }
    yield env, restarts
    fake.stop_host()
    remove_tree(short)


def test_enable_restarts_a_supervised_host_that_runs_an_older_release(stale_env, monkeypatch):
    from orch.host.release import code_digest

    env, restarts = stale_env
    monkeypatch.setattr(host_cli, "_host_release", lambda control: "old000000000")
    code, out = host(["enable", "--dev"], env)
    data = json.loads(out)["result"]["data"]
    assert code == 0 and restarts == [code_digest()]
    assert data["restarted"] == code_digest() and data["message"] == (
        f"restarted the host on the new release {code_digest()}"
    )


def test_enable_does_not_restart_a_host_on_the_current_release(stale_env, monkeypatch):
    from orch.host.release import code_digest

    env, restarts = stale_env
    monkeypatch.setattr(host_cli, "_host_release", lambda control: code_digest())
    code, out = host(["enable", "--dev"], env)
    data = json.loads(out)["result"]["data"]
    assert code == 0 and restarts == [] and "restarted" not in data and "message" not in data


def test_enable_only_tells_about_a_foreground_host_and_never_restarts_it(stale_env, monkeypatch):
    env, restarts = stale_env
    monkeypatch.setattr(host_cli, "_host_release", lambda control: None)  # an older host: no release in its welcome
    monkeypatch.setattr(svc.Manager, "active", lambda self: False)  # not started by the service manager
    code, out = host(["enable", "--dev"], env)
    data = json.loads(out)["result"]["data"]
    assert code == 0 and restarts == [] and data["restart_needed"] is True
    assert "not under the service manager" in data["message"]


# --- the restart falls back to the service manager when the drain never finishes ------------------------------------


class _FakeManager:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    def active(self) -> bool:
        return True

    def start(self, *, changed: bool) -> list:
        self.calls.append("start")
        return []

    def restart_now(self) -> list:
        self.calls.append("restart_now")
        if self.fail:
            raise OSError("kickstart failed")
        return []


class _DrainClient:
    reply: dict = {"ok": True}

    def __init__(self, *a, **k) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def call(self, op):
        assert op == "host.restart"
        return self.reply


def _restart_world(monkeypatch, *, running_after_kick: bool):
    """The host reports the old release until the service manager kicks it (when ``running_after_kick``)."""
    import orch.host.control_client as cc

    manager = _FakeManager()
    monkeypatch.setattr(cc, "ControlClient", _DrainClient)
    monkeypatch.setattr("orch.host.enable.START_WAIT", 0.3)
    monkeypatch.setattr(
        host_cli,
        "_host_release",
        lambda control: "new" if running_after_kick and "restart_now" in manager.calls else "old",
    )
    return manager


def test_a_drain_that_never_finishes_is_followed_by_a_forced_service_restart(monkeypatch):
    manager = _restart_world(monkeypatch, running_after_kick=True)
    assert host_cli._restart(manager, Path("/nope"), "new") == "forced"
    assert manager.calls == ["restart_now"]


def test_a_graceful_drain_needs_no_forced_restart(monkeypatch):
    manager = _FakeManager()
    import orch.host.control_client as cc

    seen = iter(["old", "new", "new"])
    monkeypatch.setattr(cc, "ControlClient", _DrainClient)
    monkeypatch.setattr(host_cli, "_host_release", lambda control: next(seen))
    assert host_cli._restart(manager, Path("/nope"), "new") == "drained"
    assert manager.calls == []


def test_someone_elses_drain_is_waited_for_not_killed(monkeypatch):
    import orch.host.control_client as cc

    manager = _FakeManager()
    seen = iter(["old", "old", "new"])
    monkeypatch.setattr(cc, "ControlClient", _DrainClient)
    monkeypatch.setattr(_DrainClient, "reply", {"ok": False, "error": {"code": "host.restarting"}})
    monkeypatch.setattr(host_cli, "_host_release", lambda control: next(seen))
    assert host_cli._restart(manager, Path("/nope"), "new") == "drained"
    assert manager.calls == []


def test_a_refused_drain_request_goes_straight_to_the_forced_restart(monkeypatch):
    import orch.host.control_client as cc

    manager = _FakeManager()
    monkeypatch.setattr(cc, "ControlClient", _DrainClient)
    monkeypatch.setattr(_DrainClient, "reply", {"ok": False, "error": {"code": "internal"}})
    monkeypatch.setattr(host_cli, "_host_release", lambda control: "new" if "restart_now" in manager.calls else "old")
    assert host_cli._restart(manager, Path("/nope"), "new") == "forced"


def test_enable_reports_a_forced_restart(stale_env, monkeypatch):
    env, restarts = stale_env
    monkeypatch.setattr(host_cli, "_host_release", lambda control: "old000000000")
    monkeypatch.setattr(host_cli, "_restart", lambda manager, control, mine: "forced")
    code, out = host(["enable", "--dev"], env)
    data = json.loads(out)["result"]["data"]
    assert code == 0 and data["forced"] is True and "may have been interrupted" in data["message"]


def test_a_restart_that_cannot_happen_is_reported_not_hidden(monkeypatch):
    manager = _restart_world(monkeypatch, running_after_kick=False)
    assert host_cli._restart(manager, Path("/nope"), "new") is None
    assert manager.calls[0] == "restart_now"


def test_enable_says_so_when_the_host_could_not_be_restarted(stale_env, monkeypatch):
    env, restarts = stale_env
    monkeypatch.setattr(host_cli, "_host_release", lambda control: "old000000000")
    monkeypatch.setattr(host_cli, "_restart", lambda manager, control, mine: None)
    code, out = host(["enable", "--dev"], env)
    data = json.loads(out)["result"]["data"]
    assert code == 0 and data["restart_needed"] is True and "did not restart" in data["message"]
    assert "orch host restart" not in data["message"] and "interrupts requests in flight" in data["message"]
    assert ("launchctl kickstart -k gui/" in data["message"]) or ("systemctl --user restart" in data["message"])


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_the_manager_restart_now_commands(tmp_path, platform):
    unit = svc.plan(
        {"HOME": str(tmp_path / "h")}, tmp_path / "s", tmp_path / "s/run", tmp_path / "r", platform=platform, uid=501
    )
    ran: list[list[str]] = []
    m = svc.Manager(unit, run=lambda argv: ran.append(list(argv)) or done())
    m.restart_now()
    expect = (
        ["launchctl", "kickstart", "-k", "gui/501/io.orch.host"]
        if platform == "darwin"
        else ["systemctl", "--user", "restart", unit.systemd_name]
    )
    assert ran == [expect]
    failing = svc.Manager(unit, run=lambda argv: done(1))
    with pytest.raises(OSError):
        failing.restart_now()


def test_enable_also_restarts_a_stale_runner_service(stale_env, monkeypatch):
    """The runner service is checked by the same rule (fake runner manager, fake release file)."""
    env, restarts = stale_env
    monkeypatch.setattr(host_cli, "_host_release", lambda control: "old000000000")
    seen = []

    def fake_runner(unit, manager, state, mine, force=False):
        seen.append(mine)
        return {
            "restarted": True,
            "message": "restarted the agent runner; agents and terminals it had started were stopped",
        }

    monkeypatch.setattr("orch.runner.service.restart_if_stale", fake_runner)
    code, out = host(["enable", "--dev"], env)
    data = json.loads(out)["result"]["data"]
    assert code == 0 and len(seen) == 1 and data["runner_restarted"] is True
    assert "agents and terminals" in data["runner_message"]


def test_register_never_touches_the_runner_and_the_flag_forces_it(stale_env, monkeypatch):
    env, restarts = stale_env
    monkeypatch.setattr(host_cli, "_host_release", lambda control: "old000000000")
    seen = []
    monkeypatch.setattr(
        "orch.runner.service.restart_if_stale",
        lambda unit, manager, state, mine, force=False: seen.append(force) or None,
    )
    code, out = host(["enable", "--dev"], env)
    assert code == 0 and seen == [False]
    code, out = host(["enable", "--dev", "--restart-runner"], env)
    assert code == 0 and seen == [False, True]
    doc = host_cli.enable(Path(env["ORCH_STATE_DIR"]), env, dev=True, runner="skip")
    assert doc["ok"] is True and seen == [False, True]  # the register path: the runner is not asked


def test_a_runner_with_live_runs_makes_enable_say_restart_needed(stale_env, monkeypatch):
    env, restarts = stale_env
    monkeypatch.setattr(host_cli, "_host_release", lambda control: "old000000000")
    monkeypatch.setattr(
        "orch.runner.service.restart_if_stale",
        lambda *a, **k: {"restarted": False, "restart_needed": True, "message": "has 1 running agents/terminals"},
    )
    code, out = host(["enable", "--dev"], env)
    data = json.loads(out)["result"]["data"]
    assert data["restart_needed"] is True and data["runner_restarted"] is False
