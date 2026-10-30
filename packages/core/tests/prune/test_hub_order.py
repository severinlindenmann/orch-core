"""``orch host prune --hub-order``: the hub removes itself from one order file (#1463). Fake machine in a temp folder:
no real unit, no real prune. Every refused case must leave EVERYTHING in place and write ``remove-result.json``."""

from __future__ import annotations

import errno
import io
import json
import os
from pathlib import Path

import pytest

from orch.prune import hub_order as H
from orch.prune.cli import main
from orch.prune.model import Ctx

NOW = 1_800_000_000.0
HUB_ID = "c0" * 16
REMOVAL = "ab" * 16


class FakeRunner:
    kind = "fake"

    def __init__(self, fail: str | None = None):
        self.stopped: list[str] = []
        self.fail = fail

    def stop(self, part):
        if part == self.fail:
            raise RuntimeError("service manager said no")
        self.stopped.append(part)

    def info(self, part):
        return {"running": True, "pid": 1, "service": f"fake.{part}"}


class Box:
    def __init__(self, tmp: Path):
        self.home = tmp / "home"
        self.state = self.home / ".config" / "orch"
        self.install = self.state / "install"
        self.hub = self.install / "parts" / "hub" / "data"
        self.runner = FakeRunner()
        self.sleeps: list[float] = []
        self.exe = "/opt/orchtool/bin/orch"
        self.euid = os.geteuid()
        self.env_extra: dict[str, str] = {}
        self.out = self.err = io.StringIO()
        self._build()

    def w(self, p: Path, text: str = "x", mode: int = 0o600) -> Path:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        p.chmod(mode)
        return p

    def _build(self) -> None:
        self.home.mkdir(parents=True)
        i = self.install
        self.w(
            i / "install.json",
            json.dumps(
                {
                    "v": 1,
                    "address": None,
                    "domain": None,
                    "parts": {
                        "hub": {"url": "http://127.0.0.1:8100", "service": "fake.hub", "runner": None},
                        "relay": {"url": "http://127.0.0.1:8101", "service": "fake.relay", "runner": None},
                    },
                    "dashboard": {"url": "http://127.0.0.1:8151/", "service": "fake.dashboard"},
                    "workspaces": [],
                }
            ),
        )
        for p in ("hub", "relay"):
            self.w(i / "parts" / p / "data" / "keys.json", f"{p}-secret")
            self.w(i / "bin" / f"orch-{p}")
            self.w(i / "tools" / f"orch-{p}" / "x")
            self.w(i / "logs" / f"{p}.log")
        self.hub.chmod(0o700)
        self.w(self.hub / "meta.json", json.dumps({"hub_id": HUB_ID, "dev": False}))
        self.w(i / "logs" / "dashboard.log")
        self.w(i / "tools" / "orch-dashboard" / "x")
        self.w(self.state / "hosts" / ("11" * 16) / "keys" / "wsk.json")
        self.w(self.state / "dashboard" / "passkeys.json")
        self.w(self.state / "person" / "custody.json")
        self.w(self.home / "Library" / "Keychains" / "login.keychain-db")
        self.w(self.home / "work" / "proj" / "notes.txt", "mine", 0o644)
        self.write_order()

    def write_order(self, **over) -> Path:
        o = {
            "v": 1,
            "kind": "orch.hub_remove_order.v1",
            "hub_id": HUB_ID,
            "removal_id": REMOVAL,
            "state_dir": os.path.realpath(self.hub),
            "issued_ms": int(NOW * 1000) - 1000,
            "exp_ms": int(NOW * 1000) - 1000 + 300_000,
            "dev": False,
        }
        o.update(over)
        return self.w(self.hub / "remove-order.json", json.dumps(o))

    def ctx(self, **kw) -> Ctx:
        self.out, self.err = io.StringIO(), io.StringIO()
        base = dict(
            env={"HOME": str(self.home), "PATH": "/usr/bin", **self.env_extra},
            out=self.out,
            err=self.err,
            platform="linux",
            run=lambda argv, env=None: (0, ""),
            runner=self.runner,
            os_accounts=lambda: [],
            dashboard_entry=lambda: None,
            euid=self.euid,
            uid=os.getuid(),
            root_exists=lambda p: False,
            clock=lambda: NOW,
            sleep=self.sleeps.append,
            orch_exe=self.exe,
        )
        base.update(kw)
        return Ctx(**base)

    def run(self, *args: str, **kw) -> int:
        ctx = self.ctx(**kw)
        return main(["--hub-order", *args], env=ctx.env, out=self.out, err=self.err, ctx=ctx)

    def snap(self) -> dict[str, tuple]:
        out: dict[str, tuple] = {}
        for dp, dn, fn in os.walk(self.home):
            for n in dn + fn:
                p = os.path.join(dp, n)
                if n == H.RESULT_NAME:
                    continue
                st = os.lstat(p)
                out[p] = (st.st_mode, 0, 0) if n in dn else (st.st_mode, st.st_size, st.st_nlink)
        return out

    def result(self) -> dict:
        return json.loads((Path(os.path.realpath(self.hub)) / H.RESULT_NAME).read_text())

    @property
    def ledger(self) -> Path:
        return self.home / H.LEDGER_NAME


@pytest.fixture
def b(tmp_path):
    return Box(tmp_path)


def refused(b: Box, code: str, rc: int | None = None, **kw) -> None:
    before = b.snap()
    got = b.run(**kw)
    assert got != 0 and (rc is None or got == rc), b.err.getvalue()
    assert b.snap() == before, "a refusal must leave everything in place"
    assert b.runner.stopped == [] and b.sleeps == []
    r = b.result()
    assert r["ok"] is False and r["code"] == code and r["message_plain"] and isinstance(r["ms"], int)
    assert oct(os.stat(Path(os.path.realpath(b.hub)) / H.RESULT_NAME).st_mode & 0o777) == "0o600"


# ------------------------------------------------------------------------------------------------ refusals


def test_symlinked_order(b):
    real = b.w(b.home / "elsewhere.json", (b.hub / H.ORDER_NAME).read_text())
    (b.hub / H.ORDER_NAME).unlink()
    os.symlink(real, b.hub / H.ORDER_NAME)
    refused(b, "bad_order")


def test_symlinked_parent_dir(b):
    moved = b.home / "moved-hub-data"
    os.rename(b.hub, moved)
    os.symlink(moved, b.hub)
    b.hub_target = moved
    before = b.snap()
    assert b.run() != 0
    assert b.snap() == before and b.runner.stopped == []
    assert json.loads((moved / H.RESULT_NAME).read_text())["code"] == "bad_folder"


def test_parent_mode_0755(b):
    b.hub.chmod(0o755)
    refused(b, "bad_folder")


def test_order_file_mode_0644(b):
    (b.hub / H.ORDER_NAME).chmod(0o644)
    refused(b, "bad_order")


def test_other_uid(b):
    b.euid = os.geteuid() + 1
    refused(b, "bad_folder")


def test_nlink_2(b):
    os.link(b.hub / H.ORDER_NAME, b.hub / "second-name")
    refused(b, "bad_order")


def test_oversize(b):
    b.w(b.hub / H.ORDER_NAME, json.dumps({"pad": "x" * 5000}))
    refused(b, "bad_order")


def test_extra_field_is_refused(b):
    b.write_order(path="/etc")
    refused(b, "bad_order")


def test_order_longer_than_ten_minutes(b):
    b.write_order(exp_ms=int(NOW * 1000) + 3_600_000)
    refused(b, "bad_order")


def test_expired(b):
    b.write_order(issued_ms=int(NOW * 1000) - 900_000, exp_ms=int(NOW * 1000) - 900_000 + 600_000)
    refused(b, "expired")


def test_issued_in_the_future(b):
    b.write_order(issued_ms=int(NOW * 1000) + 400_000, exp_ms=int(NOW * 1000) + 500_000)
    refused(b, "bad_order")


def test_other_hub_id(b):
    b.write_order(hub_id="d1" * 16)
    refused(b, "other_hub")


def test_other_state_dir(b):
    b.write_order(state_dir=os.path.realpath(b.home / "somewhere"))
    refused(b, "other_state")


def test_dev_flag_must_match_the_install(b):
    b.write_order(dev=True)
    refused(b, "dev_mismatch")


def test_removal_id_already_in_the_ledger(b):
    b.w(b.ledger, f"2026-10-30T10:00:00Z removal {REMOVAL} hub c0c0c0c0\n")
    refused(b, "replayed")


def test_agent_env(b):
    b.env_extra = {"ORCH_SESSION": "s1"}
    refused(b, "agent", rc=3)


def test_dev_fence_still_refuses_and_there_is_no_bypass(b):
    b.env_extra = {"ORCH_DEV": "1"}
    refused(b, "dev_kit", rc=4)
    b.env_extra = {}
    assert b.run("--i-know-dev") == 2 and "takes only --dry-run" in b.err.getvalue()
    assert b.run("--yes") == 2
    b.w(b.state / "host" / "dev.json", "{}")
    assert b.run() == 4


def test_orch_program_inside_a_removed_path(b):
    b.exe = str(b.install / "bin" / "orch-hub")
    refused(b, "own_program")
    b.exe = str(b.install / "parts" / "relay" / "data" / "keys.json")
    refused(b, "own_program")


def test_no_hub_installed(b):
    rec = json.loads((b.install / "install.json").read_text())
    rec["parts"].pop("hub")
    (b.install / "install.json").write_text(json.dumps(rec))
    refused(b, "no_hub")


def test_the_runner_is_unavailable(b):
    class Broken(FakeRunner):
        def info(self, part):
            raise OSError("no systemd")

    b.runner = Broken()
    refused(b, "no_runner")


def test_missing_backup_destination_is_refused(b):
    b.w(b.hub / "backups" / "hub-c0c0c0c0-20261030T100000Z.orchbak", "BAK")
    import time as _t

    dest = b.home / f"orch-hub-backup-{HUB_ID[:8]}-{_t.strftime('%Y%m%dT%H%M%SZ', _t.gmtime(NOW))}"
    dest.mkdir()
    refused(b, "backup_exists")


# --------------------------------------------------------------------------------------------- dry run


def test_dry_run_checks_everything_and_changes_nothing(b):
    before = b.snap()
    assert b.run("--dry-run") == 0
    assert "every check passed" in b.out.getvalue()
    assert b.snap() == before and not b.ledger.exists() and b.runner.stopped == []
    assert not (b.hub / H.RESULT_NAME).exists() and (b.hub / H.ORDER_NAME).exists()


def test_dry_run_fails_with_the_reason_and_changes_nothing(b):
    b.write_order(hub_id="d1" * 16)
    before = b.snap()
    assert b.run("--dry-run") != 0 and "other_hub" in b.err.getvalue()
    assert b.snap() == before and not (b.hub / H.RESULT_NAME).exists()


# --------------------------------------------------------------------------------------- the real run


def test_removes_the_hub_and_relay_and_nothing_else(b):
    other = b.snap()
    keep = [
        p
        for p in other
        if "/parts/hub" not in p
        and "/parts/relay" not in p
        and not p.endswith(("/bin/orch-hub", "/bin/orch-relay", "/logs/hub.log", "/logs/relay.log"))
        and "/tools/orch-hub" not in p
        and "/tools/orch-relay" not in p
        and p != str(b.install / "install.json")
    ]
    assert b.run() == 0, b.err.getvalue()
    assert b.sleeps == [3.0]
    assert b.runner.stopped == ["hub", "relay"]  # the hub first
    for gone in ("parts/hub", "parts/relay", "bin/orch-hub", "tools/orch-relay", "logs/hub.log"):
        assert not os.path.lexists(b.install / gone), gone
    for p in keep:
        if p != str(b.install / "parts"):
            assert os.path.lexists(p), f"{p} must stay"
    for stay in (
        b.state / "hosts",
        b.state / "dashboard" / "passkeys.json",
        b.state / "person" / "custody.json",
        b.home / "Library" / "Keychains" / "login.keychain-db",
        b.home / "work" / "proj" / "notes.txt",
        b.install / "tools" / "orch-dashboard",
        b.install / "logs" / "dashboard.log",
    ):
        assert os.path.lexists(stay), stay
    rec = json.loads((b.install / "install.json").read_text())
    assert rec["parts"] == {} and rec["dashboard"]["service"] == "fake.dashboard"
    text = b.ledger.read_text()
    assert REMOVAL in text and "done" in text and "secret" not in text
    assert oct(b.ledger.stat().st_mode & 0o777) == "0o600"
    assert REMOVAL in text.splitlines()[0]  # written first, before any change


def test_publish_part_is_in_scope_too(b):
    rec = json.loads((b.install / "install.json").read_text())
    rec["parts"]["publish"] = {"url": "http://127.0.0.1:8102", "service": "fake.publish", "runner": None}
    (b.install / "install.json").write_text(json.dumps(rec))
    b.w(b.install / "parts" / "publish" / "data" / "k")
    assert b.run() == 0, b.err.getvalue()
    assert b.runner.stopped == ["hub", "relay", "publish"]
    assert not os.path.lexists(b.install / "parts" / "publish")


def test_a_spent_order_cannot_run_twice(b):
    b.runner.fail = None
    b.runner = FakeRunner(fail="hub")
    assert b.run() == 1
    assert (b.hub / H.USED_NAME).exists() and not (b.hub / H.ORDER_NAME).exists()
    assert b.result()["code"] == "remove_failed"
    assert REMOVAL in b.ledger.read_text()
    assert b.run() != 0  # no order any more; the earlier real result stays (F3)
    assert b.result()["code"] == "remove_failed"
    # a new order file with the same removal id is refused by the ledger
    b.write_order()
    assert b.run() != 0 and b.result()["code"] == "replayed"
    assert os.path.lexists(b.install / "parts" / "hub")


def test_ledger_must_be_private(b):
    b.w(b.ledger, "", 0o644)
    before = b.snap()
    assert b.run() != 0
    assert b.snap() == before and b.runner.stopped == []
    assert b.result()["code"] == "bad_ledger"


def _bak(b: Box) -> dict[str, str]:
    files = {f"hub-c0c0c0c0-2026102{n}T100000Z.orchbak": f"backup-{n}" * 100 for n in (7, 8)}
    for n, t in files.items():
        b.w(b.hub / "backups" / n, t)
    b.hub.joinpath("backups").chmod(0o700)
    return files


def _kept(b: Box) -> Path:
    dirs = [p for p in b.home.iterdir() if p.name.startswith(f"orch-hub-backup-{HUB_ID[:8]}-")]
    assert len(dirs) == 1 and dirs[0].name.endswith("Z")
    return dirs[0]


def test_backups_move_out_before_the_trees_go(b):
    files = _bak(b)
    assert b.run() == 0, b.err.getvalue()
    kept = _kept(b)
    assert oct(kept.stat().st_mode & 0o777) == "0o700"
    assert {p.name: p.read_text() for p in kept.iterdir()} == files
    assert not os.path.lexists(b.install / "parts" / "hub")
    assert str(kept) in b.ledger.read_text()


def test_backups_move_across_file_systems(b, monkeypatch):
    files = _bak(b)
    real = os.rename

    def fake(src, dst):
        if str(src).endswith(".orchbak"):
            raise OSError(errno.EXDEV, "cross-device")
        real(src, dst)

    monkeypatch.setattr(H, "_rename", fake)
    assert b.run() == 0, b.err.getvalue()
    kept = _kept(b)
    assert {p.name: p.read_text() for p in kept.iterdir()} == files
    assert all(oct(p.stat().st_mode & 0o777) == "0o600" for p in kept.iterdir())


def test_a_bad_copy_keeps_the_trees(b, monkeypatch):
    _bak(b)
    monkeypatch.setattr(H, "_rename", lambda s, d: (_ for _ in ()).throw(OSError(errno.EXDEV, "x")))
    monkeypatch.setattr(H, "_sha", lambda p: p)  # source and copy never match
    assert b.run() == 1
    assert os.path.lexists(b.install / "parts" / "hub" / "data" / "backups")  # nothing deleted
    assert b.result()["code"] == "remove_failed"


# ------------------------------------------------------------------- Opus review F3 / F5


def test_f3_launchd_job_is_removed_on_success_and_on_refusal(b):
    calls: list[list[str]] = []
    run = lambda argv, env=None: (calls.append(list(argv)), (0, ""))[1]  # noqa: E731
    assert b.run(platform="darwin", run=run) == 0, b.err.getvalue()
    assert calls[-1] == ["launchctl", "remove", "io.orch.hub-remove." + REMOVAL]
    b2 = Box(b.home.parent / "second")
    b2.write_order(hub_id="d1" * 16)
    calls2: list[list[str]] = []
    run2 = lambda argv, env=None: (calls2.append(list(argv)), (0, ""))[1]  # noqa: E731
    assert b2.run(platform="darwin", run=run2) != 0
    assert [
        "launchctl",
        "remove",
        "io.orch.hub-remove." + REMOVAL,
    ] in calls2  # a refusal leaves no job to re-run


def test_f3_dry_run_and_linux_never_call_launchctl(b):
    calls: list[list[str]] = []
    run = lambda argv, env=None: (calls.append(list(argv)), (0, ""))[1]  # noqa: E731
    assert b.run("--dry-run", platform="darwin", run=run) == 0
    assert not any(c[:2] == ["launchctl", "remove"] for c in calls)


def test_f3_a_second_run_without_an_order_does_not_overwrite_the_real_result(b):
    b.write_order(hub_id="d1" * 16)
    assert b.run() != 0
    first = b.result()
    assert first["code"] == "other_hub"
    (b.hub / H.ORDER_NAME).unlink()
    assert b.run() != 0  # the job came back: no order
    assert b.result() == first


def test_f5_a_backup_written_after_the_preflight_is_kept(b, monkeypatch):
    files = _bak(b)
    real = H._backups

    def late(ctx, plan):
        real(ctx, plan)
        (b.hub / "backups" / "hub-aaaaaaaa-20260101T000000Z.orchbak").write_text("late")

    monkeypatch.setattr(H, "_backups", late)
    assert b.run() == 0, b.err.getvalue()
    kept = {p.name: p.read_text() for p in _kept(b).iterdir()}
    assert kept == {**files, "hub-aaaaaaaa-20260101T000000Z.orchbak": "late"}
